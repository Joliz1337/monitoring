# Monitoring Panel

Веб-панель для мониторинга серверов. Собирает метрики с нод с настраиваемым интервалом (по умолчанию 10 сек) и хранит историю локально.

## Возможности

- **Dashboard** — карточки серверов с drag-and-drop, статус SSL, Load Average; IP-адрес ноды копируется в буфер обмена кликом ЛКМ
- **Server Details** — графики CPU/RAM/Network/TCP States/Load Average History, процессы с фильтрацией, управление питанием (перезагрузка/выключение)
- **HAProxy** — управление правилами, сертификатами, firewall (UFW)
- **Traffic** — статистика по интерфейсам и портам, TCP/UDP соединения
- **Bulk Actions** — массовое создание/удаление правил HAProxy, портов трафика и firewall; терминал с поддержкой режима скрипта (многострочный bash)
- **IP Blocklist** — блокировка IP/CIDR через ipset с автообновлением списков из GitHub
- **Remnawave** — интеграция с Remnawave Panel: пользователи, IP-адреса, HWID-устройства, обнаружение аномалий (только ACTIVE пользователи)
- **Alerts** — Telegram-уведомления о состоянии серверов (offline, CPU, RAM, сеть, TCP)
- **Billing** — отслеживание оплаты серверов: помесячная, ресурсная и Yandex Cloud модели; автосинхронизация баланса YC, уведомления об истечении через Telegram
- **Синхронизация времени** — автоматическая установка часового пояса и синхронизация NTP на всех серверах и хосте панели
- **SSH Security** — управление SSH-безопасностью серверов: настройки sshd, fail2ban, SSH-ключи с пресетами безопасности и bulk-применением
- **Xray Monitor** — мониторинг Xray-серверов через подписки/ключи (vless, vmess, trojan, ss): Ookla CLI через proxychains4 с флагом `--no-upload` (только download), последовательное тестирование, ручной speedtest, Telegram-уведомления (down/recovery/slow speed); предварительная проверка канала панели с задержкой уведомления через счётчик `_panel_slow_streak`
- **Infrastructure Tree** — двухуровневая иерархия серверов на странице Servers: Аккаунт (облачный email) → Проект (кластер) → Серверы; дерево встроено в существующую страницу, сворачивается, состояние сохраняется в localStorage
- **Shared Notes & Tasks** — совместный блокнот и список задач с синхронизацией в реальном времени через SSE; открывается через плавающий жёлтый таб на правом крае экрана (amber-500); две вкладки: «Блокнот» и «Задачи»
- **Wildcard SSL** — выпуск wildcard сертификатов через certbot + Cloudflare DNS challenge, продление, деплой на ноды через API порта 9100; фоновое автопродление каждые 24ч; настройка пути деплоя и reload-команды для каждого сервера
- **HAProxy Configs** — централизованные профили конфигурации HAProxy с массовой раскаткой на серверы: CRUD профилей и правил, балансировщик нагрузки, привязка серверов, history синхронизаций; запуск HAProxy per-server и bulk-запуск всех остановленных нод одним кликом
- **Firewall Profiles** — шаблоны UFW с массовой раскаткой на серверы: CRUD профилей, привязка 1 сервер ↔ 1 активный профиль, history синхронизаций, node-API-port-guard (защита связи панели с нодой через порт 9100), drift-детекция по SHA256-хэшу
- **Авторазвёртывание ноды** — установка ноды мониторинга прямо из вкладки «Серверы»: подключение по SSH (пароль или приватный ключ), запуск `install.sh --unattended` на целевом сервере; установка выполняется **в фоне на бэкенде** — закрытие вкладки браузера не прерывает процесс; живой лог переподключаем (GET-стрим с реплеем); опционально устанавливает WARP и ноду Remnawave с сохранёнными именованными сертификатами; **массовый деплой** — произвольное количество дополнительных целей, каждая со своим SSH и опциями; после успешного деплоя бэкенд автоматически привязывает сервер к выбранным HAProxy-профилю и/или Firewall-профилю; незавершённые задачи переживают перезагрузку страницы (восстановление через localStorage + `GET /deploy/jobs`)

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
- cron автопродления не устанавливается (`setup_cert_renewal_cron` пропускает шаг); если старая cron-задача уже существует — она удаляется
- `print_credentials` показывает путь к источнику и пометку "Managed externally"

**Пример:** wildcard `*.nexyonn.com` лежит в `/etc/letsencrypt/live/nexyonn.com/`. Домен панели `panel.nexyonn.com` — скрипт найдёт сертификат автоматически и создаст symlink, certbot не запустится.

**Управление через панель:**
- В разделе **Настройки** отображается информация о сертификате панели
- Показывается домен, дата истечения и дней до истечения
- Кнопка "Продлить" для ручного продления через веб-интерфейс

**Cron автопродления (только для сертификатов, выпущенных certbot напрямую):**

`setup_cert_renewal_cron()` устанавливает ежедневную задачу с pre/post-hook:

```
certbot renew --cert-name '<DOMAIN>' --quiet \
  --pre-hook  'docker stop panel-nginx ...' \
  --post-hook 'docker start panel-nginx ...'
```

- `--cert-name <DOMAIN>` — ограничивает задачу только сертификатом панели; wildcard-сертификаты (продлеваются через Cloudflare DNS) не затрагиваются
- `--pre-hook` останавливает `panel-nginx` перед certbot, освобождая порт 80 для standalone-плагина
- `--post-hook` поднимает `panel-nginx` обратно независимо от результата
- `renew-cert.sh` (ручное продление через UI) использует аналогичный подход: `trap 'docker start panel-nginx ...' EXIT` сразу после остановки nginx — контейнер гарантированно поднимается при любом исходе, включая ошибку certbot
- Идемпотентность: при повторном запуске `deploy.sh` старая строка `certbot renew` удаляется из crontab и заменяется актуальной

**Требования для certbot (только если нет wildcard/SAN):**
- Домен должен указывать на IP сервера
- Порт 80 должен быть открыт

## Структура

