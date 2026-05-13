"""One-shot full-exchange scanner triggered by /scan.

Walks every page of kwork.ru/projects (pagination.last_page tells us how
many there are — currently ~50), runs each project through the same
hard-category + AI filter as the continuous poller, and streams
interesting projects back to the requesting chat. Supports /cancel via
an asyncio.Event.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.enums import ParseMode

from .config import Config
from .deepseek_filter import DeepSeekFilter
from .kwork_parser import KworkClient
from .pipeline import PipelineStats, process_project
from .storage import Storage

if TYPE_CHECKING:
    from .health import HealthMonitor

log = logging.getLogger(__name__)

# How often to edit the "progress" message — Telegram rate-limits edits.
PROGRESS_UPDATE_EVERY = 8  # processed projects
PROGRESS_UPDATE_MIN_INTERVAL = 3.0  # seconds

# Politeness: small delay between page fetches so we don't hammer kwork.
INTER_PAGE_DELAY = 0.6  # seconds


@dataclass
class ScanProgress:
    total_projects: int = 0
    page: int = 0
    total_pages: int = 0
    stats: PipelineStats = None  # type: ignore[assignment]
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if self.stats is None:
            self.stats = PipelineStats()


class Scanner:
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

        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._progress = ScanProgress()

    # ---------- public API ---------- #

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def progress(self) -> ScanProgress:
        return self._progress

    async def start(self, chat_id: int, max_pages: int | None = None) -> bool:
        """Returns False if a scan is already running."""
        if self.is_running:
            return False
        self._cancel.clear()
        self._progress = ScanProgress(started_at=time.time())
        self._task = asyncio.create_task(
            self._run(chat_id, max_pages), name="kwork-scan"
        )
        return True

    def cancel(self) -> bool:
        if not self.is_running:
            return False
        self._cancel.set()
        return True

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ---------- internals ---------- #

    async def _run(self, chat_id: int, max_pages: int | None) -> None:
        start_msg = await self._bot.send_message(
            chat_id,
            "🔎 Запускаю скан биржи…",
            parse_mode=ParseMode.HTML,
        )
        progress_message_id = start_msg.message_id
        last_edit_at = 0.0
        last_edit_counter = 0

        try:
            async with KworkClient(timeout=25.0) as client:
                first = await client.fetch_page(1)
                self._progress.total_pages = first.last_page
                self._progress.total_projects = first.total
                if max_pages is not None:
                    self._progress.total_pages = min(self._progress.total_pages, max_pages)

                settings = await self._storage.get_settings(self._owner_chat_id)
                if settings.filter_prompt:
                    self._ai.set_system_prompt(settings.filter_prompt)

                pages_to_scan = range(1, self._progress.total_pages + 1)
                for page_num in pages_to_scan:
                    if self._cancel.is_set():
                        break

                    self._progress.page = page_num
                    if page_num == 1:
                        page = first
                    else:
                        await asyncio.sleep(INTER_PAGE_DELAY)
                        try:
                            page = await client.fetch_page(page_num)
                        except Exception as exc:
                            log.warning("Scan: failed to fetch page %d: %s", page_num, exc)
                            continue

                    # Keep only unseen projects on this page.
                    ids = [p.id for p in page.projects]
                    unseen_ids = set(await self._storage.filter_unseen(ids))
                    unseen = [p for p in page.projects if p.id in unseen_ids]

                    # Oldest-first on each page so results stream chronologically.
                    for project in reversed(unseen):
                        if self._cancel.is_set():
                            break
                        try:
                            _, stats = await process_project(
                                project,
                                bot=self._bot,
                                storage=self._storage,
                                ai=self._ai,
                                settings=settings,
                                owner_chat_id=self._owner_chat_id,
                                prefix="🎯",
                                health=self._health,
                            )
                        except Exception:
                            log.exception("Scan: process_project failed for %d", project.id)
                            continue
                        self._progress.stats = self._progress.stats.add(stats)

                        # Throttled progress updates.
                        last_edit_counter += 1
                        now = time.time()
                        should_edit = (
                            last_edit_counter >= PROGRESS_UPDATE_EVERY
                            and now - last_edit_at >= PROGRESS_UPDATE_MIN_INTERVAL
                        )
                        if should_edit:
                            last_edit_at = now
                            last_edit_counter = 0
                            await self._edit_progress(chat_id, progress_message_id)

            await self._finalize(chat_id, progress_message_id)
        except asyncio.CancelledError:
            await self._finalize(chat_id, progress_message_id, cancelled=True)
            raise
        except Exception as exc:
            log.exception("Scan failed: %s", exc)
            try:
                await self._bot.send_message(
                    chat_id,
                    f"❌ Скан упал: <code>{type(exc).__name__}: {exc}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    async def _edit_progress(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=self._render_progress(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            # Most common failure: "message is not modified". Ignore.
            pass

    async def _finalize(
        self, chat_id: int, message_id: int, *, cancelled: bool = False
    ) -> None:
        header = "🟡 Скан отменён" if cancelled or self._cancel.is_set() else "✅ Скан завершён"
        body = self._render_progress(header=header)
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=body,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await self._bot.send_message(
                    chat_id, body, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    def _render_progress(self, header: str = "🔎 Сканирую биржу…") -> str:
        st = self._progress.stats
        elapsed = 0
        if self._progress.started_at:
            elapsed = int(time.time() - self._progress.started_at)
        return (
            f"{header}\n"
            f"📄 Страница: <b>{self._progress.page}</b> / {self._progress.total_pages}"
            f" (всего проектов в выдаче: {self._progress.total_projects})\n"
            f"✅ Отправлено интересных: <b>{st.notified}</b>\n"
            f"🤖 AI-проверок: {st.ai_calls} (ошибок: {st.ai_errors})\n"
            f"🚫 Отсеяно по категории: {st.hard_blocked}\n"
            f"⏱ {elapsed} сек"
        )


__all__ = ["Scanner", "ScanProgress"]
