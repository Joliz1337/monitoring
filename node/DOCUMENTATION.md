# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — история по интерфейсам и портам (SQLite + iptables)
- **HAProxy** — управление нативным systemd сервисом, конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API
- **IPSet Blocklist** — блокировка IP/CIDR через ipset (постоянный и временный списки)
- **Терминал** — выполнение произвольных команд и bash-скриптов на хосте (max 65000 символов)
- **Remnawave** — проверка доступности контейнера remnanode
- **Синхронизация времени** — установка IANA timezone через `timedatectl`, включение NTP и принудительная синхронизация через `systemd-timesyncd`
- **SSH Security** — управление SSH-безопасностью сервера: настройки sshd, fail2ban, SSH-ключи
- **Wildcard SSL** — приём и деплой wildcard сертификатов от панели: запись файлов на хост, бэкап, откат при ошибке reload, валидация PEM через openssl
- **Firewall Profiles** — атомарное применение UFW-профилей от панели: backup → reset → apply → enable, авторолбэк при ошибке, node-API-port-guard (порт 9100), drift-детекция по SHA256-хэшу
- **Анти-DDoS** — многослойная защита: дежурный режим без лимитов, аварийный режим (SYNPROXY + connlimit + hashlimit в отдельной iptables-цепочке `ANTIDDOS`), автодетект атаки по сигналам из `/proc` (watchdog), whitelist на ipset, переживающий ребут и недоступность панели

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

Прямые вызовы `systemctl start docker` внутри `ensure_docker_running` работают без изменений — гард не влияет на прямые вызовы systemctl.

## HAProxy

HAProxy работает как **нативный systemd сервис** на хосте (не в Docker). При установке ноды HAProxy устанавливается автоматически если не установлен.

**Конфиг**: `/etc/haproxy/haproxy.cfg`

**DNS Resolver**: В базовом конфиге включена секция `resolvers mydns` (DNS 1.1.1.1 + 8.8.8.8, hold valid 60s). Если target правила — доменное имя (а не IP), к server-линии автоматически добавляются параметры `resolvers mydns resolve-prefer ipv4 init-addr none`, что обеспечивает периодическое обновление IP домена без перезапуска HAProxy.

**Wildcard SSL**: Поле `use_wildcard: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` используется родительский домен для поиска сертификата вместо точного. Например, для правила с `cert_domain=sub.nexyonn.com` и `use_wildcard=True` нода применит сертификат `nexyonn.com` (покрывает `*.nexyonn.com`). Вспомогательные методы: `_extract_parent_domain()` — извлечение родительского домена, `_resolve_cert_domain()` — выбор итогового домена сертификата с учётом флага `use_wildcard`.

**PROXY protocol (accept_proxy)**: Поле `accept_proxy: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` добавляет `accept-proxy` к bind-строке frontend — нода принимает PROXY protocol header от вышестоящего HAProxy. Применяется к TCP и HTTPS правилам, в одиночном режиме и в режиме балансировщика. Парсинг `accept-proxy` из существующего конфига поддерживается; `update_rule()` обрабатывает изменение через пересоздание блока правила.

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
│   │   └── firewall_profile.py  # Pydantic модели: ProfileRule, ProfileApplyRequest/Response, ProfileStateResponse
│   ├── routers/          # API эндпоинты (metrics, haproxy, traffic, speedtest, ssh, ssl, firewall, antiddos и др.)
│   └── services/         # Сбор метрик, HAProxy, трафик, speedtest runner, SSH менеджер
│       ├── ssl_manager.py          # Деплой wildcard сертификатов: запись на хост, бэкап, откат, валидация
│       ├── firewall_manager.py     # UFW: apply_profile, backup/restore, compute_rules_hash, get_full_state
│       └── antiddos_manager.py     # Тонкая обёртка над ddos-watchdog.sh (nsenter): enable/disable emergency, watchdog, whitelist sync
├── scripts/
│   └── apply-update.sh   # Логика обновления (запускается из свежего репо)
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
| POST | /api/system/update | Запуск обновления (target_ref: branch/tag/commit, по умолчанию main) |
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
Панель использует этот endpoint для получения всей информации о ноде одним запросом вместо двух.

**Выполнение команд на хосте**:

Эндпоинт `/api/system/execute` позволяет выполнять произвольные shell-команды и многострочные bash-скрипты на хост-системе через `nsenter`. Работает из Docker контейнера благодаря `privileged: true` и `pid: host`. Максимальная длина поля `command` — 65000 символов.

