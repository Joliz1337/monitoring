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

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 2) Установить ноду
```

При установке скрипт запросит:
1. **Installer Token** — скопируйте из формы Add Server в панели (общий для всех нод, не меняется).
2. **IP-адрес панели** — для настройки firewall (UFW).

Можно передать токен через переменную окружения: `NODE_SECRET=<base64> bash deploy.sh`

> **Версия 9.0.0 — BREAKING:** аутентификация полностью переведена на mTLS. `X-API-Key` удалён. Нода при установке получает CA cert, серверный cert и ключ из Installer Token. Nginx требует от клиента валидный сертификат (`ssl_verify_client on`).

> **Версия 9.1.0:** все ноды теперь используют единый shared node cert (CN=`shared-node`). `NODE_NAME` берётся из `hostname` автоматически. Добавлен endpoint `POST /api/system/replace-node-cert` для приёма нового сертификата от панели (автомиграция без переустановки).

## HAProxy

HAProxy работает как **нативный systemd сервис** на хосте (не в Docker). При установке ноды HAProxy устанавливается автоматически если не установлен.

**Конфиг**: `/etc/haproxy/haproxy.cfg`

**DNS Resolver**: В базовом конфиге включена секция `resolvers mydns` (DNS 1.1.1.1 + 8.8.8.8, hold valid 60s). Если target правила — доменное имя (а не IP), к server-линии автоматически добавляются параметры `resolvers mydns resolve-prefer ipv4 init-addr none`, что обеспечивает периодическое обновление IP домена без перезапуска HAProxy.

**Wildcard SSL**: Поле `use_wildcard: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` используется родительский домен для поиска сертификата вместо точного. Например, для правила с `cert_domain=sub.nexyonn.com` и `use_wildcard=True` нода применит сертификат `nexyonn.com` (покрывает `*.nexyonn.com`). Вспомогательные методы: `_extract_parent_domain()` — извлечение родительского домена, `_resolve_cert_domain()` — выбор итогового домена сертификата с учётом флага `use_wildcard`.

**Load Balancing**: Каждое правило может быть балансировщиком (`is_balancer=True`) с несколькими backend-серверами. Dataclass'ы `BackendServer` и `BalancerOptions` зеркалят реализацию панели. `parse_rules()` в `node/app/services/haproxy_manager.py` восстанавливает multi-server правила из конфига. Роутер `node/app/routers/haproxy.py` использует хелпер `_rule_to_response()` для корректной сериализации балансировщиков в API-ответе. Pydantic-модели `BackendServerModel`, `BalancerOptionsModel` и расширенные `HAProxyRuleBase/Create/Update/Response` находятся в `node/app/models/haproxy.py`.

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
│   ├── main.py           # FastAPI приложение (без зависимостей verify_api_key — авторизация через nginx mTLS)
│   ├── config.py         # Pydantic Settings (api_key удалён, добавлен ca_cert_path)
│   ├── models/
│   │   └── ssl.py        # Pydantic модели: WildcardDeployRequest/Response, WildcardStatusResponse
│   ├── routers/          # API эндпоинты (metrics, haproxy, traffic, speedtest, ssh, ssl и др.)
│   └── services/         # Сбор метрик, HAProxy, трафик, speedtest runner, SSH менеджер
│       └── ssl_manager.py  # Деплой wildcard сертификатов: запись на хост, бэкап, откат, валидация
├── scripts/
│   ├── apply-update.sh   # Логика обновления (запускается из свежего репо)
│   └── network-tune.sh   # RPS/RFS оптимизация сети
├── nginx/                # Reverse proxy с SSL
├── docker-compose.yml
├── update.sh             # Скачивает репо и запускает apply-update.sh
└── deploy.sh
```

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| NODE_NAME | Имя ноды | берётся из `hostname` автоматически при установке (v9.1.0) |
| PANEL_IP | IP панели (для UFW) | задаётся при установке |
| CA_CERT_PATH | Путь к CA cert (для информации) | `/etc/nginx/ssl/ca.pem` |
| TRAFFIC_COLLECT_INTERVAL | Интервал сбора (сек) | 60 |
| TRAFFIC_RETENTION_DAYS | Хранение данных (дни) | 7 |

`API_KEY` удалён начиная с v9.0.0. Аутентификация — mTLS через nginx.
## Порты

