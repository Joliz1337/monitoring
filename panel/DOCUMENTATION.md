# Monitoring Panel

Веб-панель для мониторинга серверов. Собирает метрики с нод с настраиваемым интервалом (по умолчанию 10 сек) и хранит историю локально.

## Возможности

- **Dashboard** — карточки серверов с drag-and-drop, статус SSL, Load Average
- **Server Details** — графики CPU/RAM/Network/TCP States/Load Average History, процессы с фильтрацией, управление питанием (перезагрузка/выключение)
- **HAProxy** — управление правилами, сертификатами, firewall (UFW); централизованные конфиг-профили с синхронизацией на несколько нод
- **Traffic** — статистика по интерфейсам и портам, TCP/UDP соединения
- **Bulk Actions** — массовое создание/удаление правил HAProxy, портов трафика и firewall; перезапуск HAProxy (если запущен — перезапускает, если остановлен — запускает); терминал с поддержкой режима скрипта (многострочный bash); выбор серверов с группировкой по папкам (tri-state чекбоксы, поиск, сворачивание; состояние в localStorage `bulk_expanded_folders`)
- **IP Blocklist** — блокировка IP/CIDR через ipset с автообновлением списков из GitHub
- **Remnawave** — интеграция с Remnawave Panel: пользователи, IP-адреса, HWID-устройства, обнаружение аномалий (только ACTIVE пользователи)
- **Alerts** — Telegram-уведомления о состоянии серверов (offline, CPU, RAM, сеть, TCP)
- **Billing** — отслеживание оплаты серверов: помесячная, ресурсная и Yandex Cloud модели; автосинхронизация баланса YC, уведомления об истечении через Telegram
- **Синхронизация времени** — автоматическая установка часового пояса и синхронизация NTP на всех серверах и хосте панели
- **SSH Security** — управление SSH-безопасностью серверов: настройки sshd, fail2ban, SSH-ключи с пресетами безопасности и bulk-применением
- **Infrastructure Tree** — двухуровневая иерархия серверов на странице Servers: Аккаунт (облачный email) → Проект (кластер) → Серверы; дерево встроено в существующую страницу, сворачивается, состояние сохраняется в localStorage
- **Shared Notes & Tasks** — совместный блокнот и список задач с синхронизацией в реальном времени через SSE; открывается через плавающий жёлтый таб на правом крае экрана (amber-500); две вкладки: «Блокнот» и «Задачи»; NotesDrawer использует z-[60], что выше модальных окон страниц (z-50)
- **Wildcard SSL** — выпуск wildcard сертификатов через certbot + Cloudflare DNS challenge, продление, деплой на ноды через API порта 9100; фоновое автопродление каждые 24ч; настройка пути деплоя, имён файлов сертификата и reload-команды для каждого сервера; режим «полностью кастомный путь» для систем с жёстко заданными путями (Proxmox и др.)
- **FAQ-справка** — встроенная справочная система: жёлтая иконка вопроса на каждой странице и у сложных разделов; при клике открывается drawer справа с markdown-статьёй на русском

## Интервалы сбора данных

Настраиваются в разделе **Настройки** панели:

| Параметр | По умолчанию | Рекомендуемый | Описание |
|----------|--------------|---------------|----------|
| Сбор метрик | 10 сек | 10-15 сек | CPU, RAM, диск, сеть |
| HAProxy/Traffic | 60 сек | 60 сек | Правила, сертификаты, трафик |

Изменения применяются автоматически в течение 30 секунд.

## Быстрый старт

```bash
# Запустите установщик
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 1) Установить панель
# Введите домен — SSL сертификат получится автоматически
```

### Установка Remnawave ноды

Пункт **9** в главном меню установщика. Упрощённая установка без домена и SSL-сертификатов:
- Спрашивает IP панели Remnawave и ключ-сертификат
- Создаёт `/opt/remnawave/` с `docker-compose.yml` и `nginx.conf`
- Nginx слушает на unix-сокете (`/dev/shm/nginx.sock`) с PROXY protocol
- Автоматически скачивает случайный маскировочный шаблон в `/var/www/html/`
- Настраивает UFW (порт 2222 для IP панели)

## SSL сертификаты

`deploy.sh` управляет SSL в три этапа:

1. **Прямой путь** — проверяет `/etc/letsencrypt/live/{DOMAIN}/`. Если там лежит symlink (ранее созданный) — берёт его без лишних действий.
2. **Поиск wildcard/SAN** (`find_existing_cert`) — сканирует все сертификаты в `/etc/letsencrypt/live/`, читает SAN-записи через `openssl` и ищет совпадение с доменом, включая wildcard (`*.example.com` покрывает `sub.example.com`). При нахождении создаёт symlink `{DOMAIN}` → найденный каталог.
3. **Certbot** — запускается только если первые два шага не дали результата.

**Поведение для symlink-сертификатов:**
- certbot не устанавливается
- cron автопродления не настраивается (`setup_cert_renewal_cron` пропускает шаг)
- `print_credentials` показывает путь к источнику и пометку "Managed externally"

**Пример:** wildcard `*.nexyonn.com` лежит в `/etc/letsencrypt/live/nexyonn.com/`. Домен панели `panel.nexyonn.com` — скрипт найдёт сертификат автоматически и создаст symlink, certbot не запустится.

### Выбор метода SSL при установке

При запуске `deploy.sh` (режим первичной установки) пользователю предлагается выбрать метод получения SSL-сертификата:

1. **Let's Encrypt (HTTP-01)** — стандартный способ: certbot получает сертификат через HTTP-challenge. Требует, чтобы домен указывал на IP сервера и порт 80 был открыт. DNS-проверка запускается только для этого метода.
2. **Cloudflare DNS API (DNS-01)** — получение сертификата через DNS-challenge. Не требует открытого порта 80, работает с любым IP, поддерживает wildcard.

**Функции `deploy.sh` для выбора метода:**

| Функция | Описание |
|---------|----------|
| `prompt_ssl_method()` | Интерактивное меню выбора метода (LE / Cloudflare) |
| `prompt_cloudflare_config()` | Запрашивает Cloudflare API Token и список дополнительных доменов для SAN |
| `verify_cloudflare_token()` | Проверяет валидность токена через Cloudflare API перед использованием |
| `save_cloudflare_credentials()` | Сохраняет токен в `/etc/letsencrypt/cloudflare.ini` (chmod 600) |
| `install_certbot_dns_cloudflare()` | Устанавливает `certbot` + `python3-certbot-dns-cloudflare` |
| `obtain_certificate_cloudflare()` | Получает сертификат через DNS-01 challenge |
| `renew_certificate_auto()` | Автопродление: определяет метод по наличию `cloudflare.ini` и продлевает соответствующим способом |

**Изменённые функции:**
- `setup_ssl_certificate()` — ветвится на Cloudflare / LE при первичном получении; продление делегируется в `renew_certificate_auto()`
- `setup_cert_renewal_cron()` — поддерживает оба метода
- `generate_env()` — добавлена переменная `SSL_METHOD` (значение: `letsencrypt` или `cloudflare`)
- `print_credentials()` — выводит, какой метод SSL используется на сервере

**Новые переменные окружения:**

| Переменная | Описание |
|-----------|----------|
| `SSL_METHOD` | Метод SSL: `letsencrypt` или `cloudflare` |
| `CF_API_TOKEN` | Cloudflare API Token (только при методе cloudflare) |
| `CF_DOMAINS` | Дополнительные домены для SAN через запятую (опционально) |
| `CF_CREDENTIALS_FILE` | Путь к credentials-файлу (default: `/etc/letsencrypt/cloudflare.ini`) |

**Управление через панель:**
- В разделе **Настройки** отображается информация о сертификате панели
- Показывается домен, дата истечения и дней до истечения
- Кнопка "Продлить" для ручного продления через веб-интерфейс

**Требования для certbot HTTP-01 (только если нет wildcard/SAN и выбран LE):**
- Домен должен указывать на IP сервера
- Порт 80 должен быть открыт

## Структура

```
panel/
├── frontend/          # React + Vite + Tailwind
│   └── src/
│       ├── pages/SSHSecurity.tsx        # SSH Security Management UI
│       ├── pages/HAProxyConfigs.tsx      # HAProxy Config Profiles: создание/редактирование/удаление профилей, привязка серверов, синхронизация, импорт, лог
│       ├── pages/WildcardSSL.tsx        # Wildcard SSL: выпуск/продление/деплой + настройки Cloudflare и серверов
│       ├── pages/Servers.tsx            # Список серверов + InfraTree + Installer Token блок + MigrationBanner
│       ├── components/MigrationBanner.tsx # Баннер миграции нод: migration-status + migrateAll, модалка для legacy
│       ├── components/ui/Skeleton.tsx   # Skeleton-лоадеры (Skeleton, ServerCardSkeleton, MetricCardSkeleton, ChartSkeleton)
│       ├── components/ui/Tooltip.tsx    # Кастомный тултип (Framer Motion + createPortal, auto-flip, a11y)
│       ├── components/Infra/            # Infrastructure Tree компоненты
│       │   ├── InfraTree.tsx            # Основной контейнер дерева
│       │   ├── AccountNode.tsx          # Строка аккаунта
│       │   ├── ProjectNode.tsx          # Строка проекта
│       │   ├── InfraServerRow.tsx       # Компактная строка сервера с метриками
│       │   └── ServerSearchDropdown.tsx # Поиск серверов для привязки
│       ├── components/Notes/
│       │   └── NotesDrawer.tsx          # Шторка с вкладками: Блокнот и Задачи (создание/выполнение/удаление)
│       ├── components/Layout/Layout.tsx # Боковая панель + плавающий amber-таб Notes + рендер NotesDrawer
│       └── stores/
│           ├── infraStore.ts            # Zustand-стор дерева инфраструктуры
│           └── notesStore.ts            # Zustand-стор: SSE, дебаунс сохранения, подавление эха, состояние задач
├── backend/           # FastAPI + PostgreSQL
│   └── app/
│       ├── routers/haproxy_profiles.py  # HAProxy Config Profiles API: CRUD профилей + sync/import эндпоинты
│       ├── routers/ssh_security.py      # SSH Security API роутер
│       ├── routers/infra.py             # Infrastructure Tree API роутер
│       ├── routers/notes.py             # Shared Notes API роутер (SSE + REST)
│       ├── routers/wildcard_ssl.py      # Wildcard SSL API роутер: CRUD сертификатов, деплой, настройки
│       └── services/
│           ├── haproxy_profile_sync.py  # Сервис синхронизации конфиг-профилей на ноды (asyncio.gather + Semaphore)
│           ├── migration.py             # classify_server / push_shared_cert_to_node / LegacyMigrationRequired
│           ├── ssh_manager.py           # Пресеты безопасности SSH + proxy helper
│           ├── http_client.py           # Глобальный HTTP-клиент с connection pooling
│           ├── notes_broadcaster.py     # asyncio.Queue-based pub/sub для SSE
│           ├── telegram_bot.py          # Централизованный сервис Telegram-ботов на aiogram v3 (TelegramBotService)
│           └── wildcard_ssl.py          # Выпуск через certbot + Cloudflare, продление, деплой на ноды, автопродление; certbot вызывается с --cert-name {base_domain} и --expand для корректного сопоставления и расширения существующих сертификатов
├── nginx/             # Reverse proxy с SSL
├── docker-compose.yml # Образы из GHCR + fallback build
├── deploy.sh          # Установка: docker compose pull + up
└── VERSION            # Версия панели (единственный источник)
```

## Деплой и образы

Docker-образы автоматически билдятся **GitHub Actions** при пуше в main и публикуются в **GHCR**:
- `ghcr.io/joliz1337/monitoring-panel-frontend:latest`
- `ghcr.io/joliz1337/monitoring-panel-backend:latest`
- `ghcr.io/joliz1337/monitoring-node-api:latest`

CI/CD: `.github/workflows/docker-publish.yml` — 3 параллельных job (node-api, panel-frontend, panel-backend) с GHA кешем.

Установка и обновление: `docker compose pull` → `docker compose up -d`. Если GHCR недоступен — fallback на локальный `docker compose build` из Dockerfile.

## Производительность

### PKI / mTLS

**Модуль `panel/backend/app/services/pki.py`** — единый PKI-модуль. Реализует Remnawave-style mTLS-защиту канала панель↔нода.

Функции:
- `generate_ca()` — ECDSA P-256 CA, 10 лет, `BasicConstraints(ca=True)`
- `generate_client_cert()` — клиентский сертификат панели, `ExtendedKeyUsage=clientAuth`
- `generate_node_cert(url)` — общий серверный сертификат (CN=`shared-node`), SAN с IP/DNS, 3 года
- `build_installer_token(keygen)` — упаковывает `{v, ca, crt, key}` в `base64(JSON)` для установки на ноды; одинаков для всех нод
- `unpack_node_secret(secret)` — обратная распаковка (поле `name` больше не требуется)
- `load_or_create_keygen(session)` — singleton-инициализация PKI при первом старте; генерирует CA + клиентский cert + shared node cert; при backfill в существующий keygen добавляет shared cert без пересоздания CA

**Dataclass `PKIKeygenData`**: `ca_cert_pem`, `ca_key_pem`, `client_cert_pem`, `client_key_pem`, `shared_node_cert`, `shared_node_key`.

**Таблица `keygen`** (singleton, модель `PKIKeygen`): `ca_cert_pem`, `ca_key_pem`, `client_cert_pem`, `client_key_pem`, `shared_node_cert_pem`, `shared_node_key_pem`. Создаётся при первом старте. CA никогда не пересоздаётся.

### Миграция нод

**`panel/backend/app/services/migration.py`**:

- `classify_server(server)` → `'shared' | 'per_server' | 'legacy'`
- `push_shared_cert_to_node(server, keygen)` — отправляет shared cert/key на ноду через `POST /api/system/replace-node-cert`; используется при автомиграции per-server нод
- `LegacyMigrationRequired` — исключение для legacy-нод (требуют ручной переустановки)

**Lifespan (`main.py`)**:
```
init_db → load_or_create_keygen → app.state.pki = keygen → init_http_clients(keygen)
```