```
panel/
├── frontend/          # React + Vite + Tailwind
│   └── src/
│       ├── components/ui/CopyableIp.tsx  # Клик по IP → копирование в буфер; Tooltip при наведении; зелёная подсветка 1.5 с; fallback на execCommand
│       ├── utils/format.ts              # Утилиты форматирования; экспортируемая функция extractHost(url) — извлекает хост (IP/домен) из URL ноды
│       ├── pages/SSHSecurity.tsx        # SSH Security Management UI
│       ├── pages/WildcardSSL.tsx        # Wildcard SSL: выпуск/продление/деплой + настройки Cloudflare и серверов
│       ├── pages/FirewallProfiles.tsx   # Firewall Profiles: двухколоночный layout (список + детали с табами Rules/Servers/Log)
│       ├── pages/Servers.tsx            # Список серверов + InfraTree
│       ├── components/ui/Skeleton.tsx   # Skeleton-лоадеры (Skeleton, ServerCardSkeleton, MetricCardSkeleton, ChartSkeleton)
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
│       ├── routers/ssh_security.py      # SSH Security API роутер
│       ├── routers/infra.py             # Infrastructure Tree API роутер
│       ├── routers/notes.py             # Shared Notes API роутер (SSE + REST)
│       ├── routers/wildcard_ssl.py      # Wildcard SSL API роутер: CRUD сертификатов, деплой, настройки
│       ├── routers/firewall_profiles.py # Firewall Profiles API роутер: CRUD профилей и правил, sync, log
│       └── services/
│           ├── ssh_manager.py           # Пресеты безопасности SSH + proxy helper
│           ├── http_client.py           # Глобальный HTTP-клиент с connection pooling
│           ├── notes_broadcaster.py     # asyncio.Queue-based pub/sub для SSE
│           ├── wildcard_ssl.py          # Выпуск через certbot + Cloudflare, продление, деплой на ноды, автопродление
│           └── firewall_profile_sync.py # Массовая раскатка UFW-профилей: compute_rules_hash, sync_profile_to_servers
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

### HTTP-клиент (backend → ноды)

`panel/backend/app/services/http_client.py` предоставляет четыре долгоживущих `httpx.AsyncClient` с connection pooling:

| Клиент | Назначение |
|--------|-----------|
| `_node_client_legacy` | Короткие запросы метрик к legacy-нодам (`verify=False`) |
| `_node_client_mtls` | Короткие запросы к mTLS-нодам |
| `_node_apply_client_legacy` | Длительные apply-операции (firewall/HAProxy profile apply, `read=300s`), отдельный пул 20 соединений — не конкурирует с потоком метрик |
| `_node_apply_client_mtls` | То же для mTLS-нод |
| `_external_client` | Внешние API (`http2=True`) |

Lifecycle управляется через `lifespan` в `main.py`. Выбор клиента — через `get_node_client(server)` / `get_node_apply_client(server)`.

**Параметры основного клиента к нодам (`_NODE_LIMITS`, `_NODE_TIMEOUT`):**
- `keepalive_expiry=30` сек — намеренно меньше `keepalive_timeout` nginx нод (65 сек), чтобы панель не переиспользовала соединения, уже закрытые сервером
- `read=10.0` сек — нагруженные ноды могут отвечать дольше 5 сек
- `http2=False` на всех клиентах к нодам — HTTP/2 state machine при попытке отправки в уже закрытое nginx соединение вызывает фатальную ошибку (`ConnectionInputs.SEND_HEADERS in state CLOSED`), HTTP/1.1 делает переподключение автоматически

**Корень проблемы с offline-нодами:** несовпадение `keepalive_expiry` клиента (ранее 120 сек) с `keepalive_timeout` nginx нод (65 сек) + `http2=True` давало фатальные h2-ошибки → срабатывал circuit breaker (3 сбоя → 30 сек skip) → `last_seen` уходил за порог offline (60 сек) → панель показывала ноду offline. Устранено в v10.4.1.

**Параллелизм Torrent Blocker:** `torrent_blocker.py` при каждом цикле рассылает POST-запросы на ноды. Константа `SEND_CONCURRENCY = 30` ограничивает число одновременных запросов через `asyncio.Semaphore(30)` — без лимита 100+ одновременных запросов забивали keepalive-пул и роняли параллельный поток метрик пачками ошибок. Отдельная константа `WEBHOOK_CONCURRENCY = 20` ограничивает параллельные POST-запросы вебхуков через внешний клиент (`get_external_client`).

**Фильтрация мёртвых нод при рассылке банов:** `_send_to_nodes` перед рассылкой отфильтровывает ноды, у которых `last_seen` старше `LIVE_THRESHOLD_SECONDS = 300` сек. Пороговое значение 300 сек выбрано с запасом относительно цикла сборщика метрик (~10 сек): нода считается мёртвой только при длительном молчании, а не разовом пропуске. Число пропущенных нод пишется в лог (`skipping N offline node(s)`); если живых нод нет — предупреждение. Статический хелпер `_is_node_live(server, cutoff)` инкапсулирует проверку.

### Frontend: авто-обновление данных

`panel/frontend/src/hooks/useAutoRefresh.ts` — хук для периодического обновления данных на страницах. Поддерживает паузу при скрытой вкладке (Page Visibility API).

Баг с двойным fetch при открытии страницы устранён: visibility effect пропускает первый mount через `mountedRef`, чтобы не дублировать запрос, который уже выполнил `useEffect` компонента при инициализации.

### Frontend: анимации (CSS вместо framer-motion)

Массовые анимации переведены с framer-motion на CSS keyframes/transitions. framer-motion вычисляет каждый кадр в JS на main thread; CSS-анимации исполняются на GPU compositor без JS-нагрузки.

**Затронутые компоненты:**

- `panel/frontend/src/index.css` — CSS-классы: `.btn-scale` (hover/tap), `.live-mode-pulse`, `.status-ping`, `.status-ping-delay`, `.status-blink`, `.card-enter` (entrance с inline `animation-delay`), `.metric-enter`, `.fade-in`, `.pb-track/.pb-fill/.pb-fill-shimmer/.pb-fill-pulse` (прогресс-бар), `.cpu-core-fill` (ядра CPU, `transition: width`), `.loading-blob/.loading-logo-wobble/.loading-text-pulse` (LoadingScreen), `.icon-float` (empty state). Из `.card` убран `backdrop-blur-md` (заменён на непрозрачный фон `bg-dark-900/80`) — backdrop-blur пересчитывается GPU на каждый кадр под слоем при N карточках.

- `panel/frontend/src/components/ui/ProgressBar.tsx` — переписан без framer-motion; ширина обновляется через `transition: width`, анимированный режим — через CSS pseudo-элементы shimmer/pulse.

- `panel/frontend/src/components/ui/Skeleton.tsx` — переписан без framer-motion; использует CSS `.skeleton` shimmer-эффект.

- `panel/frontend/src/components/ui/StatusBadge.tsx` — переписан без framer-motion; online-ореол → CSS `.status-ping` + `.status-ping-delay`, offline-blink → `.status-blink`, loading-спин → `.icon-spin`. Устраняет 2–3 infinite JS-анимации на каждый online-сервер (60+ при 30 серверах).

- `panel/frontend/src/components/Dashboard/ServerCard.tsx` — убраны motion.div entrance/hover variants, заменены на `.card-enter` с `animation-delay` (capped на индексе 20); ядра CPU через `.cpu-core-fill` вместо motion.div (критично: 16 JS-анимаций на обновление метрик при 16-ядерном сервере); `memo` comparator заменён с `JSON.stringify` на цепочку прямых сравнений примитивов.

- `panel/frontend/src/pages/Dashboard.tsx` — staggered entrance-анимации заголовка убраны; motion.button → `<button>` + `.btn-scale`; live-mode бейдж `animate opacity Infinity` → `.live-mode-pulse`; AnimatePresence mode="wait" вокруг loading/empty/servers удалён; motion-обёртки оставлены только там где JS-анимация неизбежна (collapsible folder height, ModalOverlay); убраны `backdrop-blur-sm` на toggle-группах header.

- `panel/frontend/src/App.tsx` — LoadingScreen полностью переписан без framer-motion: `blur(80px)` → `blur(48px)` через `.loading-blob` (нагрузка на GPU квадратична по радиусу), 3 кольца спиннера → CSS `.icon-spin` с `animation-duration` через inline style.

**Результат:** при 30 online-серверах устранено ~200+ постоянных JS-таймеров framer-motion на main thread (StatusBadge ×2 = 60, ProgressBar ×3–6 на карточку = 100+, live-mode = 1 и др.). Все infinite-анимации идут через CSS на GPU compositor.

### nginx keepalive и TCP-оптимизации

`panel/nginx/nginx.conf.template`:
- `worker_processes auto` — автоопределение числа воркеров по CPU
- keepalive в upstream: 32 соединения для backend, 16 для frontend
- `sendfile on`, `tcp_nopush on`, `tcp_nodelay on` — TCP-оптимизации
- Общий лимит тела запроса: `client_max_body_size 10m`
- Отдельный location `= /api/backup/restore`: `client_max_body_size 100m`, `proxy_send_timeout 120s`, `proxy_read_timeout 120s` — для импорта бэкапов размером до 100 MB

`panel/frontend/nginx.conf`: TCP-оптимизации включены; WebSocket-заголовки (Upgrade/Connection upgrade) убраны из API-проксирования.

### Docker ресурсные лимиты

`panel/docker-compose.yml`:
- `nginx`: 1 CPU / 256 MB RAM
- `backend`: 2 CPU

## База данных

Панель использует **PostgreSQL 16** для хранения данных:
- Метрики серверов (история 24ч raw + 30 дней hourly + 365 дней daily, включая TCP states)
- Remnawave статистика (xray_stats: user → IP → count, эфемерная — заменяется каждый цикл; remnawave_hwid_devices: HWID-устройства)
- Кэш пользователей, blocklist правила, настройки
- ASN-кэш (asn_cache — IP → ASN/prefix, TTL 7 дней)

**Преимущества PostgreSQL:**
- Concurrent writes — одновременная запись с множества серверов
- Connection pooling — эффективное использование соединений
- Batch upsert (ON CONFLICT) — 10-100x быстрее записи статистики
- Надёжность и масштабируемость

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

## API

### Система

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/system/panel-ip | IP-адрес панели (резолвится из домена) |
| GET | /api/system/version | Версии панели, нод и оптимизаций (всё в одном запросе, параллельные запросы к нодам) |
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

Проверка версий: панель скачивает `panel/VERSION`, `node/VERSION` и `configs/VERSION` файлы с GitHub и сравнивает с локальными. Все запросы к нодам выполняются параллельно через `asyncio.gather` для быстрой загрузки.

**Системные оптимизации**:
- Не применяются автоматически при обновлении нод
- Применяются только через UI панели (раздел Обновления) или API
- Включают: sysctl настройки, limits, systemd limits

**Страница «Системные оптимизации» (SystemOptimizations.tsx):**

Отображает диагностику NIC на каждой ноде через `GET /api/system/nic-info`. Оператор видит бейджи диагностики и выбирает режим (rps/hybrid/multiqueue) самостоятельно — авто-рекомендация не используется.

Диагностика на карточке ноды:
- Бейдж «Multiqueue: N очередей» или «Multiqueue: нет» (по `max_hw_queues` из `interfaces[]`)
- Бейдж «CPU: Nя / Nп» — физические ядра / логические потоки

**Интерфейс `NicInfo` (`panel/frontend/src/api/client.ts`):**
```typescript
interface NicInfo {
    nic_mode: string;
    multiqueue_supported: boolean;
    cpu_cores: number;
    cpu_threads: number;
    interfaces: { name: string; max_hw_queues: number; current_hw_queues: number }[];
}
```

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
| GET | /api/servers | Список серверов |
| POST | /api/servers | Добавить сервер |
| PUT | /api/servers/{id} | Обновить (включая is_active для вкл/выкл мониторинга) |
| DELETE | /api/servers/{id} | Удалить |
| POST | /api/servers/{id}/test | Тест подключения |
| POST | /api/servers/deploy | Запустить авторазвёртывание ноды (возвращает `{"job_id": "..."}`) |
| GET | /api/servers/deploy/jobs | Список активных и недавно завершённых задач деплоя |
| GET | /api/servers/deploy/{job_id}/stream | NDJSON-стрим лога задачи (переподключаемый) |
| GET | /api/servers/remnawave-certs | Список сохранённых сертификатов Remnawave (без секретов) |
| POST | /api/servers/remnawave-certs | Сохранить сертификат {name, secret_key} |
| DELETE | /api/servers/remnawave-certs/{id} | Удалить сохранённый сертификат |

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
- `panel/frontend/src/components/Infra/AccountNode.tsx` — строка аккаунта: создание/переименование/удаление проектов
- `panel/frontend/src/components/Infra/ProjectNode.tsx` — строка проекта: привязка/отвязка серверов
- `panel/frontend/src/components/Infra/InfraServerRow.tsx` — компактная строка сервера: статус-точка, имя, IP, CPU/RAM/сеть, клик → детали сервера
- `panel/frontend/src/components/Infra/ServerSearchDropdown.tsx` — поиск по имени/IP при привязке сервера
- `panel/frontend/src/pages/Servers.tsx` — InfraTree вставлен выше списка серверов, переключён на `fetchServersWithMetrics`; URL ноды обёрнут в `<CopyableIp>` (показывает полный URL, копирует только хост)

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

Dashboard (`ServerCard.tsx`) читает скорость из `total.rx_bytes_per_sec` / `total.tx_bytes_per_sec` напрямую, не суммируя по интерфейсам. IP-адрес ноды (во всех 4 местах отображения в карточке) рендерится через `<CopyableIp>` — клик ЛКМ копирует адрес в буфер, Tooltip показывает подсказку при наведении, зелёная подсветка держится 1.5 с.

**Load Average:** нода собирает `load_avg_1`, `load_avg_5`, `load_avg_15` из `/proc/loadavg`. На dashboard (стандартный и подробный вид) Load Average отображается в футере карточки с иконкой Activity. На странице `ServerDetails.tsx` — в строке Uptime (`LA: X.XX / X.XX / X.XX`), в секции System Information и в виде графика Load Average History рядом с Network Traffic (цвет `#f59e0b`). Поле `load_avg_1` добавлено в интерфейс `HistoryData`.

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
- `accept_proxy: bool` — принять PROXY protocol от вышестоящего HAProxy. При `True` добавляет `accept-proxy` к bind-строке frontend. Используется для цепочек HAProxy → HAProxy → итоговый сервер, когда первый HAProxy передаёт реальный IP клиента через PROXY protocol. Применяется к TCP и HTTPS правилам (одиночный режим и балансировщик).

