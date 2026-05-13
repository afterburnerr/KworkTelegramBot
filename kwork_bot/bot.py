"""aiogram v3 bot: commands, access control, and wiring to Poller/Scanner."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .config import Config
from .deepseek_filter import DEFAULT_FILTER_PROMPT, DeepSeekFilter
from .health import HealthMonitor
from .kwork_parser import KworkClient
from .notifier import send_project
from .pipeline import process_project
from .poller import Poller
from .scanner import Scanner
from .storage import MODE_ALL, MODE_INTERESTING, Storage, VALID_MODES

log = logging.getLogger(__name__)


BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Приветствие и статус"),
    BotCommand(command="status", description="Показать текущее состояние"),
    BotCommand(command="mode_all", description="Присылать все новые проекты"),
    BotCommand(command="mode_interesting", description="Только интересные (через AI)"),
    BotCommand(command="pause", description="Пауза уведомлений"),
    BotCommand(command="resume", description="Возобновить уведомления"),
    BotCommand(command="check", description="Сейчас проверить страницу 1"),
    BotCommand(command="scan", description="Сканировать всю биржу"),
    BotCommand(command="cancel", description="Отменить текущий скан"),
    BotCommand(command="filter", description="Показать/задать AI-промпт"),
    BotCommand(command="filter_reset", description="Сбросить AI-промпт к дефолту"),
    BotCommand(command="set_token", description="Обновить DeepSeek auth-токен"),
    BotCommand(command="refresh_cookies", description="Обновить cookies DeepSeek"),
    BotCommand(command="health", description="Диагностика системы"),
]


@dataclass
class BotContext:
    config: Config
    storage: Storage
    ai: DeepSeekFilter
    poller: Poller
    scanner: Scanner
    health: HealthMonitor
    # The effective owner chat id. Set from config; if config is empty, the
    # first user to /start this bot becomes the owner.
    owner_chat_id: int | None


def _is_owner(ctx: BotContext, chat_id: int) -> bool:
    return ctx.owner_chat_id is not None and ctx.owner_chat_id == chat_id


async def _claim_owner_if_open(ctx: BotContext, chat_id: int) -> bool:
    """If no owner is configured yet, the first caller claims the role."""
    if ctx.owner_chat_id is None:
        ctx.owner_chat_id = chat_id
        ctx.poller._owner_chat_id = chat_id  # type: ignore[attr-defined]
        ctx.scanner._owner_chat_id = chat_id  # type: ignore[attr-defined]
        ctx.health.set_owner(chat_id)
        log.info("Claimed owner_chat_id=%s from first /start", chat_id)
        return True
    return False


def _deny_message() -> str:
    return (
        "⛔ Этот бот закреплён за другим пользователем.\n"
        "Если бот ваш — пропишите ваш chat_id в <code>TELEGRAM_OWNER_CHAT_ID</code> "
        "и перезапустите."
    )


def build_router(ctx: BotContext) -> Router:
    router = Router(name="kwork-owner")

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        chat_id = message.chat.id
        claimed = await _claim_owner_if_open(ctx, chat_id)
        if not _is_owner(ctx, chat_id):
            await message.answer(_deny_message(), parse_mode=ParseMode.HTML)
            return

        settings = await ctx.storage.get_settings(chat_id)
        await ctx.storage.upsert_settings(settings)

        lines = [
            "👋 Бот подключён к мониторингу <b>kwork.ru/projects</b>.",
            "",
            f"🔧 Режим: <b>{_mode_label(settings.mode)}</b>",
            f"🔕 Пауза: <b>{'да' if settings.paused else 'нет'}</b>",
            f"🤖 Модель: <code>{ctx.config.deepseek_model}</code>",
            f"⏱ Интервал опроса: {ctx.config.kwork_poll_interval} сек",
            "",
            "Команды:",
            "• /mode_all — присылать все новые проекты",
            "• /mode_interesting — только интересные (через AI)",
            "• /pause, /resume — отключить / включить уведомления",
            "• /check — проверить 1-ю страницу прямо сейчас",
            "• /scan [pages] — пройтись по всей бирже и найти интересные",
            "• /cancel — отменить текущий скан",
            "• /status — текущее состояние",
            "• /filter [текст] — посмотреть / задать AI-промпт",
            "• /filter_reset — сбросить AI-промпт к дефолту",
            "",
            "Сервис / самолечение:",
            "• /set_token TOKEN — обновить DeepSeek auth-токен в чате",
            "• /refresh_cookies — обновить cookies DeepSeek",
            "• /health — диагностика (ошибки AI, kwork, cookies и т.п.)",
        ]
        if claimed:
            lines.append("")
            lines.append(f"✅ Ваш chat_id ({chat_id}) закреплён за ботом.")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        s = await ctx.storage.get_settings(message.chat.id)
        seen_count = await ctx.storage.count_seen()
        last = ctx.poller.last_poll_at
        last_txt = _fmt_ago(last) if last else "ни разу"
        err = ctx.poller.last_error or "—"
        prompt_status = "дефолтный" if not s.filter_prompt else "кастомный"
        await message.answer(
            "📊 <b>Статус</b>\n"
            f"Режим: <b>{_mode_label(s.mode)}</b>\n"
            f"Пауза: <b>{'да' if s.paused else 'нет'}</b>\n"
            f"AI-промпт: <b>{prompt_status}</b>\n"
            f"Модель: <code>{ctx.config.deepseek_model}</code>\n"
            f"Опрос: каждые {ctx.config.kwork_poll_interval} сек, последний — {last_txt}\n"
            f"Ошибка последнего цикла: <code>{_esc(err)}</code>\n"
            f"Запомнено проектов (seen): <b>{seen_count}</b>\n"
            f"Скан выполняется: <b>{'да' if ctx.scanner.is_running else 'нет'}</b>",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("mode_all"))
    async def cmd_mode_all(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await ctx.storage.set_mode(message.chat.id, MODE_ALL)
        await message.answer(
            "✅ Режим: <b>все проекты</b>.\n"
            "Бот будет присылать каждый новый проект без фильтрации AI.",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("mode_interesting"))
    async def cmd_mode_interesting(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await ctx.storage.set_mode(message.chat.id, MODE_INTERESTING)
        await message.answer(
            "✅ Режим: <b>только интересные</b>.\n"
            "Бот будет фильтровать заказы через DeepSeek и присылать только "
            "те, что попадают под профиль разработчика.",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await ctx.storage.set_paused(message.chat.id, True)
        await message.answer(
            "🔕 Уведомления поставлены на паузу. /resume — возобновить."
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await ctx.storage.set_paused(message.chat.id, False)
        await message.answer("🔔 Уведомления снова включены.")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        notice = await message.answer("🔍 Проверяю 1-ю страницу…")
        try:
            async with KworkClient() as client:
                page = await client.fetch_page(1)
        except Exception as exc:
            await notice.edit_text(f"❌ Ошибка: <code>{_esc(str(exc))}</code>", parse_mode=ParseMode.HTML)
            return

        settings = await ctx.storage.get_settings(message.chat.id)
        if settings.filter_prompt:
            ctx.ai.set_system_prompt(settings.filter_prompt)

        ids = [p.id for p in page.projects]
        unseen_ids = set(await ctx.storage.filter_unseen(ids))
        unseen = [p for p in page.projects if p.id in unseen_ids]

        await notice.edit_text(
            f"Страница 1: всего {len(page.projects)} проектов, новых {len(unseen)}. "
            f"Обрабатываю…"
        )

        notified = 0
        for project in reversed(unseen):
            try:
                did_notify, _ = await process_project(
                    project,
                    bot=ctx.poller._bot,  # type: ignore[attr-defined]
                    storage=ctx.storage,
                    ai=ctx.ai,
                    settings=settings,
                    owner_chat_id=message.chat.id,
                    prefix="🔔",
                )
                if did_notify:
                    notified += 1
            except Exception:
                log.exception("/check: process_project failed for %d", project.id)

        await message.answer(
            f"Готово. Отправлено интересных: <b>{notified}</b> "
            f"(из {len(unseen)} новых, всего на странице {len(page.projects)}).",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("scan"))
    async def cmd_scan(message: Message, command: CommandObject) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        if ctx.scanner.is_running:
            await message.answer(
                "⚠ Сейчас уже выполняется скан. Дождитесь или /cancel."
            )
            return

        max_pages: int | None = None
        if command.args:
            try:
                max_pages = max(1, int(command.args.strip().split()[0]))
            except ValueError:
                await message.answer(
                    "Использование: <code>/scan</code> или <code>/scan 10</code> "
                    "(ограничить N страниц).",
                    parse_mode=ParseMode.HTML,
                )
                return

        started = await ctx.scanner.start(message.chat.id, max_pages=max_pages)
        if not started:
            await message.answer("⚠ Не удалось запустить скан.")

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        if ctx.scanner.cancel():
            await message.answer("🟡 Остановка скана запрошена…")
        else:
            await message.answer("Нет активного скана.")

    @router.message(Command("filter"))
    async def cmd_filter(message: Message, command: CommandObject) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        if command.args:
            prompt = command.args.strip()
            await ctx.storage.set_filter_prompt(message.chat.id, prompt)
            ctx.ai.set_system_prompt(prompt)
            await message.answer("✅ Кастомный AI-промпт сохранён.")
        else:
            s = await ctx.storage.get_settings(message.chat.id)
            current = s.filter_prompt or DEFAULT_FILTER_PROMPT
            label = "кастомный" if s.filter_prompt else "дефолтный"
            body = f"📝 Текущий AI-промпт ({label}):\n<pre>{_esc(current)}</pre>"
            if len(body) > 3800:
                body = body[:3800] + "…</pre>"
            await message.answer(body, parse_mode=ParseMode.HTML)

    @router.message(Command("filter_reset"))
    async def cmd_filter_reset(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await ctx.storage.set_filter_prompt(message.chat.id, None)
        ctx.ai.set_system_prompt(DEFAULT_FILTER_PROMPT)
        await message.answer("✅ AI-промпт сброшен к дефолту.")

    @router.message(Command("set_token"))
    async def cmd_set_token(message: Message, command: CommandObject) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        raw = (command.args or "").strip()
        # Tolerate users pasting key=value or DEEPSEEK_AUTH_TOKEN=value.
        if "=" in raw:
            raw = raw.split("=", 1)[1].strip()
        token = raw.split()[0] if raw else ""

        if len(token) < 30:
            await message.answer(
                "Использование: <code>/set_token &lt;новый токен DeepSeek&gt;</code>\n"
                "Где взять:\n"
                "1. https://chat.deepseek.com — залогинься\n"
                "2. F12 → Console →\n"
                "<code>JSON.parse(localStorage.getItem(\"userToken\")).value</code>\n"
                "3. Пришли токен (обычно ~60 символов).",
                parse_mode=ParseMode.HTML,
            )
            return

        # Try to delete the message to keep the token out of the chat log.
        try:
            await message.delete()
        except Exception:
            pass  # may fail in private chats without delete rights

        await ctx.storage.set_deepseek_token(token)
        await ctx.ai.update_token(token)

        thinking_msg = await ctx.poller._bot.send_message(  # type: ignore[attr-defined]
            message.chat.id,
            "🔑 Токен обновлён. Проверяю живой вызов к DeepSeek…",
        )
        ok, detail = await _validate_token(ctx)
        if ok:
            await thinking_msg.edit_text(
                "✅ Новый токен принят DeepSeek и сохранён в базе.\n"
                f"<i>Пробный ответ: {_esc(detail)}</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await thinking_msg.edit_text(
                f"⚠️ Токен сохранён, но пробный вызов не прошёл:\n<code>{_esc(detail)}</code>\n"
                "Возможно, cookies тоже протухли — попробуй <code>/refresh_cookies</code>.",
                parse_mode=ParseMode.HTML,
            )

    @router.message(Command("refresh_cookies"))
    async def cmd_refresh_cookies(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        notice = await message.answer("🔄 Запускаю обновление cookies (Chrome открывается, 10–30 сек)…")
        ok, detail = await ctx.health.force_refresh_cookies()
        text = (
            f"✅ Cookies обновлены.\n<i>{_esc(detail)}</i>"
            if ok
            else f"❌ Не получилось.\n<code>{_esc(detail)}</code>"
        )
        try:
            await notice.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            await message.answer(text, parse_mode=ParseMode.HTML)

    @router.message(Command("health"))
    async def cmd_health(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        s = ctx.health.stats_snapshot()
        last_refresh = (
            _fmt_ago(s["last_cookie_refresh_at"])
            if s["last_cookie_refresh_at"]
            else "никогда"
        )
        token_source = "БД (/set_token)" if await ctx.storage.get_deepseek_token() else "env .env"
        ai_last_ok = "—"
        if s["ai_total"] and s["ai_ok"]:
            pct = int(100 * s["ai_ok"] / s["ai_total"])
            ai_last_ok = f"{pct}% успешных ({s['ai_ok']}/{s['ai_total']})"
        await message.answer(
            "🩺 <b>Диагностика</b>\n"
            f"AI: {ai_last_ok}\n"
            f" • пустых: {s['ai_empty']} (стрик {s['ai_empty_streak']})\n"
            f" • 401 auth: {s['ai_auth']} (стрик {s['ai_auth_streak']})\n"
            f" • не-JSON: {s['ai_unparseable']} (стрик {s['ai_unparseable_streak']})\n"
            f" • сеть/прокси: {s['ai_transport']} (стрик {s['ai_transport_streak']})\n"
            f"kwork: {s['kwork_total']} опросов, ошибок {s['kwork_fail']} "
            f"(стрик {s['kwork_fail_streak']}), пустые стрики {s['kwork_empty_streak']}\n"
            f"Последний refresh cookies: {last_refresh}\n"
            f"Refresh сейчас идёт: {'да' if s['refresh_in_progress'] else 'нет'}\n"
            f"Источник DeepSeek токена: {token_source}",
            parse_mode=ParseMode.HTML,
        )

    # Fallback: swallow anything else quietly for non-owners, friendly note for owner.
    @router.message(F.text)
    async def cmd_fallback(message: Message) -> None:
        if not _is_owner(ctx, message.chat.id):
            return
        await message.answer(
            "Не понял команду. /start — список команд.",
        )

    return router


def _mode_label(mode: str) -> str:
    if mode == MODE_ALL:
        return "все проекты"
    if mode == MODE_INTERESTING:
        return "только интересные (AI)"
    return mode


def _fmt_ago(ts: float) -> str:
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta} сек назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    return f"{delta // 3600} ч назад"


def _esc(s: str) -> str:
    from html import escape

    return escape(s or "", quote=False)


async def _validate_token(ctx: "BotContext") -> tuple[bool, str]:
    """Quickly check that the current DeepSeek token actually works.

    Sends a trivial prompt and expects a non-empty, non-error reply. Used
    right after /set_token to confirm the new credential end-to-end.
    """
    # Re-use a tiny Project-like stub. We go through DeepSeekFilter so both
    # the session pool and cookies layers are exercised.
    class _Stub:
        id = 0
        title = "ping"
        description = "Reply with exactly one word: PONG"
        price_limit = 0.0
        possible_price_limit = 0.0
        category_id = 0
        parent_category_id = 0
        parent_category_name = "health"
        username = None
        profile_url = None
        offers_count = 0
        date_create = ""
        date_active = ""
        price_human = "0 ₽"
        is_hard_blocked = False
        url = ""

    try:
        decision = await ctx.ai.classify(_Stub())  # type: ignore[arg-type]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if decision.status == "ok":
        return True, decision.reason or decision.raw[:120]
    if decision.status == "empty":
        return False, "AI вернул пусто (cookies?)"
    if decision.status == "auth":
        return False, "401 — токен отвергнут DeepSeek"
    if decision.status == "unparseable":
        # That's actually a success for us — AI responded with text.
        return True, f"ответ без JSON: {decision.raw[:120]}"
    return False, decision.reason[:160]


def build_bot(config: Config) -> Bot:
    return Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def setup_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        log.exception("Failed to set bot commands")


def build_dispatcher(ctx: BotContext) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(ctx))
    return dp


__all__ = ["BotContext", "build_bot", "build_dispatcher", "setup_commands"]
