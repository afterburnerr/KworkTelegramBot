"""Continuous poller: fetch kwork.ru/projects page 1 every `poll_interval` seconds."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import Bot

from .config import Config
from .deepseek_filter import DeepSeekFilter
from .kwork_parser import KworkClient
from .pipeline import process_project
from .storage import Storage

if TYPE_CHECKING:
    from .health import HealthMonitor

log = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        ai: DeepSeekFilter,
        config: Config,
        owner_chat_id: int,
        health: "HealthMonitor | None" = None,
    ):
        self._bot = bot
        self._storage = storage
        self._ai = ai
        self._config = config
        self._owner_chat_id = owner_chat_id
        self._health = health
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_poll_at: float | None = None
        self._last_error: str | None = None
        self._seeded = False

    @property
    def last_poll_at(self) -> float | None:
        return self._last_poll_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kwork-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        log.info(
            "Poller started, interval=%ds, owner_chat_id=%s",
            self._config.kwork_poll_interval, self._owner_chat_id,
        )
        while not self._stop.is_set():
            try:
                await self._cycle()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Poller cycle failed: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.kwork_poll_interval
                )
            except asyncio.TimeoutError:
                continue
        log.info("Poller stopped")

    async def _cycle(self) -> None:
        try:
            async with KworkClient() as client:
                page = await client.fetch_page(1)
        except Exception as exc:
            if self._health is not None:
                await self._health.observe_kwork_error(exc)
            raise

        self._last_poll_at = time.time()
        projects = page.projects
        if self._health is not None:
            await self._health.observe_kwork_success(len(projects))
        if not projects:
            log.warning("Poller: 0 projects returned — parser regression?")
            return

        # First-run seeding: mark the oldest N projects as "seen" so the bot
        # doesn't dump the whole first page on the user.
        if not self._seeded and self._config.kwork_seed_seen > 0:
            seeded_count = await self._storage.count_seen()
            if seeded_count == 0:
                to_seed = [p.id for p in projects[-self._config.kwork_seed_seen :]]
                await self._storage.mark_seen_bulk(to_seed)
                log.info("Seeded %d already-seen ids on first run", len(to_seed))
                self._seeded = True

        unseen_ids = await self._storage.filter_unseen([p.id for p in projects])
        unseen = [p for p in projects if p.id in set(unseen_ids)]
        log.info(
            "Cycle: page1=%d projects, unseen=%d, last_page=%d, total=%d",
            len(projects), len(unseen), page.last_page, page.total,
        )
        if not unseen:
            return

        settings = await self._storage.get_settings(self._owner_chat_id)
        if settings.filter_prompt:
            self._ai.set_system_prompt(settings.filter_prompt)

        # Cap AI calls per cycle to avoid thrashing the proxy when the bot
        # was offline for a long time.
        ai_budget = self._config.kwork_max_ai_per_cycle
        ai_used = 0

        # Oldest first so Telegram shows them in chronological order.
        for project in reversed(unseen):
            if self._stop.is_set():
                break

            if settings.mode != "all" and not project.is_hard_blocked:
                if ai_used >= ai_budget:
                    # Defer: leave unseen, will be classified next cycle.
                    log.info("AI budget %d reached, deferring remaining", ai_budget)
                    break
                ai_used += 1

            try:
                await process_project(
                    project,
                    bot=self._bot,
                    storage=self._storage,
                    ai=self._ai,
                    settings=settings,
                    owner_chat_id=self._owner_chat_id,
                    health=self._health,
                )
            except Exception:
                log.exception("Failed to process project %d", project.id)


__all__ = ["Poller"]
