"""Entry point: wire config, storage, AI, poller, scanner and bot together."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import cast

from .bot import BotContext, build_bot, build_dispatcher, setup_commands
from .config import Config, load_config
from .deepseek_filter import DEFAULT_FILTER_PROMPT, DeepSeekFilter
from .health import HealthMonitor
from .poller import Poller
from .scanner import Scanner
from .storage import Storage

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Lower the noise from http libs at INFO.
    for noisy in ("aiogram.event", "httpcore", "httpx", "openai._base_client", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def run() -> None:
    config = load_config()
    _configure_logging(config.log_level)

    log.info(
        "Starting kwork bot: model=%s base=%s interval=%ds owner=%s",
        config.deepseek_model,
        config.deepseek_api_base,
        config.kwork_poll_interval,
        config.telegram_owner_chat_id,
    )

    storage = Storage(config.sqlite_path)

    # Recover owner across restarts: prefer env var, otherwise pick up the
    # chat that ran /start during a previous launch.
    owner_chat_id = config.telegram_owner_chat_id
    if owner_chat_id is None:
        owner_chat_id = await storage.find_owner_chat_id()
        if owner_chat_id is not None:
            log.info("Recovered owner_chat_id=%s from storage", owner_chat_id)

    # DeepSeek auth token precedence:
    #   1. token previously set via /set_token (meta table)
    #   2. DEEPSEEK_API_KEY from env (.env)
    stored_token = await storage.get_deepseek_token()
    effective_token = stored_token or config.deepseek_api_key
    if stored_token:
        log.info("Using DeepSeek token from storage (/set_token)")

    # If the owner has a saved custom prompt, use it; otherwise default.
    saved_prompt: str | None = None
    if owner_chat_id is not None:
        saved = await storage.get_settings(owner_chat_id)
        saved_prompt = saved.filter_prompt
    ai = DeepSeekFilter(
        base_url=config.deepseek_api_base,
        api_key=effective_token,
        model=config.deepseek_model,
        system_prompt=saved_prompt or DEFAULT_FILTER_PROMPT,
    )

    bot = build_bot(config)

    # Locate the cookie-refresh helper. Precedence:
    #   1. COOKIE_REFRESH_URL (set in Docker/server deployments where the
    #      proxy exposes /admin/refresh-cookies)
    #   2. COOKIE_REFRESH_SCRIPT env (explicit path)
    #   3. ../FreeDeepSeekAPI/refresh_cookies.sh sibling repo (local dev)
    refresh_script: Path | None = config.cookie_refresh_script
    if refresh_script is None and not config.cookie_refresh_url:
        refresh_script = (
            Path(__file__).resolve().parent.parent.parent
            / "FreeDeepSeekAPI"
            / "refresh_cookies.sh"
        )
    if refresh_script is not None and not refresh_script.exists():
        log.warning(
            "Cookie refresh script not found at %s — auto-refresh via script disabled.",
            refresh_script,
        )
        refresh_script = None

    refresh_secret = config.cookie_refresh_secret or ""
    # If the caller didn't set a dedicated secret, fall back to whatever
    # DEEPSEEK key the bot has (that's what the proxy's /admin endpoint
    # accepts by default).
    if config.cookie_refresh_url and not refresh_secret:
        stored = await storage.get_deepseek_token()
        refresh_secret = stored or config.deepseek_api_key

    health = HealthMonitor(
        bot=bot,
        storage=storage,
        ai=ai,
        owner_chat_id=owner_chat_id,
        cookie_refresh_script=refresh_script,
        cookie_refresh_url=config.cookie_refresh_url or None,
        cookie_refresh_secret=refresh_secret or None,
    )

    poller = Poller(bot, storage, ai, config, owner_chat_id or 0, health=health)
    scanner = Scanner(bot, storage, ai, config, owner_chat_id or 0, health=health)

    ctx = BotContext(
        config=config,
        storage=storage,
        ai=ai,
        poller=poller,
        scanner=scanner,
        health=health,
        owner_chat_id=owner_chat_id,
    )

    dp = build_dispatcher(ctx)

    await setup_commands(bot)

    # Only start the poller if we know the owner. Otherwise wait until the
    # user claims the bot via /start, then start it from there.
    if ctx.owner_chat_id is not None:
        poller.start()
    else:
        log.warning(
            "TELEGRAM_OWNER_CHAT_ID is not set — waiting for the first /start to claim ownership."
        )
        _install_owner_claim_hook(dp, ctx, poller)

    stop_event = asyncio.Event()

    def _stop_handler(*_: object) -> None:
        log.info("Stop signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except NotImplementedError:
            pass  # Windows

    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False),
        name="aiogram-polling",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, _ = await asyncio.wait(
            {polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc:
                log.exception("Task crashed", exc_info=exc)
    finally:
        log.info("Shutting down…")
        await dp.stop_polling()
        polling_task.cancel()
        await poller.stop()
        if scanner.is_running:
            scanner.cancel()
            await scanner.wait()
        await ai.close()
        await bot.session.close()
        log.info("Goodbye")


def _install_owner_claim_hook(dp, ctx: BotContext, poller: Poller) -> None:  # type: ignore[no-untyped-def]
    """Start the poller as soon as the owner is claimed via /start.

    We inject a lightweight outer middleware that checks ctx.owner_chat_id
    after each update is dispatched.
    """

    started = False

    @dp.update.outer_middleware()
    async def _hook(handler, event, data):  # type: ignore[no-untyped-def]
        nonlocal started
        result = await handler(event, data)
        if not started and ctx.owner_chat_id is not None:
            started = True
            poller.start()
            log.info("Poller started after owner was claimed")
        return result


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
