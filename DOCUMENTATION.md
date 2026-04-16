# Monitoring — Документация проекта

Система мониторинга серверов с веб-панелью и агентами на нодах.

## Состав проекта

```
monitiring/
├── panel/             # Веб-панель (FastAPI + React + PostgreSQL 16)
│   ├── DOCUMENTATION.md
│   ├── backend/
│   ├── frontend/
│   ├── nginx/
│   └── docker-compose.yml
├── node/              # API-агент на каждой ноде (FastAPI + SQLite)
│   ├── DOCUMENTATION.md
│   ├── app/
│   ├── nginx/
│   └── docker-compose.yml
├── configs/           # Версионированные конфиги оптимизаций
├── scripts/           # Вспомогательные скрипты CLI
├── .github/workflows/ # CI/CD — сборка и публикация Docker-образов
├── install.sh         # Главный установщик (~2800+ строк)
└── CLAUDE.md          # Правила разработки
```

## Архитектура

```
Browser → nginx (SSL :443) → panel frontend (React 18)
                           → panel backend (FastAPI, Python 3.11)
                                   ↓
                              PostgreSQL 16
                                   ↓ proxy /api/proxy/{server_id}/...
                                   ↓ mTLS (клиентский сертификат панели + CA-валидация)
                              node nginx (SSL :9100, ssl_verify_client on)
                                   ↓
                              node API (FastAPI, Python 3.12)
                                   ↓
                              SQLite + host system (psutil, iptables, HAProxy)
```

Панель опрашивает ноды через proxy-роутер каждые 10 секунд. Ноды хранят данные трафика локально в SQLite (WAL mode), панель агрегирует историю метрик в PostgreSQL.

Канал панель↔нода защищён **mTLS (Remnawave-style)**. CA генерируется на панели при первом старте. Все новые ноды получают единый **shared node cert** (CN=`shared-node`), подписанный этим CA, — один общий сертификат на все ноды вместо уникального per-server. Панель ходит к нодам через `httpx.AsyncClient` с реальной валидацией CA — MITM невозможен. Nginx на ноде требует предъявить валидный клиентский сертификат (`ssl_verify_client on`). Старые ноды с per-server сертификатом мигрируются автоматически; ноды с `X-API-Key` (legacy) требуют ручной переустановки.

**Транспортные оптимизации (production-grade):** между панелью и нодами включён **HTTP/2** (`httpx[http2]`), разделены таймауты (connect/read/write/pool), пул увеличен до 200/50 keepalive-соединений с `keepalive_expiry=120s`. Nginx на ноде: `ssl_session_cache 64m`, `ssl_session_tickets on`, upstream keepalive 64 с `keepalive_requests 1000` и `keepalive_timeout 75s`, `proxy_connect_timeout 5s`. Uvicorn на ноде и панели запускается с `--loop uvloop --http httptools` (ускорение event loop в 2–4 раза) и `--timeout-keep-alive 75`. Нода дополнительно отдаёт **GZip** (минимум 1 KB, уровень 5) через `GZipMiddleware`.

**Оптимизации канала panel↔node (v9.1.1):**

- **Timing-логи в proxy_request** — `proxy.py` измеряет `elapsed_ms` через `time.perf_counter()` для каждого запроса к ноде; для 5xx — WARNING, для успешных — DEBUG (не спамит при 100 нодах).
- **HTTP_CONCURRENCY 50** — параллельных HTTP-запросов к нодам увеличено с 20 до 50; пул httpx (200/50) это поддерживает.
- **Сниженные таймауты `_NODE_TIMEOUT`** — `connect=2s, read=5s, write=2s, pool=2s` (было `connect=5s, read=10s, write=5s, pool=5s`); тяжёлые запросы (haproxy, traffic, certs) используют явный override в proxy_request (15/120/300 сек) без изменений.
- **Batch UPDATE серверов** — `metrics_collector._collect_all_servers` использует `executemany` вместо цикла `execute`: один вызов для успешных (4 поля), один для ошибочных (2 поля); ошибка не затирает `last_metrics` / `last_seen`.
- **Circuit breaker для сбоящих нод** — после `CB_FAILURE_THRESHOLD=3` подряд неудач нода пропускается `CB_SKIP_CYCLES=3` циклов (~30 сек), затем повторная попытка; счётчик сбрасывается при успехе. Поля: `_node_failures`, `_node_skip_cycles`.
- **`trust_env=False`** — mTLS и legacy httpx-клиенты к нодам игнорируют `HTTP_PROXY`/`HTTPS_PROXY` из окружения.
- **`proxy_buffering off` для `/api/metrics` (нода)** — nginx ноды имеет отдельный `location = /api/metrics` перед общим `location /`; буферизация добавляла 5–10 мс на real-time JSON.
- **`proxy_read_timeout 120s` для `/api/ssh/` (нода)** — отдельный `location /api/ssh/` в `node/nginx/nginx.conf` с увеличенным таймаутом чтения (было 30 с из общего `location /`). Предотвращает 504 при установке fail2ban через apt-get.
- **OCSP stapling + resolver (нода)** — `ssl_stapling on`, `ssl_stapling_verify on`, `resolver 1.1.1.1 8.8.8.8 valid=300s ipv6=off`, `resolver_timeout 3s` добавлены в http-блок nginx ноды.
- **`ssl_session_timeout 12h`** — на ноде и на внешнем nginx панели (было `1d`); более безопасный scope session tickets.
- **Uvicorn `--no-server-header`** — убран Server-заголовок из каждого ответа (нода и панель); `--backlog 2048` на ноде (было 512).

**Оптимизации канала panel↔user (v9.1.1):**

