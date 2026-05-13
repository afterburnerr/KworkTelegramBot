"""Persistent state: seen project ids + per-user settings.

We keep SQLite access synchronous because all operations are tiny (single-row
upserts, small IN queries). Calls are wrapped in `asyncio.to_thread` so they
don't block the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MODE_ALL = "all"
MODE_INTERESTING = "interesting"
VALID_MODES = frozenset({MODE_ALL, MODE_INTERESTING})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    chat_id         INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL DEFAULT 'interesting',
    paused          INTEGER NOT NULL DEFAULT 0,
    filter_prompt   TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_projects (
    project_id  INTEGER PRIMARY KEY,
    first_seen  INTEGER NOT NULL,
    decision    TEXT,
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_projects(first_seen);

-- Generic key/value store for secrets and runtime state we don't want to
-- lose on restart (e.g. the DeepSeek auth token updated via /set_token).
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


@dataclass
class ChatSettings:
    chat_id: int
    mode: str = MODE_INTERESTING
    paused: bool = False
    filter_prompt: str | None = None


class Storage:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------- Settings ------------- #

    async def get_settings(self, chat_id: int) -> ChatSettings:
        return await asyncio.to_thread(self._get_settings_sync, chat_id)

    def _get_settings_sync(self, chat_id: int) -> ChatSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id, mode, paused, filter_prompt FROM settings WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return ChatSettings(chat_id=chat_id)
        return ChatSettings(
            chat_id=int(row["chat_id"]),
            mode=str(row["mode"]) if row["mode"] in VALID_MODES else MODE_INTERESTING,
            paused=bool(row["paused"]),
            filter_prompt=row["filter_prompt"],
        )

    async def upsert_settings(self, settings: ChatSettings) -> None:
        await asyncio.to_thread(self._upsert_settings_sync, settings)

    def _upsert_settings_sync(self, s: ChatSettings) -> None:
        now = int(time.time())
        if s.mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {s.mode!r}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (chat_id, mode, paused, filter_prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    mode=excluded.mode,
                    paused=excluded.paused,
                    filter_prompt=excluded.filter_prompt,
                    updated_at=excluded.updated_at
                """,
                (s.chat_id, s.mode, int(s.paused), s.filter_prompt, now, now),
            )

    async def set_mode(self, chat_id: int, mode: str) -> ChatSettings:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode!r}")
        settings = await self.get_settings(chat_id)
        settings.mode = mode
        await self.upsert_settings(settings)
        return settings

    async def set_paused(self, chat_id: int, paused: bool) -> ChatSettings:
        settings = await self.get_settings(chat_id)
        settings.paused = paused
        await self.upsert_settings(settings)
        return settings

    async def set_filter_prompt(self, chat_id: int, prompt: str | None) -> ChatSettings:
        settings = await self.get_settings(chat_id)
        settings.filter_prompt = prompt
        await self.upsert_settings(settings)
        return settings

    # ------------- Seen projects ------------- #

    async def has_seen(self, project_id: int) -> bool:
        return await asyncio.to_thread(self._has_seen_sync, project_id)

    def _has_seen_sync(self, project_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_projects WHERE project_id=?", (project_id,)
            ).fetchone()
        return row is not None

    async def filter_unseen(self, project_ids: list[int]) -> list[int]:
        if not project_ids:
            return []
        return await asyncio.to_thread(self._filter_unseen_sync, project_ids)

    def _filter_unseen_sync(self, project_ids: list[int]) -> list[int]:
        unique = list({int(x) for x in project_ids})
        if not unique:
            return []
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT project_id FROM seen_projects WHERE project_id IN ({placeholders})",
                unique,
            ).fetchall()
        seen = {int(r["project_id"]) for r in rows}
        return [pid for pid in unique if pid not in seen]

    async def mark_seen(
        self,
        project_id: int,
        decision: str | None = None,
        reason: str | None = None,
    ) -> None:
        await asyncio.to_thread(self._mark_seen_sync, project_id, decision, reason)

    def _mark_seen_sync(
        self, project_id: int, decision: str | None, reason: str | None
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_projects (project_id, first_seen, decision, reason)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, now, decision, reason),
            )

    async def mark_seen_bulk(self, project_ids: list[int]) -> None:
        if not project_ids:
            return
        await asyncio.to_thread(self._mark_seen_bulk_sync, project_ids)

    def _mark_seen_bulk_sync(self, project_ids: list[int]) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO seen_projects (project_id, first_seen) VALUES (?, ?)",
                [(int(pid), now) for pid in project_ids],
            )

    async def count_seen(self) -> int:
        return await asyncio.to_thread(self._count_seen_sync)

    def _count_seen_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM seen_projects").fetchone()
        return int(row["c"])

    # ------------- Owner discovery ------------- #

    async def find_owner_chat_id(self) -> int | None:
        """Return the earliest chat_id that has ever run /start.

        Used on startup to recover the owner when TELEGRAM_OWNER_CHAT_ID is
        unset in the environment (e.g. after the very first /start claimed
        ownership and the user didn't bother writing it back to .env).
        """
        return await asyncio.to_thread(self._find_owner_sync)

    def _find_owner_sync(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM settings ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        return int(row["chat_id"]) if row else None

    # ------------- Meta (generic kv) ------------- #

    async def get_meta(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_meta_sync, key)

    def _get_meta_sync(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    async def delete_meta(self, key: str) -> None:
        await asyncio.to_thread(self._delete_meta_sync, key)

    def _delete_meta_sync(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))

    # Convenience wrappers for the DeepSeek auth token. Stored in meta so
    # /set_token persists across bot restarts without touching any .env file.
    META_DEEPSEEK_TOKEN = "deepseek_auth_token"

    async def get_deepseek_token(self) -> str | None:
        return await self.get_meta(self.META_DEEPSEEK_TOKEN)

    async def set_deepseek_token(self, token: str) -> None:
        await self.set_meta(self.META_DEEPSEEK_TOKEN, token)


__all__ = ["Storage", "ChatSettings", "MODE_ALL", "MODE_INTERESTING", "VALID_MODES"]
