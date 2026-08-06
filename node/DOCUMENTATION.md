# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — история по интерфейсам и портам (SQLite + iptables)
- **HAProxy** — управление нативным systemd сервисом, конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API
- **IPSet Blocklist** — блокировка IP/CIDR через ipset (постоянный и временный списки), отказ от приватных/служебных диапазонов, массовое применение одним `ipset restore`
- **Терминал** — выполнение произвольных команд и bash-скриптов на хосте (max 65000 символов)
- **Remnawave** — проверка доступности контейнера remnanode
- **Remnawave Nginx** — обнаружение установки Remnawave на хосте (`/opt/remnawave` по умолчанию), приём и атомарное применение nginx.conf от панели (backup → in-place запись → `nginx -t` → reload, откат при ошибке), автоподстановка host-специфичных лимитов (`worker_rlimit_nofile`/`worker_connections`/`ssl_session_cache`) под MemTotal/nofile самой ноды, проверка существования сертификатов на хосте с автомонтированием недостающих каталогов в контейнер, валидация конфига через живой или одноразовый контейнер, reload/restart сервиса
- **Синхронизация времени** — установка IANA timezone через `timedatectl`, включение NTP и принудительная синхронизация через `systemd-timesyncd`
- **SSH Security** — управление SSH-безопасностью сервера: настройки sshd, fail2ban, SSH-ключи
- **Wildcard SSL** — приём и деплой wildcard сертификатов от панели: запись файлов на хост, бэкап, откат при ошибке reload, валидация PEM через openssl
- **Firewall Profiles** — атомарное применение UFW-профилей от панели: backup → reset → apply → enable, авторолбэк при ошибке, node-API-port-guard (порт 9100), drift-детекция по SHA256-хэшу
- **Анти-DDoS** — многослойная защита: дежурный режим без лимитов, аварийный режим (SYNPROXY + hashlimit в отдельной iptables-цепочке `ANTIDDOS`, пороги авто-масштабируются по CPU/RAM хоста), автодетект атаки по сигналам из `/proc` (watchdog), whitelist на ipset, переживающий ребут и недоступность панели, self-check доступности ноды во время аварийного режима
- **Системные оптимизации** — sysctl/лимиты/HAProxy `maxconn` вычисляются на самой ноде из её MemTotal/nproc единым рендерером (`tune-sysctl.sh`), а не приходят готовыми от панели; авто-ре-рендер при каждой загрузке подхватывает ресайз VPS

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 2) Установить ноду
```

При установке скрипт запросит **IP-адрес панели** для настройки firewall.

## dpkg Self-Heal (deploy.sh)

На свежих серверах — особенно сразу после провижининга, до полной готовности systemd — postinst-скрипты пакетов (например `openssh-server`) падают на `systemctl restart`, оставляя dpkg в битом состоянии. После этого любая команда apt завершается ошибкой `Sub-process /usr/bin/dpkg returned an error code (1)`, и установка Docker/UFW не проходит.

`deploy.sh` решает это через два механизма:

**`enable_apt_guard()`** — вызывается перед `install_docker`:
- Создаёт `/usr/sbin/policy-rc.d` со скриптом `exit 101`, если файл ещё не существует; выставляет флаг `POLICY_RC_D_OWNED=1`
- `policy-rc.d` запрещает пакетным maintainer-скриптам трогать сервисы во время установки (читается только `invoke-rc.d`/`deb-systemd-invoke`, но **не** самим `systemctl`)
- Дожидается apt-lock (`wait_for_apt_lock`)
- Запускает `dpkg --configure -a` для донастройки застрявших пакетов

**`disable_apt_guard()`** — вызывается в `cleanup()` при любом выходе (успех/ошибка/прерывание):
- Удаляет `/usr/sbin/policy-rc.d` только если флаг `POLICY_RC_D_OWNED=1` — не трогает pre-existing файл
- Сбрасывает флаг в 0

Прямые вызовы `systemctl start docker` внутри `ensure_docker_running` гард не затрагивает — он влияет только на maintainer-скрипты пакетов.

## HAProxy

HAProxy работает как **нативный systemd сервис** на хосте (не в Docker). При установке ноды HAProxy устанавливается автоматически если не установлен.

**Конфиг**: `/etc/haproxy/haproxy.cfg`

**DNS Resolver**: В базовом конфиге включена секция `resolvers mydns` (DNS 1.1.1.1 + 8.8.8.8, hold valid 60s). Если target правила — доменное имя (а не IP), к server-линии автоматически добавляются параметры `resolvers mydns resolve-prefer ipv4 init-addr none`, что обеспечивает периодическое обновление IP домена без перезапуска HAProxy.

**Wildcard SSL**: Поле `use_wildcard: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` используется родительский домен для поиска сертификата вместо точного. Например, для правила с `cert_domain=sub.example.com` и `use_wildcard=True` нода применит сертификат `example.com` (покрывает `*.example.com`). Вспомогательные методы: `_extract_parent_domain()` — извлечение родительского домена, `_resolve_cert_domain()` — выбор итогового домена сертификата с учётом флага `use_wildcard`.

**PROXY protocol (accept_proxy)**: Поле `accept_proxy: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` добавляет `accept-proxy` к bind-строке frontend — нода принимает PROXY protocol header от вышестоящего HAProxy. Применяется к TCP и HTTPS правилам, в одиночном режиме и в режиме балансировщика. Парсинг `accept-proxy` из существующего конфига поддерживается; `update_rule()` обрабатывает изменение через пересоздание блока правила.

**Лимит соединений (maxconn) — по RAM хоста:**

Профиль конфигурации от панели один на много серверов с разной RAM, поэтому потолок `maxconn` в секции `global` вычисляет и подставляет сама нода при применении конфига, а не панель. `apply_config()` вызывает `_ensure_global_maxconn(content)`, которая вставляет расчётное значение в `global`, только если `maxconn` там ещё не задан явно (явное значение из профиля не трогается).

`_compute_maxconn()` (`haproxy_manager.py`): `MAXCONN_PER_RAM_MB = 10` соединений на МБ RAM — худший случай 2×16 КБ буфера (клиент+сервер) на соединение даёт HAProxy занять не больше ~40% памяти хоста. Дополнительно ограничен реальным лимитом дескрипторов HAProxy: `(NOFILE_LIMIT − 1024) // 3` (~2 fd на соединение с запасом). Итог зажат в `[MAXCONN_MIN = 10000, MAXCONN_MAX = 500000]`.

`_read_nofile_limit()` читает `NOFILE_LIMIT` из `/opt/monitoring/configs/tuning-facts.env` — это фактический `RLIMIT_NOFILE` HAProxy, в отличие от `/proc/sys/fs/nr_open` (потолок того, что процесс *может* установить): расчёт от `nr_open=2097152` при лимите юнита 65536 дал бы `maxconn` до 500000, и HAProxy со `strict-limits` (дефолт с 2.5) отказался бы стартовать. Рендерер (`tune-sysctl.sh`) утверждает всю цепочку файловых дескрипторов численно и пишет drop-in `LimitNOFILE` для `haproxy.service` — см. «Системные оптимизации» ниже.

**Таймауты и TCP keepalive — детект мёртвых туннелей:**

Мёртвые туннели без FIN от клиентов за NAT/ТСПУ (типично для мобильных клиентов) держали бы буферы вплоть до `timeout tunnel` и раздували бы память HAProxy, поэтому базовый шаблон конфига (`_generate_base_config()` на ноде, зеркалируется `generate_base_config()` в `panel/backend/app/services/haproxy_config.py`) задаёт:
- `tune.bufsize 16384` — вдвое меньше памяти на соединение, чем дефолтные 32768
- `timeout tunnel 1h`
- Интервалы TCP keepalive: `clitcpka-idle 60s`, `clitcpka-intvl 10s`, `clitcpka-cnt 3` и аналогично `srvtcpka-*` — без явных интервалов ядро использовало бы свой дефолт (обычно 2ч+), с ними мёртвое соединение детектится ядром за ~1.5 минуты вместо часов ожидания `timeout tunnel`.

Существующий шаблон в уже применённых конфигах обновляется через «Перегенерировать конфиг» на странице профиля в панели или через `POST /api/haproxy/config/apply` с новым содержимым — сама по себе установка/обновление ноды конфиг не трогает.

**Управление через терминал панели**:
```bash
systemctl status haproxy       # Статус
systemctl start haproxy        # Запуск
systemctl stop haproxy         # Остановка
systemctl restart haproxy      # Полный перезапуск
systemctl reload haproxy       # Reload конфига (без разрыва соединений)
haproxy -c -f /etc/haproxy/haproxy.cfg  # Проверка конфига
journalctl -u haproxy -n 100   # Логи
```

**При установке/обновлении ноды**:
- Если HAProxy уже работает — не перезапускается, конфиг не меняется
- Если не установлен — устанавливается через apt
- API адаптируется к текущему состоянию сервиса

**Миграция с контейнерной версии**:

При обновлении со старой версии (где HAProxy работал в Docker контейнере) скрипт автоматически:
1. Обнаруживает старый контейнер `monitoring-haproxy`
2. Устанавливает native HAProxy если не установлен (`apt install haproxy`)
3. Останавливает и удаляет контейнер (конфиг уже на хосте — был bind mount)
4. Включает автозапуск и запускает native HAProxy как systemd сервис

