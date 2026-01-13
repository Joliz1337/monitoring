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
  1. Останавливает нативный HAProxy
  2. Отключает автозапуск systemd сервиса
  3. Копирует конфиг `/etc/haproxy/haproxy.cfg` в Docker volume
  4. Запускает контейнерный HAProxy с тем же конфигом

- **Если HAProxy не работает**: контейнерный HAProxy остаётся выключенным

Оригинальный конфиг сохраняется в `/tmp/haproxy.cfg.backup.*`.

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
| RAM_CHANGE_THRESHOLD_MB | Порог изменения RAM для реоптимизации HAProxy | 500 |

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

### Системные оптимизации

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy/system/info | Информация о системе |
| GET | /api/haproxy/system/optimizations | Статус оптимизаций |
| POST | /api/haproxy/system/optimize | Применить оптимизации |

## Системные оптимизации

`deploy.sh` автоматически применяет:

- **sysctl** — TCP/network настройки, BBR congestion control
- **limits.conf** — 1M file descriptors для HAProxy
- **systemd** — LimitNOFILE для сервиса
- **SSL auto-renewal** — cron для автообновления сертификатов (3:00 AM daily)

### Авто-оптимизация HAProxy

Конфиг HAProxy автоматически перегенерируется при изменении ресурсов сервера:
- **CPU**: любое изменение количества ядер
- **RAM**: изменение объёма на ±500 MB (настраивается через `RAM_CHANGE_THRESHOLD_MB`)

Проверка выполняется каждые 60 секунд вместе со сбором трафика. При изменениях:
1. Перегенерируется конфиг с новыми `maxconn`/`nbthread`
2. Правила (frontend/backend) сохраняются
3. HAProxy перезагружается (reload)

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
