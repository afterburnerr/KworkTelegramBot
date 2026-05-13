"""Classify kwork projects as 'interesting' or not via the local FreeDeepSeekAPI.

FreeDeepSeekAPI exposes an OpenAI-compatible endpoint, so we use the official
OpenAI SDK pointed at 127.0.0.1:8080/v1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI
from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

from .kwork_parser import Project

log = logging.getLogger(__name__)

# Default filter prompt. Editable at runtime via /filter command in the bot.
DEFAULT_FILTER_PROMPT = (
    "Ты — помощник фрилансера-разработчика, у которого под рукой AI. Тебе дают "
    "описание заказа с биржи kwork.ru, нужно решить — интересен ли этот заказ.\n"
    "\n"
    "Принцип: интересно всё, что можно выполнить силами разработчика ИЛИ AI. "
    "Не интересно — то, для чего нужны физический труд, живой голос, художественный "
    "навык руками, очная встреча, специализированная лицензия или постоянное "
    "дежурство человека.\n"
    "\n"
    "ИНТЕРЕСНО:\n"
    "• Разработка и IT: веб/бэкенд/фронтенд, мобильные приложения, скрипты, "
    "парсинг, чат-боты, Telegram Mini Apps, ИИ/ML, автоматизация, интеграции, "
    "API, базы данных, DevOps, администрирование серверов, десктоп-программы, "
    "разработка игр, тестирование, кибербезопасность.\n"
    "• Любые тексты (AI делает это за минуты): копирайт, рерайт, переводы "
    "(письменные), набор текста, OCR с изображений, расшифровка аудио/видео "
    "в текст, SEO-тексты, статьи и наполнение сайтов, карточки товаров, "
    "продающие и бизнес-тексты, сценарии, посты для соцсетей, комментарии, "
    "корректура, резюме и сопроводительные письма, скрипты продаж.\n"
    "• ИИ-тексты и ИИ-обработка текстов.\n"
    "• Работа с данными: сбор, анализ, чистка, структурирование, Excel/Google "
    "Sheets, поиск информации, любая интеллектуальная удалённая работа.\n"
    "• Персональный помощник — если задача сводится к работе с текстом или "
    "данными (не к телефонным звонкам).\n"
    "• Консультации — только если они про IT/разработку/AI.\n"
    "\n"
    "НЕ ИНТЕРЕСНО:\n"
    "• Весь дизайн: графический, веб-дизайн, логотипы, иллюстрации, баннеры, "
    "презентации (если требуется именно дизайн, а не текст), полиграфия, "
    "наружная реклама, UI-макеты.\n"
    "• 3D-моделирование, 3D-визуализация, интерьер/экстерьер, ландшафтный "
    "дизайн, промышленный дизайн.\n"
    "• Видеосъёмка, монтаж и обработка видео, интро, анимация, слайд-шоу, "
    "ИИ-генерация видео (требует долгих рендеров).\n"
    "• Озвучка и запись голоса, музыка и песни, аранжировки.\n"
    "• Устные переводы, синхрон, репетиторство вживую.\n"
    "• SMM-ведение страниц, рассылки в личку, отправка сообщений подписчикам.\n"
    "• Холодный обзвон, продажи по телефону, приём звонков.\n"
    "• Подбор персонала, подбор резюме (поиск живых людей).\n"
    "• SEO-ссылки в профилях/форумах/комментариях/крауд, накрутки.\n"
    "• Юридические услуги, договора, судебные документы, визы.\n"
    "• Бухгалтерия, налоги, финансовый консалтинг.\n"
    "• Строительство, ремонт, инженерные системы (отопление, водопровод), "
    "проектирование зданий, машиностроение.\n"
    "• Продажа готовых сайтов/групп/доменов."
)

SYSTEM_INSTRUCTIONS = (
    "Отвечай ТОЛЬКО валидным JSON объектом вида:\n"
    '{"interesting": true|false, "reason": "кратко, 1-12 слов", '
    '"pitch": "текст отклика для клиента или пустая строка"}\n'
    "\n"
    "Никакого текста до или после JSON. Никаких markdown-блоков.\n"
    "Поле interesting — boolean, никогда не null.\n"
    "Поле reason — краткое пояснение на русском (1-12 слов).\n"
    "\n"
    "Поле pitch:\n"
    "• Если interesting=false — pusto (\"\").\n"
    "• Если interesting=true — готовое сообщение-отклик клиенту на русском, "
    "которое можно отправить как есть. 2-3 коротких предложения, "
    "СТРОГО до 230 символов. Без markdown и эмодзи.\n"
    "• Дружелюбное, живое, без канцелярита (не «Здравствуйте, уважаемый «клиент»», "
    "не «Профессионально и качественно выполню»). Обычное человеческое начало — "
    "«Здравствуйте!» или «Привет!». Заверши открытой фразой, побуждающей ответить "
    "(например, «Напишите, если удобно обсудить»).\n"
    "• Обязательно упомяни конкретику задачи (например: «парсер Яндекс-карт», "
    "«Telegram-бот на Python», «вёрстка лендинга на Tilda»), а не общие слова.\n"
    "• Можно кратко сказать об опыте в 1-2 слова, но без хвастовства и без списков.\n"
    "\n"
    "Пример хорошего pitch:\n"
    '"Привет! Гляну ваш список URL и соберу таблицу с нужными полями — '
    'на Python такое делаю регулярно. Готов обсудить детали и назвать сроки. '
    'Напишите, если интересно."'
)


@dataclass(frozen=True)
class FilterDecision:
    interesting: bool
    reason: str
    raw: str
    # Health signals the caller (pipeline / HealthMonitor) uses to detect
    # stale credentials without having to re-parse the reason string.
    #   'ok'          — normal classification
    #   'empty'       — AI returned empty body (likely WAF/cookie expiry)
    #   'unparseable' — non-empty body that isn't JSON (model ignored prompt)
    #   'auth'        — DeepSeek rejected the auth token (HTTP 401)
    #   'transport'   — network / timeout / other API error
    status: str = "ok"
    # Short, friendly, ready-to-send response for the project author.
    # Empty string when the project was classified as not interesting.
    pitch: str = ""


class DeepSeekFilter:
    """Async DeepSeek classifier with a small per-request timeout and retries."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "deepseek-chat",
        request_timeout: float = 60.0,
        max_retries: int = 2,
        system_prompt: str = DEFAULT_FILTER_PROMPT,
    ):
        self._base_url = base_url
        self._request_timeout = request_timeout
        self._api_key = api_key or "unused"
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=self._api_key,
            timeout=request_timeout,
            max_retries=0,  # we do our own retry loop
        )
        self._model = model
        self._max_retries = max_retries
        self._system_prompt = system_prompt

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    @property
    def api_key(self) -> str:
        return self._api_key

    async def update_token(self, token: str) -> None:
        """Swap the DeepSeek auth token at runtime (e.g. from /set_token).

        The OpenAI SDK doesn't expose mutating api_key after construction in
        a documented way, so we just rebuild the client — it's cheap and
        keeps no persistent server-side state.
        """
        token = (token or "").strip() or "unused"
        old_client = self._client
        self._api_key = token
        self._client = AsyncOpenAI(
            base_url=self._base_url,
            api_key=token,
            timeout=self._request_timeout,
            max_retries=0,
        )
        try:
            await old_client.close()
        except Exception:
            log.debug("Failed to close previous OpenAI client", exc_info=True)

    async def close(self) -> None:
        await self._client.close()

    async def classify(self, project: Project) -> FilterDecision:
        """Ask the model whether a project is interesting.

        On any network/API failure we conservatively return interesting=False
        so the user does not get spammed with noise when the proxy is down,
        but the decision carries an error reason so it can be surfaced.
        """
        user_content = self._render_project(project)

        last_exc: Exception | None = None
        last_kind: str = "transport"
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": self._system_prompt + "\n\n" + SYSTEM_INSTRUCTIONS},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.0,
                    stream=False,
                )
                content = (resp.choices[0].message.content or "").strip()
                return _parse_decision(content)
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                last_exc = exc
                last_kind = "transport"
                wait = 1.5 * (attempt + 1)
                log.warning(
                    "DeepSeek transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, self._max_retries + 1, exc, wait,
                )
                await asyncio.sleep(wait)
            except APIError as exc:
                last_exc = exc
                # Treat 401 specifically so HealthMonitor can differentiate
                # "stale token" from "stale cookies".
                status = getattr(exc, "status_code", None)
                msg_lower = str(exc).lower()
                if status == 401 or "401" in msg_lower or "invalid or expired" in msg_lower:
                    last_kind = "auth"
                else:
                    last_kind = "transport"
                log.error("DeepSeek API error: %s", exc)
                break
            except Exception as exc:  # pragma: no cover - unexpected
                last_exc = exc
                last_kind = "transport"
                log.exception("Unexpected DeepSeek error")
                break

        err = str(last_exc) if last_exc else "unknown error"
        return FilterDecision(
            interesting=False,
            reason=f"ошибка AI: {err[:120]}",
            raw="",
            status=last_kind,
        )

    @staticmethod
    def _render_project(project: Project) -> str:
        # Trim very long descriptions to keep the prompt compact; kwork itself
        # caps descriptions, but some sellers paste extremely long briefs.
        desc = project.description.strip()
        if len(desc) > 1800:
            desc = desc[:1800].rsplit(" ", 1)[0] + " …"

        return (
            f"Категория: {project.parent_category_name} / id={project.category_id}\n"
            f"Бюджет: {project.price_human}\n"
            f"Заголовок: {project.title}\n"
            f"Описание:\n{desc}"
        )