### HTTP-клиент (backend → ноды)

`panel/backend/app/services/http_client.py` — dual-client с поддержкой mTLS и legacy-режима:

- `_node_client_mtls` — `httpx.AsyncClient(verify=ssl_context, http2=True)` с mTLS: SSLContext собирается через `tempdir` с клиентским cert/key, `load_verify_locations(cadata=CA)`, `check_hostname=False`, `CERT_REQUIRED`. Используется для нод с `pki_enabled=true`.
- `_node_client_legacy` — `verify=False, http2=True`, для нод, которые ещё не обновлены до mTLS.
- `_external_client` — для внешних API (подписки Xray и т.п.), `http2=True`, `keepalive_expiry=60s`.
- `get_node_client(server)` — диспатчер: возвращает mTLS-клиент или legacy в зависимости от `server.pki_enabled`.
- `node_auth_headers(server)` — возвращает `{"X-API-Key": ...}` только для legacy-нод; для mTLS-нод возвращает `{}`.

**Параметры пула (оба node-клиента):** `max_connections=200`, `max_keepalive_connections=50`, `keepalive_expiry=120s`. Таймауты (`_NODE_TIMEOUT`): `connect=2s, read=5s, write=2s, pool=2s`. Тяжёлые запросы (haproxy, traffic, certs) используют явный timeout override в `proxy_request` (15/120/300 с). `trust_env=False` — игнорируют `HTTP_PROXY`/`HTTPS_PROXY` из окружения.

**HTTP/2:** все три клиента используют `http2=True` (зависимость `httpx[socks,http2]`).

Lifecycle управляется через `lifespan` в `main.py`. Все роутеры и сервисы вызывают `get_node_client(server)` + `headers=node_auth_headers(server)` вместо глобального клиента.

### Frontend: авто-обновление данных

`panel/frontend/src/hooks/useAutoRefresh.ts` — хук для периодического обновления данных на страницах. Поддерживает паузу при скрытой вкладке (Page Visibility API).

Баг с двойным fetch при открытии страницы устранён: visibility effect пропускает первый mount через `mountedRef`, чтобы не дублировать запрос, который уже выполнил `useEffect` компонента при инициализации.

**Умное авто-обновление на странице Updates:**

`panel/frontend/src/pages/Updates.tsx` реализует собственный цикл авто-обновления независимо от хука `useAutoRefresh`. Каждые **12 секунд** страница перезапрашивает базовую информацию о версиях панели и нод, но только при одновременном выполнении трёх условий:

- Пользователь **неактивен** более 5 секунд — отслеживается через события `mousemove`, `mousedown`, `keydown`, `scroll`, `touchstart` в `lastActivityRef`
- Вкладка браузера **видима** — проверяется через `document.hidden`
- **Не выполняется** никакое обновление — нет активных `updatingPanel`, `updatingNodes`, `updatingAll`, `isChecking`

Константы: `IDLE_THRESHOLD = 5000` мс, `AUTO_REFRESH_INTERVAL = 12000` мс.

### nginx keepalive и TCP-оптимизации

`panel/nginx/nginx.conf.template` (внешний TLS-терминирующий nginx):
- `worker_processes auto` — автоопределение числа воркеров по CPU
- keepalive в upstream: 32 соединения для backend, 16 для frontend
- `sendfile on`, `tcp_nopush on`, `tcp_nodelay on` — TCP-оптимизации
- `ssl_session_cache 64m`, `ssl_session_tickets on`, `ssl_buffer_size 4k`, `ssl_session_timeout 12h`
- OCSP stapling: `ssl_stapling on`, `ssl_stapling_verify on`, резолверы 1.1.1.1/8.8.8.8
- HSTS max-age=63072000 (2 года) с `preload`
- `proxy_connect_timeout 5s` для `/api/`
- Отдельный location `~ ^/api/proxy/\d+/(metrics|haproxy/status)$`: `proxy_buffering off`, `proxy_connect_timeout 5s`, `proxy_send_timeout 15s`, `proxy_read_timeout 15s` — real-time данные без буферизации; размещён перед общим `/api/`
- Общий лимит тела запроса: `client_max_body_size 10m`
- Отдельный location `= /api/backup/restore`: `client_max_body_size 100m`, `proxy_send_timeout 120s`, `proxy_read_timeout 120s` — для импорта бэкапов размером до 100 MB

`panel/frontend/nginx.conf` (внутренний nginx фронтенда):
- upstream `backend_upstream` с keepalive 32, `keepalive_requests 5000`, `keepalive_timeout 75s`
- gzip: уровень 6, минимум 512 байт, расширенный список типов (text, json, js, css, svg, wasm, fonts)
- `/assets/`: `expires 1y`, `Cache-Control: public, max-age=31536000, immutable`
- `/index.html`: `no-store, must-revalidate`
- `proxy_connect_timeout 5s`
- TCP-оптимизации включены; WebSocket-заголовки (Upgrade/Connection upgrade) убраны из API-проксирования.

### Docker ресурсные лимиты

`panel/docker-compose.yml`:
- `nginx`: 1 CPU / 256 MB RAM
- `backend`: 2 CPU

## База данных

Панель использует **PostgreSQL 16** для хранения данных:
- Метрики серверов (история 24ч raw + 30 дней hourly + 365 дней daily, включая TCP states)
- Remnawave статистика (xray_stats: user → IP → count, эфемерная — заменяется каждый цикл; remnawave_hwid_devices: HWID-устройства)
- Кэш пользователей, blocklist правила, настройки
- ASN-кэш (asn_cache — IP → ASN/prefix/holder, TTL 7 дней)

**Преимущества PostgreSQL:**
- Concurrent writes — одновременная запись с множества серверов
- Connection pooling — эффективное использование соединений
- Batch upsert (ON CONFLICT) — 10-100x быстрее записи статистики
- Надёжность и масштабируемость

### Схема таблицы servers (v9.1.0)

Актуальные поля: `id`, `name`, `url`, `api_key` (nullable, legacy), `pki_enabled`, `uses_shared_cert`. Поля `node_cert_pem`, `node_key_pem`, `node_cert_fingerprint`, `node_cert_issued_at`, `node_cert_expires_at` удалены через `DROP COLUMN`.

### Миграции и FK-ограничения

`database.py` содержит функцию `run_migrations()`, которая запускается при каждом старте приложения в режиме `AUTOCOMMIT` (каждый DDL в своей транзакции — неудачный ALTER TABLE не ломает остальные).

**`_ensure_fk_constraints()`** — идемпотентная миграция, добавляющая FK с `ON DELETE CASCADE` к таблицам, которые были созданы до того, как SQLAlchemy начал их генерировать.

Проблема: `Base.metadata.create_all` использует `CREATE TABLE IF NOT EXISTS` — если таблица уже существует, изменения схемы (в том числе добавление FK) к ней не применяются. Из-за этого при удалении сервера каскадное удаление не срабатывало, накапливались "сиротские" записи; `pg_dump/pg_restore` падал при попытке восстановить constraint.

Покрытые FK (все ссылаются на `servers.id`):
- `server_cache.server_id`
- `metrics_snapshots.server_id`
- `aggregated_metrics.server_id`
- `blocklist_rules.server_id`
- `alert_history.server_id`

Алгоритм: проверяет наличие constraint в `pg_constraint` → если отсутствует, сначала удаляет "сиротские" строки (`server_id NOT IN servers`), затем добавляет `FOREIGN KEY ... ON DELETE CASCADE`. Ошибки при добавлении логируются, но не прерывают запуск.

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| DOMAIN | Домен панели | required |
| PANEL_UID | Секретный путь для доступа (domain.com/{uid}) | auto |
| PANEL_PASSWORD | Пароль для входа | auto |
| JWT_SECRET | Секрет для JWT | auto |
| JWT_EXPIRE_MINUTES | Время жизни токена | 1440 |
| POSTGRES_USER | Пользователь PostgreSQL | panel |
| POSTGRES_PASSWORD | Пароль PostgreSQL | auto |
| POSTGRES_DB | Имя базы данных | panel |

## Порты

| Порт | Описание |
|------|----------|
| 443  | HTTPS интерфейс |
| 80   | HTTP → HTTPS редирект |

## Безопасность

- **Секретный URL**: панель доступна только по `domain.com/{PANEL_UID}` — любой другой путь разрывает соединение (nginx return 444)
- **Двойная проверка UID**: на уровне nginx + на уровне API (timing-safe сравнение)
- **JWT в httpOnly cookie** (secure, samesite=strict)
- **Anti-brute force**: 5 попыток = бан на 15 минут
- **TLS 1.2/1.3** с сильными шифрами
- **Rate limiting**: 60 req/min для неавторизованных
- **Connection drop**: все ошибки авторизации (401/403/429) и неверный UID/путь приводят к разрыву соединения без HTTP-ответа — атакующий не получает никакой информации
- **HTTP запросы**: разрываются без редиректа на HTTPS
- **mTLS канал панель↔нода**: CA генерируется на панели при первом старте; каждая нода получает уникальный cert, подписанный CA; `httpx.AsyncClient` с реальной валидацией CA — MITM невозможен
- **SSRF-фильтр** в `_clean_url`: при добавлении сервера блокируются loopback/link-local/multicast/unspecified адреса
- **X-Forwarded-For/X-Real-IP**: доверяются только от `127.0.0.1`/`::1` (защита от header spoofing в `auth.py` и `security.py`)

## API

### Система

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/system/panel-ip | IP-адрес панели (резолвится из домена) |
| GET | /api/system/version | Версии панели, нод и оптимизаций (всё в одном запросе, параллельные SSH-запросы к нодам; оставлен для обратной совместимости) |
| GET | /api/system/version/base | Информация о панели + GitHub-версии + список нод из БД (без SSH-запросов к нодам, возвращается мгновенно) |
| GET | /api/system/nodes/{node_id}/version | Версия одной конкретной ноды (отдельный SSH-запрос) |
| GET | /api/system/stats | Статистика сервера панели (CPU, RAM, диск) |
| POST | /api/system/update | Обновление панели (target_ref: branch/tag/commit, по умолчанию main) |
| GET | /api/system/update/status | Статус обновления |
| GET | /api/system/certificate | Информация о SSL сертификате панели |
| POST | /api/system/certificate/renew?force=bool | Продление SSL сертификата (force=true для принудительного) |
| GET | /api/system/certificate/renew/status | Статус продления сертификата |
| POST | /api/proxy/{id}/system/optimizations/apply | Применить системные оптимизации на ноду |

**Механизм обновления**:
1. API создаёт временный контейнер `panel-updater` (образ `docker:cli`)
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка)
3. Запускает `update.sh` из склонированной папки
4. `update.sh` останавливает контейнеры, копирует файлы, скачивает новые образы (docker compose pull), запускает
5. Контейнер удаляется после завершения

**Layout карточек нод:**

Ноды отображаются в адаптивной grid-сетке: 1 колонка на мобильных, 2 на средних экранах, 3 на широких. Внутри каждой карточки — вертикальный layout: шапка (имя ноды + версия) сверху, статус + кнопка обновления снизу. Иконки и текст уменьшены для компактности. Длинные имена серверов обрезаются через `truncate`.

**Проверка версий — поднодная загрузка:**

Страница обновлений (Updates) использует двухэтапную загрузку для мгновенного отображения при большом числе нод:

1. `GET /api/system/version/base` — возвращает информацию о панели (текущая версия из `VERSION`, последняя версия с GitHub) и список нод из БД без SSH-подключений. Страница появляется сразу.
2. `GET /api/system/nodes/{node_id}/version` — запрашивается отдельно для каждой ноды параллельно на фронтенде. Каждая нода показывает спиннер, пока её данные не загрузятся, затем плавно появляется результат.

Старый `GET /api/system/version` (один тяжёлый запрос, ждёт все ноды) оставлен для обратной совместимости.

**Frontend API-клиент (`panel/frontend/src/api/client.ts`):**

Типы для поднодной загрузки:
- `VersionBaseInfo` — информация о панели и GitHub-версиях
- `VersionBaseNode` — нода из БД (id, name, url)
- `SingleNodeVersion` — версия и статус одной конкретной ноды

Методы: `getVersionBase()`, `getNodeVersionById(nodeId)`.

**Системные оптимизации**:
- Не применяются автоматически при обновлении нод
- Применяются только через UI панели (раздел Обновления) или API
- Включают: sysctl настройки, limits, systemd limits

### Авторизация

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/auth/validate-uid | Проверка UID (drop connection при неверном) |
| POST | /api/auth/login | Вход |
| POST | /api/auth/logout | Выход |
| GET | /api/auth/check | Проверка сессии |
| GET | /api/auth/ban-status | Статус бана текущего IP (для диагностики) |
| POST | /api/auth/clear-ban | Сбросить бан текущего IP (требует авторизации) |
| DELETE | /api/auth/clear-all-bans | Сбросить все IP баны (требует авторизации) |

### Серверы

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/servers | Список серверов (поля: `uses_shared_cert`, `auth_kind`) |
| POST | /api/servers | Добавить сервер (создаёт с `pki_enabled=true, uses_shared_cert=true`) |
| PUT | /api/servers/{id} | Обновить (включая is_active для вкл/выкл мониторинга) |
| DELETE | /api/servers/{id} | Удалить |
| POST | /api/servers/{id}/test | Тест подключения |
| GET | /api/servers/installer-token | Получить общий Installer Token (одинаков для всех нод) |
| GET | /api/servers/migration-status | Статистика нод по типу: `{total, shared, per_server, legacy, needs_migration}` |
| POST | /api/servers/{id}/migrate | Мигрировать ноду на shared cert (авто для per_server, ручная для legacy) |
| POST | /api/servers/{id}/confirm-migration | Подтвердить ручную переустановку legacy-ноды |
| POST | /api/servers/migrate-all | Параллельная миграция всех per-server нод |

**Создание сервера (`POST /api/servers`)** (v9.1.0):
- `ServerCreate` принимает только `name` и `url`.
- Создаёт запись с `pki_enabled=true`, `uses_shared_cert=true`, `api_key=null`.
- Ответ **не содержит** `node_secret` — токен получается отдельно через `/installer-token`.
- `_clean_url` блокирует SSRF: loopback, link-local, multicast, unspecified адреса — через `ipaddress`.