Миграция происходит автоматически при вызове `./update.sh`.

## Структура

```
node/
├── app/
│   ├── main.py           # FastAPI приложение
│   ├── config.py         # Pydantic Settings
│   ├── auth.py           # API Key авторизация
│   ├── models/
│   │   ├── ssl.py        # Pydantic модели: WildcardDeployRequest/Response, WildcardStatusResponse
│   │   ├── firewall_profile.py  # Pydantic модели: ProfileRule, ProfileApplyRequest/Response, ProfileStateResponse
│   │   └── remnawave_nginx.py   # Pydantic модели: NginxDiscoverResponse, NginxConfigResponse, NginxStatusResponse, NginxValidateRequest/Response, NginxApplyRequest/Response, NginxActionResponse
│   ├── routers/          # API эндпоинты (metrics, haproxy, traffic, ssh, ssl, firewall, antiddos, remnawave и др.)
│   └── services/         # Сбор метрик, HAProxy, трафик, SSH менеджер
│       ├── ssl_manager.py          # Деплой wildcard сертификатов: запись на хост, бэкап, откат, валидация
│       ├── firewall_manager.py     # UFW: apply_profile, backup/restore, compute_rules_hash, get_full_state
│       ├── antiddos_manager.py     # Тонкая обёртка над ddos-watchdog.sh (nsenter): enable/disable emergency, watchdog, whitelist sync
│       ├── host_files.py           # read_host_file()/write_host_file()/read_host_file_exact() — общая работа с файлами на хосте через nsenter+base64
│       └── remnawave_nginx_manager.py  # RemnawaveNginxManager: discover, get_config, status, logs, validate_content, apply_config, reload, restart
├── scripts/
│   └── apply-update.sh   # Логика обновления (запускается из свежего репо)
├── tests/
│   └── test_verify_sysctl.py  # Юнит-тесты верификации тюнинга (stdlib unittest)
├── nginx/                # Reverse proxy с SSL
├── docker-compose.yml
├── update.sh             # Скачивает репо и запускает apply-update.sh
└── deploy.sh
```

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| API_KEY | Ключ авторизации | auto |
| NODE_NAME | Имя ноды | node-01 |
| PANEL_IP | IP панели (для UFW) | задаётся при установке |
| TRAFFIC_COLLECT_INTERVAL | Интервал сбора (сек) | 60 |
| TRAFFIC_RETENTION_DAYS | Хранение данных (дни) | 7 |
| MON_IMAGE_TAG | Тег Docker-образа api в `docker-compose.yml` (`image: ...:${MON_IMAGE_TAG:-latest}`); `deploy.sh` при установке пишет `dev`, если `MON_BRANCH=dev`, иначе `latest`; апдейтер (`apply-update.sh`) переписывает при обновлении на `main`/`dev` | latest |

## Порты

| Порт | Доступ | Описание |
|------|--------|----------|
| 9100 | Только Panel IP | API мониторинга |
| 80 | Все | Let's Encrypt верификация |
| 22 | Все | SSH |

## Безопасность

- **API Key авторизация** (заголовок `X-API-Key`)
- **Rate limiting**: 100 запросов/минуту
- **Anti-brute force**: 10 попыток = бан на 1 час
- **TLS 1.2/1.3** с сильными шифрами
- **UFW**: порт 9100 доступен только с IP панели
- **Connection drop**: все ошибки авторизации (401/403/429) приводят к разрыву соединения без HTTP-ответа — атакующий не получает никакой информации

## API

### Система

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/version | Версия ноды |
| GET | /api/system/versions | Объединённый endpoint: версия ноды + оптимизации |
| POST | /api/system/update | Запуск обновления (target_ref: branch/tag/commit, по умолчанию main; при вызове через панель панель подставляет выбранный канал обновлений — main/dev) |
| GET | /api/system/update/status | Статус обновления |
| GET | /api/system/optimizations/version | Версия системных оптимизаций (installed + version) |
| POST | /api/system/optimizations/apply | Применить системные оптимизации |
| POST | /api/system/optimizations/remove | Удалить все системные оптимизации |
| GET | /api/system/nic-info | Режим NIC и аппаратные возможности multiqueue |
| POST | /api/system/execute | Выполнить команду на хосте |
| POST | /api/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |
| POST | /api/system/time-sync | Установить часовой пояс и синхронизировать NTP |

**Объединённый endpoint версий** (`/api/system/versions`):
```json
{
    "node_version": "1.2.3",
    "optimizations": {
        "installed": true,
        "version": "2.0.0"
    }
}
```
Панель использует этот endpoint для получения всей информации о ноде одним запросом.

**Выполнение команд на хосте**:

Эндпоинт `/api/system/execute` позволяет выполнять произвольные shell-команды и многострочные bash-скрипты на хост-системе через `nsenter`. Работает из Docker контейнера благодаря `privileged: true` и `pid: host`. Максимальная длина поля `command` — 65000 символов.