| Порт | Доступ | Описание |
|------|--------|----------|
| 9100 | Только Panel IP | API мониторинга |
| 80 | Все | Let's Encrypt верификация |
| 22 | Все | SSH |

## Безопасность

- **mTLS авторизация**: nginx требует от клиента валидный сертификат, подписанный CA панели (`ssl_verify_client on`, `ssl_client_certificate /etc/nginx/ssl/ca.pem`). Авторизация происходит на уровне TLS-рукопожатия — до попадания запроса в FastAPI. `X-API-Key` удалён.
- **Оптимизации nginx**: `ssl_session_cache 64m`, `ssl_session_tickets on`, `ssl_session_timeout 12h`, `ssl_buffer_size 4k`; OCSP stapling (`ssl_stapling on`, `ssl_stapling_verify on`, resolver 1.1.1.1/8.8.8.8); upstream keepalive 64 / `keepalive_requests 1000` / `keepalive_timeout 75s`; `proxy_connect_timeout 5s`; proxy buffers (`proxy_buffer_size 8k`, `proxy_buffers 16 16k`); `location = /api/metrics` с `proxy_buffering off` (отдельный location перед общим — убирает 5–10 мс буферизации для real-time JSON).
- **Rate limiting**: 100 запросов/минуту
- **Anti-brute force**: 10 попыток = бан на 1 час (IP-ban в `security.py`)
- **TLS 1.2/1.3** с сильными шифрами
- **UFW**: порт 9100 доступен только с IP панели
- **Connection drop**: все ошибки авторизации приводят к разрыву соединения без HTTP-ответа
- **X-Forwarded-For/X-Real-IP**: доверяются только от `127.0.0.1`/`::1` (защита от header spoofing)
- **SSH authorized_keys**: запись через `tee -a` по stdin (защита от shell injection)
- **SSL deploy paths**: любой абсолютный путь разрешён (whitelist ALLOWED_DEPLOY_ROOTS удалён в v9.1.1); единственная проверка — путь начинается со `/`
- **UFW from_ip**: валидация через regex + `ipaddress.ip_network()` (защита от UFW metacharacter injection)
- **Audit headers**: nginx пробрасывает `X-SSL-Client-Verify` и `X-SSL-Client-DN` в backend для логирования

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
| POST | /api/system/execute | Выполнить команду на хосте |
| POST | /api/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |
| POST | /api/system/time-sync | Установить часовой пояс и синхронизировать NTP |
| POST | /api/system/replace-node-cert | Заменить серверный cert/key ноды (shared cert миграция) |

**`POST /api/system/replace-node-cert`** — принимает `{cert_pem, key_pem}`. Проверяет: подпись CA (текущий CA ноды), совпадение public key cert и key, cert не expired. Атомарная замена `cert.pem`/`key.pem` в `NGINX_SSL_DIR` (cp → .bak → write .new → os.replace), reload nginx через Docker SDK (`container.kill(signal="SIGHUP")` для `monitoring-nginx`) с fallback на `host_executor nginx -s reload`. При ошибке reload — rollback из `.bak`. Используется панелью при автомиграции per-server нод на shared cert.

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
| GET | /api/haproxy/config | Получить конфиг |
| POST | /api/haproxy/config/apply | Применить конфиг |
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

Блокировка IP/CIDR через ipset. Два списка: `blocklist_permanent` (постоянный) и `blocklist_temp` (временный с таймаутом).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ipset/status | Статус списков (count, timeout) |
| GET | /api/ipset/list/{set_type} | Получить IP из списка (permanent/temp) |
| POST | /api/ipset/add | Добавить IP/CIDR |
| POST | /api/ipset/bulk-add | Массовое добавление |
| DELETE | /api/ipset/remove | Удалить IP/CIDR |
| POST | /api/ipset/bulk-remove | Массовое удаление |
| POST | /api/ipset/clear/{set_type} | Очистить список |
| PUT | /api/ipset/timeout | Изменить timeout temp списка |
| POST | /api/ipset/sync | Синхронизация (замена всего списка) |

**Особенности:**
- Тип ipset: `hash:net` (поддержка IP и CIDR)
- Правила iptables: `INPUT -m set --match-set blocklist_* src -j DROP`
- Постоянные правила сохраняются в `/var/lib/monitoring/blocklist.json`
- При старте ноды: постоянные правила восстанавливаются, временный список пустой

