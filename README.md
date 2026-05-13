# KworkTelegramBot

Telegram-бот, который в реальном времени мониторит биржу заказов
[kwork.ru/projects](https://kwork.ru/projects) и через локальный DeepSeek
([github.com/afterburnerr/FreeDeepSeekAPI](../FreeDeepSeekAPI)) отбирает интересные заказы для
разработчика / инженера.

> **Важно:** проект работает в паре с локальным OpenAI-совместимым
> прокси к chat.deepseek.com. Ванильный
> [`LeadroyaL/FreeDeepSeekAPI`](https://github.com/LeadroyaL/FreeDeepSeekAPI)
> из-за смены стримингового формата и AWS-WAF **не заработает** без патчей
> из этого репо (см. `DEPLOY.md` — раздел про установку и наши изменения в
> `dsk/api.py`, `main.py`, `refresh_cookies.sh`). Проще всего поднять
> через приложенный `docker-compose.yml` — он собирает прокси из
> сиблинг-директории `../FreeDeepSeekAPI`.

## Что умеет

- Читает `window.stateData` прямо с HTML-страницы биржи (без входа, без API,
  без Playwright).
- Переключает режимы:
  - **все проекты** — присылает каждый новый заказ.
  - **только интересные** (по умолчанию) — сначала отсекает заведомо
    неинтересные категории (весь дизайн, 3D, видео-монтаж, музыка, озвучка),
    потом отправляет описание в DeepSeek. Бот присылает только те, что
    попадают под профиль инженера.
- Помнит уже показанные проекты в SQLite, так что при рестарте ничего не
  дублируется.
- Команда `/scan` проходится по всем ~50 страницам биржи разом и
  пушит в чат все интересные проекты с живым прогрессом.
- AI-промпт редактируется на лету (`/filter ...`), сброс — `/filter_reset`.
- Пауза/возобновление — `/pause`, `/resume`.

## Команды бота

| Команда | Что делает |
| --- | --- |
| `/start` | Приветствие + закрепление chat_id как владельца |
| `/status` | Режим, пауза, число seen-проектов, время последнего опроса |
| `/mode_all` | Присылать все новые проекты без AI-фильтра |
| `/mode_interesting` | Только интересные (AI) — дефолт |
| `/pause`, `/resume` | Выключить / включить уведомления |
| `/check` | Прямо сейчас проверить 1-ю страницу |
| `/scan [N]` | Пройти всю биржу (или первые N страниц) и найти интересные |
| `/cancel` | Прервать текущий `/scan` |
| `/filter [текст]` | Показать / задать AI-промпт |
| `/filter_reset` | Сбросить AI-промпт к дефолту |

## Архитектура

```
kwork.ru/projects (HTML, window.stateData)
        │
        ▼
 kwork_parser.py ── Project dataclass
        │
        ▼ (для каждого unseen проекта)
 pipeline.process_project ──▶ DeepSeek (FreeDeepSeekAPI 127.0.0.1:8080/v1)
        │                          │
        │                          ▼
        │                  {interesting, reason}
        ▼
 notifier.send_project ─▶ aiogram 3 ─▶ Telegram
        │
        ▼
 storage.mark_seen (SQLite)
```

Два триггера пайплайна:

- `poller.Poller` — цикл `while True: fetch page 1; process unseen`.
- `scanner.Scanner` — по `/scan`: обходит `pagination.last_page` страниц.

## Установка

1. Поднят и работает `FreeDeepSeekAPI`:
   ```bash
   cd ../FreeDeepSeekAPI
   cp .env.example .env            # положи туда свой DeepSeek auth token
   pip install -r requirements.txt
   python main.py                  # слушает 127.0.0.1:8080
   ```

2. Создай бота у [@BotFather](https://t.me/BotFather) и возьми токен.

3. Настрой `.env`:
   ```bash
   cp .env.example .env
   $EDITOR .env
   ```
   Минимум: `TELEGRAM_BOT_TOKEN`. Остальное — по вкусу. `TELEGRAM_OWNER_CHAT_ID`
   можно не указывать: первый пользователь, написавший `/start`, автоматически
   станет владельцем и запишется в конфиг-стейт бота.

4. Зависимости:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

   Если системный Python слишком свежий для сборки `pydantic-core`, поставь
   Python 3.12 через [uv](https://github.com/astral-sh/uv):
   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ```

5. Запусти:
   ```bash
   python run.py
   # или
   python -m kwork_bot.main
   ```

## Docker

```bash
cp .env.example .env       # и отредактируй
docker compose up --build -d
docker compose logs -f kwork-bot
```

`docker-compose.yml` поднимает и FreeDeepSeekAPI, и бота в одной сети.
Если прокси уже запущен отдельно — закомментируй сервис `deepseek` и
пропиши `DEEPSEEK_API_BASE` в `.env`.

## Тонкости

- При первом запуске бот помечает `KWORK_SEED_SEEN` (по умолчанию 12 —
  ровно одна страница) самых старых проектов как «уже видел», чтобы не
  завалить чат сразу всеми текущими заказами.
- На цикл опроса бот тратит максимум `KWORK_MAX_AI_PER_CYCLE` AI-вызовов
  (по умолчанию 8). Если бот был долго оффлайн и накопилось много новых
  проектов, остаток обработается в следующих циклах.
- Команда `/scan` обрабатывает только **новые** проекты (те, что бот ещё
  не видел). Если хочешь переклассифицировать всё с нуля — удали файл
  `data/kwork_bot.sqlite3` и запусти бота заново.
- Фильтр «заведомо неинтересных» категорий (`HARD_BLOCKED_CATEGORIES` в
  `kwork_parser.py`) срабатывает ДО AI, чтобы сэкономить запросы:
  дизайн, 3D, видео, озвучка, музыка отбрасываются сразу.
- DeepSeek используется в **не-reasoning** режиме (`deepseek-chat`) —
  быстрее и дешевле для бинарной классификации. Хотите умнее, но
  медленнее — поставьте `DEEPSEEK_MODEL=deepseek-reasoner`.
