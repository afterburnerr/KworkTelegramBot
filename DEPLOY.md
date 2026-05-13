# Как поднять всё это на сервере

Система состоит из двух сервисов, запускаемых одним `docker compose up -d`:

- **`deepseek`** — локальный OpenAI-совместимый прокси к chat.deepseek.com (из
  репозитория `FreeDeepSeekAPI`). В образе сразу лежит Chrome + Xvfb + bypass,
  так что cookies обновляются прямо внутри контейнера — сервер может быть
  полностью headless.
- **`kwork-bot`** — сам Telegram-бот. Лёгкий python-контейнер, триггерит
  обновление cookies в прокси по HTTP когда AI начинает отвечать пусто.

Оба сервиса с `restart: unless-stopped`, healthcheck'ами и именованными
волумами — переживают перезагрузку сервера, rebuild, `docker compose down`.

---

## 1. Требования

Минимальная VPS:

- **OS:** любой Linux с `systemd` (Ubuntu 22.04/24.04, Debian 12 — проверено).
- **RAM:** 2 GB минимум. Chrome внутри прокси может съедать ~400 MB в пике
  при обновлении cookies.
- **CPU:** 1 vCPU хватает.
- **Диск:** ~2 GB (образы + Chrome).
- **Сеть:** исходящий HTTPS к `chat.deepseek.com`, `kwork.ru`,
  `api.telegram.org` и `dl.google.com` (для Chrome при сборке образа).
  Входящие порты не требуются.

Если VPS в стране, откуда DeepSeek блокирован — **не сработает.** Используй
VPS в EU/US.

---

## 2. Первый деплой — Docker

```bash
# 1. Поставь Docker, если ещё не стоит
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker

# 2. Склонируй оба репозитория бок о бок
mkdir -p ~/kwork && cd ~/kwork
git clone https://github.com/ВАШ_ФОРК/FreeDeepSeekAPI.git
git clone https://github.com/ВАШ_ФОРК/KworkTelegramBot.git
cd KworkTelegramBot

# 3. Заполни .env (редактируй всё, кроме того что захардкожено в compose)
cp .env.example .env
$EDITOR .env
#   обязательно: TELEGRAM_BOT_TOKEN
#   обязательно: DEEPSEEK_API_KEY (токен с chat.deepseek.com)
#   TELEGRAM_OWNER_CHAT_ID — можно оставить пустым (первый /start его закрепит)
#   остальное трогать не надо — docker-compose.yml сам подставит
#   правильные DEEPSEEK_API_BASE, COOKIE_REFRESH_URL, SQLITE_PATH

# 4. Поднимай
docker compose up -d --build

# 5. Смотри что получилось
docker compose ps
docker compose logs -f kwork-bot
```

После первого запуска **напиши боту `/start`** — он закрепит твой chat_id
как владельца.

### Что поднимется

```
$ docker compose ps
NAME                 IMAGE                      STATUS
freedeepseekapi      freedeepseekapi:latest     Up (healthy)
kwork-telegram-bot   kwork-telegram-bot:latest  Up
```

Прокси на внутренней compose-сети доступен как `http://deepseek:8080`.
Наружу порт не опубликован — это безопасность по умолчанию. Если надо
подключаться с хоста для отладки, добавь в `docker-compose.yml` в сервис
`deepseek`:

```yaml
    ports:
      - "127.0.0.1:8080:8080"
```

---

## 3. Перенос существующего состояния

Если бот уже работает локально и жалко терять «уже виденных» проекты и
настройки, скопируй SQLite + cookies на сервер **до первого запуска**:

```bash
# на локальной машине
scp FreeDeepSeekAPI/dsk/cookies.json      server:~/kwork/FreeDeepSeekAPI/dsk/
scp KworkTelegramBot/data/kwork_bot.sqlite3 server:~/kwork/KworkTelegramBot/data/

# на сервере, ДО docker compose up -d:
# named-volume-ы ещё не созданы, поэтому compose возьмёт эти файлы с хоста
# во время первого старта. После старта файлы уже будут жить в Docker
# volumes — обновлять надо через `docker cp`:
docker cp dsk/cookies.json freedeepseekapi:/app/dsk/cookies.json
docker cp data/kwork_bot.sqlite3 kwork-telegram-bot:/data/kwork_bot.sqlite3
docker compose restart
```

