"""Runtime self-healing: detect expired cookies / auth token / kwork outages
and either auto-recover (cookie refresh) or poke the owner in Telegram.

Signals observed:
    AI.status == 'empty'         → cookies likely dead (WAF challenge)
    AI.status == 'auth'          → DeepSeek auth token rejected (401)
    AI.status == 'unparseable'   → model isn't following prompt (soft)
    AI.status == 'transport'     → proxy down / network flake
    kwork fetch raised           → kwork blocked / rate-limited us
    kwork returned 0 projects    → parser regression
    kwork returned HTTP >= 400   → ban / captcha
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode

from .deepseek_filter import DeepSeekFilter, FilterDecision
from .storage import Storage

log = logging.getLogger(__name__)


# --- thresholds / throttles ------------------------------------------------- #

# AI-side
EMPTY_STREAK_FOR_REFRESH = 3       # empty replies in a row → trigger refresh
AUTH_STREAK_FOR_ALERT = 2          # 401s in a row → alert user
UNPARSEABLE_STREAK_FOR_ALERT = 5   # model giving prose → soft alert
TRANSPORT_STREAK_FOR_ALERT = 5     # proxy/network failure streak

# Kwork-side
KWORK_FETCH_FAILS_FOR_ALERT = 3
KWORK_EMPTY_PAGE_FOR_ALERT = 2

# How often we re-alert about the SAME issue (seconds).
ALERT_THROTTLE = 30 * 60            # 30 min per kind
COOKIE_REFRESH_COOLDOWN = 10 * 60   # don't re-run refresh more often than this

# Hard cap on a single refresh run (safety in case Chrome hangs).
COOKIE_REFRESH_TIMEOUT = 180        # seconds


class HealthMonitor:
    """Owns all resilience counters + auto-recovery triggers."""

    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        ai: DeepSeekFilter,
        owner_chat_id: int | None,
        *,
        cookie_refresh_script: Optional[Path] = None,
        cookie_refresh_url: Optional[str] = None,
        cookie_refresh_secret: Optional[str] = None,
    ):
        self._bot = bot
        self._storage = storage
        self._ai = ai
        self._owner_chat_id = owner_chat_id
        self._cookie_refresh_script = cookie_refresh_script
        self._cookie_refresh_url = cookie_refresh_url
        self._cookie_refresh_secret = cookie_refresh_secret

        # Rolling streak counters.
        self._ai_empty_streak = 0
        self._ai_auth_streak = 0
        self._ai_unparseable_streak = 0
        self._ai_transport_streak = 0
        self._kwork_fail_streak = 0
        self._kwork_empty_streak = 0

        # Totals since start (for /health).
        self._ai_total = 0
        self._ai_ok_total = 0
        self._ai_empty_total = 0
        self._ai_auth_total = 0
        self._ai_unparseable_total = 0
        self._ai_transport_total = 0
        self._kwork_total = 0
        self._kwork_fail_total = 0

        # Timestamps for throttling.
        self._last_alert_at: dict[str, float] = {}
        self._last_cookie_refresh_at = 0.0

        # Refresh task + lock.
        self._refresh_lock = asyncio.Lock()
        self._refresh_in_progress = False

    # -------- public setters (used when owner changes via /start) ------- #

    def set_owner(self, chat_id: int | None) -> None:
        self._owner_chat_id = chat_id

    # -------- observation points --------------------------------------- #

    async def observe_ai(self, decision: FilterDecision) -> None:
        self._ai_total += 1
        status = decision.status

        if status == "ok":
            self._ai_ok_total += 1
            self._ai_empty_streak = 0
            self._ai_auth_streak = 0
            self._ai_unparseable_streak = 0
            self._ai_transport_streak = 0
            return

        if status == "empty":
            self._ai_empty_total += 1
            self._ai_empty_streak += 1
            self._ai_auth_streak = 0
            self._ai_unparseable_streak = 0
            self._ai_transport_streak = 0
            if self._ai_empty_streak >= EMPTY_STREAK_FOR_REFRESH:
                await self._handle_cookies_expired()
            return

        if status == "auth":
            self._ai_auth_total += 1
            self._ai_auth_streak += 1
            self._ai_empty_streak = 0
            self._ai_unparseable_streak = 0
            self._ai_transport_streak = 0
            if self._ai_auth_streak >= AUTH_STREAK_FOR_ALERT:
                await self._handle_auth_expired()
            return

        if status == "unparseable":
            self._ai_unparseable_total += 1
            self._ai_unparseable_streak += 1
            self._ai_empty_streak = 0
            self._ai_auth_streak = 0
            self._ai_transport_streak = 0
            if self._ai_unparseable_streak >= UNPARSEABLE_STREAK_FOR_ALERT:
                await self._alert(
                    "unparseable",
                    "⚠️ AI перестал отвечать в формате JSON. "
                    "Проверь промпт (/filter) или сбрось: /filter_reset.",
                )
            return

        if status == "transport":
            self._ai_transport_total += 1
            self._ai_transport_streak += 1
            self._ai_empty_streak = 0
            self._ai_auth_streak = 0
            self._ai_unparseable_streak = 0
            if self._ai_transport_streak >= TRANSPORT_STREAK_FOR_ALERT:
                await self._alert(
                    "transport",
                    "❌ AI недоступен (сеть/прокси). Проверь FreeDeepSeekAPI: "
                    "<code>tail -f /tmp/kwork-bot-logs/freedeepseek.log</code>",
                )

    async def observe_kwork_success(self, projects_on_page: int) -> None:
        self._kwork_total += 1
        self._kwork_fail_streak = 0
        if projects_on_page == 0:
            self._kwork_empty_streak += 1
            if self._kwork_empty_streak >= KWORK_EMPTY_PAGE_FOR_ALERT:
                await self._alert(
                    "kwork_empty",
                    "⚠️ kwork.ru возвращает 0 проектов на странице. "
                    "Возможно, изменилась структура HTML или бот забанен по IP.",
                )
        else:
            self._kwork_empty_streak = 0

    async def observe_kwork_error(self, exc: Exception) -> None:
        self._kwork_total += 1
        self._kwork_fail_total += 1
        self._kwork_fail_streak += 1
        if self._kwork_fail_streak >= KWORK_FETCH_FAILS_FOR_ALERT:
            await self._alert(
                "kwork_error",
                f"❌ kwork.ru {self._kwork_fail_streak} раза подряд вернул ошибку: "
                f"<code>{type(exc).__name__}: {str(exc)[:120]}</code>",
            )

    # -------- public actions (also wired to /commands) ----------------- #

    async def force_refresh_cookies(self) -> tuple[bool, str]:
        """Manually trigger cookie refresh. Returns (success, message)."""
        return await self._refresh_cookies_now(initiated_by="manual")

    def stats_snapshot(self) -> dict:
        return {
            "ai_total": self._ai_total,
            "ai_ok": self._ai_ok_total,
            "ai_empty": self._ai_empty_total,
            "ai_auth": self._ai_auth_total,
            "ai_unparseable": self._ai_unparseable_total,
            "ai_transport": self._ai_transport_total,
            "ai_empty_streak": self._ai_empty_streak,
            "ai_auth_streak": self._ai_auth_streak,
            "ai_unparseable_streak": self._ai_unparseable_streak,
            "ai_transport_streak": self._ai_transport_streak,
            "kwork_total": self._kwork_total,
            "kwork_fail": self._kwork_fail_total,
            "kwork_fail_streak": self._kwork_fail_streak,
            "kwork_empty_streak": self._kwork_empty_streak,
            "last_cookie_refresh_at": self._last_cookie_refresh_at,
            "refresh_in_progress": self._refresh_in_progress,
        }

    # -------- internals ------------------------------------------------ #

    async def _handle_cookies_expired(self) -> None:
        now = time.time()
        if now - self._last_cookie_refresh_at < COOKIE_REFRESH_COOLDOWN:
            # We recently refreshed and it's still failing — problem is elsewhere.
            await self._alert(
                "cookies_still_bad",
                "⚠️ Cookies уже обновлялись недавно, но AI опять молчит. "
                "Возможно, протух сам токен DeepSeek — пришли новый через "
                "<code>/set_token НОВЫЙ_ТОКЕН</code>.",
            )
            return

        await self._alert(
            "cookies_expired",
            "🔄 AI вернул пусто 3 раза подряд — вероятно, истекли cookies. "
            "Запускаю автоматическое обновление…",
            throttle=False,
        )

        ok, msg = await self._refresh_cookies_now(initiated_by="auto")
        if ok:
            self._ai_empty_streak = 0
            await self._send(
                f"✅ Cookies обновлены автоматически.\n<i>{msg}</i>\n"
                "Продолжаю мониторинг."
            )
        else:
            await self._send(
                f"❌ Авто-обновление cookies не удалось:\n<code>{msg}</code>\n"
                "Запусти вручную: <code>cd FreeDeepSeekAPI && ./refresh_cookies.sh</code>"
            )

    async def _handle_auth_expired(self) -> None:
        await self._alert(
            "auth_expired",
            "🔑 DeepSeek отвергает токен (401). Время обновить:\n"
            "1. Открой https://chat.deepseek.com и залогинься\n"
            "2. F12 → Console:\n"
            "<code>JSON.parse(localStorage.getItem(\"userToken\")).value</code>\n"
            "3. Пришли: <code>/set_token НОВЫЙ_ТОКЕН</code>",
        )

    async def _refresh_cookies_now(self, *, initiated_by: str) -> tuple[bool, str]:
        async with self._refresh_lock:
            if self._refresh_in_progress:
                return False, "refresh уже идёт"
            self._refresh_in_progress = True
        try:
            if self._cookie_refresh_url:
                return await self._refresh_cookies_via_http(initiated_by)
            return await self._refresh_cookies_via_script(initiated_by)
        finally:
            self._refresh_in_progress = False

    async def _refresh_cookies_via_http(self, initiated_by: str) -> tuple[bool, str]:
        url = self._cookie_refresh_url or ""
        headers = {}
        if self._cookie_refresh_secret:
            headers["Authorization"] = f"Bearer {self._cookie_refresh_secret}"
        log.info("Running cookie refresh via HTTP (%s): %s", initiated_by, url)
        try:
            timeout = aiohttp.ClientTimeout(total=COOKIE_REFRESH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        return False, f"HTTP {resp.status}: {body[:400]}"
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {"ok": True, "log_tail": body[:400]}
        except asyncio.TimeoutError:
            return False, "refresh HTTP timed out"
        except aiohttp.ClientError as exc:
            return False, f"refresh HTTP failed: {exc}"
        self._last_cookie_refresh_at = time.time()
        tail = str(data.get("log_tail", ""))[:500]
        if data.get("ok"):
            return True, tail or "ok"
        return False, tail or "refresh endpoint reported failure"

    async def _refresh_cookies_via_script(self, initiated_by: str) -> tuple[bool, str]:
        script = self._cookie_refresh_script
        if not script or not script.exists():
            return False, f"refresh_cookies.sh не найден: {script}"
        if not os.access(script, os.X_OK):
            return False, f"{script} не исполняемый"

        log.info("Running cookie refresh via script (%s): %s", initiated_by, script)
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        proc = await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(script.parent),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw_out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=COOKIE_REFRESH_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "refresh timed out"

        out = raw_out.decode("utf-8", "ignore")
        tail = "\n".join(out.splitlines()[-6:])
        self._last_cookie_refresh_at = time.time()

        if proc.returncode == 0:
            return True, tail[:500]
        return False, f"exit={proc.returncode}\n{tail[:500]}"

    async def _alert(self, kind: str, text: str, *, throttle: bool = True) -> None:
        if throttle:
            now = time.time()
            last = self._last_alert_at.get(kind, 0.0)
            if now - last < ALERT_THROTTLE:
                return
            self._last_alert_at[kind] = now
        await self._send(text)

    async def _send(self, text: str) -> None:
        if self._owner_chat_id is None:
            log.info("Health alert (no owner yet): %s", text)
            return
        try:
            await self._bot.send_message(
                self._owner_chat_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Failed to deliver health alert to owner")


__all__ = ["HealthMonitor"]