### Firewall

Управление UFW через API. Все операции выполняются через `nsenter` на хост-системе.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/firewall/status | Статус UFW (active/inactive) и список правил |
| POST | /api/firewall/rules | Создать правило |
| DELETE | /api/firewall/rules | Удалить правило по порту |

**Получение правил при выключенном UFW:**

`list_rules()` в `node/app/services/firewall_manager.py` поддерживает два режима:
- Если UFW активен — использует `ufw status numbered` (стандартный путь)
- Если UFW неактивен (status: inactive) — вызывает fallback `_list_rules_from_added()`, которая парсит вывод `ufw show added`

`_list_rules_from_added()` поддерживает два формата парсинга:
- Простой: `ufw allow 80/tcp`
- Расширенный: `ufw allow in from IP to any port PORT proto PROTOCOL`

Нумерация правил в обоих случаях последовательная (1, 2, 3...). Благодаря fallback панель отображает добавленные правила даже если UFW выключен.

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

**`_build_sshd_content()` — генерация конфига с учётом Match-блоков:**

В `sshd_config` директивы после строки `Match ...` синтаксически принадлежат этому блоку до конца файла. Метод учитывает это при добавлении новых глобальных ключей:
- Отслеживает вход в Match-блок (строки, начинающиеся с `Match `).
- Не модифицирует директивы внутри Match-блока — они относятся к scope блока.
- Недостающие глобальные ключи вставляет перед первым `Match`-блоком, а не в конец файла; это предотвращает ошибку `sshd -t` вида "Directive 'X' is not allowed within a Match block".

**Смена порта SSH на Ubuntu 22.04+ (socket activation):**

Начиная с Ubuntu 22.04, `sshd` запускается через systemd socket activation (`ssh.socket`). Директива `Port` в `sshd_config` игнорируется — реальный порт задаётся через `ListenStream=` в socket unit. Скрипт обрабатывает оба случая автоматически:

1. `_detect_ssh_socket_unit()` — определяет активный socket unit: сначала проверяет существование unit через `systemctl cat`, затем `is-active`/`is-enabled`; результат логируется. Возвращает имя unit или `None` если socket activation не используется.
2. `_write_socket_port_override(socket_name, port)` — пишет drop-in `/etc/systemd/system/{socket_name}.d/listen-port.conf`. Файл содержит `ListenStream=0` (очищает дефолт), `ListenStream=0.0.0.0:{port}` и `ListenStream=[::]:{port}` — IPv4 и IPv6 явно, без зависимости от флага `BindIPv6Only` базового unit. Возвращает `(ok, path, err)`.
3. При смене порта и обнаруженном socket unit порядок операций детерминирован: `systemctl stop ssh.service` (освобождает дублирующий fd на старом порту) → `systemctl restart ssh.socket` (пересоздаёт listening fd с новым `ListenStream`) → `systemctl start ssh.service` (новый sshd получает fd через socket activation). Этот порядок решает проблему, когда `restart ssh.socket` сам по себе не помогал.
4. После применения вызывается `_verify_port_listening(new_port)` — использует `ss -tln`, делает до 10 попыток с задержкой; если порт не слушается — инициируется откат.
5. При ошибке на любом шаге — откатываются и `sshd_config`, и drop-in override.
6. Если socket activation не обнаружена — поведение прежнее: `systemctl restart sshd` или `ssh.service`.

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
- `deploy_path` — базовая папка на хосте (опциональный, default `""`)
- `reload_cmd` — команда перезагрузки сервиса (например `systemctl reload nginx`)
- `fullchain_filename` — имя файла сертификата (опциональный, default `fullchain.pem`)
- `privkey_filename` — имя файла ключа (опциональный, default `privkey.pem`)
- `custom_fullchain_path` — абсолютный путь к файлу сертификата (кастомный режим)
- `custom_privkey_path` — абсолютный путь к файлу ключа (кастомный режим)

Алгоритм деплоя:
1. Валидация сертификата через `openssl x509 -noout` и ключа через `openssl rsa -noout`
2. `_resolve_target_paths` — выбирает итоговые пути: если переданы `custom_*_path` — использует их напрямую; иначе собирает путь как `deploy_path + filename`
3. Пофайловый бэкап (`_backup_existing`) текущих файлов по целевым путям
4. Создание родительских директорий через `mkdir -p`
5. Запись новых файлов на хост через `nsenter`
6. Выполнение `reload_cmd`
7. Откат из бэкапа при ошибке reload