**Нормализация конфига при применении шаблона (`patchSendProxy` в `HAProxy.tsx`):**

Кнопка «Применить стандартный шаблон» в UI не только перегенерирует обёртку конфига (global/defaults/resolvers), но и нормализует строки `server` в правилах. Функция `patchSendProxy` обходит все строки с `send-proxy` и дописывает `check-send-proxy`, если его ещё нет. Это гарантирует корректность существующих конфигов, созданных до введения обязательного `check-send-proxy`.
- `target_ssl: bool` — SSL при подключении к target (только для HTTPS-правил)
- `cert_domain` — домен Let's Encrypt сертификата (только для HTTPS-правил)
- `use_wildcard: bool` — при `True` система ищет сертификат по родительскому домену вместо точного совпадения. Например, для `sub.nexyonn.com` будет использован сертификат `nexyonn.com` (wildcard `*.nexyonn.com`). Применяется только для HTTPS-правил.

**Wildcard-сертификаты в HAProxy:**

В форме создания/редактирования HTTPS-правила в `HAProxy.tsx` доступен тумблер «Wildcard сертификат». При включении `use_wildcard=True` нода извлекает родительский домен через `_extract_parent_domain()` в `haproxy_manager.py` и использует его для поиска сертификата вместо точного домена из поля `cert_domain`. Аналогичный параметр доступен в форме Bulk Actions (`BulkActions.tsx`).

Логика в `panel/backend/app/services/haproxy_config.py` и `node/app/services/haproxy_manager.py` при `use_wildcard=True` заменяет домен сертификата на родительский перед генерацией строки `crt` в конфиге HAProxy.

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
| POST | /api/bulk/haproxy/rules | Создать HAProxy правило на выбранных серверах |
| DELETE | /api/bulk/haproxy/rules | Удалить по listen_port + target_ip + target_port |
| POST | /api/bulk/traffic/ports | Добавить отслеживаемый порт |
| DELETE | /api/bulk/traffic/ports | Удалить отслеживаемый порт |
| POST | /api/bulk/firewall/rules | Создать правило firewall |
| DELETE | /api/bulk/firewall/rules | Удалить правило по порту |
| POST | /api/bulk/terminal/execute | Выполнить команду на выбранных серверах |
| POST | /api/bulk/haproxy/config | Заменить конфиг HAProxy на выбранных серверах |

Все bulk-эндпоинты принимают `server_ids: list[int]` и возвращают результат для каждого сервера.
При удалении выполняется проверка наличия правила перед удалением.

**Массовый терминал** (`/bulk/terminal/execute`): принимает `command`, `timeout` (1-600), `shell` (sh/bash). Выполняет команду параллельно на всех серверах через `asyncio.gather`. Возвращает расширенный результат с `stdout`, `stderr`, `exit_code`, `execution_time_ms`.

**Массовая замена конфига HAProxy** (`/bulk/haproxy/config`): принимает `config_content` и `reload_after` (default true). Полностью заменяет конфиг HAProxy на выбранных серверах и опционально перезагружает сервис.

**Frontend (BulkActions.tsx):** инпуты port и from_ip занимают доступную ширину колонки (убран max-w-xs).

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

**Frontend (Blocklist.tsx):** select сервера без ограничения ширины (убран max-w-xs). Двухколоночный grid на вкладках:
- **Global** — форма добавления правила слева, список правил справа
- **Servers** — выбор сервера + форма добавления слева, список правил сервера справа
- **Sources** — список источников слева, форма добавления источника + настройки справа

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

**Эфемерные IP:** данные IP не накапливаются. Каждый цикл сбора полностью заменяет таблицу `xray_stats`. Период-фильтр на эндпоинтах отсутствует — данные всегда актуальные. Автоочистка (`_cleanup_loop`) больше не затрагивает `xray_stats`.

**Telegram-бот для аномалий:** если `anomaly_use_custom_bot=False` (по умолчанию), используется bot_token и chat_id из `AlertSettings`. Если `True` — используются отдельные поля в `RemnawaveSettings`.

**Обнаружение аномалий:**

Четыре типа аномалий, все — только для пользователей с `status == 'ACTIVE'`:

| Тип | Логика | TG-кнопка |
|-----|--------|-----------|
| `ip_exceeds_limit` | IP-адресов > `hwid_device_limit + 2` | [Игнор IP] |
| `hwid_exceeds_limit` | HWID-устройств > `hwid_device_limit`; триггерит авто-очистку через API | нет уведомления |
| `unknown_user_agent` | User-Agent не совпадает с известными клиентами (`KNOWN_UA_PATTERN`) | [Игнор HWID] |
| `traffic_exceeds_limit` | Потребление трафика за 30 минут > `traffic_threshold_gb` ГБ N раз подряд | нет кнопки |

**3-кратное подтверждение IP аномалий**: уведомление в Telegram отправляется только после 3 подряд обнаружений (`IP_CONFIRM_THRESHOLD = 3`). Если IP-count упал ниже лимита — streak (`_ip_anomaly_streak`) сбрасывается. Защита от ложных срабатываний при кратковременных всплесках.

**3-кратное подтверждение трафик-аномалий**: уведомление отправляется только после N подряд превышений порога (настраивается через `traffic_confirm_count`, default 2). Если текущий снимок меньше предыдущего (сброс трафика) — delta считается 0. Streak сбрасывается при падении ниже порога.

**Cooldown уведомлений**: 24 часа между повторными уведомлениями по одному пользователю (`COOLDOWN_SECONDS = 86400`).

