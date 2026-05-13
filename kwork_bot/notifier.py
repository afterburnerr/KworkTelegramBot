"""Format + send Kwork project notifications to Telegram."""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter

from .deepseek_filter import FilterDecision
from .kwork_parser import Project

log = logging.getLogger(__name__)

# Telegram hard-caps messages at 4096 chars. We leave headroom for HTML tags.
MAX_DESCRIPTION_CHARS = 900


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _truncate(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def render_project_message(
    project: Project,
    decision: FilterDecision | None = None,
    *,
    prefix: str = "🔔",
) -> str:
    parts: list[str] = []

    title = _esc(project.title) or "(без названия)"
    parts.append(f"{prefix} <b>{title}</b>")

    category = _esc(project.parent_category_name)
    parts.append(f"💰 <b>{_esc(project.price_human)}</b> · <i>{category}</i>")

    desc = _esc(_truncate(project.description))
    if desc:
        parts.append("")
        parts.append(desc)

    parts.append("")

    user_bits: list[str] = []
    if project.username and project.profile_url:
        user_bits.append(
            f'👤 <a href="{_esc(project.profile_url)}">{_esc(project.username)}</a>'
        )
    elif project.username:
        user_bits.append(f"👤 {_esc(project.username)}")
    if project.offers_count:
        user_bits.append(f"📝 откликов: {project.offers_count}")
    if project.date_create:
        user_bits.append(f"🕒 {_esc(project.date_create)}")
    if user_bits:
        parts.append(" · ".join(user_bits))

    if decision and decision.reason:
        parts.append(f"🤖 <i>{_esc(decision.reason)}</i>")

    parts.append(f'<a href="{_esc(project.url)}">Открыть на kwork →</a>')

    return "\n".join(parts)


async def send_project(
    bot: Bot,
    chat_id: int,
    project: Project,
    decision: FilterDecision | None = None,
    *,
    prefix: str = "🔔",
) -> None:
    text = render_project_message(project, decision, prefix=prefix)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramRetryAfter as exc:
        log.warning("Telegram rate limit: waiting %ss", exc.retry_after)
        import asyncio

        await asyncio.sleep(exc.retry_after + 0.5)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


__all__ = ["render_project_message", "send_project"]