# Hard cap on the pitch length we pass to Telegram's CopyTextButton
# (the API allows up to 256 chars; we stay below that for safety).
_PITCH_MAX_LEN = 240

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _clean_pitch(value: object) -> str:
    """Normalise the pitch string coming back from the model.

    Strips markdown, stray quotes and whitespace; collapses internal newlines
    (Telegram CopyTextButton renders them oddly); enforces the 240-char cap
    with a trailing ellipsis so callers can feed it directly into the button.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    # Sometimes the model wraps the pitch in extra quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'", "«", "»"):
        text = text[1:-1].strip()
    # Collapse newlines / tabs into single spaces.
    text = " ".join(text.split())
    if len(text) > _PITCH_MAX_LEN:
        text = text[: _PITCH_MAX_LEN - 1].rsplit(" ", 1)[0] + "…"
    return text


def _parse_decision(content: str) -> FilterDecision:
    """Tolerantly parse the JSON reply.

    The model *should* obey the system prompt and return strict JSON, but we
    also handle the common case where it wraps the JSON in a ```json ...``` block.
    """
    cleaned = content.strip()

    # Strip markdown fences if present.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    # Empty body — almost always means DeepSeek returned a WAF/CF challenge
    # and the proxy's streaming parser produced no text chunks. Surface
    # this clearly so HealthMonitor can trigger a cookies refresh.
    if not cleaned:
        return FilterDecision(
            interesting=False,
            reason="пустой ответ AI (возможно, протухли cookies)",
            raw=content,
            status="empty",
        )

    # Try direct JSON parse, then fall back to "find first {...}".
    for candidate in (cleaned, _first_json_object(cleaned)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        interesting = bool(obj.get("interesting"))
        reason = str(obj.get("reason") or "").strip()[:160] or "—"
        pitch = _clean_pitch(obj.get("pitch")) if interesting else ""
        return FilterDecision(
            interesting=interesting,
            reason=reason,
            raw=content,
            status="ok",
            pitch=pitch,
        )

    # Last resort: look for the words "yes"/"да"/"true" near "interesting".
    low = cleaned.lower()
    positive = any(tok in low for tok in ("\"interesting\": true", "«да»", "интересно: да"))
    return FilterDecision(
        interesting=positive,
        reason="ответ AI не распознан",
        raw=content,
        status="unparseable",
    )


def _first_json_object(text: str) -> str | None:
    match = _JSON_RE.search(text)
    return match.group(0) if match else None


__all__ = ["DeepSeekFilter", "FilterDecision", "DEFAULT_FILTER_PROMPT"]