**Фильтрация топ-пользователей:**
- Поиск по email выполняется в SQL через JOIN с `remnawave_user_cache`
- Фильтр по статусу: `ACTIVE`, `DISABLED`, `LIMITED`, `EXPIRED`; дефолт: `ACTIVE`
- Фильтр по IP: подзапрос на `xray_stats.source_ip`

**Ноды в настройках:** раздел управления нодами удалён. Ноды получаются автоматически из Remnawave Panel API.

**Frontend:**
- Страница Remnawave: 4 карточки в overview — Users, IPs, Devices, Nodes Online
- Вкладка Anomalies: 5 карточек в summary (добавлена карточка `traffic_exceeds`)
- HWID-устройства пользователя показываются в деталях пользователя
- PeriodSelector убран из Overview и Users (данные всегда актуальные)
- Status filter по умолчанию: `ACTIVE`
- Toggle "Использовать другого Telegram бота" в настройках аномалий; поля token/chat_id показываются только при включённом toggle
- Кнопка игнора аномалии — контекстная: `ip_exceeds_limit` → «Игнор IP», остальные типы → «Игнор HWID»
- Вкладка Settings: двухколоночный grid (lg-брейкпоинт). Левая колонка: API / Collection / Anomaly Notifications. Правая колонка: Traffic Anomaly Triggers (порог в ГБ + confirm count). Save, Ignored Users, списки игнора и Danger Zone — на всю ширину под grid. Секции Ignored Users / Ignore IP / Ignore HWID вынесены в трёхколоночный grid. Ограничение ширины (max-w-5xl/max-w-3xl) снято — контент растянут на всю ширину. Поле поиска и IP-фильтр расширены (max-w-sm убран).
- Все строки локализованы через i18n (ru.json / en.json)

**Файлы:**
- `panel/backend/app/routers/remnawave.py` — API роутер
- `panel/backend/app/services/xray_stats_collector.py` — сбор IP через Remnawave Panel API + HWID-синхронизация
- `panel/backend/app/services/remnawave_api.py` — клиент: `get_all_nodes()`, `poll_users_ips()`, `get_all_hwid_devices_paginated()`
- `panel/frontend/src/pages/Remnawave.tsx` — страница (overview, users, settings)
- `panel/frontend/src/api/client.ts` — API-клиент
- `panel/frontend/src/locales/en.json`, `ru.json` — переводы

### Server Alerts

Система алертов мониторинга серверов с Telegram-уведомлениями. Фоновый сервис `ServerAlerter` проверяет серверы каждые N секунд (default 60) и отправляет уведомления при проблемах.

**Логика:**
- **Offline**: сервер считается недоступным после N последовательных неответов (default 3). Уведомление о восстановлении.
- **CPU/RAM**: критический порог (default 90%) — алерт при длительном превышении. Адаптивное EMA-отслеживание скачков.
- **Network**: спайк/падение трафика относительно EMA baseline.
- **TCP**: отслеживание Established, Listen, Time Wait, Close Wait, SYN Sent, SYN Recv, FIN Wait по отдельности.
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

**Файлы:**
- `panel/backend/app/services/server_alerter.py` — фоновый сервис
- `panel/backend/app/routers/alerts.py` — API роутер
- `panel/backend/app/models.py` — `AlertSettings`, `AlertHistory`
- `panel/frontend/src/pages/Alerts.tsx` — вкладка настроек и истории; секции Telegram Settings + General Settings + Excluded Servers организованы в двухколоночный grid (lg:grid-cols-2); Triggers также в двухколоночном grid

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

**Yandex Cloud — детали:**
- Баланс получается через `GET https://billing.api.cloud.yandex.net/billing/v1/billingAccounts/{id}`
- IAM-токен хранится в БД, но в API-ответах не возвращается — только `has_yc_token: bool`
- Порог отрицательного баланса задаётся вручную (`yc_balance_threshold`)
- Фоновая синхронизация запускается автоматически через `billing_checker.py`
- Frontend: оранжевая иконка Cloud, кнопка "Обновить", поля для токена/billing account ID/порога

**Схема BillingServer (новые поля для YC):**
`yc_iam_token`, `yc_billing_account_id`, `yc_balance_threshold`, `yc_daily_cost`, `yc_last_sync_at`, `yc_last_error`

**Файлы:**
- `panel/backend/app/routers/billing.py` — API роутер
- `panel/backend/app/services/billing_checker.py` — фоновая проверка сроков + Telegram + синхронизация YC
- `panel/backend/app/services/yandex_billing.py` — клиент Yandex Cloud Billing API (баланс, EMA потребления, дней осталось)
- `panel/backend/app/models.py` — `BillingServer` (6 новых полей), `BillingSettings`
- `panel/backend/app/database.py` — миграция `_migrate_yandex_cloud_billing()`
- `panel/frontend/src/pages/Billing.tsx` — вкладка оплаты (AddModal, EditModal, ProjectCard с поддержкой YC)
- `panel/frontend/src/api/client.ts` — интерфейс `BillingServerData` и API методы
- `panel/frontend/src/locales/ru.json`, `en.json` — переводы для YC полей

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

Централизованное управление SSH-безопасностью серверов. Панель проксирует запросы к нодам и предоставляет пресеты безопасности для быстрой настройки. Bulk-операции работают через NDJSON-стриминг: результат по каждому серверу поступает в реальном времени по мере выполнения.

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

**Пресеты:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ssh/presets | Доступные пресеты (встроенные + кастомные) |
| GET | /api/ssh/presets/{name} | Конфиг встроенного пресета (recommended/maximum) |
| POST | /api/ssh/presets/custom | Сохранить текущие настройки как кастомный пресет |
| DELETE | /api/ssh/presets/custom | Удалить кастомный пресет |

**Bulk-эндпоинты (NDJSON-стриминг):**

| Метод | Endpoint | Тело запроса | Описание |
|-------|----------|--------------|----------|
| POST | /api/ssh-security/bulk/apply | `{server_ids, ssh?, fail2ban?}` | Применить SSH-конфиг и/или fail2ban; покрывает настройки и пресеты |
| POST | /api/ssh-security/bulk/keys | `{server_ids, key}` | Добавить SSH-ключ на набор серверов |
| POST | /api/ssh-security/bulk/password | `{server_ids, password}` | Сменить пароль на наборе серверов |
| POST | /api/ssh-security/bulk/status | `{server_ids}` | Получить SSH-статус набора серверов (для обзор-таблицы) |
| POST | /api/ssh-security/server/{id}/password | `{password}` | Сменить пароль на одном сервере |

**NDJSON-протокол стриминга** (`Content-Type: application/x-ndjson`):

```json
{"type": "start", "total": 5}
{"type": "result", "server_id": 3, "server_name": "...", "steps": [{"step": "ssh_config", "success": true}, {"step": "fail2ban", "success": false, "error": "..."}]}
{"type": "result", "server_id": 7, ...}
{"type": "done", "success": 4, "failed": 1}
```

Результаты поступают по мере завершения через `asyncio.as_completed` — порядок не гарантирован. При отмене (клиент оборвал соединение) висящие задачи отменяются. Каждый шаг (`ssh_config`, `fail2ban`, `key`, `password`) пишется отдельной записью с полями `success`, `message`, `error`, `warnings`.

**Встроенные пресеты безопасности:**
- `recommended` — вход только root по паролю: `permit_root_login: yes`, `password_authentication: true`, `pubkey_authentication: false`, `allow_users: [root]`, fail2ban с мягкими настройками
- `maximum` — максимальная защита: только ключи, `permit_root_login: no`, агрессивный fail2ban

**Кастомные пресеты:**
- Сохраняются в `panel_settings` под ключом `ssh_custom_presets` (JSON-массив)
- Позволяют сохранить произвольный набор настроек sshd/fail2ban с именем и применить его к любым серверам

**nginx:** location `~ ^/api/(ssh-security/bulk/(apply|keys|password|status))$` с `proxy_buffering off; gzip off; proxy_read_timeout 620s` — иначе NDJSON буферизуется.

**GZipMiddlewareNoSSE** в `main.py` пропускает пути `/ssh-security/bulk/` без gzip-буферизации.