**`GET /api/servers`** возвращает `uses_shared_cert: bool`, `auth_kind: 'shared'|'per_server'|'legacy'`.

**`GET /api/servers/installer-token`** — возвращает `{token: str}`. Одинаков при каждом вызове. Токен обновляется только при пересоздании keygen (пересоздание CA).

### Infrastructure Tree (иерархия серверов)

Двухуровневая иерархия для организации серверов на странице Servers: **Аккаунт** (облачный email/имя) → **Проект** (кластер/группа) → **Серверы**.

**Архитектурное решение — junction table:**

Серверы связываются с проектами через отдельную таблицу `infra_project_servers` (junction table), а не через FK в модели `Server`. Это не загрязняет основную модель `Server`, которую используют Dashboard, алерты, billing и сборщик метрик.

**Схема БД (`panel/backend/app/models.py`):**

- `InfraAccount` — id, name (облачный email/метка), created_at
- `InfraProject` — id, account_id (FK → InfraAccount, cascade), name (кластер), created_at
- `InfraProjectServer` — project_id (FK → InfraProject, cascade), server_id (FK → Server, cascade), PK(project_id, server_id)

Каскадное удаление: при удалении аккаунта удаляются все его проекты и все привязки серверов к ним. Сами серверы не удаляются.

**API (`panel/backend/app/routers/infra.py`):**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/infra/tree | Полное дерево: аккаунты → проекты → server_ids + unassigned_server_ids |
| POST | /api/infra/accounts | Создать аккаунт |
| PUT | /api/infra/accounts/{id} | Переименовать аккаунт |
| DELETE | /api/infra/accounts/{id} | Удалить аккаунт (каскадно) |
| POST | /api/infra/projects | Создать проект (account_id, name) |
| PUT | /api/infra/projects/{id} | Переименовать проект |
| DELETE | /api/infra/projects/{id} | Удалить проект (каскадно) |
| POST | /api/infra/projects/{id}/servers | Привязать сервер к проекту (server_id) |
| DELETE | /api/infra/projects/{id}/servers/{server_id} | Отвязать сервер от проекта |

Ответ `GET /api/infra/tree`:
```json
{
  "accounts": [
    {
      "id": 1,
      "name": "user@cloud.com",
      "projects": [
        {
          "id": 1,
          "name": "prod-cluster",
          "server_ids": [3, 7, 12]
        }
      ]
    }
  ],
  "unassigned_server_ids": [1, 2, 5]
}
```

**Frontend:**

- `panel/frontend/src/api/client.ts` — `infraApi`, интерфейсы `InfraAccount`, `InfraProject`, `InfraTree`
- `panel/frontend/src/stores/infraStore.ts` — Zustand-стор: загрузка дерева, оптимистичные обновления
- `panel/frontend/src/components/Infra/InfraTree.tsx` — контейнер: сворачиваемое дерево, состояние открытых узлов хранится в localStorage
- `panel/frontend/src/components/Infra/AccountNode.tsx` — строка аккаунта: создание/переименование/удаление проектов; в заголовке отображается агрегированная скорость сети (сумма по всем серверам всех проектов аккаунта)
- `panel/frontend/src/components/Infra/ProjectNode.tsx` — строка проекта: привязка/отвязка серверов; в заголовке отображается агрегированная скорость сети (сумма по всем серверам папки) из useServersStore
- `panel/frontend/src/components/Infra/InfraServerRow.tsx` — компактная строка сервера: статус-точка, имя, IP, CPU/RAM/сеть (моноширинный font-mono font-medium, цвет text-dark-200, стрелки accent-400), клик → детали сервера
- `panel/frontend/src/components/Infra/ServerSearchDropdown.tsx` — поиск по имени/IP при привязке сервера
- `panel/frontend/src/pages/Servers.tsx` — InfraTree + MigrationBanner; список серверов в grid `grid-cols-1 md:grid-cols-2 xl:grid-cols-3`; карточка: бейдж `Old key` / `Legacy` на `uses_shared_cert === false`; форма Add Server показывает Installer Token **сверху** (textarea + Copy + 4 шага установки) до создания записи; удалены: `nodeSecret`/`certInfo` state, `handleRegenerateSecret`, модалка NODE_SECRET

**i18n:** ключи пространства имён `infra` добавлены в `en.json` и `ru.json`.

**Отключение мониторинга сервера:**

Сервер можно временно отключить от мониторинга через `PUT /api/servers/{id}` с `is_active: false`. При этом:
- Сервер остаётся в списке, но коллектор метрик его пропускает
- На Dashboard карточка отображается затемнённой с иконкой PowerOff
- В статистике отключённые серверы не учитываются в online/offline
- На странице Servers есть переключатель для быстрого вкл/выкл
- **Remnawave**: коллектор не опрашивает выключенные серверы (новые данные не собираются), но вся историческая статистика продолжает отображаться
- **Blocklist**: синхронизация пропускает отключённые серверы

### Метрики

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/proxy/{id}/metrics | Кешированные метрики |
| GET | /api/proxy/{id}/metrics/live | Прямой запрос к ноде |
| GET | /api/proxy/{id}/metrics/history | История (1h/24h/7d/30d/365d) |

**Сетевые метрики — фильтрация виртуальных интерфейсов:**

Нода помечает виртуальные интерфейсы (veth*, docker*, br-*, virbr*, flannel*, cni*, cali*) флагом `is_virtual: true` в модели `NetworkInterface`. Поле доступно в `/api/proxy/{id}/metrics` в `network.interfaces[].is_virtual`.

Расчёт скоростей (`enrich_metrics_with_speeds` в `routers/servers.py` и `routers/proxy.py`):
- Скорость (`rx_bytes_per_sec` / `tx_bytes_per_sec`) распределяется пропорционально **только между физическими** интерфейсами (`is_virtual=false`)
- Виртуальные интерфейсы получают `0` скорость, чтобы исключить двойной счёт (трафик Docker bridge/veth дублирует трафик физического интерфейса)
- Суммарная скорость `network.total.rx_bytes_per_sec` / `total.tx_bytes_per_sec` вычисляется из байт только физических интерфейсов

Dashboard (`ServerCard.tsx`) читает скорость из `total.rx_bytes_per_sec` / `total.tx_bytes_per_sec` напрямую, не суммируя по интерфейсам. Показания speedtest скрываются, если последний тест был более 24 часов назад (функция `isSpeedtestFresh()`).

**Load Average:** нода собирает `load_avg_1`, `load_avg_5`, `load_avg_15` из `/proc/loadavg`. На dashboard (стандартный и подробный вид) Load Average отображается в футере карточки с иконкой Activity — используется `load_avg_1` (1-минутное, наиболее актуальное значение), отображается как абсолютное число (например `0.45`) с цветовой индикацией на основе процента от количества CPU-ядер. На странице `ServerDetails.tsx` — в строке Uptime (`LA: X.XX / X.XX / X.XX`), в секции System Information и в виде графика Load Average History рядом с Network Traffic (цвет `#f59e0b`). Поле `load_avg_1` добавлено в интерфейс `HistoryData`.

**Скорость сети (NET) на dashboard:** берётся из последнего `MetricsSnapshot` (таблица `metrics_snapshots`), где хранятся рассчитанные скорости `net_rx_bytes_per_sec` / `net_tx_bytes_per_sec`. Ранее скорость некорректно бралась из `last_metrics` JSON через поле `rx_bytes_per_sec`, которого в этом JSON не существовало.

### HAProxy

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/proxy/{id}/haproxy/status | Статус |
| GET | /api/proxy/{id}/haproxy/rules | Список правил |
| POST | /api/proxy/{id}/haproxy/rules | Создать правило |
| DELETE | /api/proxy/{id}/haproxy/rules/{name} | Удалить правило |
| POST | /api/proxy/{id}/haproxy/start | Запустить |
| POST | /api/proxy/{id}/haproxy/stop | Остановить |
| POST | /api/proxy/{id}/haproxy/reload | Перезагрузить конфиг |

**Проброс ошибок через прокси:**

`proxy_request` в `panel/backend/app/routers/proxy.py` при ответе ноды со статусом != 200 извлекает поле `detail` из JSON-тела ответа и пробрасывает его в `HTTPException`. Все `HTTPException` в `node/app/routers/haproxy.py` содержат `detail` с конкретным сообщением об ошибке (например «Certificate for domain not found»). Благодаря этому конкретная причина ошибки доходит до клиента, а не теряется как пустой «400 Bad Request».

**Генерация конфигурации HAProxy:**

Конфигурация генерируется на стороне панели в `panel/backend/app/services/haproxy_config.py` (dataclass `HAProxyRule` + класс `HAProxyConfigGenerator`) и дублируется в `node/app/services/haproxy_manager.py`.

Поля `HAProxyRule`:
- `rule_type` — `tcp` или `https`
- `send_proxy: bool` — включить PROXY protocol к backend. При `True` генерируется `send-proxy check-send-proxy`: `check-send-proxy` обязателен, чтобы health check также передавал PROXY protocol header (иначе backend разрывает соединение и HAProxy помечает сервер как DOWN).
- `target_ssl: bool` — SSL при подключении к target (только для HTTPS-правил)
- `cert_domain` — домен Let's Encrypt сертификата (только для HTTPS-правил)
- `use_wildcard: bool` — при `True` система ищет сертификат по родительскому домену вместо точного совпадения. Например, для `sub.nexyonn.com` будет использован сертификат `nexyonn.com` (wildcard `*.nexyonn.com`). Применяется только для HTTPS-правил.
- `is_balancer: bool` — если `True`, правило является балансировщиком нагрузки (несколько backend-серверов). При `False` — обычное правило с одним сервером (обратная совместимость).
- `servers: list[BackendServer]` — список backend-серверов для балансировщика (используется при `is_balancer=True`).
- `balancer_options: BalancerOptions | None` — настройки балансировщика (алгоритм, health checks, sticky sessions и др.).

**Dataclass `BackendServer`** (поля):
- `address` — IP или доменное имя backend-сервера
- `port` — порт backend-сервера
- `weight` — вес сервера (1–256, default 1)
- `maxconn` — максимальное число соединений (опционально)
- `check: bool` — включить health check
- `backup: bool` — резервный сервер (используется только при недоступности основных)
- `slowstart` — время плавного запуска в секундах (опционально)
- `send_proxy` — тип PROXY protocol: `""`, `"send-proxy"`, `"send-proxy-v2"`
- `on_marked_down` — действие при падении: `""` или `"shutdown-sessions"`
- `on_marked_up` — действие при восстановлении: `""` или `"shutdown-backup-sessions"`
- `disabled: bool` — временно отключить сервер без удаления

**Dataclass `BalancerOptions`** (поля):
- `algorithm` — алгоритм балансировки: `roundrobin`, `static-rr`, `leastconn`, `source`, `uri`, `url_param`, `hdr`, `random`, `first`, `rdp-cookie`
- `hash_type_consistent: bool` — добавить `hash-type consistent` (для `source` и `uri`)
- `health_check_type` — тип health check: `""`, `"tcp-check"`, `"http-check"`
- `http_check_method` — HTTP-метод для httpchk (default `GET`)
- `http_check_uri` — URI для httpchk (default `/`)
- `http_check_expect` — ожидаемый ответ для httpchk (например `status 200`)
- `cookie_name` — имя cookie для sticky sessions
- `cookie_mode` — режим cookie: `""`, `"insert"`, `"rewrite"`, `"prefix"`
- `cookie_nocache`, `cookie_indirect`, `cookie_postonly`, `cookie_preserve` — флаги cookie
- `stick_table_type` — тип stick table: `""`, `"ip"`, `"string"`, `"integer"`
- `stick_table_size` — размер stick table (например `100k`)
- `stick_table_expire` — TTL записи в stick table
- `stick_on`, `stick_store` — выражения для stick table
- `retries` — число попыток переключения при ошибке
- `redispatch: bool` — разрешить переключение на другой сервер при недоступности
- `allbackups: bool` — использовать все резервные серверы, а не только первый
- `fullconn` — порог maxconn при расчёте `slow-start` (опционально)
- `timeout_queue` — timeout queue в миллисекундах (опционально)
- `resolver` — имя резолвера HAProxy для DNS-резолвинга серверов (default `""`)

**Метод `_generate_balancer_block()`** в `HAProxyConfigGenerator`:

При `is_balancer=True` генератор создаёт расширенный backend-блок вместо стандартной строки `server`. Алгоритм:
1. Блок `balance <algorithm>` + опциональный `hash-type consistent`
2. Секция health checks (`option tcp-check` или `option httpchk <method> <uri>` + `http-check expect`)
3. Секция sticky sessions (cookie или stick-table)
4. Параметры надёжности (retries, option redispatch, allbackups, fullconn, timeout queue)
5. Строки `server` для каждого `BackendServer` с индивидуальными параметрами
6. DNS-резолвинг: для доменных адресов автоматически добавляются `resolvers mydns resolve-prefer ipv4 init-addr none`

**Парсер `parse_rules_from_config()`** расширен для обратного разбора multi-server бэкендов: при обнаружении нескольких строк `server` в одном backend-блоке правило помечается как `is_balancer=True` и восстанавливаются все `BackendServer` с их параметрами.

**Нормализация конфига при применении шаблона (`patchSendProxy` в `HAProxy.tsx`):**

Кнопка «Применить стандартный шаблон» в UI не только перегенерирует обёртку конфига (global/defaults/resolvers), но и нормализует строки `server` в правилах. Функция `patchSendProxy` обходит все строки с `send-proxy` и дописывает `check-send-proxy`, если его ещё нет. Это гарантирует корректность существующих конфигов, созданных до введения обязательного `check-send-proxy`.

**Wildcard-сертификаты в HAProxy:**

В форме создания/редактирования HTTPS-правила в `HAProxy.tsx` доступен тумблер «Wildcard сертификат». При включении `use_wildcard=True` нода извлекает родительский домен через `_extract_parent_domain()` в `haproxy_manager.py` и использует его для поиска сертификата вместо точного домена из поля `cert_domain`. Аналогичный параметр доступен в форме Bulk Actions (`BulkActions.tsx`).