- **`GZipMiddleware minimum_size 1024`** — сжатие не применяется для ответов <1 KB (было 500 байт).
- **`proxy_buffering off` для real-time API** — в `panel/nginx/nginx.conf.template` добавлен отдельный location `~ ^/api/proxy/\d+/(metrics|haproxy/status)$` с `proxy_buffering off` и укороченными таймаутами (5s connect / 15s read/send); размещён перед общим `/api/`.
- **`keepalive_requests 5000`** — в upstream `backend_upstream` фронтенда (было 1000); реже переустанавливает соединения при постоянном polling.
- **Preload шрифтов** — `index.html` содержит `<link rel="dns-prefetch">` к fonts.gstatic.com и `<link rel="preload" as="style">` к Google Fonts CSS; сокращает задержку первой отрисовки.
- **`modulePreload.polyfill: false` (Vite)** — целевые браузеры (es2020) поддерживают modulepreload нативно; убирает ~2 KB inline-кода из каждого чанка.
- **Чанк `dnd-vendor`** — @dnd-kit/core/sortable/utilities вынесены в отдельный чанк; загружаются только на страницах с drag-n-drop.
- **Adaptive axios timeout** — request-interceptor выбирает timeout по URL: `speedtest/backup` → 300 с, `system/update / wildcard-ssl` → 180 с, `metrics/haproxy/status/auth/check` → 15 с, остальное → 30 с.
- **Улучшенный ключ GET-дедупликации** — ключ включает нормализованные params (`JSON.stringify(params ?? {})`) и заголовок Accept; раньше `undefined` и `{}` давали разные ключи.

Образы публикуются в GHCR:
- `ghcr.io/joliz1337/monitoring-panel-frontend:latest`
- `ghcr.io/joliz1337/monitoring-panel-backend:latest`
- `ghcr.io/joliz1337/monitoring-node-api:latest`

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
```

После установки доступна команда `mon` — интерактивный менеджер.

### Добавление ноды (v9.1.0)

1. В UI панели → **Servers** → **Add Server** — вверху формы отображается **Installer Token** (общий для всех нод).
2. Скопировать Installer Token.
3. Ввести имя и URL ноды → **Создать**.
4. На сервере запустить установщик — вставить Installer Token при запросе (или `NODE_SECRET=... bash deploy.sh`).
5. Установщик распакует `ca.pem` / `cert.pem` / `key.pem` в `nginx/ssl/` и нода стартует с mTLS.

Один и тот же токен используется для любого количества нод — он не меняется.

Если в панели есть ноды на старом per-server сертификате — автоматически появляется баннер **«Мигрировать все»**. Per-server ноды мигрируются без переустановки; legacy (X-API-Key) требуют ручной переустановки с тем же токеном.

## Установщик (install.sh / mon)

**Главное меню:**

| Пункт | Действие |
|-------|----------|
| 1 | Установить Panel → `/opt/monitoring-panel` |
| 2 | Установить Node → `/opt/monitoring-node` |
| 3/4 | Обновить Panel / Node |
| 5/6 | Удалить Panel / Node |
| 7 | Системные оптимизации (профиль + режим NIC) |
| 8 | Настроить прокси (apt/docker через `/etc/monitoring/proxy.conf`) |
| 9 | Установить Remnawave ноду (порт 2222, `cap_add: NET_ADMIN`) |
| w | Установить Cloudflare WARP (SOCKS5 на порту 9091) |
| s | Проверка скорости (подменю: Ookla / iperf3) |
| l | Смена языка |
| 0 | Выход |

**Константы:** `PANEL_DIR=/opt/monitoring-panel`, `NODE_DIR=/opt/monitoring-node`, `REMNAWAVE_DIR=/opt/remnawave`, `BIN_PATH=/usr/local/bin/mon`, `WARP_PORT=9091`, `REPO_URL=https://github.com/Joliz1337/monitoring.git`. Таймауты: git_clone=180 сек, apt=300 сек, curl=60 сек. Lockfile `/tmp/monitoring-installer.lock`, `MAX_RETRIES=3`.

После успешного старта контейнеров Remnawave нода (`install_remnawave()`) автоматически определяет внешний IP сервера и выводит его в лог. Порядок определения: `curl ifconfig.me` → `curl icanhazip.com` → `curl ident.me` → fallback через `ip route get 1`. Ключи локализации: `MSG_EN[remnawave_server_ip]`, `MSG_RU[remnawave_server_ip]`.

### Speed Test (пункт `s`)

Точка входа — `run_speed_test_menu()`: рисует бокс-заголовок, предлагает два инструмента.

**1) Ookla Speedtest (`run_speedtest_ookla()`)**

Использует исключительно snap-версию Ookla Speedtest CLI.

- `ensure_snapd()` — устанавливает `snapd` через apt если отсутствует; ждёт `snap wait system seed.loaded`.
- `ensure_speedtest_snap()` — удаляет конфликтующий python-пакет `speedtest-cli` если установлен, затем выполняет `snap install speedtest`.
- Первый запуск автоматически принимает лицензию и GDPR (`--accept-license --accept-gdpr`).

**2) iperf3 Russian servers (`run_speedtest_iperf()`)**