**Frontend:**
- `panel/frontend/src/pages/SSHSecurity.tsx` — два режима в шапке: «Обзор» и «Настройка»; разделены состояния `activeServerId` (просмотр/правка) и `selectedServerIds` (множество для bulk-применения)
- `panel/frontend/src/components/ssh/ServerSelector.tsx` — мультивыбор серверов: поиск, «выбрать все/снять», группировка по папкам со сворачиванием, чекбоксы с indeterminate
- `panel/frontend/src/components/ssh/BulkProgressPanel.tsx` — live-список прогресса: статус ✓/✗ по серверу, разбивка по шагам с текстом ошибок, прогресс-бар
- `panel/frontend/src/components/ssh/SSHOverviewTable.tsx` — обзор-таблица состояния SSH всех серверов (порт, метод авторизации, fail2ban, кол-во ключей, доступность ноды); данные через `/bulk/status`
- `panel/frontend/src/components/ssh/useSSHBulkStream.ts` — хук запуска стриминговой bulk-операции и сбора прогресса
- `panel/frontend/src/utils/ndjsonStream.ts` — `streamNdjson()`: чтение NDJSON-потока через `fetch` + `ReadableStream`, обработка 401 (редирект на логин) и обрыва

**Файлы:**
- `panel/backend/app/services/ssh_manager.py` — пресеты безопасности; `proxy_to_node()` с параметром `use_apply_client` (HTTP/1.1 для долгих шагов fail2ban/password)
- `panel/backend/app/routers/ssh_security.py` — API роутер; хелперы: `_ndjson()`, `_apply_steps()`, `_fetch_ssh_status()`, `_stream_ndjson()`

### Авторазвёртывание ноды

Установка ноды мониторинга на новый сервер прямо из вкладки «Серверы» панели. Подключается к целевому серверу по SSH, скачивает `install.sh` и запускает его в режиме `--unattended`. Установка выполняется в **фоновой asyncio-задаче** — закрытие вкладки браузера не прерывает процесс (SSH-сессию держит backend).

**Принцип работы (job-модель):**
1. Пользователь открывает форму «Добавить сервер», включает чекбокс «Автоустановка ноды по SSH»
2. Вводит SSH-данные (порт, логин, пароль или приватный ключ + passphrase) и выбирает доп. компоненты
3. Frontend отправляет `POST /api/servers/deploy` → бэкенд немедленно возвращает `{"job_id": "<hex>"}` и запускает `asyncio.create_task`
4. Frontend подписывается на лог через `GET /api/servers/deploy/{job_id}/stream` (NDJSON)
5. При успехе backend создаёт запись `Server`, применяет SSH-пресет/пароль (`_post_install`) и привязывает к выбранным HAProxy/Firewall-профилям (`_bind_profiles`)
6. Завершённые задачи хранятся 600 секунд (`FINISHED_TTL_SECONDS`) для переподключения, затем удаляются из памяти

**Ограничение:** перезапуск backend-контейнера во время установки прерывает её.

**Менеджер фоновых задач (`DeployJobManager`):**

Singleton-сервис `panel/backend/app/services/deploy_job_manager.py`. Управляет задачами установки нод:
- Лог буферизуется в памяти (лимит 5000 строк) и раздаётся подписчикам через pub/sub (`asyncio.Queue`)
- Дедупликация строк между реплеем и live-потоком — по индексу `_idx`
- `_create_server` — создание записи `Server` после успешной установки
- `_post_install` — постустановочные шаги (SSH-пресет, fail2ban, смена пароля root)
- `_bind_profiles` — привязка к HAProxy/Firewall-профилям (выполняется на бэке внутри задачи — срабатывает даже при закрытой странице)
- `get_deploy_job_manager()` — dependency для получения singleton через FastAPI DI

**SSH-подключение:**
- Авторизация: пароль или приватный ключ (+ passphrase опционально)
- Пароль SSH нигде не сохраняется
- `known_hosts` отключён — целевые серверы заранее неизвестны
- Таймаут установки: 25 минут

**Дополнительные компоненты (чекбоксы в форме):**
- **Системные оптимизации** — устанавливается через `MON_INSTALL_OPTIMIZATIONS=1`; при включении появляются переключатель профиля и переключатель NIC-режима (см. ниже)
- **Cloudflare WARP** — устанавливается через `MON_INSTALL_WARP=1`
- **Нода Remnawave** — устанавливается через `MON_INSTALL_REMNAWAVE=1`; сертификат передаётся через `REMNAWAVE_CERT`; доступен ввод сертификата вручную или выбор сохранённого профиля; сохранённые сертификаты отображаются кликабельными чипами с именами (клик — выбор, крестик — удаление), рядом кнопка «Новый сертификат» переключает на ввод вручную
- **HTTP-прокси** — передаётся через `MON_PROXY_URL` для окружений без прямого доступа

**Профиль sysctl-оптимизаций (`opt_profile`) и NIC-режим (`nic_mode`):**

Отображаются под чекбоксом «Системные оптимизации». Профиль — два переключателя, NIC-режим — четыре. Оба поля передаются в `POST /api/servers/deploy` и далее через `MON_OPT_PROFILE` / `MON_NIC_MODE` в `install.sh`.

**NIC-режим (`nic_mode`):**

| Кнопка в UI | Значение `nic_mode` | Поведение |
|-------------|---------------------|-----------|
| **Авто** | `auto` (по умолчанию) | `auto_detect_nic_mode()` на целевом сервере определяет режим автоматически |
| **Multiqueue** | `multiqueue` | Принудительно устанавливается аппаратный multiqueue (через `MON_NIC_MODE=multiqueue`) |
| **Hybrid** | `hybrid` | Принудительно устанавливается гибридный режим |
| **RPS** | `rps` | Принудительно устанавливается программный RPS |

При `nic_mode != auto` backend передаёт env `MON_NIC_MODE=<значение>` в команду установки; `install.sh` в `run_unattended()` пропускает автодетект и использует заданный режим напрямую.

| Кнопка в UI | Значение `opt_profile` | Конфиг | Характеристики |
|-------------|------------------------|--------|----------------|
| **VPN-сервер** | `vpn` (по умолчанию) | `configs/vpn/` | Агрессивный тюнинг для VPN/прокси-нод: IPv6 отключён, `file-max 2097152`, `nf_conntrack_max 2097152` |
| **Универсальные** | `panel` | `configs/panel/` | Умеренный тюнинг для смешанных нагрузок: IPv6 не трогается, `file-max 524288`, `nf_conntrack_max 262144`, расслабленные conntrack-таймауты |

**Сохранённые сертификаты Remnawave (`RemnawaveCertProfile`):**

Модель `RemnawaveCertProfile` (таблица `remnawave_cert_profiles`): id, name (unique), secret_key, created_at. Позволяет сохранить именованный сертификат один раз и переиспользовать при последующих развёртываниях. В ответах на `GET /api/servers/remnawave-certs` поле `secret_key` не возвращается.

**Поля запроса `POST /api/servers/deploy`:**

`DeployRequest` содержит:
- `haproxy_profile_id: int | None` — привязать к HAProxy-профилю после установки
- `firewall_profile_id: int | None` — привязать к Firewall-профилю после установки
- `install_optimizations: bool` — при `true` передаётся `MON_INSTALL_OPTIMIZATIONS=1`
- `opt_profile: str` — профиль sysctl (`vpn` по умолчанию или `panel`)
- `nic_mode: str` — NIC-режим (`auto` по умолчанию, либо `multiqueue`/`hybrid`/`rps`)
- `ssh_preset: str | None` — пресет защиты SSH: `None` / `recommended` / `maximum`
- `new_root_password: str | None` — новый пароль root (минимум 8 символов)

Ответ: `{"job_id": "<hex>"}`.

**API deploy-задач:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/servers/deploy | Запустить задачу деплоя → `{"job_id": "..."}` |
| GET | /api/servers/deploy/jobs | Список задач: job_id, name, host, status, exit_code, server_id, error |
| GET | /api/servers/deploy/{job_id}/stream | Переподключаемый NDJSON-стрим лога (реплей + live) |

**NDJSON-протокол стрима (`GET /api/servers/deploy/{job_id}/stream`):**

```json
{"type": "start"}
{"type": "log", "line": "Installing monitoring node...", "_idx": 0}
{"type": "log", "line": "[OK] Node installed", "_idx": 1}
{"type": "done", "success": true, "server_id": 42}
```

При ошибке: `{"type": "error", "line": "..."}` и `{"type": "done", "success": false}`. Клиент дедуплицирует строки по полю `_idx` — при переподключении реплей не создаёт дублей. GZip-middleware пропускает путь `/servers/deploy/.../stream` без буферизации.

**Постустановочная защита SSH и привязка профилей:**

Всё выполняется внутри фоновой задачи (не в HTTP-контексте):
1. Пауза ~8 секунд — нода поднимается после установки
2. Если `ssh_preset` задан — SSH-конфиг + fail2ban через ноду API
3. Если `new_root_password` задан — смена пароля root через ноду API
4. Если `haproxy_profile_id` задан — `POST /haproxy-profiles/{id}/servers/{server_id}`
5. Если `firewall_profile_id` задан — `POST /firewall-profiles/{id}/servers/{server_id}`

Все шаги **best-effort**: ошибки пишутся в лог как NDJSON-события, нода считается успешно добавленной.