Логика в `panel/backend/app/services/haproxy_config.py` и `node/app/services/haproxy_manager.py` при `use_wildcard=True` заменяет домен сертификата на родительский перед генерацией строки `crt` в конфиге HAProxy.

**HAProxy Load Balancing — UI (`panel/frontend/src/pages/HAProxyConfigs.tsx`):**

Форма `RuleForm` переработана для поддержки двух режимов:
- Тумблер «Балансировщик» переключает между режимом одного сервера и multi-server
- `BackendServerRow` — строка одного backend-сервера с настройками weight, maxconn, check, backup, slowstart, send-proxy, on-marked-down/up, disabled; поддерживает добавление/удаление серверов
- `BalancerSettingsSection` — секция настроек: алгоритм балансировки с параметрами, health checks (TCP/HTTP), sticky sessions (Cookie / Stick Table), параметры надёжности (retries, redispatch, allbackups, fullconn, timeout queue), DNS resolver
- Бейдж «LB» в списке правил маркирует правила-балансировщики

**Pydantic-модели в `panel/backend/app/routers/haproxy_profiles.py`:**
- `BackendServerData` — зеркало `BackendServer` для API
- `BalancerOptionsData` — зеркало `BalancerOptions` для API
- `RuleData` расширен полями `is_balancer`, `servers`, `balancer_options`
- `_serialize_rule()` — конвертирует `HAProxyRule` → `RuleData`
- `_rule_from_data()` — конвертирует `RuleData` → `HAProxyRule`

**TypeScript-интерфейсы в `panel/frontend/src/api/client.ts`:**
- `BackendServer` — параметры одного backend-сервера
- `BalancerOptions` — настройки балансировщика
- `HAProxyProfileRule` расширен полями `isBalancer`, `servers`, `balancerOptions`

**i18n:** новая секция `"balancer"` в `ru.json` и `en.json` (~70 ключей) покрывает все элементы UI балансировщика.

### HAProxy Config Profiles

Централизованное управление конфигурациями HAProxy. Профили хранятся в PostgreSQL как полный текст конфига и синхронизируются на привязанные серверы через существующий эндпоинт ноды `/api/haproxy/config/apply`.

**Схема БД:**

- `HAProxyConfigProfile` — `id`, `name`, `description`, `config_content` (полный текст конфига), `created_at`, `updated_at`
- `HAProxySyncLog` — `id`, `profile_id` (FK), `server_id` (FK), `status` (synced/failed), `error_message`, `synced_at`

**Новые поля модели `Server`:**
- `active_haproxy_profile_id` — ID привязанного профиля (nullable, FK)
- `haproxy_config_hash` — SHA256 хэш последнего применённого конфига (для детекции изменений)
- `haproxy_last_sync_at` — время последней синхронизации
- `haproxy_sync_status` — статус: `synced` / `pending` / `failed`

**Принцип работы синхронизации:**

Сервис `haproxy_profile_sync.py` при вызове sync:
1. Вычисляет SHA256 хэш нового конфига
2. Сравнивает с `haproxy_config_hash` сервера — пропускает если конфиг не изменился
3. Отправляет конфиг на ноду через `POST /api/haproxy/config/apply`
4. Обновляет хэш, статус и `haproxy_last_sync_at` в БД
5. Записывает результат в `HAProxySyncLog`

Параллельная отправка: `asyncio.gather` с `asyncio.Semaphore(10)` — максимум 10 одновременных запросов к нодам.

**Авто-синхронизация при сохранении raw конфига:**

`PUT /api/haproxy-profiles/{id}` принимает `BackgroundTasks` из FastAPI. После обновления конфига в БД запускается фоновая задача `_bg_sync_profile`, которая выполняет синхронизацию на все привязанные серверы без блокировки HTTP-ответа.

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy-profiles | Список профилей |
| POST | /api/haproxy-profiles | Создать профиль |
| GET | /api/haproxy-profiles/{id} | Детали профиля |
| PUT | /api/haproxy-profiles/{id} | Обновить профиль |
| DELETE | /api/haproxy-profiles/{id} | Удалить профиль |
| POST | /api/haproxy-profiles/{id}/servers/{server_id} | Привязать сервер к профилю |
| DELETE | /api/haproxy-profiles/{id}/servers/{server_id} | Отвязать сервер |
| POST | /api/haproxy-profiles/{id}/sync | Синхронизировать профиль на все привязанные серверы |
| POST | /api/haproxy-profiles/{id}/sync/{server_id} | Синхронизировать профиль на один сервер |
| POST | /api/haproxy-profiles/{id}/import/{server_id} | Импортировать конфиг с существующего сервера в профиль |
| POST | /api/haproxy-profiles/{id}/regenerate-config | Перегенерировать конфиг из текущих правил по стандартному шаблону |
| GET | /api/haproxy-profiles/{id}/sync-log | История синхронизаций профиля |

**Обратная совместимость:**

Серверы без привязанного профиля (`active_haproxy_profile_id = NULL`) работают как прежде. Профили — opt-in механизм, ничего не ломает существующую конфигурацию.

**Banner в HAProxy.tsx:**

На странице конкретного сервера, если сервер привязан к профилю, отображается баннер с именем профиля, статусом синхронизации и кнопкой для перехода на страницу управления профилями.

**Frontend (`panel/frontend/src/pages/HAProxyConfigs.tsx`):**
- Список профилей с CRUD
- Редактор текста конфига в модальном окне: кнопки «Закрыть», «Сохранить» и «Применить шаблон» — последняя перегенерирует конфиг из текущих правил по стандартному шаблону и подставляет результат в textarea без автосохранения
- Inline-редактирование правил: форма раскрывается прямо под строкой правила по клику; кнопка Edit убрана, при наведении остаётся только кнопка удаления
- Управление привязанными серверами (link/unlink) с поиском по имени в dropdown
- Кнопки синхронизации (все серверы / один сервер)
- Импорт конфига с существующего сервера
- Лог синхронизаций с отображением статуса по серверам
- Суммарная скорость сети (↓/↑) всех привязанных серверов в карточке профиля; шрифт text-sm font-medium, цвет text-dark-200, иконка Activity accent-400
- Автообновление списка профилей каждые 3 секунды — скорости сети (total_net_rx/total_net_tx) на карточках обновляются в реальном времени

**Файлы:**
- `panel/backend/app/routers/haproxy_profiles.py` — API роутер (CRUD + sync + import + regenerate-config)
- `panel/backend/app/services/haproxy_profile_sync.py` — сервис синхронизации
- `panel/backend/app/models.py` — модели `HAProxyConfigProfile`, `HAProxySyncLog`, новые поля `Server`
- `panel/backend/app/database.py` — миграция новых колонок `Server`
- `panel/backend/app/main.py` — регистрация роутера
- `panel/frontend/src/pages/HAProxyConfigs.tsx` — страница управления профилями
- `panel/frontend/src/pages/HAProxy.tsx` — баннер привязанного профиля
- `panel/frontend/src/api/client.ts` — интерфейсы и `haproxyProfilesApi`
- `panel/frontend/src/App.tsx` — роут `/haproxy-configs`
- `panel/frontend/src/components/Layout/Layout.tsx` — пункт навигации «HAProxy Configs»
- `panel/frontend/src/locales/en.json`, `ru.json` — i18n ключи пространства имён `haproxy_configs`

### Сертификаты

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/proxy/{id}/haproxy/certs/all | Все сертификаты |
| POST | /api/proxy/{id}/haproxy/certs/generate | Создать Let's Encrypt |
| POST | /api/proxy/{id}/haproxy/certs/upload | Загрузить свой |
| DELETE | /api/proxy/{id}/haproxy/certs/{domain} | Удалить |

### Traffic

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/proxy/{id}/traffic/summary | Сводка трафика |
| GET | /api/proxy/{id}/traffic/hourly | Почасовая статистика |
| GET | /api/proxy/{id}/traffic/daily | Дневная статистика |
| GET | /api/proxy/{id}/traffic/ports/tracked | Отслеживаемые порты |
| POST | /api/proxy/{id}/traffic/ports/add | Добавить порт |
| POST | /api/proxy/{id}/traffic/ports/remove | Удалить порт |

### Массовые действия (Bulk Actions)

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/bulk/haproxy/start | Запустить HAProxy на выбранных серверах |
| POST | /api/bulk/haproxy/stop | Остановить HAProxy на выбранных серверах |
| POST | /api/bulk/haproxy/restart | Перезапустить HAProxy (если запущен — restart, если остановлен — start) |
| POST | /api/bulk/haproxy/rules | Создать HAProxy правило на выбранных серверах |
| DELETE | /api/bulk/haproxy/rules | Удалить по listen_port + target_ip + target_port |
| POST | /api/bulk/traffic/ports | Добавить отслеживаемый порт |
| DELETE | /api/bulk/traffic/ports | Удалить отслеживаемый порт |
| POST | /api/bulk/firewall/rules | Создать правило firewall |
| DELETE | /api/bulk/firewall/rules | Удалить правило по порту |
| POST | /api/bulk/terminal/execute | Выполнить команду на выбранных серверах |

Все bulk-эндпоинты принимают `server_ids: list[int]` и возвращают результат для каждого сервера.
При удалении выполняется проверка наличия правила перед удалением.

**Массовый терминал** (`/bulk/terminal/execute`): принимает `command`, `timeout` (1-600), `shell` (sh/bash). Выполняет команду параллельно на всех серверах через `asyncio.gather`. Возвращает расширенный результат с `stdout`, `stderr`, `exit_code`, `execution_time_ms`.

**Frontend (BulkActions.tsx):** инпуты port и from_ip занимают доступную ширину колонки (убран max-w-xs). Checkbox «SSL to target server» отображается только внутри блока `rule_type === 'https'` — ранее показывался для всех типов правил, хотя работает исключительно с HTTPS.

**Выбор серверов:** серверы группируются по папкам (`server.folder`), порядок папок из `dashboard_folder_order`. Каждая папка имеет tri-state чекбокс (none/some/all) для массового выбора. Поиск фильтрует по имени/URL. Папки сворачиваются — по умолчанию все свёрнуты, состояние хранится в localStorage (`bulk_expanded_folders`). Серверы без папки отображаются в группе «Без папки». Если папок нет — плоский список с поиском. Компонент `Checkbox` поддерживает `indeterminate` проп для отображения частичного выбора.

### IP Blocklist

Блокировка IP/CIDR через ipset с поддержкой двух направлений:
- **Входящие (in)** — блокировка входящего трафика (iptables INPUT chain, match src)
- **Исходящие (out)** — блокировка исходящего трафика (iptables OUTPUT chain, match dst)

Поддержка глобальных правил (для всех серверов), правил по серверам и автоматических списков из GitHub. Каждое правило и источник привязаны к направлению.

На ноде создаются 4 ipset-списка: `blocklist_permanent`, `blocklist_temp` (входящие), `blocklist_out_permanent`, `blocklist_out_temp` (исходящие).

**Глобальные правила:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/blocklist/global?direction=in\|out | Все глобальные правила (фильтр по направлению) |
| POST | /api/blocklist/global | Добавить глобальное правило (direction в теле) |
| POST | /api/blocklist/global/bulk | Массовое добавление (direction в теле) |
| DELETE | /api/blocklist/global/{id} | Удалить правило |

**Правила по серверам:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/blocklist/server/{id}?direction=in\|out | Правила сервера (фильтр по направлению) |
| POST | /api/blocklist/server/{id} | Добавить правило для сервера (direction в теле) |
| DELETE | /api/blocklist/server/{id}/{rule_id} | Удалить правило |
| GET | /api/blocklist/server/{id}/status | Статус ipset на ноде (оба направления) |

**Автоматические списки:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/blocklist/sources?direction=in\|out | Источники (опциональный фильтр по направлению) |
| POST | /api/blocklist/sources | Добавить источник (direction в теле) |
| PUT | /api/blocklist/sources/{id} | Обновить (вкл/выкл) |
| DELETE | /api/blocklist/sources/{id} | Удалить источник |
| POST | /api/blocklist/sources/{id}/refresh | Обновить источник |
| POST | /api/blocklist/sources/refresh-all | Обновить все |

**Настройки и синхронизация:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/blocklist/settings | Текущие настройки (включая глобальный порог детекции) |
| PUT | /api/blocklist/settings | Обновить настройки |
| POST | /api/blocklist/sync | Синхронизировать все ноды (параллельно, оба направления) |
| POST | /api/blocklist/sync/{id} | Синхронизировать одну ноду (оба направления) |
| GET | /api/blocklist/sync/status | Статус последней синхронизации (результат по серверам) |

**Hot-apply**: все изменения правил (добавление/удаление глобальных, серверных правил, переключение источников) автоматически запускают синхронизацию в фоне через BackgroundTasks. Кнопка "Синхронизировать всё" убрана — применение происходит автоматически.

**Frontend (Blocklist.tsx):** три вкладки, IN и OUT показываются одновременно в двух колонках — глобальный переключатель Incoming/Outgoing убран. Переиспользуемые компоненты: `RulesList` и `SourceCard`.

- **Global (Глобальные правила)** — форма добавления сверху с выбором направления (IN / OUT / Оба), ниже двухколоночный grid: входящие правила слева, исходящие справа.
- **Servers (По серверам)** — серверы отображаются адаптивной сеткой (1/2/3 колонки: `grid-cols-1 md:grid-cols-2 xl:grid-cols-3`). По клику карточка раскрывается на всю ширину (`col-span-full`): форма добавления + двухколоночный grid IN/OUT. Счётчики правил (IN/OUT/Global) загружаются при открытии страницы параллельно через `fetchAllServerRules()` (данные из БД панели, не с нод), что даёт моментальный отклик без ожидания.
- **Sources (Автоматические списки)** — двухколоночный grid источников (IN слева, OUT справа), форма добавления с выбором направления.

Уведомления о синхронизации — одна строка крупным шрифтом: «Правила успешно применены» или «Ошибка на: &lt;сервер&gt;».