**Валидация путей деплоя (`ssl_manager.py`):**

Единственное ограничение на целевые пути — они должны быть абсолютными (начинаться со `/`). Whitelist директорий удалён: панель считается доверенной стороной (канал panel↔node защищён mTLS), поэтому деплой разрешён в любой абсолютный путь на хосте, включая кастомные директории сервисов (например, `/opt/myapp/ssl`).

**Файлы:**
- `node/app/models/ssl.py` — Pydantic модели
- `node/app/services/ssl_manager.py` — логика деплоя
- `node/app/routers/ssl.py` — API роутер

## Производительность

### Async сбор метрик

`node/app/services/metrics_collector.py`: `get_all_metrics()` выполняет 7 методов сбора (CPU, RAM, диск, сеть, процессы и др.) параллельно через `asyncio.gather()` + `asyncio.to_thread()` для блокирующих вызовов psutil.

**LACP / bond-интерфейсы:** `_get_bond_slaves()` читает `/sys/class/net/*/bonding/slaves` через sysfs и возвращает множество имён slave-интерфейсов. В `get_network_metrics()` slave-интерфейсы помечаются `is_virtual: true` и исключаются из суммарного счётчика скорости/трафика. Это исправляет двойной подсчёт (x2) на серверах с LACP: в `/proc/net/dev` трафик присутствует и на агрегированном bond0, и на каждом slave (eth0, eth1), но в итоговые метрики попадает только bond0.

### Async трафик и iptables

`node/app/services/traffic_collector.py`:
- Все `subprocess.run()` заменены на `asyncio.create_subprocess_exec()`
- Чтение `/proc/net/dev` выполняется через `asyncio.to_thread()`
- Все iptables-методы каскадно асинхронизированы
- **LACP / bond-интерфейсы:** `_get_bond_slaves()` читает `/sys/class/net/*/bonding/slaves` и исключает slave-интерфейсы из `_read_interface_bytes_sync()`. Байты, прочитанные через slave (eth0/eth1), не записываются в SQLite — только байты bond0. Исправляет x2 накопленного трафика в hourly/daily/monthly таблицах.

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

## deploy.sh — установка ноды

**Ключевые функции:**

| Функция | Описание |
|---------|----------|
| `prompt_node_secret()` | Запрашивает NODE_SECRET у администратора (таймаут 600 сек, валидация base64); пропускает если `nginx/ssl/` уже содержит cert/key |
| `unpack_node_secret_to_ssl()` | base64→JSON через python3, пишет `ca.pem`/`cert.pem`/`key.pem` в `nginx/ssl/` (chmod 600 для key) |
| `setup_env()` | Записывает `PANEL_IP` и `NODE_NAME` в `.env` (поле `API_KEY` удаляется если было) |
| `show_status()` | Показывает CA fingerprint SHA256 вместо API_KEY для визуальной проверки |
| `ensure_docker_running()` | Проверяет доступность Docker daemon (`docker info`); если недоступен — unmask и enable `docker.socket`/`docker.service`, затем `systemctl start docker`, ждёт до 10 секунд пока `docker info` начнёт отвечать. Возвращает код ошибки без `log_error` — чтобы не пугать пользователя когда это предварительная проверка перед фолбэком |
| `install_docker()` | Если Docker не установлен — выполняет полную установку. Если установлен — вызывает `ensure_docker_running()`; при ошибке (пакет неполный, engine/systemd-units отсутствуют) выводит `log_warn` и запускает полную установку как фолбэк. После переустановки, если `ensure_docker_running` всё ещё завершается ошибкой — только тогда `log_error` с инструкцией проверить systemctl/journalctl |

**Порядок установки:**
```
prompt_node_secret → install_docker → install_node → unpack_node_secret_to_ssl → setup_env → docker compose up
```

**Обработка Docker на проблемных серверах:** если Docker CLI установлен, но daemon недоступен (не активирован или пакет неполный — только `docker-ce-cli` без engine/systemd-units), `install_docker()` сначала пробует запустить daemon через `ensure_docker_running()`. При неудаче выводит предупреждение и выполняет полную переустановку как фолбэк. `log_error` с инструкцией проверить systemctl/journalctl появляется только если и после переустановки daemon не стартует.

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
