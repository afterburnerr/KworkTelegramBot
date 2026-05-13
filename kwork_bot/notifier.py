"""Format and send Kwork project notifications to Telegram.

Every notification carries an inline keyboard with:
  • a URL button "Открыть заказ" that jumps straight to kwork.ru
  • (when AI produced a pitch) a CopyTextButton "Скопировать отклик"
    — tapping copies the suggested response into the user's clipboard
    so they can paste it on kwork with one tap.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from .deepseek_filter import FilterDecision
from .kwork_parser import Project

log = logging.getLogger(__name__)

# Telegram hard-caps messages at 4096 chars; we leave headroom for HTML.
MAX_DESCRIPTION_CHARS = 900

# CopyTextButton.text is limited to 256 chars by the Bot API.
_COPY_TEXT_LIMIT = 256


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _truncate(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def render_project_message(
    project: Project,
    decision: FilterDecision | None = None,
    *,
    prefix: str = "🔔",
) -> str:
    """Build the HTML message body that accompanies the inline keyboard."""
    parts: list[str] = []

    title = _esc(project.title) or "(без названия)"
    parts.append(f"{prefix} <b>{title}</b>")

    category = _esc(project.parent_category_name)
    # Offers counter inline in the header so the user sees how saturated the
    # project is at a glance. Bold + warning tint above a threshold.
    offers_marker = ""
    if project.offers_count:
        emoji = "🔥" if project.offers_count >= 15 else "📝"
        offers_marker = (
            f" · {emoji} <b>{project.offers_count}</b> откликов"
        )
    parts.append(
        f"💰 <b>{_esc(project.price_human)}</b> · <i>{category}</i>{offers_marker}"
    )

    desc = _esc(_truncate(project.description))
    if desc:
        parts.append("")
        parts.append(desc)

    # Pitch block — the exact string the Copy button puts into clipboard.
    if decision and decision.pitch:
        parts.append("")
        parts.append("<b>Отклик</b> (кнопка ниже скопирует его):")
        parts.append(f"<blockquote>{_esc(decision.pitch)}</blockquote>")

    parts.append("")

    user_bits: list[str] = []
    if project.username and project.profile_url:
        user_bits.append(
            f'👤 <a href="{_esc(project.profile_url)}">{_esc(project.username)}</a>'
        )
    elif project.username:
        user_bits.append(f"👤 {_esc(project.username)}")
    if project.date_create:
        user_bits.append(f"🕒 {_esc(project.date_create)}")
    if user_bits:
        parts.append(" · ".join(user_bits))

    if decision and decision.reason:
        parts.append(f"🤖 <i>{_esc(decision.reason)}</i>")

    return "\n".join(parts)


def build_keyboard(
    project: Project,
    decision: FilterDecision | None = None,
) -> InlineKeyboardMarkup:
    """URL button + optional CopyTextButton for the AI-generated pitch."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔗 Открыть заказ", url=project.url)]
    ]

    if decision and decision.pitch:
        # The pitch has already been clamped to 240 in the filter, but
        # belt-and-braces guard against future callers.
        pitch = decision.pitch
        if len(pitch) > _COPY_TEXT_LIMIT:
            pitch = pitch[: _COPY_TEXT_LIMIT - 1] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Скопировать отклик",
                    copy_text=CopyTextButton(text=pitch),
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_project(
    bot: Bot,
    chat_id: int,
    project: Project,
    decision: FilterDecision | None = None,
    *,
    prefix: str = "🔔",
) -> None:
    text = render_project_message(project, decision, prefix=prefix)
    markup = build_keyboard(project, decision)

    async def _do_send() -> None:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )

    try:
        await _do_send()
    except TelegramRetryAfter as exc:
        log.warning("Telegram flood-control: waiting %ss", exc.retry_after)
        await asyncio.sleep(exc.retry_after + 0.5)
        await _do_send()


__all__ = ["render_project_message", "build_keyboard", "send_project"]