**Параллельная синхронизация**: `sync_all_nodes()` использует `asyncio.gather` — все серверы синхронизируются одновременно с per-server таймаутом 30 секунд. Если сервер не отвечает — он получает статус ошибки, остальные не блокируются.

**Дефолтные списки (включены по умолчанию, направление: входящие):**
- AntiScanner: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list`
- Government Networks: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/government_networks.list`

Списки автоматически обновляются каждые 24 часа. Блоклисты синхронизируются со всеми активными нодами каждый цикл (независимо от изменений в источниках), что гарантирует получение блоклиста новыми серверами. Также sync автоматически запускается при создании сервера и при его активации (`is_active: false → true`).

### Автоопределение xray-нод

Поле `has_xray_node` в таблице `servers` — обновляется каждые 2 минуты фоновой задачей `MetricsCollector._xray_check_loop()`. Проверка делается через `GET /api/remnawave/status` на каждой ноде — если `available: true`, значит контейнер `remnanode` запущен. Используется для отображения бейджа "xray" / "no xray" в настройках Remnawave.

### Remnawave Integration

Интеграция с Remnawave Panel: отслеживание пользователей, их IP-адресов, количества посещений и HWID-устройств. Сбор IP происходит напрямую через Remnawave Panel API (без агентов на нодах).

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET/PUT | /api/remnawave/settings | Настройки (api_url, api_token, cookie_secret, enabled, collection_interval, anomaly_use_custom_bot, traffic_threshold_gb, traffic_confirm_count) |
| POST | /api/remnawave/settings/test | Проверить подключение к Remnawave API |
| GET/POST/DELETE | /api/remnawave/ignored-users | Управление игнорируемыми пользователями |
| GET | /api/remnawave/status | Статус коллектора |
| POST | /api/remnawave/collect | Принудительный сбор статистики |
| GET | /api/remnawave/stats/summary | Сводка (unique_users, unique_ips, total_devices) |
| GET | /api/remnawave/stats/top-users?status=ACTIVE\|DISABLED\|LIMITED\|EXPIRED&source_ip=x.x.x.x | Топ пользователей (дефолт: status=ACTIVE) |
| GET | /api/remnawave/stats/user/{email} | IP-адреса пользователя и его HWID-устройства |
| DELETE | /api/remnawave/stats/clear | Очистить всю статистику |
| DELETE | /api/remnawave/stats/user/{email}/ips | Очистить IP пользователя |
| GET | /api/remnawave/users | Кэш пользователей |
| POST | /api/remnawave/users/refresh | Обновить кэш |
| GET | /api/remnawave/users/cache-status | Статус кэша |
| GET | /api/remnawave/devices | Список HWID-устройств (с фильтром по user_uuid) |
| GET | /api/remnawave/devices/user/{uuid} | HWID-устройства конкретного пользователя |
| GET | /api/remnawave/anomalies | Только подтверждённые аномалии: IP (streak >= 5), трафик (streak >= traffic_confirm_count), и мгновенные invalid_device_data |

**Схема БД:**

- **xray_stats** — `PK(email, source_ip)` → count, last_seen (эфемерные данные: полностью заменяются каждый цикл сбора через DELETE ALL + INSERT)
- **remnawave_user_cache** — кэш пользователей (обновляется каждые 30 минут); поля `sub_last_user_agent` и `sub_last_opened_at` удалены в Remnawave Panel 2.7.0
- **remnawave_settings** — настройки подключения; поле `anomaly_use_custom_bot` (Boolean) — использовать отдельного бота для аномалий или бот из AlertSettings; поля `traffic_threshold_gb` (Float, default 30.0) и `traffic_confirm_count` (Integer, default 2) — настройки детектора трафик-аномалий
- **remnawave_hwid_devices** — HWID-устройства из Remnawave API (user_uuid, hwid, platform, created_at)

**Принцип работы:**
1. `xray_stats_collector.py` вызывает `remnawave_api.get_all_nodes()` — получает список нод из Remnawave Panel
2. Для каждой ноды вызывает `poll_users_ips()` — получает IP-адреса пользователей напрямую из Remnawave Panel API (только ACTIVE пользователи)
3. Панель делает DELETE ALL + INSERT в `xray_stats` (данные полностью заменяются — эфемерная модель без истории)
4. Параллельно синхронизируются HWID-устройства через `get_all_hwid_devices_paginated()`
5. Кэш пользователей обновляется из Remnawave API каждые 30 минут; удаление устаревших записей выполняется через вычитание множеств в Python (`current_emails - fetched_emails`) с батчевым DELETE по `in_()` (батчи по 500) — вместо одного `DELETE ... WHERE email NOT IN (...)`, который не работает при 57k+ параметров
6. После обновления кэша вызывается `_check_traffic_anomalies()` — снимок `used_traffic_bytes` для всех ACTIVE пользователей сравнивается с предыдущим, delta > порога увеличивает streak
7. Аномалии проверяются только для ACTIVE пользователей (фильтрация по `status == 'ACTIVE'` в коллекторе и в роутере `/api/remnawave/anomalies`)
8. После синхронизации HWID-устройств вызывается `_check_invalid_device_data()` — каждое устройство проверяется на корректность полей: `platform` не пустая, `os_version` соответствует regex `^[\d._]+$`, `model` не пустая; при нарушении — мгновенная отправка уведомления в Telegram без streak-накопления
9. `GET /api/remnawave/anomalies` показывает только **подтверждённые** аномалии: IP — streak >= 5 (данные из `get_ip_anomaly_streaks()` коллектора), трафик — streak >= `traffic_confirm_count`, `invalid_device_data` — мгновенно при обнаружении

**Эфемерные IP:** данные IP не накапливаются. Каждый цикл сбора полностью заменяет таблицу `xray_stats`. Период-фильтр на эндпоинтах отсутствует — данные всегда актуальные. Автоочистка (`_cleanup_loop`) больше не затрагивает `xray_stats`.

**Telegram-бот для аномалий:** если `anomaly_use_custom_bot=False` (по умолчанию), используется bot_token и chat_id из `AlertSettings`. Если `True` — используются отдельные поля в `RemnawaveSettings`.

**Обнаружение аномалий:**

Пять типов аномалий, все — только для пользователей с `status == 'ACTIVE'`:

| Тип | Логика | TG-кнопка |
|-----|--------|-----------|
| `ip_exceeds_limit` | IP-адресов > `hwid_device_limit + 2` | [Игнор IP] |
| `hwid_exceeds_limit` | HWID-устройств > `hwid_device_limit`; триггерит авто-очистку через API | нет уведомления |
| `unknown_user_agent` | User-Agent не совпадает с известными клиентами (`KNOWN_UA_PATTERN`) | [Игнор HWID] |
| `traffic_exceeds_limit` | Потребление трафика за 30 минут > `traffic_threshold_gb` ГБ N раз подряд | нет кнопки |
| `invalid_device_data` | Поле `platform` пустое, `os_version` не соответствует формату `^[\d._]+$`, или поле `model` пустое | мгновенно в TG |

**5-кратное подтверждение IP аномалий**: уведомление в Telegram отправляется только после 5 подряд обнаружений (streak >= 5). Если IP-count упал ниже лимита — streak (`_ip_anomaly_streak`) сбрасывается. Защита от ложных срабатываний при кратковременных всплесках.

**ASN-фильтрация IP аномалий**: после подтверждения streak >= 5 система дополнительно проверяет ASN (провайдера) каждого IP через RIPE Stat API + ip-api.com (`lookup_ips_cached()` → `group_ips_by_asn()` → `effective_ip_count()`). Если все IP принадлежат одному или небольшому числу провайдеров (unique ASN <= лимит устройств) — аномалия подавляется, уведомление не отправляется. Логика: динамические IP одного ISP не являются реальным сливом аккаунта.

- `asn_lookup.py` — функции `lookup_ips_cached()`, `group_ips_by_asn()`, `effective_ip_count()`, `enrich_with_names()` (batch-запрос к ip-api.com для получения ISP name)
- `ASNCache` (таблица `asn_cache`) — хранит `ip`, `asn`, `prefix`, `holder` (имя провайдера), `cached_at`; TTL 7 дней

**Формат TG-уведомления об IP аномалии** включает:
- Количество уникальных IP / лимит / количество ASN
- Группировку IP по провайдерам (ASN + ISP name)
- Список IP-адресов в каждой группе (до 5)

**3-кратное подтверждение трафик-аномалий**: уведомление отправляется только после N подряд превышений порога (настраивается через `traffic_confirm_count`, default 2). Если текущий снимок меньше предыдущего (сброс трафика) — delta считается 0. Streak сбрасывается при падении ниже порога.

**Cooldown уведомлений**: 24 часа между повторными уведомлениями по одному пользователю (`COOLDOWN_SECONDS = 86400`).

**Фильтрация топ-пользователей:**
- Поиск по email выполняется в SQL через JOIN с `remnawave_user_cache`
- Фильтр по статусу: `ACTIVE`, `DISABLED`, `LIMITED`, `EXPIRED`; дефолт: `ACTIVE`
- Фильтр по IP: подзапрос на `xray_stats.source_ip`

**Ноды в настройках:** раздел управления нодами удалён. Ноды получаются автоматически из Remnawave Panel API.

**Frontend:**
- Страница Remnawave: 4 карточки в overview — Users, IPs, Devices, Nodes Online
- Вкладка Anomalies: 6 карточек в summary (карточки `ip_exceeds`, `hwid_exceeds`, `unknown_ua`, `traffic_exceeds`, `invalid_device`); при клике на строку аномалии раскрывается профиль пользователя с полными данными — список IP-адресов (ссылка на check-host.net, кнопка удаления каждого IP, кнопка «Удалить все IP»), список HWID-устройств (модель, ОС, дата); данные загружаются через `getUserStats(email)` (`GET /api/remnawave/stats/user/{email}`); chevron-иконка индицирует раскрытое/свёрнутое состояние; фильтр типов аномалий включает `invalid_device_data`
- HWID-устройства пользователя показываются в деталях пользователя
- PeriodSelector убран из Overview и Users (данные всегда актуальные)
- Status filter по умолчанию: `ACTIVE`
- Toggle "Использовать другого Telegram бота" в настройках аномалий; поля token/chat_id показываются только при включённом toggle
- Кнопка игнора аномалии — контекстная: `ip_exceeds_limit` → «Игнор IP», остальные типы → «Игнор HWID»
- Вкладка Settings: двухколоночный grid (lg-брейкпоинт). Левая колонка: API / Collection / Anomaly Notifications. Правая колонка: Traffic Anomaly Triggers (порог в ГБ + confirm count). Save, Ignored Users, списки игнора и Danger Zone — на всю ширину под grid. Секции Ignored Users / Ignore IP / Ignore HWID вынесены в трёхколоночный grid. Ограничение ширины (max-w-5xl/max-w-3xl) снято — контент растянут на всю ширину. Поле поиска и IP-фильтр расширены (max-w-sm убран).
- Все строки локализованы через i18n (ru.json / en.json)

**Файлы:**
- `panel/backend/app/routers/remnawave.py` — API роутер; фильтрует аномалии по подтверждению, IP-аномалии берёт из `get_ip_anomaly_streaks()`
- `panel/backend/app/services/xray_stats_collector.py` — сбор IP + HWID-синхронизация; ASN-проверка после streak >= 5; метод `get_ip_anomaly_streaks()`; callback handler для кнопок игнора зарегистрирован через `TelegramBotService.register_callback()`; отправка уведомлений через `TelegramBotService.send_message()`
- `panel/backend/app/services/asn_lookup.py` — ASN-резолвинг: `lookup_ips_cached()`, `group_ips_by_asn()`, `effective_ip_count()`, `enrich_with_names()` (ip-api.com)
- `panel/backend/app/services/remnawave_api.py` — клиент: `get_all_nodes()`, `poll_users_ips()`, `get_all_hwid_devices_paginated()`
- `panel/backend/app/models.py` — модель `ASNCache` (ip, asn, prefix, holder, cached_at)
- `panel/frontend/src/pages/Remnawave.tsx` — страница (overview, users, settings)
- `panel/frontend/src/api/client.ts` — API-клиент
- `panel/frontend/src/locales/en.json`, `ru.json` — переводы

### Telegram Bot Service

Централизованный сервис управления Telegram-ботами на базе **aiogram v3**. Все сервисы (алерты, аномалии Remnawave, Xray Monitor, тестовые уведомления из Settings) отправляют сообщения через него — прямых HTTP-вызовов к Telegram API в сервисах нет.

**Зависимость:** `aiogram>=3.7.0` (`panel/backend/requirements.txt`)

**Файл:** `panel/backend/app/services/telegram_bot.py`

**Класс `TelegramBotService`** — единственный класс, менеджер и маршрутизатор:
- Дедупликация по токену: если несколько сервисов настроены с одним токеном — создаётся один `aiogram.Bot`
- `aiogram.Bot` создаётся с `DefaultBotProperties(parse_mode=ParseMode.HTML)`
- Единый `Dispatcher` с главным `Router` (`_main_router`) + поддержка внешних роутеров
- Long polling: `bot.get_updates(offset, timeout=30)` → `dp.feed_update(bot, update)`
- `send_message(bot_token, chat_id, text, reply_markup)` — автоконверсия dict → `InlineKeyboardMarkup`
- `send_test(bot_token, chat_id, text)` — тестовое сообщение с результатом `{success, message|error}`
- `include_router(child: Router)` — подключение внешних aiogram Router (используется `xray_stats_collector`)
- `_cleanup_stale_bots()` каждые 60 сек — читает активные токены из `AlertSettings`, `RemnawaveSettings`, `XrayMonitorSettings`; останавливает боты с устаревшими токенами

**Встроенные команды** (регистрируются через `@router.message(Command(...))`):
- `/start` — отвечает chat_id (помогает узнать ID чата для настройки)
- `/status` — количество активных/всего серверов и число запущенных ботов

**Callback-хендлеры внешних сервисов** подключаются как aiogram Router через `include_router()`. Пример: `xray_stats_collector` экспортирует `_rw_callback_router` с декоратором `@_rw_callback_router.callback_query(F.data.startswith("rw_ignore:"))`, хендлер принимает типизированный `CallbackQuery` и вызывает `callback.answer()`.