**Восстановление задач при перезагрузке страницы:**

Frontend сохраняет незавершённые job_id в `localStorage` (`deploy_active_jobs_v1`). При загрузке страницы вызывается `restoreDeployJobs` — сверяет сохранённые job_id с ответом `GET /api/servers/deploy/jobs` и переподключает активные задачи.

**Авто-скрытие после успеха:** через ~6 секунд (`AUTO_HIDE_MS`) после успешного деплоя лог скрывается, а extra-карточка удаляется.

**Регистрация роутера:** `server_deploy.router` зарегистрирован в `main.py` до `servers.router`, чтобы статичные пути (`/servers/deploy`, `/servers/remnawave-certs`) матчились раньше параметрического `GET /servers/{id}`.

**Frontend (`panel/frontend/src/pages/Servers.tsx`):**
- Чекбокс «Автоустановка ноды по SSH» в форме добавления сервера; при выключенном — сервер добавляется как раньше через `POST /api/servers`
- SSH-поля: порт, логин, пароль или приватный ключ + passphrase
- Чекбоксы компонентов: **Системные оптимизации**, WARP, Remnawave, HTTP-прокси
- При включении оптимизаций: переключатель профиля и переключатель NIC-режима
- Выбор сертификата Remnawave: кликабельные чипы с именами
- Раздел **«Защита SSH»**: переключатель пресета + смена пароля root
- При submit: POST startDeploy → job_id → подписка через `streamNdjsonGet`; живой лог в форме

**Массовый авто-деплой (multi-target):**

Кнопка «Установить ещё один сервер» добавляет карточку дополнительной цели. Кнопка дублируется внизу каждой extra-карточки рядом с «Повторить».

При нажатии «Развернуть ноды (N)»:
- `handleDeployAll` запускает `Promise.all` — каждый таргет деплоится параллельно
- Каждый таргет: POST startDeploy → job_id → `streamNdjsonGet` для чтения лога
- Незавершённые job_id сохраняются в `localStorage` и переподключаются при перезагрузке страницы
- Уже успешные таргеты при повторном запуске пропускаются
- Retry-кнопки: per-target (`retryExtra(id)` / `retryPrimary`)

**NDJSON-утилиты (`panel/frontend/src/utils/ndjsonStream.ts`):**
- `streamNdjson(url, body, onEvent)` — POST-стрим (SSH bulk-операции)
- `streamNdjsonGet(url, onEvent)` — GET-стрим (подписка на лог задачи деплоя)
- `readNdjsonResponse` — общая логика чтения, вынесена из обеих функций

**Новые компоненты:**
- `panel/frontend/src/components/servers/DeployTargetFields.tsx` — переиспользуемые поля SSH/опций/прокси/Remnawave/SSH-пресета/смены пароля; блок «Привязать к профилям» (HAProxy + Firewall); экспортирует `DEPLOY_DEFAULTS` и тип `DeployFormData`
- `panel/frontend/src/components/servers/ExtraServerCard.tsx` — карточка дополнительной цели; поле `jobId?` в типе `ExtraTarget`

**i18n:** ключи `deploy_add_extra`, `deploy_btn_multi`, `deploy_success_multi`, `deploy_failed_multi`, `deploy_primary`, `deploy_extra_default_name`, `deploy_extra_ok`, `deploy_extra_failed`, `deploy_extra_retry`, `deploy_extra_remove`, `deploy_bindings`, `deploy_haproxy_profile`, `deploy_firewall_profile`, `deploy_profile_none` в `ru.json` и `en.json`.

**Зависимости:**
- `asyncssh` добавлен в `panel/backend/requirements.txt`

**Файлы:**
- `panel/backend/app/services/deploy_job_manager.py` — `DeployJobManager`: singleton, in-memory буфер лога, pub/sub, `_create_server`, `_post_install`, `_bind_profiles`; `PostDeployOptions` dataclass; `get_deploy_job_manager()`
- `panel/backend/app/services/deploy_service.py` — SSH-подключение через `asyncssh`, скачивание и запуск `install.sh --unattended`, построчный стриминг лога
- `panel/backend/app/routers/server_deploy.py` — роутер (prefix `/servers`): `POST /deploy`, `GET /deploy/jobs`, `GET /deploy/{job_id}/stream`, remnawave-certs CRUD
- `panel/backend/app/models.py` — модель `RemnawaveCertProfile` (таблица `remnawave_cert_profiles`)
- `panel/backend/app/main.py` — `GZipMiddlewareNoSSE` расширен: bypass для `/servers/deploy/.../stream`
- `panel/frontend/src/pages/Servers.tsx` — job-модель деплоя, `restoreDeployJobs`, `AUTO_HIDE_MS`
- `panel/frontend/src/utils/ndjsonStream.ts` — `streamNdjson`, `streamNdjsonGet`, `readNdjsonResponse`
- `panel/frontend/src/api/client.ts` — `serversApi.startDeploy()`, `serversApi.listDeployJobs()`, `serverDeployJobStreamUrl(jobId)`, интерфейс `DeployJobInfo`
- `panel/frontend/src/locales/ru.json`, `en.json` — ключи `servers.deploy_*`

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
1. Панель вызывает certbot с плагином `certbot-dns-cloudflare` для DNS-01 challenge — получает wildcard `*.domain.com`
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
- `wildcard_ssl_deploy_path` — путь на хосте для записи файлов (например `/etc/nginx/ssl/`)
- `wildcard_ssl_reload_cmd` — команда перезагрузки сервиса после деплоя (например `systemctl reload nginx`)
- `active_firewall_profile_id` — FK на активный профиль UFW (nullable; один профиль на сервер)
- `firewall_sync_status` — статус последней синхронизации профиля (success/failed/rolled_back)
- `firewall_rules_hash` — SHA256-хэш применённого профиля (для drift-детекции)
- `firewall_last_sync_at` — время последней синхронизации

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

### Firewall Profiles (профили UFW)

Централизованное управление UFW-шаблонами с массовой раскаткой на серверы. Архитектурно аналогично HAProxy Profiles: один активный профиль на сервер, JSON-блоб правил, history синхронизаций.

**Принцип работы:**
1. В панели создаётся профиль с набором UFW-правил (JSON-блоб) и политиками по умолчанию
2. Серверы привязываются к профилю через `Server.active_firewall_profile_id` (M:1)
3. `POST /{id}/sync` запускает массовую раскатку: панель вызывает `POST /api/firewall/profile/apply` на каждой ноде
4. Нода атомарно заменяет состояние UFW (backup → reset → apply → enable), при ошибке — авторолбэк из бэкапа
5. Результат записывается в `firewall_sync_logs`; статус и хэш обновляются в модели `Server`

**Хранение правил:**

Правила хранятся в одной колонке `rules_json TEXT` в виде JSON-массива. Никаких отдельных таблиц для правил. Поля правила (v1): `port`, `protocol` (tcp/udp/any), `action` (allow/deny), `from_ip` (IP/CIDR/null), `direction` (in/out), `comment`.

**Стратегия применения:**

Replace-атомарно: полное состояние ноды = состояние профиля. Локальные UFW-правила при первом применении перезаписываются, но сохраняются в бэкапе (`/etc/monitoring/ufw_backup_<timestamp>.json`) для возможного rollback. При отвязке сервера от профиля правила на ноде **не откатываются** (сознательное решение, аналогично HAProxy).

**Node-API-port-guard (три уровня):**
1. Новый профиль автозаполняется правилом для порта 9100 (`allow 9100/tcp`, comment: "Monitoring node API")
2. В UI — баннер-предупреждение и иконка-индикатор при отсутствии правила для порта 9100; пересчёт флага выполняется локально через `computeNodePortAllowed` после каждого изменения правил без перезагрузки профиля
3. Node-API-port-guard на ноде: `apply_profile` отказывается применять профиль, если в правилах нет `allow 9100/tcp IN` и `default_incoming != allow`; обойти можно через `force=true` (применяется при подтверждении в UI). Гарантирует, что панель не потеряет связь с нодой. SSH-доступ — отдельная зона ответственности.

**Drift-детекция:**

Каждый профиль и каждое применённое состояние имеют каноничный SHA256-хэш (`compute_rules_hash(rules_json, default_in, default_out)`). Поле `comment` из правил в хэш **не входит** — UFW не сохраняет комментарии при применении правил, поэтому после apply значение всегда пустое и включение `comment` в хэш давало бы постоянный ложный drift. Формула хэша на панели (`firewall_profile_sync.py`) и на ноде (`firewall_manager.py`) синхронна.

**Синхронизация с нодами:**

Панель использует `get_node_client(server)` + `node_auth_headers(server)` — поддерживаются как обычные ноды (API-ключ), так и mTLS-ноды. Таймаут apply — 120 секунд на ноду.

**Схема БД:**

