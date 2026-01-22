# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — история по интерфейсам и портам (SQLite + iptables)
- **HAProxy** — управление нативным systemd сервисом, конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API
- **Терминал** — выполнение произвольных команд на хосте

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 2) Установить ноду
```

При установке скрипт запросит **IP-адрес панели** для настройки firewall.

## HAProxy

HAProxy работает как **нативный systemd сервис** на хосте (не в Docker). При установке ноды HAProxy устанавливается автоматически если не установлен.

**Конфиг**: `/etc/haproxy/haproxy.cfg`

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
│   ├── routers/          # API эндпоинты
│   └── services/         # Сбор метрик, HAProxy, трафик
├── nginx/                # Reverse proxy с SSL
├── docker-compose.yml
└── deploy.sh
```

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| API_KEY | Ключ авторизации | auto |
| NODE_NAME | Имя ноды | node-01 |
| PANEL_IP | IP панели (для UFW) | задаётся при установке |
| TRAFFIC_COLLECT_INTERVAL | Интервал сбора (сек) | 60 |
| TRAFFIC_RETENTION_DAYS | Хранение данных (дни) | 90 |
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
| POST | /api/system/update | Запуск обновления (target_ref: branch/tag/commit, по умолчанию main) |
| GET | /api/system/update/status | Статус обновления |
| POST | /api/system/execute | Выполнить команду на хосте |
| POST | /api/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |

**Выполнение команд на хосте**:

Эндпоинт `/api/system/execute` позволяет выполнять произвольные shell-команды на хост-системе через `nsenter`. Работает из Docker контейнера благодаря `privileged: true` и `pid: host`.

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
4. `update.sh` останавливает контейнеры, копирует файлы, пересобирает образ, запускает
5. Контейнер удаляется после завершения

Обновление **всегда** использует актуальную версию из GitHub.

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

## Системные оптимизации

Оптимизации применяются **отдельно** через главный установщик (`monitoring` → пункт 7):

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