Либо воспользуйся фичей бота — по рестарту он сам подтянет `chat_id`
владельца из БД, но если БД нет, то придётся снова `/start`.

---

## 4. Обновление

```bash
cd ~/kwork/KworkTelegramBot
git pull
cd ../FreeDeepSeekAPI && git pull && cd ../KworkTelegramBot
docker compose build
docker compose up -d   # перезапустит только изменённые образы
```

Volumes `deepseek_cookies` и `bot_state` сохраняются — cookies, БД и
сохранённый DeepSeek-токен не теряются.

---

## 5. Проверка здоровья

### В Telegram

- `/status` — общее состояние, когда последний раз опрашивали kwork
- `/health` — % успешных AI-ответов, стрики ошибок, когда последний раз
  обновлялись cookies

### В shell

```bash
docker compose ps                   # оба контейнера должны быть Up
docker compose logs -f kwork-bot    # активность бота
docker compose logs -f deepseek     # активность прокси

# Ручная проверка admin-эндпоинта (подставь свой токен)
curl -H "Authorization: Bearer $DEEPSEEK_KEY" \
     http://localhost:8080/admin/health
# Если не публикуешь порт наружу — войди в контейнер бота:
docker exec -it kwork-telegram-bot sh -c \
   'curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" $COOKIE_REFRESH_URL/../health'
```

---

## 6. Типичные поломки

**AI отвечает пустыми строками.**
→ Cookies истекли. Бот попробует сам через `/admin/refresh-cookies` (ты
увидишь в Telegram «cookies обновил»). Если не удалось — `/refresh_cookies`
из чата.

**`401 Invalid or expired authentication token` в логах прокси.**
→ Сам DeepSeek-токен протух. Обнови токен прямо из чата:
1. https://chat.deepseek.com → F12 → Console →
   `JSON.parse(localStorage.getItem("userToken")).value`
2. В боте: `/set_token НОВЫЙ_ТОКЕН`

Токен сохранится в Docker-volume `bot_state` и переживёт любой рестарт.

**Chrome падает в контейнере при refresh.**
→ Поставь `shm_size: "1g"` в docker-compose.yml для сервиса `deepseek`
(по умолчанию 512m). Проверяй память: `docker stats`.

**«0 проектов на странице» в алерте.**
→ Либо VPS забанен kwork'ом, либо kwork поменял HTML. Проверь доступность
напрямую: `docker exec kwork-telegram-bot curl -sI https://kwork.ru/projects`.

---

## 7. Альтернатива без Docker — systemd

Если не хочется Docker, оба сервиса можно запустить как systemd-юниты.
Понадобится Chrome на хосте (`google-chrome-stable`) + `xvfb-run`:

```bash
# FreeDeepSeekAPI
cd /opt/kwork/FreeDeepSeekAPI
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt \
              DrissionPage pyvirtualdisplay 'setuptools<81'
cp .env.example .env && $EDITOR .env

sudo tee /etc/systemd/system/freedeepseekapi.service <<'EOF'
[Unit]
Description=FreeDeepSeekAPI proxy
After=network-online.target

[Service]
User=kwork
WorkingDirectory=/opt/kwork/FreeDeepSeekAPI
EnvironmentFile=/opt/kwork/FreeDeepSeekAPI/.env
ExecStart=/opt/kwork/FreeDeepSeekAPI/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# KworkTelegramBot
cd /opt/kwork/KworkTelegramBot
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env && $EDITOR .env

sudo tee /etc/systemd/system/kwork-bot.service <<'EOF'
[Unit]
Description=Kwork Telegram Bot
After=network-online.target freedeepseekapi.service
Requires=freedeepseekapi.service

[Service]
User=kwork
WorkingDirectory=/opt/kwork/KworkTelegramBot
EnvironmentFile=/opt/kwork/KworkTelegramBot/.env
ExecStart=/opt/kwork/KworkTelegramBot/.venv/bin/python -m kwork_bot.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now freedeepseekapi.service kwork-bot.service
sudo journalctl -fu kwork-bot.service
```

В systemd-варианте `COOKIE_REFRESH_SCRIPT=/opt/kwork/FreeDeepSeekAPI/refresh_cookies.sh`
и бот сам вызовет его — не забудь установить `google-chrome-stable` и
`xvfb` на хост. Для headless-сервера без X-сессии всё равно рекомендую
Docker-путь — он значительно проще.