**PATH**: Все команды выполняются с расширенным PATH (`/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), что позволяет использовать snap-пакеты и локально установленные бинарники.

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
- `command` (required) — shell-команда для выполнения
- `timeout` (optional) — таймаут в секундах, 1-600 (default: 30)
- `shell` (optional) — shell: "sh" или "bash" (default: "sh")

**Потоковое выполнение команд (SSE)**:

Эндпоинт `/api/system/execute-stream` выполняет команду с потоковым выводом через Server-Sent Events.

```
// SSE Events
event: stdout
data: {"line": "output line"}

event: stderr
data: {"line": "error line"}

event: done
data: {"exit_code": 0, "execution_time_ms": 1234, "success": true}

event: error
data: {"message": "error description"}
```

**Диагностика NIC** (`GET /api/system/nic-info`):

Определяет активный режим NIC-тюнинга и аппаратные возможности multiqueue для каждого физического интерфейса с поднятым линком. Используется панелью на вкладке «Системные оптимизации» для отображения диагностики — оператор выбирает режим самостоятельно.

```json
// Response
{
    "nic_mode": "multiqueue",
    "multiqueue_supported": true,
    "cpu_cores": 4,
    "cpu_threads": 8,
    "interfaces": [
        {
            "name": "eth0",
            "max_hw_queues": 4,
            "current_hw_queues": 4
        }
    ]
}
```

Поля:
- `nic_mode` — активный режим: `"rps"`, `"multiqueue"`, `"hybrid"` или `"none"` (определяется по enabled-статусу systemd-сервисов)
- `multiqueue_supported` — `true`, если хотя бы один интерфейс имеет `max_hw_queues > 1`
- `cpu_cores` — число физических ядер CPU (через `lscpu`)
- `cpu_threads` — число логических потоков CPU (`nproc`)
- `interfaces[].max_hw_queues` — максимальное число аппаратных очередей; при наличии `Combined` — берётся оно, иначе `max(RX, TX)`; если ethtool не поддерживает channels API — fallback на подсчёт `rx-*` в sysfs
- `interfaces[].current_hw_queues` — текущее активное число очередей

Алгоритм определения очередей (`detect_iface_hw_queues`) зеркалит `get_max_hw_queues()` из `install.sh` и корректно обрабатывает карты с `Combined: n/a` (mlx4_en, часть igb/ixgbe), которые показывают только раздельные RX/TX.

**Механизм обновления**:
1. API создаёт временный контейнер `monitoring-updater` (образ `docker:cli`)
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка — панель подставляет выбранный канал обновлений, если ref не передан явно)
3. Запускает `update.sh` из склонированной папки, передавая исходный `TARGET_REF` 4-м аргументом
4. `update.sh` скачивает репо и запускает **свежий** `scripts/apply-update.sh` из скачанной версии, тоже с `TARGET_REF` 4-м аргументом
5. `apply-update.sh` выполняет обновление: копирование файлов и получение образов (pull/сборка) — до остановки контейнеров, затем миграции, `docker compose down` + `up`. Если ref — `main`/`dev`, перед `pull` в `.env` пишется `MON_IMAGE_TAG=latest|dev` (при обновлении на конкретный тег/коммит канал не меняется — используется уже сохранённое значение); если `apply-update.sh` запущен старым апдейтером без ref-аргумента — ветка вычитывается из `.git/HEAD` скачанного клона
6. Контейнер удаляется после завершения

Обновление **всегда** использует актуальную версию логики из GitHub (двойная загрузка гарантирует свежесть).

**Устойчивость к медленной сети**

Порядок обновления — «сначала скачать, потом рестартовать»: rsync файлов и `docker compose pull` выполняются **до** остановки контейнеров, нода продолжает работать на старой версии всё время скачивания; `docker compose down` + `up` — только после успешного получения образов, даунтайм сокращается до секунд рестарта. Если pull и fallback-сборка не удались — обновление отменяется **без остановки контейнеров**, нода остаётся на старой версии.

- Бюджет pull: `DOCKER_PULL_TIMEOUT` 1800с на попытку (переопределяется env) × 3 попытки — docker переиспользует уже скачанные слои между попытками; таймаут fallback-сборки 900с. Причина: на нодах с очень медленной сетью слой образа может качаться 10-15+ минут (наблюдалось 755с) — меньший бюджет обрывал бы обновление на середине.
- Если `docker compose up -d` упал на ожидании healthy у api (`depends_on`), оставив nginx в статусе `Created`, контейнеры поднимаются напрямую: `docker compose start` → `docker start monitoring-api monitoring-nginx`; тот же прямой старт выполняется и в recovery-trap.
- Ожидание updater-контейнера в node-API (`system.py`): `container.wait` с таймаутом `UPDATER_WAIT_TIMEOUT = 7200` (2 часа) — при коротком таймауте обновление дольше него ошибочно помечалось бы «failed», а повторный запуск убивал бы ещё работающий updater.
- rsync при работающих контейнерах безопасен: замена файлов идёт через rename (новый inode), bind-mounts запущенных контейнеров видят старые файлы до рестарта.

### Метрики

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/metrics | Все метрики |
| GET | /api/metrics/cpu | CPU |
| GET | /api/metrics/memory | RAM |
| GET | /api/metrics/disk | Диски |
| GET | /api/metrics/network | Сеть |
| GET | /api/metrics/processes | Процессы |
| GET | /health | Health check |

**Поле `antiddos` в `/api/metrics` (модель `AntiDdosInfo`, `AllMetrics.antiddos`):** режим, источник, время перехода в аварийный режим, состояние watchdog, заполнение conntrack, `insert_failed`, `SyncookiesSent`, дропы из `/proc/net/softnet_stat`, `ListenDrops`/`ListenOverflows`. Метод `MetricsCollector.get_antiddos_info()` (`metrics_collector.py`) читает `/proc` напрямую из смонтированного `/host/proc` — без `nsenter`, дешевле на каждый опрос метрик, в отличие от `antiddos_manager.py`, который дёргает `ddos-watchdog.sh` через nsenter только для управляющих команд. Хелперы: `_read_proc_int()`, `_read_hex_column_sum()` (суммирование по CPU из `/proc/net/stat/nf_conntrack`), `_read_netstat_counters()`.

### Traffic

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/traffic/summary | Сводка (days=1-90) |
| GET | /api/traffic/hourly | Почасовая (hours=1-168) |
| GET | /api/traffic/daily | Дневная (days=1-90) |
| GET | /api/traffic/monthly | Месячная (months=1-24) |
| GET | /api/traffic/ports | Трафик по портам |
| GET | /api/traffic/interfaces | Трафик по интерфейсам |
| GET | /api/traffic/ports/tracked | Отслеживаемые порты |
| POST | /api/traffic/ports/add | Добавить порт |
| POST | /api/traffic/ports/remove | Удалить порт |

### HAProxy

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy/status | Статус сервиса |
| GET | /api/haproxy/rules | Список правил |
| POST | /api/haproxy/rules | Создать правило |
| PUT | /api/haproxy/rules/{name} | Обновить правило |
| DELETE | /api/haproxy/rules/{name} | Удалить правило |
| POST | /api/haproxy/start | Запустить (systemctl start) |
| POST | /api/haproxy/stop | Остановить (systemctl stop) |
| POST | /api/haproxy/reload | Reload конфига (systemctl reload) |
| POST | /api/haproxy/restart | Restart сервиса (systemctl restart) |

При ошибке `/start`, `/stop`, `/reload`, `/restart` возвращают `HTTP 500` с полем `detail`, содержащим реальную причину от менеджера (например: `"Restart failed: ..."`, `"Config validation failed: ..."`, `"HAProxy is not installed"`, `"Failed to stop: ..."`). Панель транслирует это сообщение в результатах массовых действий по каждому серверу.
| GET | /api/haproxy/config | Получить конфиг |
| POST | /api/haproxy/config/apply | Применить конфиг; тело `ConfigApplyRequest`: `config_content`, `ensure_started: bool = False` — при `True` поднимает остановленный HAProxy через `reload(auto_start=True)` |
| GET | /api/haproxy/logs | Логи (journalctl, tail=100) |

### Сертификаты

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy/certs | Список доменов |
| GET | /api/haproxy/certs/all | Все с деталями |
| GET | /api/haproxy/certs/{domain} | Детали сертификата |
| POST | /api/haproxy/certs/generate | Создать Let's Encrypt |
| POST | /api/haproxy/certs/upload | Загрузить свой |
| POST | /api/haproxy/certs/{domain}/renew | Продлить |
| DELETE | /api/haproxy/certs/{domain} | Удалить |

### Remnawave

Проверка доступности контейнера `remnanode`, плюс управление nginx-конфигом установки Remnawave на хосте (каталог по умолчанию `/opt/remnawave`).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/status | Статус контейнера remnanode (available: true/false) |
| GET | /api/remnawave/nginx/discover?path= | Обнаружить установку: каталог/compose/nginx.conf/ssl, mount-путь конфига в контейнере, состояние `remnawave-nginx` (exists/running/image), sha256 конфига |
| GET | /api/remnawave/nginx/config?path= | Получить содержимое nginx.conf с хоста |
| GET | /api/remnawave/nginx/status?path= | Лёгкий статус: состояние контейнера + хэш конфига |
| GET | /api/remnawave/nginx/logs?tail= | Логи контейнера `remnawave-nginx` (`docker logs`) |
| POST | /api/remnawave/nginx/validate | Проверить кандидат-конфиг (`nginx -t`) без применения |
| POST | /api/remnawave/nginx/config/apply | Применить конфиг: `{path, content, reload_after, restart, ensure_started}` |
| POST | /api/remnawave/nginx/reload | `nginx -s reload` с pre-check `nginx -t` |
| POST | /api/remnawave/nginx/restart | `docker restart remnawave-nginx` |

`GET /api/remnawave/status` используется панелью для определения поля `has_xray_node` у сервера (обновляется каждые 2 минуты). Остальные эндпоинты — для страницы «Remnawave Nginx» в панели (см. panel/DOCUMENTATION.md), путь по умолчанию `/opt/remnawave`.

**Критический инвариант: `nginx.conf` — single-file bind-mount.** Docker Compose монтирует конфиг в контейнер `remnawave-nginx` как один файл, не каталог. Любая запись или откат обязаны идти **in-place** (перезапись содержимого существующего inode) — `mv`/`rename` заменили бы файл новым inode, который перестал бы быть тем же смонтированным файлом внутри контейнера, и nginx продолжил бы читать старое содержимое до пересоздания контейнера. `RemnawaveNginxManager` (`remnawave_nginx_manager.py`) и общий `write_host_file()` (`host_files.py`) соблюдают это на каждом шаге.

**Точное чтение файлов (`read_host_file_exact`) — обязательно для nginx.conf и docker-compose.yml.** Общий `read_host_file()` (`host_files.py`) делает `.strip()` над результатом `HostExecutor.execute()` (`host_executor.py`) — для построчного парсинга в `system.py` это безобидно, но для nginx.conf терялся бы завершающий перевод строки в двух местах: откат после неудачного apply записал бы контент без финального `\n` (функционально верно, но не байт-в-байт исходный файл), а хэш, который нода отдаёт в `GET /api/remnawave/nginx/config` и `/nginx/status`, никогда не совпал бы с эталоном `Server.remnawave_nginx_node_hash` (считается от записанного контента) — reconciler видел бы мнимый drift и делал лишний ресинк при каждом переходе ноды offline→online. Поэтому `read_host_file_exact(path)` (`host_files.py`) переносит содержимое через `base64 -w0` и декодирует — у base64 нет пробельных краёв, `.strip()` его не портит; пустой файл возвращается как `""`, ошибка чтения — `None`. Используется во всех чтениях nginx.conf и docker-compose.yml (`discover`, `get_config`, `status`, `_compose_nofile`, `_prepare_compose_patch`, `apply_config`); `read_host_file()` остаётся только для `system.py`.

**Транзакция `apply_config()`:**
1. Backup текущего файла (`cp` на `.bak`)
2. In-place запись нового содержимого
3. `docker exec remnawave-nginx nginx -t` — проверка синтаксиса внутри контейнера
4. При ошибке — откат из backup, конфиг остаётся прежним
5. `nginx -s reload` (или `docker restart`, если запрошено) — при неудаче reload тоже откат + повторный reload
6. `asyncio.Lock` защищает от конкурентных apply

Если контейнер `remnawave-nginx` остановлен: валидация идёт через одноразовый `docker run --rm` с монтированием `/etc/letsencrypt` и `ssl/` вместо `docker exec` в живой контейнер; после записи опционально поднимается `docker compose up -d` через nsenter — единственная nsenter-операция во всём модуле, остальное (`docker exec`, `docker logs`, `docker restart`, `docker inspect`) идёт через Docker CLI напрямую через сокет.

**Валидация (`validate_content`):** кандидат-конфиг нельзя проверить через `docker cp` — путь на хосте не существует в файловой системе контейнера агента (агент сам работает в контейнере). Вместо этого содержимое загружается в живой контейнер `remnawave-nginx` через `docker exec -i sh -c 'cat > /tmp/... '`, затем `nginx -t -c` на этом временном файле.

Путь установки валидируется regex `^/[A-Za-z0-9._/-]+$` (`InvalidInstallPathError` при нарушении) — путь приходит от панели, и это единственная защита от произвольной записи по хосту.

**Автоподстановка host-специфичных лимитов (`# auto: node`) — прямой аналог `_ensure_global_maxconn` у HAProxy.** Профиль от панели один на много нод с разной RAM/nofile, поэтому три строки шаблона, помеченные маркером `AUTO_MARKER = "# auto: node"` (`worker_rlimit_nofile`, `worker_connections`, `ssl_session_cache`), нода пересчитывает под свой хост при каждом применении. Если оператор вручную убрал маркер — строка считается заданной явно и не трогается.

- `compute_host_limits(compose_nofile)` — `worker_rlimit_nofile` = `DOCKER_NOFILE` (иначе `NOFILE_LIMIT`) из `/opt/monitoring/configs/tuning-facts.env`; при недоступности facts — `ulimits.nofile.soft` из docker-compose.yml установки, иначе 65536. `worker_connections` = nofile/4 (2 файловых дескриптора на проксируемое соединение с запасом), clamp `[1024, 65536]`. `ssl_session_cache` = MemTotal_MB/100 МБ, clamp `[10, 100]`.
- `patch_host_limits(content, limits)` — идемпотентная regex-замена значения только у строк с `AUTO_MARKER`.
- `RemnawaveNginxManager.apply_host_limits(path, content)` вызывается и в `apply_config()` (до вычисления хэша и записи), и в `validate_content()` — валидация и реальное применение видят один и тот же итоговый контент.
- Причина: жёсткий ulimit контейнера `remnawave-nginx` задаётся `DOCKER_NOFILE` из фактов рендерера (`clamp(pow2_floor(MemMB*64), 65536, 2097152)`) — статический `worker_rlimit_nofile` (например 131072) на 1-ГБ ноде давал бы `setrlimit(...) failed (EPERM)` и молча оставлял бы старый лимит, а на 8–16 ГБ занижал бы потолок вместо использования доступного.

**Автоперевод install.sh-установок с фрагментного монтирования на полный конфиг.** Установки Remnawave через пункт меню 9 монтируют `nginx.conf` как фрагмент http-контекста (`./nginx.conf:/etc/nginx/conf.d/default.conf:ro` в docker-compose.yml установки) — внутри лежит кусок конфига (`map`, `ssl_*`, маскировочный `server` с `listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server`), без обёртки `worker_*`/`events`/`http`. Профиль панели генерирует **полный** `nginx.conf` — на такой ноде `nginx -t` падал бы (`http{}` внутри `http{}`), apply откатывался бы, сервер уходил бы в failed. При первом применении профиля нода сама переводит установку на полный конфиг.

- `FULL_CONFIG_MOUNT = "/etc/nginx/nginx.conf"` — целевая точка монтирования.
- `compose_mount_target(compose)` — читает текущую точку монтирования `nginx.conf` из docker-compose.yml установки.
- `patch_compose_mount(compose)` — переводит её на `FULL_CONFIG_MOUNT`; regex не трогает остальные тома и суффикс `:ro`; возвращает `None`, если менять нечего (конфиг уже полный или монтирования нет).
- Побочный эффект перевода: маскировочный server-блок с unix-сокетом из старого фрагмента исчезает вместе с ним — если он используется, оператор переносит его в профиль через raw-редактор панели.

**Проверка и автомонтирование путей сертификатов.** Профиль панели рендерит пути `ssl_certificate`/`ssl_certificate_key` как есть (обычно `/etc/letsencrypt/live/{{DOMAIN}}/...`), но если `docker-compose.yml` установки Remnawave не монтирует `/etc/letsencrypt` в контейнер `remnawave-nginx` — `nginx -t` внутри контейнера не видит существующий на хосте сертификат и падает с малопонятной `cannot load certificate ... BIO_new_file() failed`. Свежие установки получают маунт сразу (`install.sh` дописывает `- /etc/letsencrypt:/etc/letsencrypt:ro` в volumes сервиса), а на установках без маунта нода сама обнаруживает и чинит нехватку при первом apply:

- `config_cert_files(content)` — извлекает пути `ssl_certificate`/`ssl_certificate_key` из конфига (без дублей, только «безопасные» пути через `_SAFE_PATH_RE`).
- `host_path_for_cert(cert_file, compose, install_path)` — путь из конфига — это путь **внутри контейнера**; функция маппит его на хостовый путь через существующие тома docker-compose.yml (относительный источник `./ssl` разворачивается в `{install_path}/ssl`), если маунта нет — возвращает путь как есть (значит на хосте он лежит по тому же пути).
- `missing_certs_on_host(path, content)` — по хостовым путям проверяет существование файлов на хосте (`[ -e ... ]` через `HostExecutor`); если файла нет, `validate_content()`/`apply_config()` возвращают понятную ошибку «Сертификат не найден на хосте: `<путь>`. Выпустите сертификат ... или исправьте пути в опциях профиля» **до** запуска `nginx -t` — это самая частая причина отказа, и понятная ошибка избавляет оператора от разбора сырого вывода nginx.
- `cert_mount_dir(cert_file)` / `is_covered_by_mounts(path, targets)` / `compose_mount_targets(compose)` / `missing_cert_mounts(compose, cert_files)` — вычисляют, какие каталоги сертификатов (включая кастомные пути из опций профиля) не покрыты ни одним volume сервиса `remnawave-nginx`. Для путей под `/etc/letsencrypt` монтируется весь корень (`LETSENCRYPT_ROOT`), а не подкаталог `live/{domain}` — файлы там являются симлинками в `../../archive`, и узкий маунт дал бы контейнеру битые ссылки.
- `patch_compose_volumes(compose, dirs)` — дописывает найденные каталоги как `- <dir>:<dir>:ro` в volumes сервиса `remnawave-nginx`; точка вставки — строка монтирования `./nginx.conf`, единственная однозначно принадлежащая этому сервису (у `remnanode` её нет), поэтому `remnanode` не затрагивается.
- `_certs_not_visible_in_container(cert_files)` — отдельно от «compose не содержит нужный volume» обнаруживается «устаревший» контейнер: compose уже пропатчен (текущим или предыдущим apply), но контейнер создан **до** этого и физически не видит маунт — сверяется через `docker inspect -f '{{range .Mounts}}{{.Destination}}...'`.
- `explain_validation_error(output)` — к ошибкам `nginx -t` с `cannot load certificate`/`BIO_new_file` дописывает `CERT_HINT`: подсказку, что nginx ищет файл внутри контейнера, а не на хосте, и что контейнер, созданный до добавления volume, нужно пересоздать вручную (`docker compose up -d --force-recreate remnawave-nginx`) — на случай, если автопатч почему-то не сработал.

**Единый транзакционный путь пересборки compose (`_prepare_compose_patch`/`_apply_with_recreate`).** Механизм перевода фрагментного монтирования на полный конфиг и механизм автомонтирования сертификатов решают разные проблемы, но оба требуют одного и того же — правки `docker-compose.yml` и пересоздания контейнера — поэтому реализованы одним общим путём:

- `_prepare_compose_patch(path, cert_files)` — считает нужный патч compose: сначала `patch_compose_mount` (перевод точки монтирования), затем `missing_cert_mounts`/`patch_compose_volumes` поверх уже переведённого compose; возвращает `None`, если менять нечего, иначе `{path, current, patched, remounted, added_volumes}`.
- В `apply_config()` пересоздание запускается, если `_prepare_compose_patch` вернул патч **или** compose уже в порядке, но `_certs_not_visible_in_container` нашла невидимые контейнеру сертификаты у уже работающего контейнера.
- `_apply_with_recreate(...)` — сама транзакция: валидация кандидата одноразовым контейнером **до** любых правок (`validate_content(..., force_one_off=True)` — живому контейнеру может не хватать тех самых маунтов, поэтому проверка идёт через `docker run --rm` с добавленными каталогами) → backup+запись пропатченного `docker-compose.yml` (если патчился) → backup+запись `nginx.conf` → `docker compose up -d --force-recreate remnawave-nginx` (смена точки монтирования и новые volume не подхватываются `reload`) → повторный `nginx -t` в новом контейнере. Любая неудача на любом шаге — откат обоих файлов и обратное пересоздание контейнера.
- `_compose_up(path, force_recreate=False)` — общий helper запуска установки, флаг пересоздания используется здесь и в обычном пути (`ensure_started`).
- Ответ содержит `remounted` (перевод монтирования) и словесное перечисление изменений («монтирование переведено на полный nginx.conf», «в docker-compose.yml добавлены тома сертификатов: ...», «контейнер пересоздан») — `NginxApplyResponse.remounted` панель логирует в лог синхронизации профиля (см. panel/DOCUMENTATION.md).

**Файлы:**
- `node/app/services/remnawave_nginx_manager.py` — `RemnawaveNginxManager` (синглтон через `get_remnawave_nginx_manager()`): `discover`, `get_config`, `status`, `logs`, `validate_content`, `apply_config`, `apply_host_limits`, `missing_certs_on_host`, `reload`, `restart`; модульные `compute_host_limits()`, `patch_host_limits()`, `compose_mount_target()`, `patch_compose_mount()`, `config_cert_files()`, `cert_mount_dir()`, `is_covered_by_mounts()`, `compose_mount_targets()`, `missing_cert_mounts()`, `patch_compose_volumes()`, `host_path_for_cert()`, `explain_validation_error()`, `AUTO_MARKER`, `FULL_CONFIG_MOUNT`, `LETSENCRYPT_ROOT`, `CERT_HINT`
- `node/app/services/host_files.py` — `read_host_file()`/`write_host_file()`/`read_host_file_exact()`, общий модуль: переиспользуется и системными оптимизациями (`routers/system.py`), и этим модулем
- `node/app/models/remnawave_nginx.py` — Pydantic-модели ответов/запросов, включая `NginxApplyResponse.remounted`
- `node/app/routers/remnawave.py` — API роутер
- `node/nginx/nginx.conf` — `location /api/remnawave/` с таймаутом 120с (apply может включать `docker compose up`)
- `node/tests/test_remnawave_nginx_limits.py` — тесты автоподстановки лимитов (подстановка только помеченных строк, идемпотентность, ручные правки без маркера не затираются, разумность вычисленных значений) плюс класс `ComposeMountTests` — определение фрагментного/полного монтирования, патч фрагмент→полный с сохранением прочих томов, отсутствие патча для уже полного конфига и для compose без монтирования
- `node/tests/test_remnawave_nginx_cert_mounts.py` — тесты на чистых функциях: извлечение путей сертификатов без дублей и с отсевом небезопасных путей, вычисление каталога монтирования (весь корень для `/etc/letsencrypt`, родительский каталог для кастомных путей), недостающие маунты (в т.ч. что уже смонтированный `/etc/letsencrypt` и относительный `./ssl` покрывают вложенные пути, совпадение по компоненту пути, а не по префиксу строки), вставка volume ровно в сервис `remnawave-nginx` без затрагивания `remnanode` и идемпотентно, маппинг путь-в-контейнере → путь-на-хосте (относительный и абсолютный источник маунта, немонтированный путь остаётся собой), подсказка `CERT_HINT` добавляется только к ошибкам про сертификат
- `node/tests/test_host_files.py` — `read_host_file()` теряет завершающий перевод строки, `read_host_file_exact()` возвращает контент байт-в-байт, отсутствующий файл даёт `None`
- Всего тестов ноды — 44 (`python -m unittest discover -s node/tests`)

### IPSet Blocklist

Блокировка IP/CIDR через ipset. Два типа списков:
- **Блок-список** — `blocklist_permanent` (постоянный) и `blocklist_temp` (временный с таймаутом); направления: in (INPUT) и out (OUTPUT).
- **Белый список (allowlist)** — доверенные IP/CIDR, которые **всегда** проходят через ноду вне зависимости от блокировок.

**Защита от приватных диапазонов:** нода самостоятельно отказывается добавлять в block-сеты IP/CIDR, пересекающиеся с приватными/служебными диапазонами (`0.0.0.0/8`, `10.0.0.0/8`, `127.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` и т.п. — полный список в `NON_PUBLIC_NETS`, `app/services/ipset_manager.py`), через `is_public_range()`. Проверка стоит в каждой точке входа: `add_ip`, `bulk_add`, `sync`, загрузка постоянного списка из `blocklist.json` при старте. Это defense-in-depth независимо от версии панели — панель фильтрует источники и ручные правила на своей стороне (см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#ip-blocklist)), но нода не доверяет входу целиком: DROP по приватному диапазону убивает loopback и docker-bridge самой ноды.

**Белый список:**

Сеты `allowlist` (in → INPUT, match src) и `allowlist_out` (out → OUTPUT, match dst), тип `hash:net`. Правило `iptables ... -j ACCEPT` всегда вставляется на позицию 1 в цепочке (выше всех DROP) — netfilter обходит цепочку сверху вниз и ACCEPT прерывает обход. Это корректно перекрывает и точечные блоки, и CIDR-перекрытия (например, разрешить `1.2.3.4` при заблокированном `1.2.3.0/24`). Белый список всегда permanent — временного режима нет.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ipset/status | Статус списков (count, timeout, allow_count) |
| GET | /api/ipset/list/{set_type} | Получить IP из списка (permanent/temp) |
| POST | /api/ipset/add | Добавить IP/CIDR |
| POST | /api/ipset/bulk-add | Массовое добавление |
| DELETE | /api/ipset/remove | Удалить IP/CIDR |
| POST | /api/ipset/bulk-remove | Массовое удаление |
| POST | /api/ipset/clear/{set_type} | Очистить список |
| PUT | /api/ipset/timeout | Изменить timeout temp списка |
| POST | /api/ipset/sync | Синхронизация блок-списка (замена всего списка, атомарный diff через `ipset restore`) |
| POST | /api/ipset/allowlist/sync | Синхронизация белого списка (замена) |

**`POST /api/ipset/allowlist/sync`** — принимает `AllowSyncRequest`:
- `ips` — массив IP/CIDR для белого списка
- `direction` — `"in"` или `"out"`

**Поля в `GET /api/ipset/status`** — `incoming.allow_count` и `outgoing.allow_count` (количество записей в allowlist).

`POST /api/ipset/sync` и `/bulk-add` дополнительно отдают `skipped_non_public` — сколько записей отброшено как приватные/служебные (см. «Защита от приватных диапазонов» выше).

**Особенности:**
- Тип ipset: `hash:net` (поддержка IP и CIDR)
- Правила iptables блок-списка: `INPUT/OUTPUT -m set --match-set blocklist_* src/dst -j DROP`
- Правила allowlist: `-I INPUT 1 ... -j ACCEPT` / `-I OUTPUT 1 ... -j ACCEPT` (позиция 1, выше DROP)
- Все постоянные правила сохраняются в `/var/lib/monitoring/blocklist.json` (ключи `in_allow`, `out_allow` для белого списка)
- При старте ноды: постоянные правила восстанавливаются, временный список пустой, allowlist загружается из персиста
- Массовые операции (`sync`, `bulk_add`, `bulk_remove`, `sync_allow`, загрузка permanent/allow из `blocklist.json` при старте) применяются одним вызовом `ipset -exist restore` вместо по-IP `ipset add`/`del` — десятки тысяч записей применяются за доли секунды; мутации сериализованы `threading.Lock`
- Счётчики в `GET /api/ipset/status` читаются из заголовка `ipset list -t` (`Number of entries`), без выгрузки всего сета
- Лимит записей: сет создаётся с `maxelem 1000000` — список крупнее 1 млн записей нода принять не сможет, `ipset restore` завершится ошибкой (физический потолок ноды). Панель знает про этот лимит (константа `NODE_MAX_IPSET_ENTRIES`) и обрезает по нему источники и общий список ещё до отправки на ноду — источники сверх лимита помечаются ошибкой и исключаются из синка вместо попытки прогнать их через `ipset restore`, см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#ip-blocklist)

### SSH Security

Управление SSH-безопасностью сервера: настройки `sshd_config`, fail2ban, authorized_keys. Все операции выполняются через `nsenter` на хост-системе.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ssh/config | Текущие настройки sshd |
| POST | /api/ssh/config | Применить новые настройки |
| POST | /api/ssh/config/test | Валидация настроек без применения |
| GET | /api/ssh/fail2ban/status | Статус и настройки fail2ban SSH jail |
| POST | /api/ssh/fail2ban/config | Обновить конфигурацию fail2ban |
| GET | /api/ssh/fail2ban/banned | Список забаненных IP |
| POST | /api/ssh/fail2ban/unban | Разбанить IP |
| POST | /api/ssh/fail2ban/unban-all | Разбанить все IP |
| GET | /api/ssh/keys | Список SSH-ключей (authorized_keys) |
| POST | /api/ssh/keys | Добавить SSH-ключ |
| DELETE | /api/ssh/keys | Удалить SSH-ключ |
| POST | /api/ssh/password | Сменить пароль пользователя (chpasswd) |
| GET | /api/ssh/status | Общий статус SSH (sshd, fail2ban, ключи) |

**Механизмы безопасности при изменении sshd_config:**
- Автобэкап перед каждым изменением (хранятся последние 5 копий)
- `sshd -t` валидация перед применением
- Атомарная запись через temp file → `mv`
- `reload` вместо `restart` — сохраняет активные сессии
- Автовосстановление из последнего бэкапа если sshd не запустился
- Запрет отключить все методы аутентификации одновременно
- При смене порта — UFW правило открывается автоматически

**Файлы:**
- `node/app/services/ssh_config_manager.py` — работа с sshd_config, fail2ban, authorized_keys
- `node/app/routers/ssh.py` — API эндпоинты

### Wildcard SSL

Деплой wildcard сертификатов на хост-систему ноды. Панель выпускает сертификат через certbot + Cloudflare DNS challenge и доставляет его на ноды через этот API.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/ssl/wildcard/deploy | Принять и задеплоить wildcard сертификат |
| GET | /api/ssl/wildcard/status | Статус последнего деплоя |

**`POST /api/ssl/wildcard/deploy`** — принимает `WildcardDeployRequest`:
- `cert_pem` — содержимое fullchain.pem
- `key_pem` — содержимое privkey.pem
- `deploy_path` — путь на хосте для записи файлов
- `reload_cmd` — команда перезагрузки сервиса (например `systemctl reload nginx`)

Алгоритм деплоя:
1. Валидация сертификата через `openssl x509 -noout` и ключа через `openssl rsa -noout`
2. Бэкап текущих файлов по `deploy_path` (если существуют)
3. Запись новых файлов на хост через `nsenter`
4. Выполнение `reload_cmd`
5. Откат из бэкапа при ошибке reload

**Файлы:**
- `node/app/models/ssl.py` — Pydantic модели
- `node/app/services/ssl_manager.py` — логика деплоя
- `node/app/routers/ssl.py` — API роутер

### Firewall Profiles

Приём и атомарное применение UFW-профилей от панели. Защищён `asyncio.Lock` — одновременный apply невозможен.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/firewall/profile/apply | Применить профиль UFW |
| GET | /api/firewall/profile/state | Текущее состояние UFW + hash |

**`POST /api/firewall/profile/apply`** — принимает `ProfileApplyRequest`:
- `rules` — массив правил (`ProfileRule[]`)
- `default_incoming`, `default_outgoing` — политика UFW по умолчанию
- `force` — обойти node-API-port-guard (default: false)

Алгоритм apply:
1. Node-API-port-guard: если `default_incoming != 'allow'` и в правилах нет `allow 9100/tcp IN` и `force=False` — возвращает ошибку "Allow rule for node API port 9100/tcp missing — panel will lose connection to node. Use force=true to apply anyway". Константа `NODE_API_PORT = 9100`. Правило `with_from_ip` допустимо — проверяется только наличие allow-правила для порта, без требования `from any`.
2. `_backup_state()` — снимок текущего UFW в `/etc/monitoring/ufw_backup_<timestamp>.json` (через nsenter); хранится максимум `MAX_BACKUPS=5`
3. `ufw reset` → установка политик → применение правил → `ufw enable`
4. При любой ошибке — `_restore_state(backup_path)` (автоматический rollback)

**Идемпотентность `add_advanced_rule()`:**

Метод `add_advanced_rule()` принимает параметр `skip_duplicate_check: bool = False`. Перед запуском ufw вызывает `_rule_already_present(port, protocol, action, from_ip, direction)` — проверяет активные правила через `list_rules`. Если идентичное правило уже существует и `skip_duplicate_check=False`, команда ufw не выполняется, возвращается `(True, "Rule already exists: ...", None)`. Результат success=True сохраняется намеренно: вызывающий код (например haproxy_manager) проверяет только флаг успеха.

`_apply_rules_list()` (путь apply_profile) вызывает `add_advanced_rule` с `skip_duplicate_check=True` — правила добавляются на чистый UFW после reset, лишние проверки `ufw status` не нужны.

Хелпер `_normalize_from(from_ip)`: нормализует источник правила — пустая строка, `any`, `anywhere` → `'anywhere'`. Используется при сравнении в `_rule_already_present`.

**`GET /api/firewall/profile/state`** — возвращает `ProfileStateResponse`:
- `rules` — активные правила UFW
- `default_incoming`, `default_outgoing` — текущие политики
- `rules_hash` — SHA256-хэш текущего состояния (для сравнения с хэшем профиля в панели)

**Node-API-port-guard:**

Три уровня защиты от потери связи панели с нодой:
1. Панель автозаполняет новый профиль правилом для порта 9100 при создании
2. Панель показывает баннер-предупреждение и индикатор-иконку в UI
3. Нода отклоняет apply, если нет `allow 9100/tcp IN` и `default_incoming != allow`, и `force=False`

**Бэкапы UFW:**

Хранятся в `/etc/monitoring/ufw_backup_<timestamp>.json` на хост-системе (через nsenter). При превышении `MAX_BACKUPS=5` старые удаляются.

**Автоустановка UFW:**

Перед применением профиля `apply_profile` проверяет наличие `ufw` на хосте (`command -v ufw`). Если `ufw` не установлен — нода автоматически ставит его через `apt-get install -y -qq ufw` (сначала из кеша, при неудаче — `apt-get update` и повтор). Если установить не удалось — apply возвращает понятную ошибку «UFW недоступен на хосте: ...» вместо сообщения nsenter.

**Файлы:**
- `node/app/models/firewall_profile.py` — Pydantic модели
- `node/app/services/firewall_manager.py` — `FirewallManager`: `apply_profile`, `_ensure_ufw`, `_ufw_available`, `_install_ufw`, `_run_host`, `_backup_state`, `_restore_state`, `compute_rules_hash`, `get_full_state`, `_rule_already_present`, `_normalize_from`
- `node/app/routers/firewall_profile.py` — API роутер (prefix `/api/firewall`)
- `node/app/main.py` — регистрация роутера с `verify_api_key`

### Анти-DDoS

Многослойная защита от DDoS-атак: дежурный режим без лимитов → аварийный режим с iptables-правилами в отдельной цепочке `ANTIDDOS` → автодетект атаки локальным watchdog-сервисом. Вся логика правил и детекции живёт в одном host-скрипте `configs/ddos-watchdog.sh` — нода лишь дёргает его CLI-команды через `nsenter`, поэтому набор правил идентичен независимо от того, кто включил режим (watchdog или панель).

Без auth-зависимости (как остальные роутеры ноды — mTLS терминируется на nginx перед uvicorn).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/antiddos/status | Текущее состояние: installed, mode (on/off), source (auto/manual/none), since, reason, watchdog (on/off), watchdog_active, client_ports, version |
| POST | /api/antiddos/emergency | Включить/выключить аварийный режим вручную (`source=manual` — автоматика не снимает) |
| POST | /api/antiddos/watchdog | Включить/выключить автодетект (сервис watchdog продолжает работать, но не трогает правила) |
| POST | /api/antiddos/whitelist/sync | Полная замена ipset-набора `antiddos_allow` (принимает `ips: string[]`) |
| POST | /api/antiddos/install | Установить/обновить `ddos-watchdog.sh` + systemd-сервис на хосте, включить (`daemon-reload` → `enable` → `restart`) |
| GET | /api/antiddos/client-ports | Автоопределённые клиентские TCP-порты (слушающие, кроме SSH (автоопределяется), 9100, 7500) |

**Аварийный режим (цепочка `ANTIDDOS`, джамп из INPUT только пока активен), порядок правил документированной схемой netfilter:**
1. DROP/ACCEPT по temp-блоклисту (`blocklist_temp`, если ipset-набор существует)
2. ACCEPT по whitelist (`antiddos_allow`, ipset `hash:net`)
3. ACCEPT established/related соединений
4. ACCEPT SSH (порт автоопределяется, см. ниже), nginx mTLS API (9100) и внутренний uvicorn-API ноды (7500) — никогда не дропаются
5. На автоопределённые клиентские порты — **SYNPROXY** (проверка TCP-рукопожатия до создания conntrack-записи, гасит SYN-флуд со спуфнутых IP; best-effort — если `xt_SYNPROXY`/`nf_synproxy_core` недоступны, шаг пропускается). `--wscale`/`--mss` вычисляются `tune-sysctl.sh` из реальных `rmem_max` и MTU хоста — захардкоженные значения зажимали бы окно проксируемых соединений и были бы неверны на туннелированном пути
6. DROP INVALID (эффективно вместе с `nf_conntrack_tcp_loose=0` из системных оптимизаций)
7. Не-SYN пакеты в состоянии NEW — DROP
8. hashlimit на клиентские порты — лимит новых соединений/сек с одного IP

Raw-правила `--notrack` (снимают SYN с трекинга до SYNPROXY) ставятся **последними** и только если правило SYNPROXY принято в каждой группе портов; при частичном отказе снимаются. Порядок «SYNPROXY раньше DROP INVALID» критичен: стоящий первым DROP INVALID терял бы завершающий handshake ACK клиента — SYN снят с трекинга в raw-таблице, ACK не находит записи conntrack и становится INVALID, до SYNPROXY не доходя никогда; при `nf_conntrack_tcp_loose=0` это полный блэкхол новых соединений на всё время аварийного режима — ровно тогда, когда нода должна принимать легитимных клиентов. `synproxy_available()` проверяет в одноразовой цепочке обе половины — доступность raw-таблицы **и** существование таргета SYNPROXY (проверка одной raw-таблицы пропустила бы ядро без `xt_SYNPROXY`: правила `--notrack` встали бы, `-j SYNPROXY` молча упал бы — тот же блэкхол без единой строки в логе), плюс `tcp_timestamps=1` (SYNPROXY кодирует wscale/MSS/SACK в timestamp).

**`connlimit` не используется.** `xt_connlimit` обходит conntrack-бакет источника на каждом NEW-пакете — дорожает ровно во время атаки — а лимит вида `--connlimit-above 100` на `/32` карал бы CGNAT-адреса операторов и любого клиента без Mux. Скорость ограничивает hashlimit, стоячее количество — conntrack-таймауты.

**Разбивка портов на группы (`build_chain`):** `iptables -m multiport --dports` принимает не более 15 портов на правило. Busy Xray-нода может слушать 30+ клиентских инбаундов, поэтому `detect_client_ports` разбивается на группы по ≤15 портов, и для каждой группы генерируется свой набор правил SYNPROXY/hashlimit. Хэш-таблица hashlimit **одна общая** на все группы (`--hashlimit-name ad_emg`) с настраиваемой `--hashlimit-srcmask` (`HASHLIMIT_SRCMASK`, по умолчанию 32) — отдельная таблица на группу умножала бы эффективный лимит на число групп (нода с 60 портами получила бы 4×`NEWRATE` и вчетверо больше памяти htable).

**Автоопределение SSH-порта (`detect_ssh_ports()`):** захардкоженный порт 22 в never-drop оставил бы ноду с нестандартным SSH-портом без ACCEPT для реального порта — тот попал бы под hashlimit клиентских портов. Порт(ы) определяются из трёх источников и объединяются: директива `Port` в `/etc/ssh/sshd_config` и `/etc/ssh/sshd_config.d/*.conf`; `ListenStream=` в systemd socket-активации (`ssh.socket` и override'ы — дефолт Ubuntu 24); живые sshd-листенеры через `ss -H -tlnp` (грепом по `sshd`). Если ни один источник не дал результата — откат на 22. `effective_never_drop()` объединяет статические management-порты (`NEVER_DROP_PORTS="9100 7500"`) с автоопределёнными SSH-портами (дедуп) — используется и при исключении клиентских портов (`detect_client_ports`), и при генерации ACCEPT-правил (`build_chain`).

Джамп ставится только на время активного режима — в дежурном режиме никаких дополнительных правил и накладных расходов.

**Watchdog (автодетект, `ddos-watchdog.sh loop` — systemd-сервис `ddos-watchdog.service`):**
- Сигналы читаются из `/proc` каждые ~10 сек: рост `insert_failed` conntrack (реальные дропы), заполнение conntrack-таблицы (%, слабый намёк), рост `SyncookiesSent` за цикл, pps при малом среднем размере пакета, softirq% (суммарно и по самому загруженному ядру отдельно), дропы из `/proc/net/softnet_stat` и `ListenDrops`/`ListenOverflows` из `/proc/net/netstat` — оба сильные сигналы, специфичнее чем pps: первый прямо означает «ядро теряет пакеты» (именно это делает осмысленным сниженный `netdev_max_backlog`), второй — переполнение очереди accept
- Сильный сигнал (резкий рост SyncookiesSent, `insert_failed`, дропы softnet/listen) включает аварийный режим немедленно; слабые сигналы — только после устойчивого удержания ~45 сек (защита от ложных срабатываний на вечернем пике)
- **Conntrack — сигнал по реальным дропам, не по заполнению.** «Заполнение ≥ порога» само по себе — только слабый намёк при near-exhaustion (`CONNTRACK_PCT=90`); реальный сигнал атаки — рост `insert_failed` (`nf_conntrack: table full, dropping packet`), суммированного по всем CPU из `/proc/net/stat/nf_conntrack`, дельта ≥ `CONNTRACK_DROP_DELTA` (=50/цикл). Если на ноде часто держится высокое заполнение conntrack без реальных дропов — это признак отсутствия sysctl-оптимизаций; вкладка «Оптимизации» поднимает `conntrack_max` и заполнение падает до единиц процентов.
- **Пороги — не абсолютные константы.** `tune-sysctl.sh` пишет их в `/opt/monitoring/antiddos/config.auto` по факту CPU/RAM хоста; порядок подключения — дефолты скрипта → `config.auto` → `/opt/monitoring/antiddos/config` (последним, выигрывает оператор). `SOFTIRQ_PCT` корректируется по числу ядер в «обратную» сторону: `/proc/stat` даёт уже нормализованный по CPU агрегат, поэтому 50% на 2 ядрах — это одно занятое ядро (штатный вечерний пик), а на 64 ядрах — 32 ядра в softirq (катастрофа); малым хостам (`CPUS≤4`) порог выше (`SOFTIRQ_PCT=70`), а не ниже.
- Автовыключение — после ~15 мин без сигналов
- Ручной пин (`source=manual`, включённый через `POST /api/antiddos/emergency`) автоматика не снимает — выключить может только явный вызов `POST /api/antiddos/emergency {enabled: false}`
- Выключение автодетекта (`watchdog=off`, через `POST /api/antiddos/watchdog {enabled: false}`) в цикле `loop` снимает активный **авто**-аварийный режим (`disable_mode`) — нода возвращается в дежурный режим. Ручные пины (`source=manual`) обрабатываются раньше в цикле и этим не затрагиваются.
- **Self-heal**: если сторонний процесс (например применение Firewall Profile через `ufw --force reset`) снёс джамп в `ANTIDDOS`, watchdog восстанавливает его в течение одного цикла
- **Self-check достижимости**: пока аварийный режим активен, каждый цикл проверяет `http://127.0.0.1:7500/health` и наличие живой SSH-сессии; `SELF_CONFIRM_FAILS=3` подряд неудачи снимают цепочку — ошибка в правилах на позиции INPUT 1 не должна оставлять сервер недоступным

**Отладочные верби:** `dry-run` (печатает точные команды `iptables`, ничего не выполняя — цепочка встаёт на INPUT-позицию 1, поэтому проверить её до применения ценно) и `self-test` (структурно доказывает, что SYNPROXY стоит раньше INVALID DROP и что raw `--notrack` есть тогда и только тогда, когда есть правило SYNPROXY — штатный `curl` этого не докажет, он проходит и когда SYNPROXY молча отсутствует).

**Whitelist:** ipset-набор `antiddos_allow` (`hash:net`), хранится на диске ноды (`/opt/monitoring/antiddos/whitelist.json`) — переживает ребут и недоступность панели. ACCEPT по нему действует только в аварийном режиме (в дежурном режиме проходит весь трафик без ipset-проверок). До первого опроса панели набор засеян IP панели и `127.0.0.0/8`. Панель наполняет набор ежечасно через `POST /api/antiddos/whitelist/sync`.

**CLI-команды `ddos-watchdog.sh`** (вызываются нодой через `nsenter`, доступны и вручную на хосте): `loop`, `enable-manual`, `disable-manual`, `watchdog-on`, `watchdog-off`, `apply`, `clear`, `selfheal`, `whitelist-sync` (IP через stdin), `detect-ports`, `dry-run`, `self-test`, `version`, `status`. Состояние — `/opt/monitoring/antiddos/state.json` (mode/source/since/reason/watchdog).

**Версионирование watchdog-скрипта:** константа `WATCHDOG_VERSION` в шапке `ddos-watchdog.sh` (сейчас `"2.0.0"`) — команда `status` возвращает её полем `version`, отдельная команда `version` печатает только её. Значение растёт при изменении логики скрипта; панель сверяет его с версией, установленной на ноде (см. «Установка» ниже).

**Установка — по умолчанию.** `install.sh` вызывает `install_antiddos_watchdog()` сразу после применения системных оптимизаций — свежая нода получает watchdog без какого-либо участия панели, в состоянии покоя (`watchdog=on`, аварийный режим выключен). Существующие ноды получают его от панели — через `apply-update.sh` это не идёт, потому что rsync обновления агента исключает `configs/`: репозиторных конфигов рядом с нодой нет. Панель в фоновом опросе статуса (см. panel/DOCUMENTATION.md) видит, что нода отвечает на `/api/antiddos/status`, и сама вызывает `POST /api/antiddos/install`, если watchdog не установлен либо его `version` отличается от актуальной версии `ddos-watchdog.sh` на GitHub. Backend-эндпоинты ручной установки (`POST /antiddos/install-all` в панели, `POST /api/antiddos/install` на ноде) остаются доступны по API.

**Требования к ядру:** `xt_SYNPROXY`/`nf_synproxy_core`, `hashlimit`. На Ubuntu 24 iptables работает через nft-бэкенд (iptables-nft) — тот же стек, что UFW/Docker/ipset_manager.

**Файлы:**
- `configs/ddos-watchdog.sh` — весь host-скрипт: правила, детект, self-heal, CLI
- `configs/ddos-watchdog.service` — systemd-unit (`Type=simple`, `Restart=always`)
- `node/app/services/antiddos_manager.py` — обёртка над скриптом через `get_host_executor()` (nsenter); валидация IP/CIDR перед whitelist-sync; `install()` пишет скрипт+сервис на хост и запускает
- `node/app/routers/antiddos.py` — API роутер (prefix `/api/antiddos`)
- `node/app/main.py` — регистрация роутера

## Производительность

### Async сбор метрик

`node/app/services/metrics_collector.py`: `get_all_metrics()` выполняет 7 методов сбора (CPU, RAM, диск, сеть, процессы и др.) параллельно через `asyncio.gather()` + `asyncio.to_thread()` для блокирующих вызовов psutil.

### CPU: прогрев замера при старте, защита от мусора коротких интервалов

Коллектор метрик — ленивый синглтон (создаётся при первом запросе `/api/metrics`). `psutil.cpu_percent(interval=None)` меряет нагрузку от предыдущего вызова — на интервалах в единицы миллисекунд гранулярность jiffies ядра (~10мс) даёт мусор вида «одно ядро 100%, остальные 0%» (это ядро, на котором в этот момент выполнялся процесс).

- `prime_cpu_baseline()` — блокирующий стартовый замер (~0.25с) per-CPU, вызывается из `lifespan` (`node/app/main.py`) через `asyncio.to_thread` до приёма запросов: первый же запрос метрик после старта получает реальные значения вместо мусора нулевого интервала.
- `_sample_per_cpu()` — пересэмплирование `psutil.cpu_percent` не чаще `CPU_SAMPLE_MIN_INTERVAL = 0.5`с под `threading.Lock`; более ранним или конкурентным запросам (`get_cpu_info` выполняется в тред-пуле) отдаётся последний валидный замер вместо укороченного гонкой интервала между двумя близкими по времени запросами.

### Async трафик и iptables

`node/app/services/traffic_collector.py`:
- Subprocess-вызовы идут через `asyncio.create_subprocess_exec()`, без блокирующего `subprocess.run()`
- Чтение `/proc/net/dev` выполняется через `asyncio.to_thread()`
- Все iptables-методы асинхронные

### IPSet: пакетное применение одним `ipset restore`

Per-IP применение (2 subprocess-вызова `ipset add`/`del` на запись) на списке в десятки тысяч записей заняло бы десятки минут, поэтому:

- `ipset_manager.py`: все массовые операции (`sync`, `bulk_add`, `bulk_remove`, `sync_allow`, загрузка permanent/allow-списков из `blocklist.json` при старте) собирают diff и применяют его одним вызовом `ipset -exist restore` (`_run_ipset_restore()`) — весь diff применяется за доли секунды независимо от размера списка.
- `routers/ipset.py`: все эндпоинты — синхронные `def`, FastAPI выполняет их в threadpool, поэтому длинная блокирующая операция не держит event loop и не замораживает остальные эндпоинты ноды (в `async def`-хендлере синхронный subprocess завесил бы **все** эндпоинты node-API — nginx отдавал бы 504 на любой запрос к ноде).
- Мутации ipset-сетов сериализованы `threading.Lock` (`_mutate_lock`) — параллельный sync с панели и ручной bulk-add не перемешивают diff-ы.

### SQLite PRAGMA оптимизации

В `traffic_collector.py` при открытии соединения применяются:
- `synchronous=NORMAL` — меньше fsync без риска потери данных при нормальном завершении
- `cache_size=-65536` — 64 MB page cache в памяти
- `temp_store=MEMORY` — временные таблицы в RAM
- `mmap_size=268435456` — 256 MB memory-mapped I/O

## Системные оптимизации

Установка/обновление ноды **не** применяет и не меняет оптимизации сама по себе — только через UI панели (раздел **Обновления**) или главный установщик (`monitoring` → пункт 7). После первого применения рендерер сам повторно накатывает значения на **каждой загрузке** хоста (`ExecStartPre` активного `*-tune.service`) — ресайз VPS подхватывается без повторного клика в панели.

Категории тюнинга: IPv6 (отключение), BBR + fq_codel, TCP/UDP-буферы, Busy Polling, TCP ECN, очереди (`somaxconn`/`netdev_max_backlog`), TCP performance (fastopen, no slow start after idle, MTU probing), TIME-WAIT (tw_reuse), syncookies/rp_filter/ICMP-protection, conntrack, лимиты файловых дескрипторов. Все размерные значения из этого списка вычисляются из MemTotal/nproc/MTU/скорости линка хоста единым рендерером `tune-sysctl.sh` (`configs/`, версия формулы отдельная от `configs/VERSION` — `FORMULA_VERSION`, сейчас `1.0.0`) — не хардкод и не флат-число, одинаковое для любого сервера.

### Контракт с панелью

`POST /api/system/optimizations/apply` (`ApplyOptimizationsRequest`) везёт **входные данные** рендерера — сам `tune-sysctl.sh`, `profiles/common.base.conf`, `profiles/<профиль>.base.conf`, `limits.tmpl`, `systemd-limits.tmpl` (+ опционально содержимое NIC-скриптов) — а не готовый sysctl.conf: панель не знает MemTotal ноды, поэтому один отрендеренный файл не может подойти и на 4 ГБ, и на 248 ГБ. Нода пишет входные файлы на хост в `/opt/monitoring/scripts/` и `/opt/monitoring/configs/profiles/` через `write_host_file()` (запись через `nsenter` + base64, не heredoc — heredoc обрезал бы файл, если строка контента совпадёт с делимитером), затем сама вызывает `tune-sysctl.sh render <профиль>` и возвращает результат верификации в ответе. Панель отправляет этот контракт только нодам версии ≥ `10.6.0` — см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md).

### Верификация и дрейф

`verify_sysctl_values()` читает ожидания из `/opt/monitoring/configs/tuning-facts.json`, который пишет рендерер (`expected_from_facts()`: computed+static минус ключи, которыми в рантайме владеет Xray), а не из захардкоженной таблицы. Все значения читаются одним `nsenter`-вызовом вместо процесса на ключ; многозначные ключи нормализуются (`sysctl -n net.ipv4.tcp_mem` отдаёт поля через таб, файл — через пробел); hashsize conntrack проверяется порогом из facts, а не точным равенством. `rp_filter` и `disable_ipv6` исключены из проверки — Xray переписывает их в рантайме при поднятии WireGuard-аутбаунда.

Поскольку значения выводятся из MemTotal/nproc, у ресайзнутого VPS файл на диске может совпадать с текущей версией `configs/VERSION` и при этом быть неверным. `read_tuning_drift()` пересчитывает хеш живых фактов хоста и сравнивает с хешем, записанным при последнем рендере; `GET /api/system/versions` отдаёт `optimizations.drift`/`drift_detail`/`formula_version` — так панель узнаёт о дрейфе раньше, чем сработает следующий загрузочный ре-рендер.

### Цепочка файловых дескрипторов

Рендерер утверждает её численно и отказывается писать при нарушении: `nginx worker_rlimit_nofile ≤ container nofile ≤ NOFILE_LIMIT == limits.conf nofile == DefaultLimitNOFILE ≤ fs.nr_open == fs.file-max`, и `haproxy maxconn ≤ (RLIMIT_NOFILE HAProxy − 1024) / 3` (см. «Лимит соединений (maxconn)» в разделе HAProxy выше). `fs.nr_open` поднимается **до** записи `limits.conf` — иначе PAM ломается о значение выше текущего `nr_open`. `node/docker-compose.yml` задаёт явные `ulimits: nofile 65536` для обоих сервисов (без явного лимита наследовалось бы ~1073741816 от dockerd) и монтирует `/opt/monitoring:ro`, чтобы контейнер видел `tuning-facts.env`/`.json`.

**Тесты:** `node/tests/test_verify_sysctl.py` — 14 тестов на stdlib `unittest` (подхватываются и pytest): нормализация значений, сборка ожидаемого набора из facts, порог hashsize, отсутствующий/битый facts-файл, чтение всех ключей одним вызовом. `configs/tests/render-matrix.sh` — 252 комбинации размеров хоста × профилей на стороне самого рендерера, см. корневой [DOCUMENTATION.md](../DOCUMENTATION.md).

### RPS/RFS Network Tuning

При установке ноды автоматически настраивается **RPS/RFS** — распределение сетевой нагрузки по ядрам CPU:

- **RPS (Receive Packet Steering)** — распределяет входящие пакеты по всем ядрам CPU
- **RFS (Receive Flow Steering)** — оптимизирует привязку потоков к ядрам
- **XPS (Transmit Packet Steering)** — распределяет исходящие пакеты по очередям TX

**Systemd сервис**: `network-tune.service`
- Автоматически определяет основной сетевой интерфейс
- Вычисляет оптимальные значения на основе количества ядер CPU
- Запускается при каждой загрузке системы

```bash
# Статус сервиса
systemctl status network-tune

# Ручной перезапуск (после изменения железа)
systemctl restart network-tune

# Логи
journalctl -u network-tune
```

`net.core.rps_sock_flow_entries` задаётся только рендерером — NIC-скрипты (`network-tune.sh`/`multiqueue-tune.sh`/`hybrid-tune.sh`) её не трогают. `rps_flow_cnt` на каждой очереди обязан быть степенью двойки (`pow2_floor(entries / rx_queues)`) — иначе ядро молча отклоняет RFS; источник `entries` — `tuning-facts.env`.

**Примечание**: Настройки универсальны для любых машин (от 1GB RAM до 128GB+). При проблемах с сетью во время установки/обновления IPv6 отключается автоматически.

## SSL сертификаты

- Создаются через certbot (установлен в API контейнере)
- Хранятся на хосте: `/etc/letsencrypt/live/{domain}/`
- HAProxy использует combined.pem (fullchain + privkey)
- **Автопродления нет.** Сертификат, выпущенный со страницы HAProxy, истекает через 90 дней, если не продлить его вручную (кнопка «Продлить» на той же странице). Wildcard-сертификаты это не затрагивает — у них отдельный механизм на стороне панели.
- HAProxy останавливается на время выпуска/продления **только если** какое-то правило слушает порт 80 — certbot в режиме standalone требует этот порт свободным. Правила на 443 и остальных портах при продлении не рвутся.

> При обновлении агент автоматически удаляет с хоста legacy-файлы `/etc/cron.d/certbot-renew` и `/opt/monitoring-node/renew-certs.sh`, если они там есть: этот крон каждую ночь безусловно делал `systemctl stop haproxy` перед `certbot renew`, обрывая все туннели на всех портах.

## Команды

```bash
# Логи API
docker compose logs -f

# Перезапуск API
docker compose restart

# Остановка API
docker compose down

# HAProxy (нативный сервис)
systemctl status haproxy    # Статус
systemctl start haproxy     # Запуск
systemctl stop haproxy      # Остановка
systemctl restart haproxy   # Перезапуск
systemctl reload haproxy    # Reload конфига

# Логи HAProxy
journalctl -u haproxy -n 100

# Изменить IP панели
ufw delete allow from OLD_IP to any port 9100 proto tcp
ufw allow from NEW_IP to any port 9100 proto tcp

# Ручное обновление
./update.sh

# Обновление до конкретной версии
./update.sh v1.1.0

# Запуск менеджера установки
monitoring
```