**PATH**: Все команды выполняются с расширенным PATH (`/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), что позволяет использовать snap-пакеты (speedtest, etc.) и локально установленные бинарники.

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
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка)
3. Запускает `update.sh` из склонированной папки
4. `update.sh` скачивает репо и запускает **свежий** `scripts/apply-update.sh` из скачанной версии
5. `apply-update.sh` выполняет обновление: миграции, копирование файлов, сборку, запуск
6. Контейнер удаляется после завершения

Обновление **всегда** использует актуальную версию логики из GitHub (двойная загрузка гарантирует свежесть).

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
| GET | /api/haproxy/certs/cron/status | Статус автообновления |
| POST | /api/haproxy/certs/cron/enable | Включить автообновление |
| POST | /api/haproxy/certs/cron/disable | Выключить автообновление |

### Remnawave

Проверка доступности контейнера `remnanode`.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/status | Статус контейнера remnanode (available: true/false) |

Используется панелью для определения поля `has_xray_node` у сервера (обновляется каждые 2 минуты).

### IPSet Blocklist

Блокировка IP/CIDR через ipset. Два типа списков:
- **Блок-список** — `blocklist_permanent` (постоянный) и `blocklist_temp` (временный с таймаутом); направления: in (INPUT) и out (OUTPUT).
- **Белый список (allowlist)** — доверенные IP/CIDR, которые **всегда** проходят через ноду вне зависимости от блокировок.

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
| POST | /api/ipset/sync | Синхронизация блок-списка (замена всего списка) |
| POST | /api/ipset/allowlist/sync | Синхронизация белого списка (замена) |

**`POST /api/ipset/allowlist/sync`** — принимает `AllowSyncRequest`:
- `ips` — массив IP/CIDR для белого списка
- `direction` — `"in"` или `"out"`

**Поля в `GET /api/ipset/status`** — добавлены `incoming.allow_count` и `outgoing.allow_count` (количество записей в allowlist).

**Особенности:**
- Тип ipset: `hash:net` (поддержка IP и CIDR)
- Правила iptables блок-списка: `INPUT/OUTPUT -m set --match-set blocklist_* src/dst -j DROP`
- Правила allowlist: `-I INPUT 1 ... -j ACCEPT` / `-I OUTPUT 1 ... -j ACCEPT` (позиция 1, выше DROP)
- Все постоянные правила сохраняются в `/var/lib/monitoring/blocklist.json` (ключи `in_allow`, `out_allow` для белого списка)
- При старте ноды: постоянные правила восстанавливаются, временный список пустой, allowlist загружается из персиста

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

Метод `add_advanced_rule()` получил параметр `skip_duplicate_check: bool = False`. Перед запуском ufw вызывает `_rule_already_present(port, protocol, action, from_ip, direction)` — проверяет активные правила через `list_rules`. Если идентичное правило уже существует и `skip_duplicate_check=False`, команда ufw не выполняется, возвращается `(True, "Rule already exists: ...", None)`. Результат success=True сохраняется намеренно: вызывающий код (например haproxy_manager) проверяет только флаг успеха.

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

**Аварийный режим (цепочка `ANTIDDOS`, джамп из INPUT только пока активен):**
1. ACCEPT по whitelist (`antiddos_allow`, ipset `hash:net`) — первым
2. ACCEPT established/related соединений
3. ACCEPT SSH (порт автоопределяется, см. ниже), nginx mTLS API (9100) и внутренний uvicorn-API ноды (7500) — никогда не дропаются
4. DROP INVALID (эффективно вместе с `nf_conntrack_tcp_loose=0` из системных оптимизаций)
5. На автоопределённые клиентские порты — SYNPROXY (проверка TCP-рукопожатия до создания conntrack-записи, гасит SYN-флуд со спуфнутых IP; best-effort — если `xt_SYNPROXY`/`nf_synproxy_core` недоступны, шаг пропускается), connlimit (лимит одновременных соединений с одного IP) и hashlimit (лимит новых соединений/сек с одного IP)

**Разбивка портов на группы (`build_chain`):** `iptables -m multiport --dports` принимает не более 15 портов на правило. Busy Xray-нода может слушать 30+ клиентских инбаундов, поэтому `detect_client_ports` разбивается на группы по ≤15 портов, и для каждой группы генерируется свой набор правил SYNPROXY/connlimit/hashlimit с уникальной hashlimit-таблицей (`--hashlimit-name antiddos1`, `antiddos2`, ...) — так все обнаруженные порты гарантированно покрыты лимитами, а не только первые 15.

**Автоопределение SSH-порта (`detect_ssh_ports()`):** раньше SSH в never-drop был захардкожен как порт 22 — на ноде с нестандартным SSH-портом реальный порт не получал бы ACCEPT и мог попасть под connlimit/hashlimit клиентских портов, а хардкод-22 просто вешал бы бесполезное правило. Порт(ы) определяются из трёх источников и объединяются: директива `Port` в `/etc/ssh/sshd_config` и `/etc/ssh/sshd_config.d/*.conf`; `ListenStream=` в systemd socket-активации (`ssh.socket` и override'ы — дефолт Ubuntu 24); живые sshd-листенеры через `ss -H -tlnp` (грепом по `sshd`). Если ни один источник не дал результата — откат на 22. `effective_never_drop()` объединяет статические management-порты (`NEVER_DROP_PORTS="9100 7500"`) с автоопределёнными SSH-портами (дедуп) — используется и при исключении клиентских портов (`detect_client_ports`), и при генерации ACCEPT-правил (`build_chain`).

Джамп ставится только на время активного режима — в дежурном режиме никаких дополнительных правил и накладных расходов.

**Watchdog (автодетект, `ddos-watchdog.sh loop` — systemd-сервис `ddos-watchdog.service`):**
- Сигналы читаются из `/proc` каждые ~10 сек: рост счётчика `insert_failed` conntrack (реальные дропы), заполнение conntrack-таблицы (%, слабый намёк), рост `SyncookiesSent` за цикл, pps при малом среднем размере пакета, загрузка softirq
- Сильный сигнал (например резкий рост SyncookiesSent или рост `insert_failed`) включает аварийный режим немедленно; слабые сигналы — только после устойчивого удержания ~45 сек (защита от ложных срабатываний на вечернем пике)
- **Conntrack — сигнал по реальным дропам, не по заполнению (`WATCHDOG_VERSION` 1.4.0).** Раньше «заполнение ≥ 80%» само по себе считалось сильным сигналом атаки — на боевой ноде это ложно сработало на просто занятой ноде (или ноде без применённых sysctl-оптимизаций, где `conntrack_max` дефолтный ~262k вместо 2M, и 81% там — нормальная нагрузка); аварийный режим conntrack не уменьшает, поэтому нода флапала по кругу без пользы. Теперь: `read_insert_failed()` суммирует счётчик `insert_failed` по всем CPU из `/proc/net/stat/nf_conntrack` (парсинг hex-строк вручную в bash — `mawk` не умеет `strtonum`); дельта `insert_failed` ≥ `CONNTRACK_DROP_DELTA` (=50/цикл) — сильный сигнал (таблица реально дропает пакеты). Заполнение таблицы стало только слабым намёком при `CONNTRACK_PCT` (поднят с 80 до 95% — near-exhaustion). Высокое заполнение без роста `insert_failed` обычно означает, что ноде не применены sysctl-оптимизации — вкладка «Оптимизации» поднимает `conntrack_max` до 2M и заполнение падает до единиц процентов.
- Автовыключение — после ~15 мин без сигналов
- Ручной пин (`source=manual`, включённый через `POST /api/antiddos/emergency`) автоматика не снимает — выключить может только явный вызов `POST /api/antiddos/emergency {enabled: false}`
- Выключение автодетекта (`watchdog=off`, через `POST /api/antiddos/watchdog {enabled: false}`) в цикле `loop` снимает активный **авто**-аварийный режим (`disable_mode`) — нода возвращается в дежурный режим. Ручные пины (`source=manual`) обрабатываются раньше в цикле и этим не затрагиваются. Раньше (до `WATCHDOG_VERSION` 1.1.0) при `watchdog=off` активный авто-режим только самолечился (`selfheal`) и оставался включённым — «выключить автодетект» не возвращало ноду в норму.
- **Self-heal**: если сторонний процесс (например применение Firewall Profile через `ufw --force reset`) снёс джамп в `ANTIDDOS`, watchdog восстанавливает его в течение одного цикла
- Тюнинг-пороги (интервал, connlimit/newrate, пороги детекции) заданы константами в шапке `ddos-watchdog.sh`, переопределяются файлом `/opt/monitoring/antiddos/config` без правки самого скрипта

**Whitelist:** ipset-набор `antiddos_allow` (`hash:net`), хранится на диске ноды (`/opt/monitoring/antiddos/whitelist.json`) — переживает ребут и недоступность панели. ACCEPT по нему действует только в аварийном режиме (в дежурном режиме проходит весь трафик без ipset-проверок). Панель наполняет набор ежечасно через `POST /api/antiddos/whitelist/sync`.

**CLI-команды `ddos-watchdog.sh`** (вызываются нодой через `nsenter`, доступны и вручную на хосте): `loop`, `enable-manual`, `disable-manual`, `watchdog-on`, `watchdog-off`, `apply`, `clear`, `selfheal`, `whitelist-sync` (IP через stdin), `detect-ports`, `version`, `status`. Состояние — `/opt/monitoring/antiddos/state.json` (mode/source/since/reason/watchdog).

**Версионирование watchdog-скрипта:** константа `WATCHDOG_VERSION` в шапке `ddos-watchdog.sh` (сейчас `"1.4.0"`) — команда `status` возвращает её полем `version`, отдельная команда `version` печатает только её. Значение растёт при изменении логики скрипта; панель сверяет его с версией, установленной на ноде (см. «Установка» ниже).

**Установка — zero-touch:** `install.sh`/обновление ноды по-прежнему **не** ставит watchdog напрямую — раскатка полностью на панели, без ручных действий и без кнопки в интерфейсе. Панель в фоновом опросе статуса (см. panel/DOCUMENTATION.md) видит, что нода отвечает на `/api/antiddos/status` (значит образ node-api уже новый), и сама вызывает `POST /api/antiddos/install`, если watchdog не установлен (`installed:false`) либо его `version` отличается от актуальной версии `ddos-watchdog.sh` на GitHub. Backend-эндпоинты ручной установки (`POST /antiddos/install-all` в панели, `POST /api/antiddos/install` на ноде) остаются доступны по API. Watchdog включён по умолчанию сразу после установки.

**Требования к ядру:** `xt_SYNPROXY`/`nf_synproxy_core`, `connlimit`, `hashlimit`. На Ubuntu 24 iptables работает через nft-бэкенд (iptables-nft) — тот же стек, что UFW/Docker/ipset_manager.

**Файлы:**
- `configs/ddos-watchdog.sh` — весь host-скрипт: правила, детект, self-heal, CLI
- `configs/ddos-watchdog.service` — systemd-unit (`Type=simple`, `Restart=always`)
- `node/app/services/antiddos_manager.py` — обёртка над скриптом через `get_host_executor()` (nsenter); валидация IP/CIDR перед whitelist-sync; `install()` пишет скрипт+сервис на хост и запускает
- `node/app/routers/antiddos.py` — API роутер (prefix `/api/antiddos`)
- `node/app/main.py` — регистрация роутера

## Производительность

### Async сбор метрик

`node/app/services/metrics_collector.py`: `get_all_metrics()` выполняет 7 методов сбора (CPU, RAM, диск, сеть, процессы и др.) параллельно через `asyncio.gather()` + `asyncio.to_thread()` для блокирующих вызовов psutil.

### Async трафик и iptables

`node/app/services/traffic_collector.py`:
- Все `subprocess.run()` заменены на `asyncio.create_subprocess_exec()`
- Чтение `/proc/net/dev` выполняется через `asyncio.to_thread()`
- Все iptables-методы каскадно асинхронизированы

### SQLite PRAGMA оптимизации

В `traffic_collector.py` при открытии соединения применяются:
- `synchronous=NORMAL` — меньше fsync без риска потери данных при нормальном завершении
- `cache_size=-65536` — 64 MB page cache в памяти
- `temp_store=MEMORY` — временные таблицы в RAM
- `mmap_size=268435456` — 256 MB memory-mapped I/O

## Системные оптимизации

Оптимизации **не применяются автоматически** при обновлении ноды. Применяются только:
- Через UI панели (раздел **Обновления**)
- Через главный установщик (`monitoring` → пункт 7)

Включают:
- **IPv6** — отключение (улучшает стабильность сети)
- **BBR + fq_codel** — лучшая комбинация для низкого джиттера и анти-bufferbloat
- **Буферы** — оптимизированы для низкого latency (16MB max вместо 512MB)
- **UDP буферы** — увеличены для игр и VoIP
- **Busy Polling** — снижает latency обработки пакетов
- **TCP ECN** — Explicit Congestion Notification (предотвращает потерю пакетов)
- **Очереди** — somaxconn, netdev_max_backlog оптимизированы
- **TCP Performance** — fastopen, no slow start after idle, MTU probing
- **TIME-WAIT** — 2M tw_buckets, tw_reuse
- **Anti-DDoS** — syncookies, rp_filter, ICMP protection, IGMP limits
- **Conntrack** — авто-масштабируемый по RAM, оптимизированные таймауты
- **File descriptors** — 10M nofile для всех пользователей

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

**Примечание**: Настройки универсальны для любых машин (от 1GB RAM до 128GB+). При проблемах с сетью во время установки/обновления IPv6 отключается автоматически.

**SSL auto-renewal** — cron для автообновления сертификатов (3:00 AM daily) настраивается при установке ноды.

## SSL сертификаты

- Создаются через certbot (установлен в API контейнере)
- Хранятся на хосте: `/etc/letsencrypt/live/{domain}/`
- Автоматически обновляются через cron (ежедневно в 3:00)
- При создании первого сертификата cron настраивается автоматически
- Логи обновления: `/var/log/certbot-renew.log`
- HAProxy использует combined.pem (fullchain + privkey)

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
