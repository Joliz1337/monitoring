# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — история по интерфейсам и портам (SQLite + iptables)
- **HAProxy** — управление конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 2) Установить ноду
```

При установке скрипт запросит **IP-адрес панели** для настройки firewall.

## Миграция нативного HAProxy

При установке скрипт автоматически обнаруживает работающий нативный HAProxy:

- **Если HAProxy работает**:
  1. Читает и сохраняет содержимое конфига ДО остановки сервиса
  2. Останавливает нативный HAProxy (systemd + kill процессов)
  3. Отключает автозапуск systemd сервиса
  4. После запуска контейнеров копирует конфиг в Docker volume
  5. Запускает контейнерный HAProxy с тем же конфигом

- **Если HAProxy не работает**: контейнерный HAProxy остаётся выключенным

**Бэкапы конфига**:
- Временный: `/tmp/haproxy-native-migration.cfg` (удаляется после миграции)
- Постоянный: `/tmp/haproxy.cfg.backup.YYYYMMDD_HHMMSS`

**Устранение проблем миграции**:
```bash
# Проверить конфиг в контейнере
docker exec monitoring-haproxy cat /usr/local/etc/haproxy/haproxy.cfg

# Проверить логи HAProxy
docker logs monitoring-haproxy

# Перезапустить HAProxy
docker compose --profile haproxy restart
```

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
| GET | /api/haproxy/status | Статус |
| GET | /api/haproxy/rules | Список правил |
| POST | /api/haproxy/rules | Создать правило |
| PUT | /api/haproxy/rules/{name} | Обновить правило |
| DELETE | /api/haproxy/rules/{name} | Удалить правило |
| POST | /api/haproxy/start | Запустить |
| POST | /api/haproxy/stop | Остановить |
| POST | /api/haproxy/reload | Reload конфига |
| POST | /api/haproxy/restart | Restart контейнера |
| GET | /api/haproxy/config | Получить конфиг |
| POST | /api/haproxy/config/apply | Применить конфиг |
| GET | /api/haproxy/logs | Логи (tail=100) |

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

### Системная информация

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy/system/info | Информация о системе (CPU, RAM, maxconn) |

## Системные оптимизации

Оптимизации применяются **отдельно** через главный установщик (`monitoring` → пункт 7):

- **IPv6** — отключение (улучшает стабильность сети)
- **BBR** — TCP congestion control
- **Буферы** — оптимизированные сетевые буферы (128MB max)
- **Очереди** — somaxconn, netdev_max_backlog = 65535
- **TCP Performance** — fastopen, no slow start after idle, MTU probing
- **TIME-WAIT** — 2M tw_buckets, tw_reuse
- **Anti-DDoS** — syncookies, rp_filter, ICMP protection
- **Conntrack** — 1M max connections, оптимизированные таймауты
- **File descriptors** — fs.file-max = 2M
- **limits.conf** — 2M nofile для всех пользователей

**Примечание**: При проблемах с сетью во время установки/обновления IPv6 отключается автоматически. Если оптимизации уже применены, настройки IPv6 берутся из основного файла конфигурации.

**SSL auto-renewal** — cron для автообновления сертификатов (3:00 AM daily) настраивается при установке ноды.

## SSL сертификаты

- Создаются через certbot внутри контейнера
- Автоматически обновляются через cron (ежедневно в 3:00)
- При создании первого сертификата cron настраивается автоматически
- Логи обновления: `/var/log/certbot-renew.log`

## Команды

```bash
# Логи
docker compose logs -f

# Перезапуск
docker compose restart

# Остановка
docker compose down

# Включить HAProxy
docker compose --profile haproxy up -d

# Выключить HAProxy
docker compose --profile haproxy down

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