**Lifecycle:** запускается и останавливается в `lifespan` (`main.py`) через `start_telegram_bot_service()` / `stop_telegram_bot_service()`. Singleton через `get_telegram_bot_service()`.

### Server Alerts

Система алертов мониторинга серверов с Telegram-уведомлениями. Фоновый сервис `ServerAlerter` проверяет серверы каждые N секунд (default 60) и отправляет уведомления при проблемах.

**Логика:**
- **Offline**: при первом обнаружении недоступного сервера запускается **активный пробинг** (`_active_probe_sequence`). Последовательность: 3 API-попытки с интервалом 1 сек → ICMP ping → если ICMP доступен, ещё 2 API-попытки с интервалом 5 сек. Два типа уведомлений: **полный офлайн** (API + ICMP не отвечают) и **частичный** (API не отвечает, ICMP доступен). Уведомление о восстановлении. Порог `OFFLINE_THRESHOLD_SECONDS` вычисляется динамически: `max(60, interval * 3 + 30)`, где `interval` — настроенный интервал сбора метрик. `ServerCard` скрывает показания speedtest, если последний тест был более 24 часов назад (`isSpeedtestFresh()`). `formatTimeAgo` показывает точное время в формате «2h 30m ago».
- **CPU/RAM**: критический порог (default 90%) — алерт при длительном превышении. Адаптивное EMA-отслеживание скачков.
- **Network**: спайк/падение трафика относительно EMA baseline.
- **TCP**: отслеживание Established, Listen, Time Wait, Close Wait, SYN Sent, SYN Recv, FIN Wait по отдельности.
- **Load Average**: алерт `load_avg_high` — отправляется, если `load_avg_1` превышает `(cpu_count + load_avg_threshold_offset)` N раз подряд. Параметры: `load_avg_threshold_offset` (добавочное смещение сверх числа ядер, default 1) и `load_avg_sustained_checks` (число последовательных проверок, default 3). Состояние хранится в `ServerAlertState.load_avg_fail_count`. Метод `_check_load_avg()` в `ServerAlerter`. Серверы из `load_avg_excluded_server_ids` пропускаются при проверке.
- **Cooldown**: между повторными алертами одного типа/сервера (default 30 мин).
- **Excluded servers**: серверы из списка исключений (`excluded_server_ids` в AlertSettings) полностью пропускаются при проверке.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/alerts/settings | Настройки алертов |
| PUT | /api/alerts/settings | Обновить настройки |
| POST | /api/alerts/test-telegram | Тест Telegram (отправить тестовое сообщение) |
| GET | /api/alerts/status | Статус алертера (running, monitored_servers, active_conditions) |
| GET | /api/alerts/history | История алертов (server_id, alert_type, limit, offset) |
| DELETE | /api/alerts/history | Очистить историю |

**Схема AlertSettings (поля для Load Average):**
- `load_avg_enabled` (Boolean, default True) — включить триггер
- `load_avg_threshold_offset` (Integer, default 1) — добавочное число сверх ядер для порога
- `load_avg_sustained_checks` (Integer, default 3) — число последовательных проверок до алерта
- `load_avg_excluded_server_ids` (JSON, default []) — серверы, исключённые из проверки load avg

**Файлы:**
- `panel/backend/app/services/server_alerter.py` — фоновый сервис; `_check_load_avg()`, `_msg_load_avg()`; отправка через `TelegramBotService.send_message()`
- `panel/backend/app/routers/alerts.py` — API роутер; поля `load_avg_*` в `AlertSettingsUpdate` и `_settings_to_dict`; тест через `TelegramBotService.send_test()`
- `panel/backend/app/models.py` — `AlertSettings` (4 новых поля load_avg), `AlertHistory`
- `panel/backend/app/database.py` — inline миграция для новых колонок load_avg
- `panel/frontend/src/pages/Alerts.tsx` — Load Average TriggerBlock с настройками offset и checks, иконка Activity; тип `load_avg_high` в фильтр истории и `alertTypeLabel`
- `panel/frontend/src/api/client.ts` — поля `load_avg_*` в интерфейсе `AlertSettingsData`
- `panel/frontend/src/locales/ru.json`, `en.json` — ключи `trigger_load_avg`, `load_avg_offset`, `load_avg_checks`, `type_load_avg_high`

### Billing (Оплата серверов)

Отслеживание сроков оплаты серверов. Три типа проектов:
- **Помесячная** — указать количество дней до следующей оплаты.
- **Ресурсная** — баланс + стоимость в месяц → автоматический расчёт оставшегося срока.
- **Yandex Cloud** — автоматическая синхронизация баланса через Yandex Cloud Billing API. Дневное потребление рассчитывается по EMA (0.3 × new + 0.7 × old), оставшиеся дни = `(balance - threshold) / daily_cost`.

Уведомления через Telegram бот из раздела Alerts.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/billing/servers | Список серверов |
| POST | /api/billing/servers | Добавить сервер |
| PUT | /api/billing/servers/{id} | Обновить |
| DELETE | /api/billing/servers/{id} | Удалить |
| POST | /api/billing/servers/{id}/extend | Продлить (дни) |
| POST | /api/billing/servers/{id}/topup | Пополнить баланс |
| POST | /api/billing/servers/{id}/yc-sync | Ручная синхронизация баланса с Yandex Cloud |
| GET | /api/billing/settings | Настройки уведомлений |
| PUT | /api/billing/settings | Обновить настройки |

**Поле `paid_until` (дата оплаты в Billing):**

`PUT /api/billing/servers/{id}` принимает `paid_until` как строку в любом из форматов:
- ISO: `2026-05-09`, `2026-05-09T21:30:00`
- Русские месяцы: `05 мая 2026`, `05 мая 2026 21:30`
- Короткие: `1/10/26`, `01.10.2026`

Пустая строка очищает поле. GET-эндпоинты возвращают `paid_until` в ISO 8601 (или `null`). Парсинг выполняется функцией `parse_flexible_date()` в `panel/backend/app/routers/servers.py`. В форме редактирования Billing дата отображается в формате ДД.ММ.ГГГГ; при отправке строка идёт на бэкенд как есть. i18n-ключи: `billing.paid_until_placeholder`, `billing.paid_until_hint`.

**Yandex Cloud — авторизация через OAuth-токен:**

Вместо IAM-токена (живёт 12 часов) используется **OAuth-токен Яндекса** (бессрочный). IAM-токены генерируются автоматически из OAuth-токена и кэшируются на ~58 минут.

Получение OAuth-токена: перейти по ссылке `https://oauth.yandex.ru/authorize?response_type=token&client_id=1a6990aa636648e9b2ef855fa7bec2fb`, скопировать токен и вставить в панель.

- Токен хранится в колонке `yc_oauth_token` (VARCHAR 200) в таблице `billing_servers`
- В API-ответах токен не возвращается — только `has_yc_token: bool`
- `YCTokenManager` (`yc_token_manager.py`) делает POST с `{"yandexPassportOauthToken": "..."}` на `https://iam.api.cloud.yandex.net/iam/v1/tokens`, кэширует IAM-токен ~58 минут
- Баланс получается через `GET https://billing.api.cloud.yandex.net/billing/v1/billingAccounts/{id}`
- Порог отрицательного баланса задаётся вручную (`yc_balance_threshold`)
- Фоновая синхронизация запускается автоматически через `billing_checker.py`
- Frontend: input для вставки OAuth-токена, оранжевая иконка Cloud, кнопка "Обновить", поля для токена/billing account ID/порога

**Схема BillingServer (поля для YC):**
`yc_oauth_token` (VARCHAR 200), `yc_billing_account_id`, `yc_balance_threshold`, `yc_daily_cost`, `yc_last_sync_at`, `yc_last_error`

**Файлы:**
- `panel/backend/app/routers/billing.py` — API роутер; валидация токена при создании/обновлении; эндпоинт yc-sync генерирует IAM-токен из OAuth-токена
- `panel/backend/app/services/billing_checker.py` — фоновая проверка сроков + Telegram + синхронизация YC через `YCTokenManager`
- `panel/backend/app/services/yandex_billing.py` — клиент Yandex Cloud Billing API (баланс, EMA потребления, дней осталось)
- `panel/backend/app/services/yc_token_manager.py` — обмен OAuth-токена на IAM-токен через Yandex IAM API, кэш ~58 минут
- `panel/backend/app/models.py` — колонка `yc_oauth_token` (VARCHAR 200)
- `panel/backend/app/database.py` — миграция: переход от `yc_iam_token` и `yc_service_key` к `yc_oauth_token`
- `panel/frontend/src/pages/Billing.tsx` — input для OAuth-токена, обновлённые placeholder и подсказки
- `panel/frontend/src/api/client.ts` — поле `yc_oauth_token` в интерфейсах
- `panel/frontend/src/locales/ru.json`, `en.json` — переводы для полей OAuth-токена

### Синхронизация времени

Автоматическая установка часового пояса и синхронизация NTP на всех серверах и хосте панели.

**Принцип работы:**
- Фоновая задача `TimeSyncService` каждые 24ч вызывает `POST /api/system/time-sync` на каждой активной ноде
- При добавлении нового сервера синхронизация запускается немедленно
- При изменении настройки `server_timezone` — синхронизация запускается на всех серверах
- Хост панели синхронизируется через Docker-контейнер с `nsenter`

**Нода** (`POST /api/system/time-sync`):
- Принимает IANA timezone (например `Europe/Moscow`)
- Устанавливает часовой пояс через `timedatectl set-timezone`
- Включает NTP через `timedatectl set-ntp true`
- Принудительно синхронизирует время через `systemd-timesyncd`

**Настройки:**

| Параметр | Описание | Default |
|----------|----------|---------|
| `server_timezone` | IANA timezone для всех серверов | Europe/Moscow |
| `time_sync_enabled` | Включить автосинхронизацию | true |

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /settings/time-sync/run | Запустить синхронизацию вручную на всех серверах |
| GET | /settings/time-sync/status | Статус последней синхронизации |

**Frontend (Settings.tsx):** блок «Синхронизация времени» с toggle включения, выбором timezone из списка, кнопкой «Синхронизировать» и отображением статуса последней синхронизации.

**Файлы:**
- `panel/backend/app/services/time_sync.py` — `TimeSyncService`: фоновый сервис, синхронизация при добавлении сервера и изменении timezone
- `panel/backend/app/routers/settings.py` — эндпоинты настроек и запуска синхронизации
- `node/app/routers/system.py` — `POST /api/system/time-sync` на ноде
- `panel/frontend/src/api/client.ts` — `timeSyncRun`, `timeSyncStatus`
- `panel/frontend/src/stores/settingsStore.ts` — `serverTimezone`, `timeSyncEnabled`
- `panel/frontend/src/pages/Settings.tsx` — UI блок синхронизации времени

### SSH Security Management

Централизованное управление SSH-безопасностью серверов. Панель проксирует запросы к нодам и предоставляет пресеты безопасности для быстрой настройки.

**Per-server эндпоинты (прокси к ноде):**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/proxy/{id}/ssh/config | Текущие настройки sshd |
| POST | /api/proxy/{id}/ssh/config | Применить настройки sshd |
| POST | /api/proxy/{id}/ssh/config/test | Валидация без применения |
| GET | /api/proxy/{id}/ssh/fail2ban/status | Статус fail2ban |
| POST | /api/proxy/{id}/ssh/fail2ban/config | Обновить fail2ban |
| GET | /api/proxy/{id}/ssh/fail2ban/banned | Забаненные IP |
| POST | /api/proxy/{id}/ssh/fail2ban/unban | Разбанить IP |
| POST | /api/proxy/{id}/ssh/fail2ban/unban-all | Разбанить все IP |
| GET | /api/proxy/{id}/ssh/keys | Список SSH-ключей |
| POST | /api/proxy/{id}/ssh/keys | Добавить SSH-ключ |
| DELETE | /api/proxy/{id}/ssh/keys | Удалить SSH-ключ |
| GET | /api/proxy/{id}/ssh/status | Общий статус SSH |

**Bulk, пресеты и смена пароля:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ssh/presets | Доступные пресеты (встроенные + кастомные) |
| GET | /api/ssh/presets/{name} | Конфиг встроенного пресета (recommended/maximum) |
| POST | /api/ssh/presets/custom | Сохранить текущие настройки как кастомный пресет |
| DELETE | /api/ssh/presets/custom | Удалить кастомный пресет |
| POST | /api/ssh/bulk/config | Применить настройки sshd на нескольких серверах |
| POST | /api/ssh/bulk/fail2ban | Применить настройки fail2ban на нескольких серверах |
| POST | /api/ssh/bulk/preset | Применить пресет на нескольких серверах |
| POST | /api/ssh-security/server/{id}/password | Сменить пароль на одном сервере |
| POST | /api/ssh-security/bulk/password | Сменить пароль на всех серверах |

**Встроенные пресеты безопасности:**
- `recommended` — вход только root по паролю: `permit_root_login: yes`, `password_authentication: true`, `pubkey_authentication: false`, `allow_users: [root]`, fail2ban с мягкими настройками
- `maximum` — максимальная защита: только ключи, `permit_root_login: no`, агрессивный fail2ban

**Кастомные пресеты:**
- Сохраняются в `panel_settings` под ключом `ssh_custom_presets` (JSON-массив)
- Позволяют сохранить произвольный набор настроек sshd/fail2ban с именем и применить его к любым серверам

**Frontend:**
- `panel/frontend/src/pages/SSHSecurity.tsx` — страница управления SSH
- Выбор сервера, применение пресетов с bulk-подтверждением
- 3 вкладки: Настройки SSH / Защита от перебора (fail2ban) / SSH-ключи
- Вкладка SSH Settings: секция «Смена пароля» — генератор пароля (20+ символов), индикатор сложности, показать/скрыть, копирование, кнопки «Сменить» / «Сменить на всех серверах»
- Sticky bar для несохранённых изменений, панель результатов bulk-операций
- Карточки кастомных пресетов с удалением, кнопка «Сохранить текущие настройки как пресет»

**Файлы:**
- `panel/backend/app/services/ssh_manager.py` — пресеты безопасности, proxy helper
- `panel/backend/app/routers/ssh_security.py` — API роутер (17 эндпоинтов)

