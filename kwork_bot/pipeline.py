"""Shared pipeline: given a list of unseen projects, decide + notify owner.

Both the continuous poller (page 1 every N seconds) and the /scan command
(walk all pages) funnel projects through this routine, so the selection
logic lives in exactly one place.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import Bot

from .deepseek_filter import DeepSeekFilter, FilterDecision
from .kwork_parser import Project
from .notifier import send_project
from .storage import ChatSettings, MODE_ALL, MODE_INTERESTING, Storage

if TYPE_CHECKING:
    from .health import HealthMonitor

log = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    processed: int = 0      # projects considered (after hard pre-filter)
    notified: int = 0       # projects actually sent to the user
    ai_calls: int = 0       # DeepSeek invocations
    hard_blocked: int = 0   # dropped by category pre-filter
    ai_errors: int = 0      # AI calls that returned an error reason

    def add(self, other: "PipelineStats") -> "PipelineStats":
        return PipelineStats(
            processed=self.processed + other.processed,
            notified=self.notified + other.notified,
            ai_calls=self.ai_calls + other.ai_calls,
            hard_blocked=self.hard_blocked + other.hard_blocked,
            ai_errors=self.ai_errors + other.ai_errors,
        )


async def process_project(
    project: Project,
    *,
    bot: Bot,
    storage: Storage,
    ai: DeepSeekFilter,
    settings: ChatSettings,
    owner_chat_id: int,
    prefix: str = "🔔",
    health: "HealthMonitor | None" = None,
) -> tuple[bool, PipelineStats]:
    """Decide whether to notify the owner about one project, and do so.

    Returns (notified, stats).
    """
    stats = PipelineStats(processed=1)

    if settings.mode == MODE_ALL:
        decision: FilterDecision | None = None
        notify = True
    else:
        # "interesting" mode: hard category block, then AI.
        if project.is_hard_blocked:
            log.debug(
                "Skip #%d '%s' — hard-blocked category %d",
                project.id, project.title[:40], project.category_id,
            )
            await storage.mark_seen(
                project.id, decision="blocked", reason="категория в стоп-листе"
            )
            return False, PipelineStats(processed=1, hard_blocked=1)

        decision = await ai.classify(project)
        stats.ai_calls = 1
        if health is not None:
            await health.observe_ai(decision)
        if decision.reason.startswith("ошибка AI") or decision.status != "ok":
            stats.ai_errors = 1
        notify = decision.interesting

    if not settings.paused and notify:
        try:
            await send_project(bot, owner_chat_id, project, decision, prefix=prefix)
            stats.notified = 1
        except Exception:
            log.exception("Failed to send Telegram message for project %d", project.id)

    decision_tag: str | None
    reason: str | None
    if settings.mode == MODE_ALL:
        decision_tag, reason = "all", None
    else:
        decision_tag = "interesting" if (decision and decision.interesting) else "skip"
        reason = decision.reason if decision else None

    await storage.mark_seen(project.id, decision=decision_tag, reason=reason)
    return stats.notified == 1, stats


__all__ = ["PipelineStats", "process_project"]