`firewall_profiles`:
- `id`, `name`, `description`
- `rules_json TEXT` — JSON-массив правил
- `default_incoming`, `default_outgoing` — политика UFW по умолчанию
- `position` — порядок отображения
- `created_at`, `updated_at`

`firewall_sync_logs`:
- `profile_id` (FK), `server_id` (FK)
- `status` — success / failed / rolled_back
- `message` — детали ошибки или успеха
- `rules_hash` — хэш примененного профиля
- `created_at`

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /firewall-profiles | Список профилей |
| POST | /firewall-profiles | Создать профиль |
| PUT | /firewall-profiles/{id} | Обновить профиль |
| DELETE | /firewall-profiles/{id} | Удалить профиль |
| GET | /firewall-profiles/{id}/rules | Правила профиля |
| POST | /firewall-profiles/{id}/rules | Добавить правило (HTTP 409 при дубликате) |
| PUT | /firewall-profiles/{id}/rules/{index} | Обновить правило по индексу (HTTP 409 если правило станет дубликатом) |
| DELETE | /firewall-profiles/{id}/rules/{index} | Удалить правило по индексу |
| POST | /firewall-profiles/{id}/servers/{server_id} | Привязать сервер |
| DELETE | /firewall-profiles/{id}/servers/{server_id} | Отвязать сервер |
| POST | /firewall-profiles/{id}/sync?force=false | Массовая раскатка на привязанные серверы |
| GET | /firewall-profiles/{id}/log | История синхронизаций |
| GET | /firewall-profiles/available-servers | Все серверы для привязки |

Параллелизм: `sync_profile_to_servers` использует `asyncio.Semaphore(MAX_CONCURRENT_SYNCS=10)` с таймаутом apply 120s на ноду.

**Защита от дубликатов правил:**

Хелпер `_rule_identity(rule)` строит каноничный ключ правила: `(port, protocol, action, from_ip, direction)` — поле `comment` не учитывается. При `POST /{id}/rules` (добавление) — если правило с таким ключом уже есть в профиле, возвращается `HTTP 409 "Такое правило уже есть в профиле"`. При `PUT /{id}/rules/{index}` (редактирование) — если изменённое правило становится дубликатом другого существующего правила профиля, также возвращается `HTTP 409`.

**Frontend (`panel/frontend/src/pages/FirewallProfiles.tsx`):**
- Двухколоночный layout: список профилей слева + детали справа
- Детали: три вкладки — Rules (список правил с CRUD), Servers (привязанные серверы + кнопка sync), Log (история синхронизаций)
- Автообновление — три независимых `setInterval` по 3 секунды: список профилей (счётчики `synced/linked`, индикатор `hasUnsync`), детали выбранного профиля (silent-refetch без `setLoading` и без тостов), лог синхронизаций (только пока активна вкладка Log, при смене таба интервал чистится)
- Тосты ошибок при фоновых опросах подавляются через `initialLoadDone` ref — toast показывается только при первом сбое загрузки
- Route: `/{uid}/firewall-profiles` (lazy-import)
- Навигация: пункт «Firewall профили» с иконкой Flame после HAProxy Configs

**Файлы:**
- `panel/backend/app/models.py` — модели `FirewallProfile`, `FirewallSyncLog`; новые поля `Server`
- `panel/backend/app/database.py` — миграция `firewall_profile_columns`; FK `firewall_sync_log_server_id_fkey` в `_ensure_fk_constraints`
- `panel/backend/app/routers/firewall_profiles.py` — API роутер (prefix `/firewall-profiles`)
- `panel/backend/app/services/firewall_profile_sync.py` — `compute_rules_hash`, `sync_profile_to_servers`
- `panel/backend/app/main.py` — регистрация роутера
- `panel/frontend/src/pages/FirewallProfiles.tsx` — страница управления
- `panel/frontend/src/api/client.ts` — `firewallProfilesApi` с интерфейсами и типами
- `panel/frontend/src/App.tsx` — роут `firewall-profiles`
- `panel/frontend/src/components/Layout/Layout.tsx` — пункт навигации «Firewall профили» (иконка Flame, после HAProxy Configs)
- `node/app/services/firewall_manager.py` — `apply_profile`, `_backup_state`/`_restore_state`, `compute_rules_hash`, `get_full_state`, `_has_node_port_allow`
- `node/app/models/firewall_profile.py` — Pydantic модели: `ProfileRule`, `ProfileApplyRequest`, `ProfileApplyResponse`, `ProfileStateResponse`
- `node/app/routers/firewall_profile.py` — `POST /api/firewall/profile/apply` (asyncio.Lock), `GET /api/firewall/profile/state`

### HAProxy Configs (профили конфигурации HAProxy)

Централизованное управление конфигурациями HAProxy с массовой раскаткой на серверы. Каждый профиль содержит набор правил и настройки балансировщика; серверы привязываются к профилю и получают конфиг через sync.

**Принцип работы:**
1. В панели создаётся профиль с набором правил (TCP/HTTPS, одиночный режим или балансировщик)
2. Серверы привязываются к профилю через `active_haproxy_profile_id` в модели `Server`
3. `POST /{id}/sync` раскатывает конфиг на все привязанные серверы параллельно; офлайн-ноды не опрашиваются — им выставляется статус `pending`, досинхронизация происходит автоматически когда нода ожила
4. Для одиночного сервера — `POST /{id}/sync/{server_id}` — точечная синхронизация
5. Статус синхронизации (synced/pending/failed) и история хранятся в `haproxy_sync_logs`

**Режимы правил:**
- **Одиночный** (`use_balancer: false`) — одно target-адрес:порт; поддерживает `send_proxy`, `accept_proxy`, `target_ssl`, wildcard-сертификат
- **Балансировщик** (`use_balancer: true`) — несколько backend-серверов с настройками алгоритма, health-check, sticky sessions, таймаутов

**Валидация конфига на стороне панели:**

Перед сохранением `config_content` через `PUT /haproxy-profiles/{id}` бэкенд запускает `haproxy -c -f <tempfile>` локально (бинарь установлен в образ backend). Поскольку реальных TLS-сертификатов нод на панели нет, все пути `crt` в конфиге подменяются self-signed dummy-сертификатом (`/tmp/haproxy-validate/dummy.pem`) — проверяется синтаксис и структура, а не наличие конкретных файлов. При провале валидации возвращается `HTTP 400` с сообщением об ошибке и конфиг не сохраняется. Если бинарь haproxy недоступен (образ не пересобран) — валидация мягко пропускается (`valid=True`).

**Отложенная синхронизация офлайн-нод:**

`haproxy_profile_sync.py` определяет онлайн/офлайн по `last_seen` ноды (порог `max(90, collect_interval*3+30)` сек). Офлайн-ноды при sync пропускаются: им выставляется `sync_status='pending'` и `SyncResult.status='queued'`. Фоновый цикл `_haproxy_pending_sync_loop` (интервал `HAPROXY_RETRY_INTERVAL=30` сек) в `metrics_collector.py` автоматически досинхронизирует ожившие pending-ноды через `retry_pending_haproxy_syncs`.

Каждый сервер синхронизируется с **собственной сессией БД** (не шареной) — статусы обновляются по мере готовности, а не разом в конце.

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /haproxy-profiles | Список профилей |
| POST | /haproxy-profiles | Создать профиль |
| PUT | /haproxy-profiles/{id} | Обновить профиль (с валидацией config_content) |
| DELETE | /haproxy-profiles/{id} | Удалить профиль |
| GET | /haproxy-profiles/{id} | Детали профиля (правила + серверы) |
| POST | /haproxy-profiles/{id}/rules | Добавить правило |
| PUT | /haproxy-profiles/{id}/rules/{index} | Обновить правило |
| DELETE | /haproxy-profiles/{id}/rules/{index} | Удалить правило |
| POST | /haproxy-profiles/{id}/servers/{server_id} | Привязать сервер |
| DELETE | /haproxy-profiles/{id}/servers/{server_id} | Отвязать сервер |
| POST | /haproxy-profiles/{id}/sync | Синхронизировать профиль на все привязанные серверы |
| POST | /haproxy-profiles/{id}/sync/{server_id} | Синхронизировать на один сервер |
| GET | /haproxy-profiles/{id}/log | История синхронизаций |
| GET | /haproxy-profiles/{id}/servers-status | Статусы серверов профиля (включая `online: bool`) |
| POST | /haproxy-profiles/validate | Валидировать config_content без сохранения → `{valid, message}` |
| GET | /haproxy-profiles/available-servers | Серверы доступные для привязки |

**SyncResult.status:** `success` | `failed` | `queued` (офлайн-нода, синхронизация отложена).