### Backup & Restore

Резервное копирование и восстановление базы данных панели. Бэкап — полный pg_dump PostgreSQL в custom format. Хранятся в `/app/data/backups/` (volume `panel-data`). Максимум 20 бэкапов, старые удаляются автоматически.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/backup/create | Создать бэкап (pg_dump в фоне) |
| GET | /api/backup/list | Список бэкапов (имя, размер, дата, версия) |
| GET | /api/backup/{filename}/download | Скачать файл бэкапа |
| DELETE | /api/backup/{filename} | Удалить бэкап |
| POST | /api/backup/restore | Загрузить и восстановить из файла (multipart, до 100 MB) |
| GET | /api/backup/status | Статус операции (idle/creating/restoring) |

После восстановления рекомендуется перезапуск: `docker compose restart`.

**Алгоритм восстановления (`_run_pg_restore`):**
1. `psql -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"` — полный сброс схемы через `docker exec`
2. `pg_restore --no-owner` — восстановление в чистую схему

Такой подход гарантирует отсутствие конфликтов FK-ограничений при восстановлении (ранее `pg_restore --clean --if-exists --single-transaction` падал при наличии зависимых таблиц, например `aggregated_metrics → servers`).

**Ограничения загрузки (nginx):**
- Общий лимит запросов: `client_max_body_size 10m`
- Эндпоинт `/api/backup/restore` имеет отдельный location-блок с `client_max_body_size 100m` и увеличенными таймаутами (`proxy_send_timeout 120s`, `proxy_read_timeout 120s`) — исправляет ошибку 413 при импорте бэкапов > 10 MB

**Файлы:**
- `panel/backend/app/routers/backup.py` — API роутер
- `panel/frontend/src/pages/Settings.tsx` — секция в настройках
- `panel/nginx/nginx.conf.template` — location `= /api/backup/restore` с увеличенными лимитами

### Wildcard SSL

Централизованный выпуск и деплой wildcard SSL-сертификатов на серверы. Панель выпускает сертификат через certbot + Cloudflare DNS-01 challenge и доставляет его на ноды через порт 9100.

**Принцип работы:**
1. Панель вызывает certbot с плагином `certbot-dns-cloudflare` для DNS-01 challenge — получает wildcard `*.domain.com`; флаг `--expand` позволяет расширить существующий сертификат без ошибки «Missing command line flag» в non-interactive режиме
2. Сертификат записывается в `/etc/letsencrypt/` внутри контейнера backend (volume смонтирован с `rw`)
3. Панель отправляет cert + key на каждую настроенную ноду через `POST /api/ssl/wildcard/deploy`
4. Нода валидирует файлы, делает бэкап текущих, записывает новые и вызывает `reload_cmd`
5. Фоновая задача проверяет сроки сертификатов каждые 24ч и автоматически продлевает

**Схема БД (`wildcard_certificates`):**
- `id`, `domain` — домен для сертификата (например `*.example.com`)
- `cert_path`, `key_path` — пути к файлам в `/etc/letsencrypt/live/`
- `expires_at` — дата истечения
- `last_deployed_at` — время последнего деплоя на ноды
- `status` — текущий статус (issued, deploying, error, etc.)

**Новые поля модели `Server`:**
- `wildcard_ssl_enabled` — деплоить ли wildcard SSL на этот сервер
- `wildcard_ssl_deploy_path` — базовая папка на хосте для записи файлов (например `/etc/nginx/ssl/`)
- `wildcard_ssl_reload_cmd` — команда перезагрузки сервиса после деплоя (например `systemctl reload nginx`)
- `wildcard_ssl_fullchain_name` — имя файла fullchain (default: `fullchain.pem`)
- `wildcard_ssl_privkey_name` — имя файла privkey (default: `privkey.pem`)
- `wildcard_ssl_custom_path_enabled` — режим полностью кастомного пути (скрывает базовую папку и имена)
- `wildcard_ssl_custom_fullchain_path` — абсолютный путь к файлу сертификата (активен при `custom_path_enabled`)
- `wildcard_ssl_custom_privkey_path` — абсолютный путь к файлу ключа (активен при `custom_path_enabled`)

**Настройки Cloudflare:**
Хранятся в `panel_settings` (ключ `cloudflare_api_token` и `cloudflare_email`). Передаются certbot через временный credentials-файл.

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/wildcard-ssl/certificates | Список сертификатов |
| POST | /api/wildcard-ssl/certificates | Выпустить новый сертификат |
| GET | /api/wildcard-ssl/certificates/{id} | Детали сертификата |
| DELETE | /api/wildcard-ssl/certificates/{id} | Удалить сертификат |
| POST | /api/wildcard-ssl/certificates/{id}/renew | Продлить сертификат |
| POST | /api/wildcard-ssl/certificates/{id}/deploy | Задеплоить на серверы |
| GET | /api/wildcard-ssl/settings/cloudflare | Настройки Cloudflare |
| PUT | /api/wildcard-ssl/settings/cloudflare | Обновить настройки Cloudflare |
| GET | /api/wildcard-ssl/servers | Конфигурация деплоя по серверам |
| PUT | /api/wildcard-ssl/servers/{server_id} | Настроить деплой для сервера (путь, reload_cmd, enabled) |

**docker-compose.yml:**
Volume `/etc/letsencrypt` изменён с `:ro` на `:rw` — backend записывает выпущенные сертификаты.

**Зависимости (requirements.txt):**
Добавлены `certbot` и `certbot-dns-cloudflare`.

**Frontend (`panel/frontend/src/pages/WildcardSSL.tsx`):**
- Выпуск нового сертификата (ввод домена, запуск certbot)
- Продление и деплой существующих сертификатов
- Настройки Cloudflare (API token, email)
- Конфигурация каждого сервера: включить деплой, путь, reload-команда

**Файлы:**
- `panel/backend/app/services/wildcard_ssl.py` — бизнес-логика: выпуск, продление, деплой, автопродление
- `panel/backend/app/routers/wildcard_ssl.py` — API роутер
- `panel/backend/app/models.py` — модель `WildcardCertificate`, новые поля `Server`
- `panel/backend/app/database.py` — миграция `_migrate_wildcard_ssl`
- `panel/backend/app/main.py` — подключение роутера, start/stop автопродления в lifespan
- `panel/frontend/src/pages/WildcardSSL.tsx` — страница управления
- `panel/frontend/src/api/client.ts` — `wildcardSSLApi` с интерфейсами
- `panel/frontend/src/App.tsx` — роут `/wildcard-ssl`
- `panel/frontend/src/components/Layout/Layout.tsx` — пункт навигации «Wildcard SSL»
- `panel/frontend/src/locales/en.json`, `ru.json` — i18n ключи пространства имён `wildcard_ssl`

### Shared Notes & Tasks (совместный блокнот и задачи)

Совместная шторка с двумя вкладками: **Блокнот** и **Задачи**. Один документ и один список задач для всех пользователей панели. Открывается через плавающий жёлтый таб (amber-500) на правом крае экрана — всегда виден, не мешает интерфейсу.

**Архитектура:**

SSE + HTTP POST без WebSocket. Клиент отправляет изменения через дебаунсированный POST (500 мс), сервер рассылает обновления всем подключённым SSE-клиентам через `NotesBroadcaster`. Keepalive каждые 30 с предотвращает таймаут nginx. Version-based подавление эха: клиент не применяет событие, если оно пришло от него же. Оптимистичное разрешение конфликтов — last-write-wins.

**Модели данных:**

`SharedNote` — singleton-строка (id=1) в таблице `shared_notes`:
- `content` — текст заметки
- `version` — монотонный счётчик (Integer, инкрементируется при каждом сохранении)
- `updated_at` — время последнего обновления

`SharedTask` — строки в таблице `shared_tasks`:
- `id` — первичный ключ
- `text` — текст задачи
- `is_done` — выполнена ли (Boolean)
- `position` — порядок отображения
- `created_at` — время создания

**API:**

| Метод | Endpoint | Описание |
|-------|----------|---------|
| GET | /api/notes/content | Текущий текст и версия |
| POST | /api/notes/content | Сохранить новый текст (с проверкой версии) |
| GET | /api/notes/stream | SSE-поток обновлений (text/event-stream) |
| GET | /api/notes/tasks | Список всех задач |
| POST | /api/notes/tasks | Создать задачу |
| PUT | /api/notes/tasks/{id} | Переключить выполнение задачи (toggle done) |
| DELETE | /api/notes/tasks/{id} | Удалить задачу |

SSE-события: `note_update` — `{"content": "...", "version": N}`, `tasks_changed` — полный список задач, `{"ping": true}` — keepalive.

**nginx:** SSE-эндпоинт вынесен в отдельный `location /api/notes/stream` с `proxy_buffering off`, `X-Accel-Buffering no`, `proxy_read_timeout 3600s`.

**Файлы:**
- `panel/backend/app/models.py` — модели `SharedNote` и `SharedTask`
- `panel/backend/app/services/notes_broadcaster.py` — `NotesBroadcaster`: поддержка типизированных событий (`note_update`, `tasks_changed`), keepalive
- `panel/backend/app/routers/notes.py` — роутер: блокнот + 4 task-эндпоинта; мутации задач рассылают `tasks_changed` через SSE
- `panel/backend/app/main.py` — импорт `SharedTask` + регистрация роутера
- `panel/nginx/nginx.conf.template` — location-блок для `/api/notes/stream`
- `panel/frontend/src/api/client.ts` — `notesApi`: интерфейс `SharedTask`, методы task CRUD
- `panel/frontend/src/stores/notesStore.ts` — состояние и CRUD задач, парсер SSE для двух типов событий
- `panel/frontend/src/components/Notes/NotesDrawer.tsx` — вкладки Блокнот/Задачи, список задач с чекбоксами, форма добавления, выполненные задачи внизу со strikethrough
- `panel/frontend/src/components/Layout/Layout.tsx` — плавающий amber-таб на правом крае экрана + рендер NotesDrawer; кнопка «Выход» удалена из сайдбара
- `panel/frontend/src/locales/en.json`, `ru.json` — ключи `tab_notes`, `tab_tasks`, `task_placeholder`, `no_tasks`, `done`

### FAQ-справка

Встроенная справочная система. На каждой странице и рядом с каждым сложным разделом отображается жёлтая иконка вопроса (`HelpCircle`). При клике открывается drawer справа с markdown-статьёй на русском языке.

**Принцип работы:**

- `FAQIcon` — кнопка открытия; вызывает `faqStore.open(screen)`, где `screen` — идентификатор экрана
- `FAQDrawer` — animated drawer (translateX 100% → 0); закрывается по Escape и клику на backdrop; z-index `[65]` (выше NotesDrawer `[60]`)
- `faqStore` — Zustand store с полями `isOpen`, `screen`, методами `open(screen)` и `close()`
- `registry.ts` — загружает все `.md` файлы через `import.meta.glob('./content/{ru,en}/*.md', { query: '?raw', eager: true })`; fallback: ru → en → ru
- Контент хранится в бандле (Vite `?raw`), не требует fetch, работает offline

**Экраны (44 шт.):**

Страницы (17):
`PAGE_DASHBOARD`, `PAGE_SERVERS`, `PAGE_SERVER_DETAILS`, `PAGE_HAPROXY`, `PAGE_TRAFFIC`, `PAGE_ALERTS`, `PAGE_BULK_ACTIONS`, `PAGE_SSH_SECURITY`, `PAGE_SYSTEM_OPTIMIZATIONS`, `PAGE_HAPROXY_CONFIGS`, `PAGE_REMNAWAVE`, `PAGE_BILLING`, `PAGE_BLOCKLIST`, `PAGE_TORRENT_BLOCKER`, `PAGE_WILDCARD_SSL`, `PAGE_UPDATES`, `PAGE_SETTINGS`

Подразделы (27):
`ALERTS_TELEGRAM`, `ALERTS_TRIGGER_OFFLINE`, `ALERTS_TRIGGER_CPU`, `ALERTS_TRIGGER_RAM`, `ALERTS_TRIGGER_NETWORK`, `ALERTS_TRIGGER_LOADAVG`, `ALERTS_TRIGGER_TCP`, `HAPROXY_SSL`, `HAPROXY_FIREWALL`, `HAPROXY_CONFIGS_BALANCER`, `HAPROXY_CONFIGS_STICKY`, `HAPROXY_CONFIGS_HEALTHCHECK`, `SSH_SECURITY_SSHD`, `SSH_SECURITY_FAIL2BAN`, `SSH_SECURITY_KEYS`, `SYS_OPT_PROFILE`, `SYS_OPT_NIC_MODE`, `BULK_ACTIONS_TERMINAL`, `TRAFFIC_TCP_STATES`, `TRAFFIC_PORT_TRACKING`, `REMNAWAVE_HWID_ANOMALIES`, `BILLING_QUOTA`, `BLOCKLIST_SOURCES`, `WILDCARD_SSL_ACME`, `SETTINGS_BACKUP`, `SETTINGS_TIME_SYNC`, `DASHBOARD_FOLDERS`, `SERVER_DETAILS_TERMINAL`

**Как добавить новый FAQ-экран:**

1. Добавить новый ID в union-тип в `src/components/FAQ/faq.types.ts`
2. Создать файл `src/data/faq/content/ru/<screen_id>.md`
3. Вставить `<FAQIcon screen="<screen_id>" />` в нужное место страницы

**Markdown-рендерер:**

Реализован без внешних зависимостей (`src/components/FAQ/markdown.tsx`, ~200 строк). Поддерживает: заголовки h1–h4, параграфы, списки `ul`/`ol`, `code` блоки и inline code, жирный/курсив, ссылки, blockquote, hr.

**i18n:** секция `faq` в `ru.json` и `en.json` с ключами `title`, `help_article`, `close`, `not_found`.