Адаптированный скрипт на базе [itdoginfo/russian-iperf3-servers](https://github.com/itdoginfo/russian-iperf3-servers).

- Доступно 5 городов: Moscow, Saint Petersburg, Nizhny Novgorod, Chelyabinsk, Tyumen. Каждый город имеет primary + fallback сервер.
- Пользователь выбирает количество городов от 1 до 10 (дефолт 5); запрос сверх доступных обрезается (`count_clamped`).
- `ensure_iperf_deps()` — автоустановка зависимостей: `iperf3`, `jq`, `bc`, `iputils-ping`.
- Параметры теста: перебор портов 5201–5209, 8 параллельных потоков (`-P 8`), 10 секунд (`-t 10`).
- Внутренние хелперы с префиксом `_iperf_*`: `find_port`, `test_server`, `parse_speed`, `get_ping`, `process_result`, `test_city`, `print_results`, `start_spinner`, `stop_spinner`, `log_debug`, `cleanup_trap`.

**`scripts/install-mon-cli.sh`** — создаёт `/usr/local/bin/mon` как обёртку, скачивающую свежий `install.sh` с GitHub при каждом вызове (fallback на локальные копии в `/opt/monitoring-*`).

## Системные оптимизации

Применяются через `mon` → пункт **7** или через панель (раздел «Оптимизации»). Не применяются автоматически.

### Профили

| Профиль | Назначение | Ключевые параметры |
|---------|-----------|-------------------|
| **vpn** | VPN/прокси с высокой нагрузкой по трафику | file-max 2M, conntrack 2M, socket buffers 128MB, nproc 1M |
| **panel** | Панели, мониторинг, лёгкая нагрузка | file-max 524k, conntrack 262k, socket buffers 16MB, nofile 65k |

После применения создаётся маркер `/opt/monitoring/configs/OPT_PROFILE` с именем активного профиля.

### Режимы NIC

| Режим | Действие |
|-------|----------|
| **Multi-queue (только аппаратный)** | Устанавливает `multiqueue-tune.sh` и `multiqueue-tune.service`; удаляет hybrid и RPS-конфигурацию |
| **Hybrid (HW multi-queue + RPS)** | Устанавливает `hybrid-tune.sh` и `hybrid-tune.service`; IRQ affinity на первые N HW-queue ядер, RPS на оставшиеся ядра через маску; если ядер ≤ HW-очередей — RPS пропускается |
| **Обычная NIC (только программный RPS)** | Устанавливает `network-tune.sh` и `network-tune.service`; удаляет hybrid и multiqueue-конфигурацию |

Hybrid-режим предназначен для серверов, где HW-очередей NIC меньше, чем CPU-ядер (например, 4 HW-queue на 12-ядерном CPU): аппаратные прерывания фиксируются на первых N ядрах, а softirq-обработка распределяется на оставшиеся через RPS-маску (64+ ядра поддерживаются корректно через awk).

NIC-скрипты динамически вычисляют параметры (hashsize и др.) на основе `conntrack_max` из уже применённого профиля.

### Структура configs/

```
configs/
├── vpn/                    # Профиль для VPN/прокси серверов
│   ├── sysctl.conf         # 2M conntrack, 128MB буферы, timeout_established 1800s
│   ├── limits.conf         # 2M nofile, 1M nproc
│   └── systemd-limits.conf
├── panel/                  # Профиль для панелей и мониторинга
│   ├── sysctl.conf         # 262k conntrack, 16MB буферы, timeout 7200s
│   ├── limits.conf         # 512k nofile
│   └── systemd-limits.conf
├── network-tune.sh         # Программный RPS/RFS/XPS (общий)
├── network-tune.service
├── multiqueue-tune.sh      # Аппаратный multiqueue + IRQ affinity (общий)
├── multiqueue-tune.service
├── hybrid-tune.sh          # Hybrid: IRQ affinity на HW-queue ядра + RPS на оставшиеся
├── hybrid-tune.service     # systemd unit (oneshot, RemainAfterExit=yes, после network-online.target)
└── VERSION                 # 4.5.0
```

### Ключевые функции install.sh

- `apply_system_optimizations()` — подменю выбора профиля (`vpn`/`panel`) → подменю NIC; копирует конфиги из поддиректории профиля; записывает маркер `OPT_PROFILE`
- `check_optimizations_status()` — читает `OPT_PROFILE` и отображает профиль в строке статуса главного меню
- `detect_multiqueue_support()` — перебирает реальные сетевые интерфейсы (фильтрует виртуальные, bridge, bond и DOWN-интерфейсы по `operstate=up`), через `ethtool -l` получает количество combined channels; в меню попадают только линки, которые фактически несут трафик
- `configure_dns()` — автонастройка DNS (1.1.1.1 + 8.8.8.8): systemd-resolved drop-in, netplan yaml с бэкапом и откатом, `/etc/resolv.conf`, dhclient prepend
- `remove_rps()` / `remove_multiqueue()` / `remove_hybrid()` — останавливают и удаляют соответствующие service/скрипты
- `install_nic_tune()` / `enable_tune_service()` — универсальная установка и активация; `install_nic_tune()` переписана на `case` (три значения: `multiqueue`, `hybrid`, `rps`); при применении всегда вызываются все три `remove_*`, затем устанавливается выбранный режим (идемпотентно)

### Страница оптимизаций в панели

Маршрут `/system-optimizations`. Карточки по каждой ноде: версия, бейджи профиля (VPN/Panel) и режима NIC (Multiqueue HW / Hybrid / RPS SW / None). Двухколоночный макет: VPN-ноды слева, Panel-ноды справа; неназначенные ноды сверху. Кнопка «Применить ко всем» для пакетного обновления.

Dropdown выбора режима NIC содержит три кнопки: **Hybrid** (teal-бейдж) → **Multiqueue** → **RPS**. Hybrid и Multiqueue отображаются только при `multiqueue_supported === true`. Если `hybrid_recommended === true` — рядом с Hybrid выводится зелёная метка «Рекомендуется» (`t('sys_opt.recommended')`).

**Адаптивное позиционирование dropdown:** все три выпадающих меню (два для выбора режима NIC + подтверждение удаления) определяют доступное пространство через `useLayoutEffect` + `getBoundingClientRect()` и автоматически открываются вверх (`bottom-full`) или вниз (`top-full`) в зависимости от позиции кнопки в viewport. Порог переключения: 280 px от верхнего края экрана до кнопки-триггера.

**Бейдж доступного режима NIC:** на карточках серверов (установленных и неназначенных) отображается дополнительный информационный бейдж:
- Если `hybrid_recommended === true` и текущий режим не `hybrid` — зелёный бейдж «рекомендуется: Hybrid».
- Если `multiqueue_supported === true`, но текущий режим `rps` или `none` — фиолетовый бейдж «MQ доступен».
- При наведении на бейдж отображается Tooltip с подробным объяснением (`t('sys_opt.mq_available_hint')`).

**Node API (`node/app/routers/system.py`):**

| Метод | Путь | Описание |
|-------|------|----------|
| `GET`  | `/api/system/versions`             | Версии компонентов + активный режим NIC и профиль |
| `GET`  | `/api/system/nic-info`             | Текущий режим NIC, аппаратные возможности, детали интерфейсов; включает поле `hybrid_recommended: bool`; в список `interfaces` и в вычисление `multiqueue_supported`/`hybrid_recommended` попадают только интерфейсы с `operstate=up` |
| `POST` | `/api/system/optimizations/apply`  | Применить; `nic_mode: Literal["rps","multiqueue","hybrid"]` (Pydantic вернёт 422 на неизвестный режим); записывает OPT_PROFILE |
| `POST` | `/api/system/optimizations/remove` | Удалить все конфиги оптимизаций с ноды (rps, multiqueue, hybrid) |

`hybrid_recommended` вычисляется как `multiqueue_supported AND max(max_combined) < nproc` — true, если у NIC меньше HW-очередей, чем ядер CPU. Порядок детектирования активного режима: hybrid → multiqueue → rps → none.

**Panel proxy API (`panel/backend/app/routers/proxy.py`):**

| Метод | Путь | Описание |
|-------|------|----------|
| `GET`  | `/{server_id}/system/nic-info`            | Прокси → node `GET /api/system/nic-info` |
| `POST` | `/{server_id}/system/optimizations/apply` | Прокси → node apply; загружает конфиги профиля с GitHub; для hybrid дополнительно тянет `hybrid-tune.sh` и `hybrid-tune.service` |
| `POST` | `/{server_id}/system/optimizations/remove`| Прокси → node remove |

`get_optimizations_from_github(profile)` в `system.py` загружает конфиги из `configs/{profile}/` репозитория на GitHub и hybrid-скрипты (`GITHUB_HYBRID_TUNE_URL`, `GITHUB_HYBRID_TUNE_SERVICE_URL`) через `asyncio.gather`. При `nic_mode == "hybrid"` и отсутствии скриптов proxy возвращает 502 с явным сообщением.

## Cloudflare WARP

При выборе `w` запускается `install_warp()`:

1. Добавляет репозиторий Cloudflare, устанавливает пакет `cloudflare-warp`.
2. Вызывает `fix_warp_network()` — фикс /32 адресации до регистрации.
3. `warp-cli --accept-tos registration delete` (сброс) → `registration new`.
4. Настраивает режим `proxy`, порт **9091**. Все вызовы `warp-cli` используют `--accept-tos` (обязателен без TTY).
5. `warp-cli connect`, ожидает статуса `Connected` (15 попыток × 2 сек).
6. Создаёт `/usr/local/bin/warp-fix-network.sh` и `warp-auto.service` с `ExecStartPre` на fix-скрипт.
7. Проверяет через `curl --socks5 127.0.0.1:9091`.
8. Выводит готовый фрагмент xray outbound.

При повторном вызове запрашивает подтверждение переустановки.

#### fix_warp_network()

Автодетект VPS с /32 адресацией (характерно для Aeza). Логика:

1. Определяет дефолтный интерфейс через `ip route show default`.
2. Парсит префикс маски через `awk` (из `ip -4 addr show dev`).
3. Если префикс равен `32` или IPv4 отсутствует — добавляет `172.30.255.1/24` на интерфейс и перезапускает `warp-svc`.

Персистентность: `/usr/local/bin/warp-fix-network.sh` вызывается из `ExecStartPre` в `warp-auto.service` при каждой перезагрузке.

## Panel — веб-панель

Подробная документация: [panel/DOCUMENTATION.md](panel/DOCUMENTATION.md)

### Стек

- **Backend**: FastAPI 0.109, Python 3.11, SQLAlchemy 2.0 + asyncpg, PostgreSQL 16
- **Frontend**: React 18.2, TypeScript 5.3 strict, Vite 5, TailwindCSS 3.4
- **Зависимости**: httpx[socks,http2], pyjwt 2.8, aiogram ≥3.7, docker ≥7.1, cryptography ≥42, certbot + certbot-dns-cloudflare, psutil, Xray-core v26.2.6 (встроен в Dockerfile), Ookla speedtest CLI v1.2.0

### Backend — сервис алертов (server_alerter)

`panel/backend/app/services/server_alerter.py` — фоновый сервис, проверяющий метрики нод и отправляющий Telegram-уведомления при отклонениях.

**Шумовой гейт (noise gate):** каждая метрика имеет настраиваемый порог минимальной активности (`min_value`). Если текущее значение метрики ниже этого порога — ни spike, ни drop не считаются инцидентом: оба типа очищаются. Это предотвращает ложные срабатывания при естественных простоях (ночная смена нагрузки, отсутствие трафика).

Ключевое условие: `if current_val < min_value` — гейт срабатывает только по текущему значению, не по базовому EMA. Ранее условие было `ema_val < min_value AND current_val < min_value`, из-за чего при высоком EMA и упавшем текущем значении гейт не срабатывал и приходил алерт «падение».

**Пороги по умолчанию (models.py, таблица `alert_settings`):**

| Поле | Старый дефолт | Новый дефолт | Назначение |
|------|--------------|-------------|-----------|
| `network_min_bytes` | 102400.0 (100 KB/s) | 1048576.0 (1 MB/s) | Минимальный сетевой трафик для учёта отклонений |
| `tcp_min_connections` | 10 | 100 | Минимальное число TCP-соединений для учёта отклонений |

При старте `run_migrations()` в `database.py` автоматически обновляет значения существующих установок, если они точно равны старым дефолтам (100 KB/s → 1 MB/s, 10 → 100). Пользовательские значения не трогаются.

**Ключевые методы:**
- `_check_deviation_both(metric, current_val, ema_val, min_value, spike_type, drop_type)` — проверяет оба направления (spike и drop).
- `_check_deviation_spike(metric, current_val, ema_val, min_value, spike_type)` — проверяет только spike.

### Backend — роутеры (panel/backend/app/routers/)

| Файл | Префикс | Описание |
|------|---------|----------|
| `auth_router.py` | `/auth/*` | login, logout, check, validate-uid, clear-ban, ban-status |
| `servers.py` | `/servers/*` | CRUD серверов, папки, reorder, test, installer-token, migration-status, migrate, migrate-all |
| `proxy.py` | `/proxy/{server_id}/*` | Прокси к нодам: metrics, haproxy, firewall, traffic, ipset, system, speedtest (~1328 строк) |
| `system.py` | `/system/*` | version, update, certificate, optimizations, panel-ip, stats |
| `remnawave.py` | `/remnawave/*` | settings, stats, users, devices, anomalies, ignore-lists (~867 строк) |
| `alerts.py` | `/alerts/*` | settings, history, test-telegram |
| `billing.py` | `/billing/*` | серверы, monthly/resource/yandex_cloud модели |
| `blocklist.py` | `/blocklist/*` | global, per-server, sources |
| `bulk_actions.py` | `/bulk/*` | haproxy, firewall, traffic, terminal |
| `haproxy_profiles.py` | `/haproxy/*` | профили конфигов, синхронизация, rules, server-cores |
| `xray_monitor.py` | `/xray-monitor/*` | мониторинг Xray подключений через подписки |
| `wildcard_ssl.py` | `/wildcard-ssl/*` | Certbot + Cloudflare DNS |
| `ssh_security.py` | `/ssh-security/*` | sshd, fail2ban, ключи, bulk-операции; bulk-эндпоинты логируют per-server результаты (DEBUG/WARNING) и итоговый `ssh_bulk_summary` (INFO/WARNING); `/api/ssh/fail2ban/config` и `/api/ssh/password` используют таймаут 120 с (apt-get install) |
| `torrent_blocker.py` | `/torrent-blocker/*` | settings, stats, reports, poll-now |
| `infra.py` | `/infra/*` | иерархия accounts → projects → servers |
| `notes.py` | `/notes/*` | shared_notes, shared_tasks, WebSocket stream |
| `backup.py` | `/backup/*` | create, list, restore, delete; после restore перезагружает PKI и HTTP-клиенты |
| `settings.py` | `/settings/*` | глобальные настройки панели |

### Backend — фоновые сервисы (panel/backend/app/services/)

`metrics_collector` (circuit breaker, batch UPDATE, HTTP_CONCURRENCY=50), `server_alerter`, `xray_stats_collector`, `xray_monitor`, `remnawave_api`, `blocklist_manager`, `telegram_bot`, `billing_checker`, `haproxy_config`, `haproxy_profile_sync`, `speedtest_scheduler`, `time_sync`, `wildcard_ssl`, `torrent_blocker`, `xray_key_parser`, `asn_lookup`, `geo_resolver`, `http_client`, `ssh_manager`, `notes_broadcaster`, `yandex_billing`, `yc_token_manager`, **`pki`**, **`migration`** (classify_server, push_shared_cert_to_node)

Запускаются в lifespan: `init_db` → `load_or_create_keygen` → `init_http_clients` → `cleanup_expired_bans` → `telegram_bot` → `metrics_collector` → `blocklist_manager` → `xray_stats_collector` → `server_alerter` → `billing_checker` → `xray_monitor` → `speedtest_scheduler` → `time_sync` → `wildcard_ssl_manager` → `torrent_blocker`.

**Восстановление бэкапа и PKI:** после успешного `pg_restore` роутер `backup.py` вызывает `_reload_pki(app)` — функция закрывает текущие HTTP-клиенты, перечитывает PKI из восстановлённой БД через `load_or_create_keygen`, пересоздаёт клиенты через `init_http_clients` и обновляет `app.state.pki`. Без этого шага при переносе панели на другой сервер через бэкап ноды возвращали `SSL: CERTIFICATE_VERIFY_FAILED` — бэкенд продолжал использовать CA, сгенерированный при первом старте, вместо восстановлённого.

### Backend — БД (models.py, ~31 таблица)

`servers`, `server_cache`, `metrics_snapshots`, `aggregated_metrics`, `failed_logins`, `panel_settings`, `blocklist_rules`, `blocklist_sources`, `remnawave_settings`, `remnawave_hwid_devices`, `remnawave_user_cache`, `xray_stats`, `torrent_blocker_settings`, `alert_settings`, `alert_history`, `billing_servers`, `billing_settings`, `xray_monitor_settings`, `xray_monitor_subscriptions`, `xray_monitor_servers`, `xray_monitor_checks`, `infra_accounts`, `infra_projects`, `infra_project_servers`, `shared_notes`, `shared_tasks`, `haproxy_config_profiles`, `haproxy_sync_log`, `wildcard_certificates`, `asn_cache`, **`keygen`** (singleton PKI).

**Поля `servers`**: `pki_enabled` (bool), `uses_shared_cert` (bool, default false), `api_key` (nullable, legacy). Поля `node_cert_pem`, `node_key_pem`, `node_cert_fingerprint`, `node_cert_issued_at`, `node_cert_expires_at` удалены в v9.1.0.

**Поля `keygen`**: `ca_cert_pem`, `ca_key_pem`, `client_cert_pem`, `client_key_pem`, `shared_node_cert_pem`, `shared_node_key_pem` (добавлены в v9.1.0).

Миграции: Alembic не используется. Функция `run_migrations()` в `database.py` при старте добавляет недостающие колонки через `ALTER TABLE`.

### Backend — конфиг (.env)

| Параметр | Описание |
|----------|----------|
| `PANEL_UID` | Секретный путь `domain.com/{uid}` |
| `PANEL_PASSWORD` | Пароль для входа |
| `JWT_SECRET` | Секрет для JWT |
| `JWT_ALGORITHM` | Алгоритм JWT |
| `JWT_EXPIRE_MINUTES` | Время жизни токена (default 1440) |
| `MAX_FAILED_ATTEMPTS` | Попыток до бана (default 5) |
| `BAN_DURATION_SECONDS` | Время бана (default 900) |
| `POSTGRES_*` | Параметры PostgreSQL |
| `DOMAIN` | Домен панели |
| `IPERF_SERVER_DISABLED` | Отключить iperf3 сервер |
| `IPERF_PORT` | Порт iperf3 |

### Backend — безопасность

- JWT в HttpOnly Secure SameSite=strict cookie
- Timing-safe сравнение пароля (`secrets.compare_digest`)
- Двухуровневая anti-brute: память + БД (`failed_logins`)
- `SecurityMiddleware` дропает соединения (444) при банах
- Валидация UID через `/auth/validate-uid` (timing-safe)
- **SSRF-фильтр** в `_clean_url` при добавлении сервера: блокируются loopback, link-local, multicast, unspecified адреса через `ipaddress`
- **X-Forwarded-For/X-Real-IP** доверяются только от `127.0.0.1`/`::1` (защита от header spoofing)

### Frontend — стек

React 18.2, TypeScript 5.3 strict, Vite 5, TailwindCSS 3.4, Framer Motion 11, Zustand 4.4, Axios 1.6 (с дедупликацией GET), ApexCharts 4, React Router DOM 6.21, i18next 23.7, @dnd-kit (core+sortable+utilities), Lucide React, Sonner (toasts).

### Frontend — страницы (все под `/:uid/`)

Login, Dashboard (drag-n-drop папки), Servers, ServerDetails, HAProxy, HAProxyConfigs, Traffic, Alerts, Billing, Blocklist, TorrentBlocker, SSHSecurity, Remnawave, WildcardSSL, SystemOptimizations, Updates, BulkActions, Settings.

**Страница Alerts (`panel/frontend/src/pages/Alerts.tsx`):** настройка параметров алертов для каждой ноды. Слайдер `tcp_min_connections` — диапазон `0–1000 step 10` (позволяет задавать адекватные минимумы для высоконагруженных TCP-метрик).

**Страница Servers (`panel/frontend/src/pages/Servers.tsx`):**
- Цветная точка и статус-бейдж отражают реальный статус сервера из поля `server.status` (`online`/`offline`/`loading`/`error`). Поле `server.is_active` отвечает только за включение/выключение мониторинга и на цвет индикатора не влияет.
- Поиск серверов — строка появляется при наличии хотя бы одного сервера, фильтрует по имени и адресу подключения (URL). Ключ i18n: `servers.search_placeholder`.
- Анимации Framer Motion ускорены: `duration` 0.15, убраны `whileHover` scale/rotate, убраны stagger-задержки карточек, начальное смещение `y` уменьшено с 20 до 8.

### Frontend — ServerDetails: адаптивная карточка CPU

`MetricCard` принимает проп `className` для управления позицией в grid. Если ядер больше 8, карточка CPU получает `col-span-2` — занимает половину ширины страницы вместо четверти, чтобы бары ядер не сжимались в нечитаемую полосу.

`CpuCoresChart` (`panel/frontend/src/components/Charts/CpuCoresChart.tsx`):
- Грид ограничен **максимум 8 колонками** (`coresPerRow = Math.min(8, cores.length)`) независимо от числа ядер — устраняет растягивание в одну строку на 32+ ядрах.
- Адаптивная высота баров: `h-8` (≤16 ядер) / `h-6` (17–48) / `h-5` (48+).
- Адаптивный размер шрифта: `12px` (≤16) / `10px` (17–32) / `9px` (32+).

### Frontend — API-клиент (api/client.ts, ~1772 строк)

16 групп методов: `authApi`, `serversApi`, `proxyApi`, `settingsApi`, `blocklistApi`, `bulkApi`, `systemApi`, `remnawaveApi`, `alertsApi`, `billingApi`, `backupApi`, `sshSecurityApi`, `infraApi`, `notesApi`, `wildcardSSLApi`, `haproxyProfilesApi`, `torrentBlockerApi`. ~90+ TypeScript интерфейсов. Дедупликация GET-запросов, интерцептор 401.

### Frontend — stores

`authStore`, `serversStore`, `infraStore`, `settingsStore`, `notesStore`, `faqStore`, `torrentBlockerStore`.

### Frontend — SSHSecurity: bulk UX

`panel/frontend/src/pages/SSHSecurity.tsx` — страница управления SSH-безопасностью на серверах.

**Bulk-применение SSH-пресетов:**

- `showBulkToast(ok, total)` — умные toast-уведомления: зелёный при полном успехе всех серверов, жёлтый при частичном, красный при полном провале. Функция вызывается из `handleBulkApply`, `handleBulkPreset`, `handleChangePasswordAll`.
- **Summary-бейдж «N из M»** в блоке Bulk Results — цвет бейджа (зелёный / жёлтый / красный) определяется долей успешных серверов.
- **Прогресс-бар** под summary-бейджем — полоска (зелёная / оранжевая / красная) визуально отображает процент успешно обработанных нод.

### Frontend — i18n

2 языка EN/RU (~1410 строк JSON), LanguageDetector (localStorage → navigator). FAQ — 44 markdown-файла на русском в `src/data/faq/content/ru/`, динамическая загрузка через `import.meta.glob`.

Ключи SSH Security bulk-уведомлений: `bulk_all_ok`, `bulk_partial`, `bulk_all_failed`, `bulk_summary`, `bulk_summary_ok`.

### Frontend — сборка

Build: `prebuild.js` (предсборочные шаги) → `tsc` → `vite build` → `dist/`. Multi-stage Dockerfile: `node:20-alpine` (build) → `nginx:alpine` (runtime). Manual chunks: `react-vendor`, `chart-vendor`, `ui-vendor`, `i18n-vendor`, `dnd-vendor` (@dnd-kit — загружается только на страницах с drag-n-drop). `modulePreload.polyfill: false` — полифил не нужен (все целевые браузеры es2020).

### Nginx (panel)

SPA fallback, прокси `/api` → `backend:8000`, SSE streaming без буферизации (`proxy_buffering off`, `read_timeout 620s`) для `/api/proxy/:id/system/execute-stream`.

Внешний TLS-терминирующий nginx (`panel/nginx/nginx.conf.template`): `ssl_session_cache 64m`, `ssl_session_tickets on`, `ssl_session_timeout 12h`, `ssl_stapling on` с резолверами 1.1.1.1/8.8.8.8, HSTS max-age=63072000 с `preload`, `proxy_connect_timeout 5s` для `/api/`. Отдельный location `~ ^/api/proxy/\d+/(metrics|haproxy/status)$` — `proxy_buffering off`, `proxy_connect_timeout 5s`, `proxy_send_timeout 15s`, `proxy_read_timeout 15s` (real-time данные без буферизации).

Внутренний nginx фронтенда (`panel/frontend/nginx.conf`): upstream `backend_upstream` с keepalive 32 / `keepalive_requests 5000` / `keepalive_timeout 75s`; gzip (уровень 6, от 512 байт, расширенный список типов включая wasm/fonts/svg); кэш `/assets/` — `expires 1y, immutable`; `/index.html` — `no-store, must-revalidate`.

### Frontend — сборка (Vite)

`panel/frontend/vite.config.ts`: `target: es2020`, `minify: esbuild`, `cssCodeSplit`, `cssMinify`, `assetsInlineLimit: 8192`, `chunkSizeWarningLimit: 800`, `reportCompressedSize: false`. В production — esbuild `legalComments: none` и `drop: ['console', 'debugger']`.

### Axios-клиент

`panel/frontend/src/api/client.ts`: adaptive timeout по URL (300/180/15/30 с); retry-интерцептор — до 2 повторов GET-запросов при 502/503/504 и сетевых ошибках с exponential backoff 300 мс × 2^attempt. Ключ GET-дедупликации включает нормализованные params и заголовок Accept.

**SSE (терминал):** `fetch().body.getReader()`, ручной парсинг SSE, события `stdout/stderr/done/error`. WebSocket — только для `/notes/stream`.

## Node — агент мониторинга

Подробная документация: [node/DOCUMENTATION.md](node/DOCUMENTATION.md)

**Версия**: 9.1.0

### Стек

FastAPI 0.109, Python 3.12, psutil 5.9.8, SQLite + aiosqlite (WAL mode). Docker: `network_mode: host`, `privileged: true`, `pid: host`. Nginx на порту 9100 с SSL proxy → `127.0.0.1:7500`. Uvicorn: `uvloop` + `httptools`, `--timeout-keep-alive 75`, `--limit-concurrency 200`. GZip ответов через `GZipMiddleware` (от 1 KB, уровень 5).

### Роутеры (node/app/routers/)

| Файл | Префикс | Описание |
|------|---------|----------|
| `metrics.py` | `/api/metrics` | CPU, RAM, диски, сеть, процессы (psutil, кэш 5 сек) |
| `traffic.py` | `/api/traffic` | Трафик по iptables, hourly/daily/monthly агрегация |
| `haproxy.py` | `/api/haproxy` | Управление HAProxy (native systemd) |
| `ipset.py` | `/api/ipset` | 4 ipset-списка, bulk-операции с per-request timeout |
| `ssl.py` | `/api/ssl` | Деплой PEM, backup, валидация, reload |
| `ssh.py` | `/api/ssh` | sshd_config, fail2ban, ключи, смена пароля, разбан |
| `speedtest.py` | `/api/speedtest` | Запуск Ookla speedtest CLI |
| `system.py` | `/api/system` | Версии, NIC-info, применение/удаление оптимизаций |
| `remnawave.py` | `/api/remnawave` | Статус контейнера `remnanode` (Docker inspect) |

### Сервисы (node/app/services/)

`metrics_collector`, `traffic_collector`, `haproxy_manager`, `ipset_manager`, `firewall_manager`, `ssl_manager`, `ssh_config_manager`, `speedtest_runner`, `host_executor`.

**`ssh_config_manager`** — управление SSH-демоном на хосте из контейнера (через nsenter). Поддерживает Ubuntu, Debian, CentOS, RHEL, Rocky, AlmaLinux, Fedora. Интерфейс класса (все методы и сигнатуры) и формат ответов API не изменились.

**Инициализация и обнаружение окружения:**

- `_detect_os()` — читает `/etc/os-release`, определяет `distro`/`version`/`pkg_manager` (apt/dnf/yum).
- `_detect_ssh_service()` — определяет имя сервиса (`ssh` или `sshd`), наличие socket activation (`ssh.socket`/`sshd.socket`), версию OpenSSH.
- `_detect_fail2ban_backend()` — автоматически выбирает `systemd` или `auto` backend через наличие `journalctl`.

**Ключевые методы:**

- `write_sshd_config(config)` — записывает `sshd_config`; при смене порта логирует `SSH port change requested: {old} -> {new}`, обрабатывает socket activation, выполняет верификацию через `_verify_sshd_responding`, откатывается при неудаче. Перед записью вызывает `_ensure_privsep_dir()`. `_build_sshd_content()` **не раскомментирует закомментированные строки** — только обновляет уже активные директивы; закомментированный `# Port 2222` более не активируется.
- `test_sshd_config()` — валидация конфига через `sshd -t`; перед запуском вызывает `_ensure_privsep_dir()`.
- `change_password(user, password)` — проверяет существование пользователя через `id` перед chpasswd; после смены верифицирует обновление хеша через `getent shadow`.
- `write_fail2ban_config(config)` — **мержит** переданные параметры с текущими настройками; при частичном обновлении (например, только `max_retry`) остальные поля не сбрасываются к дефолтам.
- `get_status()` — возвращает `os_info` (distro, version, pkg_manager) и `ssh_service` (имя, socket, версия OpenSSH) для диагностики.
- `_ensure_privsep_dir()` — создаёт `/run/sshd` (mkdir -p, chmod 0755, chown root:root). Необходим потому что `sshd -t` проверяет не только синтаксис, но и наличие privsep-каталога.
- `_detect_socket_unit()` — определяет активный socket unit (`ssh.socket` или `sshd.socket`) через `is-active`/`is-enabled` (не `systemctl cat` — не работает для generated units на Ubuntu 22.04+); допустимые значения `is-enabled`: `enabled`, `static`, `generated`, `alias`.
- `_get_actual_listening_port()` — возвращает реально слушающий порт SSH через `ss`; используется в `port_changed` вместо сравнения с Port из `sshd_config`. Устраняет баг: если предыдущая неудачная попытка записала `Port 1794` в конфиг, а socket остался на 22 — код теперь корректно определяет, что смена нужна.
- `_write_socket_port_override(socket_unit, port)` — пишет drop-in `/etc/systemd/system/{socket}.d/listen-port.conf` с двумя строками `ListenStream=` (IPv4 и IPv6).
- `_try_socket_recovery()` — auto-recovery: если после рестарта порт не слушает и socket unit не определён — повторная попытка определить socket и применить override.
- `_verify_sshd_responding(port)` — 15 попыток × 0.5 с; проверяет не только что порт слушает, но что sshd реально отвечает SSH-хэндшейком (`ssh-keyscan`, фолбэк `nc`); при недоступности утилит — `/proc/net/tcp` + `/proc/net/tcp6`. При провале записывает диагностический лог.
- `_rollback()` — перезапускает **только** определённый при инициализации сервис (не перебирает все варианты).
- `_restart_sshd()` — использует определённое имя сервиса; ожидание fail2ban до 5 попыток × 1 с с диагностикой при неудаче.
- `_validate_public_key()` — валидирует ключ через `ssh-keygen -lf` (проверяет формат и base64), а не по списку строк-префиксов.
- `_install_fail2ban()` — использует определённый пакетный менеджер (apt/dnf/yum) вместо хардкода apt-get.
- Container detection поддерживает containerd и kubepods помимо docker.
- Порядок операций при смене порта (socket activation): `stop service` → `stop socket` → `daemon-reload` → `start socket` → `start service`; каждый шаг логируется (`socket_apply_start`, `socket_state_after_start`, `socket_listen_info`, `service_state_after_start`, `verify_port_all_listeners`).

**Примечание:** на ноде нет реализации Xray Log Collector и Torrent Blocker. Torrent Blocker реализован исключительно на панели: панель опрашивает Remnawave API и рассылает команды `bulk-add` на ноды через ipset API.

### Метрики

CPU (cores, freq, load avg, per-cpu, temp sensors), RAM (+swap), диски (partitions + IO stats), сеть (интерфейсы + TCP states), процессы (топ N), система (hostname, uptime). Сбор on-the-fly, кэш 5 сек для тяжёлых операций.

### Трафик

SQLite WAL, цепочки iptables `TRAFFIC_ACCOUNTING_IN/OUT`, сбор каждые 60 сек, агрегация в hourly/daily/monthly. Retention 90 дней. Кэш summary 120 сек. База: `/var/lib/monitoring/traffic.db`.

**LACP / bond-интерфейсы:** `traffic_collector._get_bond_slaves()` читает `/sys/class/net/*/bonding/slaves` через sysfs. Slave-интерфейсы исключаются из `_read_interface_bytes_sync()` — трафик пишется только для агрегированного bond0. Аналогично `metrics_collector._get_bond_slaves()` помечает slave-интерфейсы флагом `is_virtual: true` и исключает их из суммарной скорости в `/api/metrics/network`. Исправляет двойной (x2) подсчёт на серверах с LACP.

### HAProxy

Native systemd на хосте. Конфиг `/etc/haproxy/haproxy.cfg`, маркеры `# === RULES START/END ===`, поддержка resolver DNS. Модели `BackendServerModel`/`BalancerOptionsModel` (Pydantic). Wildcard SSL через `_extract_parent_domain`/`_resolve_cert_domain`. Управление через `systemctl reload/restart/start/stop`, валидация `haproxy -c -f`. Certbot (standalone/webroot), автообновление через cron.

### ipset

4 списка: `blocklist_permanent`, `blocklist_temp`, `blocklist_out_permanent`, `blocklist_out_temp`. Persistent JSON `/var/lib/monitoring/blocklist.json`. Операции: add, remove, bulk_add (с per-request timeout), bulk_remove, sync (atomic replace), clear, timeout-change. Работает через nsenter из контейнера.

**`BulkIpRequest`** — поле `timeout` (optional): переопределяет глобальный `_temp_timeout` для конкретного запроса. Это позволяет Torrent Blocker и Blocklist задавать разное время бана в одном ipset-списке.

### Auth (нода)

mTLS через nginx `ssl_verify_client on`. Панель предъявляет клиентский сертификат, подписанный CA. Проверка происходит на уровне TLS-рукопожатия — до попадания запроса в FastAPI. `X-API-Key` удалён. SecurityManager: 10 failed → 1 час бана. Connection drop (444) без HTTP-ответа.

### Docker (нода)

`python:3.12-slim` + certbot + openssl + iptables + iperf3 + Docker CLI. Тома: `/proc`, `/sys`, `/etc/haproxy`, `/etc/letsencrypt`, `/var/run/docker.sock`, `traffic_data`, `/opt/monitoring-node`. Healthcheck: `curl localhost:7500/health`.

## Torrent Blocker

Функция автоматической блокировки IP-адресов из отчётов Remnawave Torrent Blocker на всех нодах через ipset.

### Принцип работы

1. Фоновый воркер (`panel/backend/app/services/torrent_blocker.py`) с настраиваемым интервалом (по умолчанию 5 минут) запрашивает `GET /api/node-plugins/torrent-blocker` у Remnawave.
2. Из отчётов извлекаются уникальные IP (поле `actionReport.ip`).
3. На все активные ноды (кроме исключённых) отправляется `POST /api/ipset/bulk-add` с `permanent: false` и `timeout: ban_seconds`.
4. После успешной рассылки отчёты усекаются через `DELETE /api/node-plugins/torrent-blocker/truncate`.

**Значения по умолчанию:** отключено, интервал — 5 минут, длительность бана — 30 минут.

### Panel Backend API (`panel/backend/app/routers/torrent_blocker.py`)

| Метод | Путь | Описание |
|-------|------|----------|
| `GET`    | `/api/torrent-blocker/settings` | Получить настройки |
| `PUT`    | `/api/torrent-blocker/settings` | Обновить настройки |
| `GET`    | `/api/torrent-blocker/status`   | Статус воркера (last_poll, ips_count, error) |
| `POST`   | `/api/torrent-blocker/poll-now` | Запустить опрос немедленно |
| `GET`    | `/api/torrent-blocker/stats`    | Статистика от Remnawave |
| `GET`    | `/api/torrent-blocker/reports`  | Список отчётов от Remnawave |
| `DELETE` | `/api/torrent-blocker/truncate` | Усечь отчёты вручную |

### Модель `TorrentBlockerSettings`

Singleton-таблица: `enabled`, `poll_interval_minutes`, `ban_duration_minutes`, `excluded_server_ids`, `last_poll_at`, `last_poll_ips_count`, `last_error`.

## CI/CD

GitHub Actions (`.github/workflows/docker-publish.yml`) — сборка и публикация Docker-образов в GHCR при push в `main`.

Образы:
- `ghcr.io/joliz1337/monitoring-panel-frontend:latest`
- `ghcr.io/joliz1337/monitoring-panel-backend:latest`
- `ghcr.io/joliz1337/monitoring-node-api:latest`

Установка/обновление: `docker compose pull` → `docker compose up -d`. При недоступности GHCR — fallback на локальную сборку.

## Компоненты — ссылки

- [panel/DOCUMENTATION.md](panel/DOCUMENTATION.md) — панель: API, БД, конфигурация, безопасность
- [node/DOCUMENTATION.md](node/DOCUMENTATION.md) — нода: API, метрики, HAProxy, трафик, ipset