**Frontend (`panel/frontend/src/pages/HAProxyConfigs.tsx`):**
- Двухколоночный layout: список профилей слева + детали справа
- Детали: три вкладки — Rules (список правил с CRUD), Servers (привязанные серверы + управление), Log (история синхронизаций)
- Route: `/{uid}/haproxy-configs` (lazy-import)
- Навигация: пункт «HAProxy Configs» с иконкой FileCode2

**Управление запуском HAProxy в разделе "Привязанные серверы":**

- **Индикатор онлайн/офлайн** (цветная точка слева от имени сервера) — зелёная пульсирующая = нода онлайн, красная = офлайн; офлайн-строки приглушены (opacity). Статус HAProxy-службы вынесен в отдельный мелкий индикатор (иконка Activity) рядом с бейджем sync-статуса.
- **Per-server кнопка "Запустить HAProxy"** (иконка Play, зелёная) — отображается только для online-серверов с `haproxy_running === false`. Расположена слева от кнопки "Sync to server". Вызывает `POST /api/proxy/{server_id}/haproxy/start`.
- **Bulk-кнопка "Запустить остановленные"** (иконка Play, зелёная) — отображается в тулбаре только когда хотя бы у одного online-сервера HAProxy остановлен. Параллельно запускает HAProxy через `Promise.allSettled`.
- **`SyncStatusBadge`** для офлайн+pending показывает «Ожидает сервер» вместо «pending».
- **Тосты sync** (`handleSyncAll`/`handleSyncOne`) раздельно считают synced/queued/failed и показывают корректный текст (включая «отложено (офлайн)»).
- **Кнопка «Проверить конфиг»** в модалке сырого конфига — вызывает `POST /haproxy-profiles/validate` и показывает результат валидации.

**i18n-ключи** (`haproxy_configs.*`): `start_haproxy`, `start_all_stopped`, `haproxy_started`, `haproxy_start_error`, `haproxy_start_bulk_success`, `haproxy_start_bulk_partial`, `sync_queued`, `sync_one_queued`, `waiting_server`, `server_online`, `server_offline`, `haproxy_running`, `haproxy_stopped`, `validate_config`, `config_valid`, `config_invalid`, `validate_error`.

**Файлы:**
- `panel/backend/app/routers/haproxy_profiles.py` — API роутер; `PUT /{id}` с валидацией; `POST /validate`
- `panel/backend/app/services/haproxy_validator.py` — `validate_config(config_content)`: запуск `haproxy -c -f`, замена путей `crt` на dummy-сертификат
- `panel/backend/app/services/haproxy_profile_sync.py` — `is_server_online`, `_sync_single_server` (отдельная DB-сессия), `SyncResult` (`status: success|failed|queued`), `retry_pending_haproxy_syncs`
- `panel/backend/app/services/metrics_collector.py` — фоновый цикл `_haproxy_pending_sync_loop` (интервал `HAPROXY_RETRY_INTERVAL=30` сек)
- `panel/backend/Dockerfile` — добавлен пакет `haproxy` для локальной валидации
- `panel/frontend/src/pages/HAProxyConfigs.tsx` — страница управления; индикаторы online/offline; `SyncStatusBadge`; кнопка «Проверить конфиг»
- `panel/frontend/src/api/client.ts` — `haproxyProfilesApi.validateConfig()`, `HAProxyServerStatus.online`, `HAProxySyncResult.status`
- `panel/frontend/src/App.tsx` — роут `haproxy-configs`
- `panel/frontend/src/components/Layout/Layout.tsx` — пункт навигации «HAProxy Configs»
- `panel/frontend/src/locales/ru.json`, `en.json` — i18n ключи пространства имён `haproxy_configs`

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
- `panel/frontend/src/components/Layout/Layout.tsx` — плавающий amber-таб на правом крае экрана (вместо кнопки в сайдбаре) + рендер NotesDrawer
- `panel/frontend/src/locales/en.json`, `ru.json` — ключи `tab_notes`, `tab_tasks`, `task_placeholder`, `no_tasks`, `done`

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

### Torrent Blocker

Обнаружение торрент-трафика с автоматической блокировкой IP. Панель опрашивает Remnawave API, получает текущие сессии пользователей, детектирует торрент-паттерны и рассылает команду бана на все активные ноды. Опционально перед баном отправляет вебхук-предупреждение с задержкой — даёт пользователю время выключить клиент.

**Принцип работы с вебхуком (greyperiod-бан):**

1. Сервис обнаруживает IP с торрент-трафиком и формирует список `BanTarget`.
2. Если `webhook_enabled=True` и URL задан — обогащает цели (`_enrich_targets`): дотягивает `telegram_id` и `short_uuid` из таблицы-кэша `remnawave_user_cache` по UUID пользователя.
3. Отправляет POST-вебхуки на внешний URL параллельно через `_send_webhooks` (concurrency 20, `WEBHOOK_CONCURRENCY`).
4. Ждёт `webhook_delay_seconds` секунд — грейс-период, во время которого пользователь может остановить торрент.
5. Банит IP на всех нодах.

Сбой вебхука или отсутствие `telegram_id` бан **не отменяет** (fail-open): система всегда банит по истечении задержки.

**Настройки (`TorrentBlockerSettings`):**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `webhook_enabled` | Boolean | False | Включить вебхук-предупреждения |
| `webhook_url` | Text | — | URL эндпоинта (только HTTPS) |
| `webhook_secret` | Text | — | Секрет для HMAC-SHA256 подписи (опционально) |
| `webhook_delay_seconds` | Integer | 60 | Задержка между вебхуком и баном (0–1800 сек) |

**Формат вебхука:**

Подпись передаётся в заголовке `X-Signature: sha256=<hex>` (HMAC-SHA256 от тела запроса + secret). Если секрет не задан — заголовок не отправляется.

```json
{
  "event": "torrent_warning",
  "ip": "1.2.3.4",
  "user": {
    "uuid": "...",
    "short_uuid": "...",
    "username": "...",
    "telegram_id": 123456789
  },
  "node": {
    "name": "...",
    "country": "..."
  },
  "ban_duration_seconds": 3600,
  "delay_seconds": 60,
  "ban_at": "2026-06-01T12:01:00Z",
  "scheduled_at": "2026-06-01T12:00:00Z"
}
```

**Миграция БД:**

`database.py` в `run_migrations()` содержит блок, добавляющий колонки `webhook_enabled`, `webhook_url`, `webhook_secret`, `webhook_delay_seconds` в таблицу `torrent_blocker_settings` для существующих установок.

**API:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/torrent-blocker/settings | Текущие настройки (включая webhook-поля) |
| PUT | /api/torrent-blocker/settings | Обновить настройки; `webhook_url` валидируется как HTTPS; `webhook_delay_seconds` 0–1800 |
| POST | /api/torrent-blocker/test-webhook | Отправить тестовый вебхук `{webhook_url, webhook_secret}` → `{success, message}` |
| GET | /api/torrent-blocker/status | Статус цикла (содержит счётчик `webhooks X/Y` при активных вебхуках) |

**Ключевые файлы:**

- `panel/backend/app/models.py` — модель `TorrentBlockerSettings`: новые колонки `webhook_*`
- `panel/backend/app/database.py` — миграция webhook-колонок в `run_migrations()`
- `panel/backend/app/services/torrent_blocker.py` — `BanTarget` dataclass; `_extract_ban_targets` (дедуп IP→пользователь/нода из Remnawave-отчёта); `_enrich_targets` (дотягивает `telegram_id`, `short_uuid` из `remnawave_user_cache`); `_send_webhooks` (HMAC-SHA256, `asyncio.Semaphore(WEBHOOK_CONCURRENCY)`); `send_test_webhook` (статический метод); `_poll_cycle` — вебхук + `asyncio.sleep(delay)` + бан; `_is_node_live(server, cutoff)` — хелпер живости ноды по `last_seen`; `_send_to_nodes` — рассылает баны только живым нодам (`last_seen` свежее `LIVE_THRESHOLD_SECONDS = 300` сек)
- `panel/backend/app/routers/torrent_blocker.py` — `UpdateSettings` с webhook-полями и field_validator; `_settings_to_dict`; эндпоинт `POST /test-webhook`
- `panel/frontend/src/api/client.ts` — тип `TorrentBlockerSettings` + метод `torrentBlockerApi.testWebhook`
- `panel/frontend/src/stores/torrentBlockerStore.ts` — action `testWebhook`
- `panel/frontend/src/pages/TorrentBlocker.tsx` — блок «Webhook-предупреждение»: toggle, HTTPS-URL, секрет, задержка, кнопка «Тестовый вебхук»
- `panel/frontend/src/locales/ru.json`, `en.json` — ключи `torrent_blocker.webhook_*`
- `panel/frontend/src/data/faq/content/ru/PAGE_TORRENT_BLOCKER.md` — описание вебхука в FAQ

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