**Файлы:**
- `panel/frontend/src/components/FAQ/FAQDrawer.tsx` — drawer с анимацией Framer Motion
- `panel/frontend/src/components/FAQ/FAQIcon.tsx` — жёлтая кнопка `HelpCircle` с hover-tooltip
- `panel/frontend/src/components/FAQ/faq.types.ts` — TypeScript union всех screen ID
- `panel/frontend/src/components/FAQ/markdown.tsx` — минимальный markdown-рендерер без зависимостей
- `panel/frontend/src/components/FAQ/index.ts` — реэкспорты
- `panel/frontend/src/stores/faqStore.ts` — Zustand store
- `panel/frontend/src/data/faq/registry.ts` — загрузка `.md` файлов через `import.meta.glob`
- `panel/frontend/src/data/faq/content/ru/` — 44 markdown-файла с контентом
- `panel/frontend/src/components/Layout/Layout.tsx` — рендер `<FAQDrawer />`
- `panel/frontend/src/locales/ru.json`, `en.json` — секция `faq`

### Выполнение команд на нодах

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/proxy/{id}/system/execute | Выполнить shell-команду на хосте ноды |
| POST | /api/proxy/{id}/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |

Позволяет выполнять произвольные shell-команды на хост-системе ноды через `nsenter`.

```json
// Request
{
    "command": "sysctl -p /etc/sysctl.d/99-network-tuning.conf",
    "timeout": 30,
    "shell": "sh"
}

// Response
{
    "success": true,
    "exit_code": 0,
    "stdout": "net.ipv4.tcp_fin_timeout = 15\n...",
    "stderr": "",
    "execution_time_ms": 45,
    "error": null
}
```

Параметры:
- `command` — shell-команда для выполнения (обязательный)
- `timeout` — таймаут в секундах, 1-600 (default: 30)
- `shell` — shell: "sh" или "bash" (default: "sh")

Примеры команд:
- `sysctl -p /etc/sysctl.d/99-network-tuning.conf` — применить сетевые настройки
- `systemctl restart nginx` — перезапустить сервис
- `reboot` — перезагрузить сервер

**Веб-терминал**:

На странице деталей сервера доступен интерактивный терминал с:
- Потоковым выводом через SSE (stdout/stderr в реальном времени)
- Историей команд (сохраняется в localStorage)
- Выбором таймаута (30s — 10m) и shell (sh/bash)
- Отменой выполняющейся команды

### Speed Test

Система проверки скорости нод. Три метода: Ookla Speedtest CLI, iperf3, авто-выбор по гео.

**Методы тестирования:**
- `auto` (по умолчанию) — Ookla для серверов вне РФ, iperf3 для серверов в РФ (Ookla заблокирован)
- `ookla` — Ookla Speedtest CLI на хосте через nsenter, авто-выбор ближайшего сервера, до 40+ Гбит/с
- `iperf3` — iperf3 с `-P` параллельными потоками и `-w` TCP window, публичные/свои серверы

**Режимы теста:**
- `quick` — 4 потока, 4MB TCP window, 5 сек (iperf3) / без upload (Ookla)
- `full` — 16 потоков, 8MB TCP window, 10 сек (iperf3) / полный тест (Ookla)

**Возможности:**
- Автоматическое тестирование всех нод по расписанию (последовательно, 5 сек между нодами)
- Гео-определение ноды по IP (ip-api.com) → авто-выбор метода + ближайшие iperf3-серверы
- Автоустановка Ookla CLI на хосте при первом запуске (packagecloud / прямой бинарник)
- Три режима iperf3 серверов: публичные / панель как сервер / оба
- Dropdown-кнопка на странице сервера: «Быстрая» / «Полная» проверка
- Цветной бейдж скорости на карточке сервера (зелёный/жёлтый/красный)
- Telegram-уведомления: низкая скорость, ошибки, восстановление

**Файлы:**
- `node/app/services/speedtest_runner.py` — iperf3 (-P -w) + Ookla CLI через nsenter
- `node/app/routers/speedtest.py` — POST /api/speedtest (method, test_mode), GET /api/speedtest/status
- `panel/backend/app/services/speedtest_scheduler.py` — фоновый планировщик, авто-выбор метода по гео
- `panel/backend/app/services/geo_resolver.py` — определение гео по IP, фильтрация iperf3-серверов
- `panel/backend/app/routers/proxy.py` — POST/GET /proxy/{id}/speedtest (method, test_mode)
- `panel/frontend/src/pages/Settings.tsx` — секция настроек Speed Test (метод, режим, серверы)
- `panel/frontend/src/pages/ServerDetails.tsx` — dropdown-кнопка Quick/Full
- `panel/frontend/src/components/Dashboard/ServerCard.tsx` — бейдж скорости в футере

### Xray Monitor

Мониторинг Xray-серверов через speedtest. Подписки и ключи парсятся, через xray-core создаётся SOCKS5 прокси — измеряется download скорость. Серверы тестируются последовательно. Автотест выключен по умолчанию.

**Метод спидтеста:**

Ookla Speedtest CLI через proxychains4 с флагом `--no-upload`. Тестируется только download; upload не замеряется по двум причинам: proxychains не всегда корректно перехватывает upload-соединения Ookla (upload шёл мимо VPN-туннеля), и флаг `--no-upload` существенно сокращает время теста.

**Предварительная проверка канала панели:**

Перед каждым циклом тестирования VPN-серверов сервис запускает Ookla speedtest напрямую на сервере панели (без proxychains/VPN). Если download ниже `PANEL_SPEED_THRESHOLD_MBPS = 1000 Mbit/s` — весь цикл пропускается с предупреждением в лог.

Telegram-уведомление о низкой скорости отправляется не с первого раза, а только когда счётчик `_panel_slow_streak` достигает `fail_threshold` (по умолчанию 2) подряд идущих обнаружений. При нормальной скорости счётчик сбрасывается в 0. Это исключает ложные срабатывания при кратковременных просадках канала.

- `_run_panel_speedtest()`: Ookla CLI без proxychains; инкрементирует/сбрасывает `_panel_slow_streak`

**Настройки (UI):**
- Один мастер-тогл включения (синхронно контролирует мониторинг и speedtest)
- Интервал проверки
- Порог скорости (Mbit/s) — ниже порога = уведомление "slow speed"
- Провалов до offline
- Игнор-лист серверов
- Telegram-уведомления: down / recovery / slow speed
- Кастомный Telegram-бот (отдельный token/chat_id)

**Таблица серверов:** отображаются колонки Download и Upload скорости (пинг не отображается).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /xray-monitor/settings | Настройки мониторинга |
| PUT | /xray-monitor/settings | Обновить настройки |
| GET | /xray-monitor/subscriptions | Список подписок (с вложенными серверами) |
| POST | /xray-monitor/subscriptions | Добавить подписку |
| PUT | /xray-monitor/subscriptions/{id} | Обновить подписку |
| DELETE | /xray-monitor/subscriptions/{id} | Удалить подписку с серверами |
| POST | /xray-monitor/subscriptions/{id}/refresh | Перезагрузить ключи |
| GET | /xray-monitor/servers | Ручные серверы (без подписки) |
| POST | /xray-monitor/servers | Добавить ключи вручную |
| DELETE | /xray-monitor/servers/{id} | Удалить сервер |
| POST | /xray-monitor/servers/{id}/speedtest | Ручной speedtest сервера |
| GET | /xray-monitor/servers/{id}/history | История проверок (ping/download/upload) |
| GET | /xray-monitor/status | Статус сервиса |
| POST | /xray-monitor/test-notification | Тест Telegram |

**Файлы:**
- `backend/app/models.py` — `XrayMonitorSettings`
- `backend/app/routers/xray_monitor.py` — роутер
- `backend/app/services/xray_monitor.py` — `_run_panel_speedtest()` (проверка канала панели), `_ookla_speedtest_via_proxychains()` (тест через VPN)
- `backend/app/services/xray_key_parser.py` — парсинг подписок и ключей (URI-ключи, base64, JSON full xray-конфиги)
- `frontend/src/pages/XrayMonitor.tsx` — страница мониторинга
- `frontend/src/api/client.ts` — тип настроек

### Tooltip (UI-компонент)

Кастомный тултип для icon-only кнопок. Заменяет нативный HTML-атрибут `title` во всём фронтенде — даёт анимацию, auto-flip позиционирование и поддержку accessibility.

**Файл:** `panel/frontend/src/components/ui/Tooltip.tsx`

**Ключевые решения:**

- Рендер через `createPortal` в `document.body` — тултип не обрезается родительскими `overflow: hidden`
- Позиционирование через `getBoundingClientRect()` с auto-flip: при нехватке места у края viewport позиция переключается (`top ↔ bottom`, `left ↔ right`)
- Пересчёт позиции на `scroll` (capture) и `resize`
- Анимация через Framer Motion: `scale-x` + `opacity`, 150 мс, easeOut — воспроизводит стиль Mantine Tooltip без добавления тяжёлой библиотеки
- `openDelay: 300 мс`, `closeDelay: 0` — поведение аналогично Remnawave (Mantine)
- Keyboard focus: тултип появляется мгновенно (без задержки) при фокусе с клавиатуры; `Escape` закрывает
- Не показывается на touch-устройствах: проверка через `matchMedia('(hover: hover)')`
- Цвета проекта: `bg-dark-800 border border-dark-700 text-dark-100`
- Стрелочка — повёрнутый квадрат с границами по двум внешним сторонам

**API:**

```tsx
<Tooltip label="Текст подсказки">
  <motion.button>...</motion.button>
</Tooltip>
```

| Проп | Тип | Default | Описание |
|------|-----|---------|----------|
| `label` | `string` | — | Текст тултипа (обязательный) |
| `position` | `'top' \| 'bottom' \| 'left' \| 'right'` | `'top'` | Предпочтительная сторона (auto-flip при нехватке места) |
| `withArrow` | `boolean` | `true` | Показывать стрелочку |
| `openDelay` | `number` | `300` | Задержка появления (мс) |
| `closeDelay` | `number` | `0` | Задержка скрытия (мс) |
| `disabled` | `boolean` | `false` | Отключить тултип |
| `offset` | `number` | `8` | Отступ от элемента (px) |
| `maxWidth` | `number` | `200` | Максимальная ширина (px) |
| `className` | `string` | — | Дополнительные классы |

**Accessibility:** `role="tooltip"` + `aria-describedby` на обёрнутом элементе.

**Где используется:** все icon-only кнопки на страницах Servers, HAProxy, HAProxyConfigs, Alerts, Billing, Dashboard, Remnawave, Blocklist, SSHSecurity, TorrentBlocker, ServerDetails, Traffic, Settings (14 страниц, 50+ кнопок). Нативные атрибуты `title={...}` на этих страницах заменены на `<Tooltip label={...}>`.

**Что намеренно НЕ оборачивается:** кнопка закрытия модальных окон (X), Eye/EyeOff в полях пароля, Chevron-стрелочки (индикатор состояния), кнопки с видимым текстом, Menu/X в мобильной навигации.

**Новые i18n-ключи** (добавлены вместе с Tooltip-обёртками):

| Ключ | RU | EN |
|------|----|----|
| `common.back` | Назад | Back |
| `common.clear_search` | Очистить поиск | Clear search |
| `common.remove_from_list` | Убрать из списка | Remove from list |
| `blocklist.enable_source` | Включить источник | Enable source |
| `blocklist.disable_source` | Отключить источник | Disable source |

## Диагностика

### Проблема: "Login failed" при правильном пароле

Возможные причины:
1. **IP забанен** — после 5 неудачных попыток IP банится на 15 минут
2. **Пробелы в пароле** — при копировании из .env могут попасть пробелы

**Решение:**
```bash
# Проверить статус бана (без авторизации)
curl https://domain.com/api/auth/ban-status

# Посмотреть логи бэкенда для диагностики
docker compose logs -f backend | grep -E "(Auth failure|banned|Login)"

# Перезапустить контейнеры (сбросит баны в памяти, но не в БД)
docker compose restart backend
```

### Проблема: Выкидывает из панели

Возможные причины:
1. **JWT токен истёк** — по умолчанию 24 часа (JWT_EXPIRE_MINUTES=1440)
2. **Контейнер перезапустился** — если JWT_SECRET изменился

**Решение:** просто перелогиньтесь

### Проблема: Таймауты к нодам (504), внешние серверы недоступны

Панель не может достучаться до нод вне хостинга, при этом внутренние (тот же хостинг) работают.

**Причины:**
1. **Оптимизации sysctl применены к хосту панели** — настройки для relay-серверов (nf_conntrack, ip_local_port_range) могут ломать малые VMs
2. **Исчерпание conntrack** — много соединений (метрики, speedtest, xray monitor) заполняют таблицу
3. **iperf3-сервер** — постоянный режим может создавать много соединений при тестах

**Диагностика:**

```bash
# На ХОСТЕ панели (не в контейнере)
cd /opt/monitoring-panel  # или путь к панели
bash scripts/diagnose-network.sh
```

Пришлите вывод — по нему можно определить причину.

**Быстрые проверки:**

1. Если на хосте есть `/etc/sysctl.d/99-vless-tuning.conf` — оптимизации применены к панели по ошибке. Удалите и перезагрузите:
   ```bash
   sudo rm /etc/sysctl.d/99-vless-tuning.conf
   sudo sysctl -p
   ```

2. Временно отключить iperf3-сервер панели (для теста):
   ```bash
   # В .env или при запуске
   IPERF_SERVER_DISABLED=1 docker compose up -d
   ```

3. В настройках Speed Test переключить режим на «только публичные серверы» — iperf3 панели не будет запускаться.

## Команды

```bash
# Логи
docker compose logs -f

# Перезапуск
docker compose restart

# Остановка
docker compose down

# SSL сертификат — статус
certbot certificates

# SSL сертификат — принудительное обновление
certbot renew --force-renewal && docker compose restart nginx

# Ручное обновление панели
./update.sh

# Обновление до конкретной версии
./update.sh v1.1.0

# Запуск менеджера установки
mon
```

## Обновления

Панель поддерживает автоматическое обновление через веб-интерфейс:

1. Перейдите в раздел **Обновления** в меню
2. Просмотрите текущие версии панели и нод
3. Нажмите "Обновить" для обновления

При обновлении:
- Сохраняется конфигурация (.env)
- Скачиваются новые Docker образы из GHCR (fallback: локальная сборка)
- Сервисы перезапускаются автоматически
