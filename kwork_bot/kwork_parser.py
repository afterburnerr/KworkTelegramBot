"""Fetch kwork.ru/projects and extract the project list from window.stateData."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Iterable

import aiohttp

log = logging.getLogger(__name__)

PROJECTS_URL = "https://kwork.ru/projects"

# A modern, desktop User-Agent is required or the server may serve a stripped
# mobile version / challenge page.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

# Parent category id -> human-readable name.
PARENT_CATEGORIES: dict[int, str] = {
    15: "Дизайн",
    11: "Разработка и IT",
    5: "Тексты и переводы",
    17: "SEO и трафик",
    45: "Соцсети и маркетинг",
    7: "Аудио, видео, съемка",
    83: "Бизнес и жизнь",
}

# Leaf category id -> parent category id.
# Derived from window.stateData.categories of kwork.ru/projects.
CATEGORY_TO_PARENT: dict[int, int] = {
    # Дизайн (15)
    25: 15, 24: 15, 28: 15, 27: 15, 90: 15, 250: 15, 306: 15,
    270: 15, 68: 15, 272: 15, 286: 15,
    # Разработка и IT (11)
    38: 11, 37: 11, 41: 11, 79: 11, 80: 11, 39: 11, 40: 11,
    255: 11, 81: 11,
    # Тексты и переводы (5)
    235: 5, 75: 5, 74: 5, 73: 5, 35: 5, 303: 5,
    # SEO и трафик (17)
    72: 17, 71: 17, 59: 17, 56: 17, 44: 17, 43: 17, 273: 17,
    # Соцсети и маркетинг (45)
    113: 45, 112: 45, 108: 45, 49: 45, 47: 45, 46: 45,
    # Аудио, видео, съемка (7)
    106: 7, 78: 7, 77: 7, 76: 7, 23: 7, 20: 7, 300: 7,
    # Бизнес и жизнь (83)
    114: 83, 84: 83, 64: 83, 63: 83, 55: 83, 262: 83, 265: 83, 65: 83,
}

# Categories that are *a priori* uninteresting for a developer/engineer: design
# of any kind, illustrations, 3D, video and audio production, voice-overs,
# music. We short-circuit these without even hitting the AI.
HARD_BLOCKED_CATEGORIES: frozenset[int] = frozenset({
    # All of Дизайн
    25, 24, 28, 27, 90, 250, 306, 270, 68, 272, 286,
    # All of Аудио, видео, съемка
    106, 78, 77, 76, 23, 20, 300,
})


@dataclass(frozen=True)
class Project:
    id: int
    title: str
    description: str
    price_limit: float
    possible_price_limit: float
    category_id: int
    parent_category_id: int
    parent_category_name: str
    username: str | None
    profile_url: str | None
    offers_count: int
    date_create: str
    date_active: str

    @property
    def url(self) -> str:
        return f"https://kwork.ru/projects/{self.id}/view"

    @property
    def price_human(self) -> str:
        if self.possible_price_limit and self.possible_price_limit != self.price_limit:
            return (
                f"{int(self.price_limit):,} – {int(self.possible_price_limit):,} ₽"
            ).replace(",", " ")
        return f"{int(self.price_limit):,} ₽".replace(",", " ")

    @property
    def is_hard_blocked(self) -> bool:
        """Categories we always filter out in 'interesting' mode."""
        return self.category_id in HARD_BLOCKED_CATEGORIES


class KworkParseError(RuntimeError):
    """Raised when the HTML does not contain the expected state blob."""


# Regex to find the start of the stateData assignment. We don't try to match
# the whole JSON with a regex (that's unreliable for nested braces / strings);
# instead we locate the starting `{` and then scan for its balanced close.
_STATE_START_RE = re.compile(r"window\.stateData\s*=\s*")


def _extract_state_data(html: str) -> dict:
    match = _STATE_START_RE.search(html)
    if not match:
        raise KworkParseError("window.stateData assignment not found in HTML")

    start = html.find("{", match.end())
    if start < 0:
        raise KworkParseError("window.stateData opening brace not found")

    depth = 0
    in_string = False
    escape = False
    i = start
    n = len(html)
    while i < n:
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start : i + 1])
        i += 1

    raise KworkParseError("window.stateData JSON was not balanced")


def _as_float(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_int(v, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


@dataclass(frozen=True)
class PageResult:
    projects: list[Project]
    current_page: int
    last_page: int
    total: int


def parse_projects_from_html(html: str) -> list[Project]:
    return parse_page_from_html(html).projects


def parse_page_from_html(html: str) -> PageResult:
    data = _extract_state_data(html)
    pagination = data.get("wantsListData", {}).get("pagination") or {}
    wants_list = (
        pagination.get("data")
        or data.get("wantsListData", {}).get("wants")
        or data.get("wants")
        or []
    )

    projects: list[Project] = []
    for item in wants_list:
        try:
            projects.append(_project_from_raw(item))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Failed to parse project id=%s: %s", item.get("id"), exc)
            continue

    return PageResult(
        projects=projects,
        current_page=_as_int(pagination.get("current_page"), 1),
        last_page=_as_int(pagination.get("last_page"), 1),
        total=_as_int(pagination.get("total"), len(projects)),
    )


def _project_from_raw(raw: dict) -> Project:
    pid = _as_int(raw.get("id"))
    if pid <= 0:
        raise ValueError("missing id")

    category_id = _as_int(raw.get("category_id"))
    parent_id = CATEGORY_TO_PARENT.get(category_id, 0)
    parent_name = PARENT_CATEGORIES.get(parent_id, "Прочее")

    user = raw.get("user") or {}
    username = user.get("username") or None
    profile_url = raw.get("wantUserGetProfileUrl") or None
    if username and not profile_url:
        profile_url = f"https://kwork.ru/user/{username.lower()}"

    return Project(
        id=pid,
        title=unescape(str(raw.get("name") or "").strip()),
        description=unescape(str(raw.get("description") or "").strip()),
        price_limit=_as_float(raw.get("priceLimit")),
        possible_price_limit=_as_float(raw.get("possiblePriceLimit")),
        category_id=category_id,
        parent_category_id=parent_id,
        parent_category_name=parent_name,
        username=username,
        profile_url=profile_url,
        offers_count=_as_int(raw.get("kwork_count"), 0),
        date_create=str(raw.get("date_create") or ""),
        date_active=str(raw.get("date_active") or ""),
    )


class KworkClient:
    """Lightweight async client for kwork.ru/projects."""

    def __init__(self, timeout: float = 20.0):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KworkClient":
        self._session = aiohttp.ClientSession(
            headers=DEFAULT_HEADERS,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_projects(self, page: int = 1) -> list[Project]:
        result = await self.fetch_page(page)
        return result.projects

    async def fetch_page(self, page: int = 1) -> PageResult:
        session = self._session
        if session is None:
            raise RuntimeError("KworkClient must be used as an async context manager")

        url = PROJECTS_URL if page <= 1 else f"{PROJECTS_URL}?page={page}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            html = await resp.text()

        result = parse_page_from_html(html)
        log.debug(
            "Fetched page %d (%d projects, last_page=%d, total=%d) from %s",
            result.current_page, len(result.projects),
            result.last_page, result.total, url,
        )
        return result


async def fetch_once(page: int = 1) -> list[Project]:
    """Convenience helper for one-off fetches (e.g. /check command)."""
    async with KworkClient() as client:
        return await client.fetch_projects(page=page)


def iter_fresh_projects(
    projects: Iterable[Project], seen_ids: set[int]
) -> list[Project]:
    """Return projects whose id is not in the seen set, preserving input order."""
    return [p for p in projects if p.id not in seen_ids]


__all__ = [
    "Project",
    "PageResult",
    "KworkClient",
    "KworkParseError",
    "fetch_once",
    "iter_fresh_projects",
    "parse_projects_from_html",
    "parse_page_from_html",
    "HARD_BLOCKED_CATEGORIES",
    "PARENT_CATEGORIES",
]
