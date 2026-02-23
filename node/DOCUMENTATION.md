# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — история по интерфейсам и портам (SQLite + iptables)
- **HAProxy** — управление нативным systemd сервисом, конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API
- **IPSet Blocklist** — блокировка IP/CIDR через ipset (постоянный и временный списки)
- **Терминал** — выполнение произвольных команд на хосте
- **Remnawave** — сбор статистики посещений из логов Xray (remnanode)

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
| GET | /api/system/versions | Объединённый endpoint: версия ноды + оптимизации |
| POST | /api/system/update | Запуск обновления (target_ref: branch/tag/commit, по умолчанию main) |
| GET | /api/system/update/status | Статус обновления |
| GET | /api/system/optimizations/version | Версия системных оптимизаций (installed + version) |
| POST | /api/system/optimizations/apply | Применить системные оптимизации |
| POST | /api/system/execute | Выполнить команду на хосте |
| POST | /api/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |

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

### Remnawave (Xray Logs)

Сбор статистики посещений из логов Xray на нодах с Remnawave Panel.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/status | Статус коллектора + batch/memory info |
| POST | /api/remnawave/stats/collect | Забрать накопленные данные и очистить память |

**Принцип работы (batch mode):**
- Сервис `XrayLogCollector` запускается автоматически при первом запросе `/collect`
- Читает логи через `docker exec remnanode tail -f /var/log/supervisor/xray.out.log`
- **Строки накапливаются в буфере**, парсятся пачками каждые 5 секунд
- Парсинг выполняется в thread pool, не блокирует event loop
- При вызове `/collect` — сначала обрабатывается буфер, затем отдаются данные

**Оптимизация производительности:**
- **Batch processing** снижает нагрузку на CPU в 3-5 раз
- Event loop свободен для API запросов между батчами
- Regex компилируется один раз, используется для всех строк

**Лимиты и защита (50k соединений, 15 мин хранение):**

| Лимит | Значение | Описание |
|-------|----------|----------|
| BATCH_INTERVAL_SEC | 5 сек | Интервал обработки буфера |
| MAX_BUFFER_LINES | 200,000 | Макс. строк в буфере (~50MB) |
| MAX_BUFFER_MB | 100 MB | Жёсткий лимит памяти буфера |
| MAX_MEMORY_MB | 512 MB | Максимум памяти для статистики |
| MAX_ENTRIES_VISITS | 1,000,000 | Макс. уникальных (destination, email) |
| MAX_ENTRIES_IP_VISITS | 2,000,000 | Макс. уникальных (email, source_ip) |
| MAX_ENTRIES_IP_DEST | 5,000,000 | Макс. уникальных триплетов |
| AUTO_FLUSH_SECONDS | 600 | Авто-сброс если панель не забирает (10 мин) |

**Механизмы защиты:**
- **Buffer overflow** — при переполнении буфера лишние строки дропаются
- **Memory limits** — при 90% лимита статистики новые записи дропаются
- **Auto-flush** — каждые 30 сек проверяется память, при превышении — автосброс
- **Timeout flush** — если панель не забирает данные > 10 минут — автосброс
- **Thread pool** — парсинг не блокирует async операции

**Формат ответа `/status`:**
```json
{
  "available": true,
  "running": true,
  "batch_mode": true,
  "batch_interval_sec": 5,
  "buffer_lines": 1234,
  "buffer_memory_mb": 0.25,
  "stats_memory_mb": 12.5,
  "total_memory_mb": 12.75,
  "memory_usage_percent": 4.9,
  "total_lines_read": 50000,
  "total_lines_parsed": 48000,
  "last_batch_duration_ms": 45.2,
  "limits": {...}
}
```

Нода **не хранит данные постоянно** — только в памяти между сборами. Если контейнер `remnanode` не найден, коллектор ожидает и проверяет каждые 30 секунд.

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

### Torrent Blocker

Мониторит логи Xray (`remnanode`) и блокирует торрент-пользователей через ipset temp ban.

**Два режима детекции:**
1. **По тегу Xray** — строки с `[... -> torrent]` блокируются мгновенно (требуется routing rule в Xray)
2. **По превышению IP** — если source IP подключается к >= N уникальных destination IP за минуту (только к голым IP, не доменам) — блокируется автоматически. Порог настраиваемый (default: 50)

**При блокировке:** IP добавляется в `blocklist_temp` + `conntrack -D -s <ip>` для мгновенного разрыва существующих соединений.

**Сохранение состояния при перезапуске:** При shutdown ноды процесс мониторинга останавливается, но флаг `enabled` **не сбрасывается**. При следующем запуске, если блокер был включён — он запустится автоматически. Пользовательский `disable` сохраняет `enabled: false`.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/torrent-blocker/status | Статус, активные блокировки из ipset, настройки |
| POST | /api/torrent-blocker/enable | Включить мониторинг |
| POST | /api/torrent-blocker/disable | Выключить мониторинг (сохраняет enabled=false) |
| POST | /api/torrent-blocker/settings | Обновить порог детекции (behavior_threshold: 5-1000) |

**Конфиг:** `/var/lib/monitoring/torrent_blocker.json` (enabled, behavior_threshold)

**Статус включает:**
- `active_blocks` / `active_ips` — текущие IP в temp-списке ipset
- `total_blocked` — кумулятивный счётчик
- `tag_blocks` — заблокировано по тегу Xray
- `behavior_blocks` — заблокировано по превышению IP соединений
- `behavior_threshold` — текущий порог

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
