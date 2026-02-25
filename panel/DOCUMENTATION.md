# Monitoring Panel

Веб-панель для мониторинга серверов. Собирает метрики с нод с настраиваемым интервалом (по умолчанию 10 сек) и хранит историю локально.

## Возможности

- **Dashboard** — карточки серверов с drag-and-drop, статус SSL
- **Server Details** — графики CPU/RAM/Network/TCP States, процессы с фильтрацией, управление питанием (перезагрузка/выключение)
- **HAProxy** — управление правилами, сертификатами, firewall (UFW)
- **Traffic** — статистика по интерфейсам и портам, TCP/UDP соединения
- **Bulk Actions** — массовое создание/удаление правил HAProxy, портов трафика и firewall
- **IP Blocklist** — блокировка IP/CIDR через ipset с автообновлением списков из GitHub
- **Remnawave** — интеграция с Remnawave Panel, статистика посещений из Xray логов
- **Alerts** — Telegram-уведомления о состоянии серверов (offline, CPU, RAM, сеть, TCP)
- **Billing** — отслеживание оплаты серверов (помесячная и ресурсная модели), уведомления об истечении через Telegram

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

## SSL сертификаты

Скрипт автоматически:
- Устанавливает certbot если его нет
- Получает Let's Encrypt сертификат для указанного домена
- Проверяет валидность существующего сертификата
- Предлагает обновить если осталось < 30 дней
- Настраивает cron для автопродления (ежедневно в 3:00)

**Управление через панель:**
- В разделе **Настройки** отображается информация о сертификате панели
- Показывается домен, дата истечения и дней до истечения
- Кнопка "Продлить" для ручного продления через веб-интерфейс

**Требования:**
- Домен должен указывать на IP сервера
- Порт 80 должен быть открыт

## Структура

```
panel/
├── frontend/          # React + Vite + Tailwind
│   └── src/
│       ├── components/ui/Skeleton.tsx  # Skeleton-лоадеры (Skeleton, ServerCardSkeleton, MetricCardSkeleton, ChartSkeleton)
│       └── ...
├── backend/           # FastAPI + PostgreSQL
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

## База данных

Панель использует **PostgreSQL 16** для хранения данных:
- Метрики серверов (история 24ч raw + 30 дней hourly + 365 дней daily, включая TCP states)
- Remnawave статистика (xray_stats — единая таблица)
- Кэш пользователей, blocklist правила, настройки
- ASN-кэш (asn_cache — IP → ASN/prefix, TTL 7 дней)

**Преимущества PostgreSQL:**
- Concurrent writes — одновременная запись с множества серверов
- Connection pooling — эффективное использование соединений
- Batch upsert (ON CONFLICT) — 10-100x быстрее записи статистики
- Надёжность и масштабируемость

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

**Параллельная синхронизация**: `sync_all_nodes()` использует `asyncio.gather` — все серверы синхронизируются одновременно с per-server таймаутом 30 секунд. Если сервер не отвечает — он получает статус ошибки, остальные не блокируются.

**Дефолтные списки (включены по умолчанию, направление: входящие):**
- AntiScanner: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list`
- Government Networks: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/government_networks.list`

Списки автоматически обновляются каждые 24 часа. При обнаружении изменений блоклисты синхронизируются со всеми активными нодами.

**Торрент-блокер:**

Автоматическая блокировка IP пользователей, использующих торренты через VPN. Мониторит логи Xray на строки `-> torrent` и блокирует source IP через ipset temp ban.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/blocklist/torrent-blocker | Статус торрент-блокера на серверах с xray |
| POST | /api/blocklist/torrent-blocker/{id}/enable | Включить на сервере |
| POST | /api/blocklist/torrent-blocker/{id}/disable | Выключить на сервере |
| POST | /api/blocklist/torrent-blocker/{id}/settings | Установить порог для конкретного сервера |
| POST | /api/blocklist/torrent-blocker/global-settings | Глобальный порог детекции (применяется на все серверы параллельно) |
| GET | /api/blocklist/torrent-blocker/whitelist | Список исключений (whitelist) |
| PUT | /api/blocklist/torrent-blocker/whitelist | Обновить whitelist и отправить на все xray-ноды |

**Глобальный порог детекции**: сохраняется в PanelSettings (`torrent_behavior_threshold`), при сохранении рассылается на все активные серверы параллельно. Результат применения показывается в UI по каждому серверу.

По умолчанию выключен. Включается через вкладку "Торрент-блокер" в разделе Blocklist. Состояние сохраняется на ноде в `/var/lib/monitoring/torrent_blocker.json` и переживает перезагрузки (при shutdown ноды `enabled` не сбрасывается). Отображаются только серверы с `has_xray_node == true`.

**Два режима блокировки:**
- "По тегу Xray" (`tag_blocks`) — блокировка по routing-тегу torrent в Xray
- "По превышению IP" (`behavior_blocks`) — блокировка по порогу уникальных IP-подключений

**Whitelist (исключения из бана):** IP-адреса и CIDR-диапазоны, которые никогда не блокируются торрент-блокером. Хранится в PanelSettings (`torrent_whitelist`, JSON массив). При сохранении рассылается на все активные xray-ноды параллельно. На ноде сохраняется в `torrent_blocker.json` и проверяется при каждом срабатывании. По умолчанию: `127.0.0.1`, `::1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.

### Автоопределение xray-нод

Поле `has_xray_node` в таблице `servers` — обновляется каждые 2 минуты фоновой задачей `MetricsCollector._xray_check_loop()`. Проверка делается через `GET /api/remnawave/status` на каждой ноде — если `available: true`, значит контейнер `remnanode` запущен. Используется для фильтрации серверов в торрент-блокере и отображения бейджа "xray" / "no xray" в настройках Remnawave.

Требуется настройка Xray на ноде:
- Routing rules: порты 6881-6999 и протокол bittorrent → outbound tag `torrent`
- Outbound: tag `torrent` с протоколом `blackhole`

Временный бан по умолчанию: 600 секунд (10 минут). Настраивается во вкладке торрент-блокера.

### Remnawave Integration

Интеграция с Remnawave Panel для сбора статистики посещений из Xray логов.

**Настройки:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/settings | Текущие настройки |
| PUT | /api/remnawave/settings | Обновить настройки (api_url, api_token, cookie_secret, enabled, collection_interval) |
| POST | /api/remnawave/settings/test | Проверить подключение к Remnawave API |

**Игнорируемые пользователи:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/ignored-users | Список игнорируемых пользователей |
| POST | /api/remnawave/ignored-users | Добавить пользователя в список (user_id) |
| DELETE | /api/remnawave/ignored-users/{user_id} | Удалить пользователя из списка |

Игнорируемые пользователи исключаются из:
- Сбора логов Xray (xray_stats_collector)
- Уведомлений анализатора аномалий (traffic_analyzer)
- Всех статистических проверок

**Исключаемые сайты:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/excluded-destinations | Список исключаемых сайтов |
| POST | /api/remnawave/excluded-destinations | Добавить сайт в список (destination, description) |
| DELETE | /api/remnawave/excluded-destinations/{id} | Удалить сайт из списка |

Сайты из этого списка полностью исключаются из сбора статистики. Хранятся только хосты (без порта) — ввод `www.google.com:443` автоматически нормализуется до `www.google.com`. Удаление существующих данных выполняется в фоне.
По умолчанию добавлены: `www.google.com`, `1.1.1.1`

**Статус коллектора:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/status | Статус коллектора (running, collecting, interval, last_collect_time, next_collect_in) |
| POST | /api/remnawave/collect | Принудительный сбор статистики со всех нод |

**Ноды:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/nodes | Список Remnawave нод и все серверы |
| POST | /api/remnawave/nodes | Добавить сервер как Remnawave ноду |
| POST | /api/remnawave/nodes/sync | Синхронизировать ноды (массовое добавление/удаление) |
| PUT | /api/remnawave/nodes/{server_id}?enabled=bool | Включить/выключить ноду |
| DELETE | /api/remnawave/nodes/{server_id} | Удалить ноду |

**Статистика:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/stats/batch | Batch: summary + destinations + users в 1 запросе (используется фронтендом) |
| GET | /api/remnawave/stats/summary | Общая сводка (total_visits, unique_users, unique_destinations) |
| GET | /api/remnawave/stats/top-destinations | Топ посещаемых сайтов |
| GET | /api/remnawave/stats/top-users | Топ активных пользователей |
| GET | /api/remnawave/stats/user/{email} | Детальная статистика пользователя |
| GET | /api/remnawave/stats/destination/users | Пользователи посещавшие сайт |
| GET | /api/remnawave/stats/timeline | Временной график посещений |
| GET | /api/remnawave/stats/db-info | Информация о БД (количество записей и размер в байтах) |
| DELETE | /api/remnawave/stats/client-ips/clear | Очистить IP клиентов всех пользователей (hourly сохраняются) |
| DELETE | /api/remnawave/stats/clear | Очистить всю статистику посещений |
| GET | /api/remnawave/users | Кэш пользователей Remnawave |

**Полная информация о пользователе:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/user/{email}/full | Полная информация из кэша (expire, traffic, subscription url и т.д.) |
| GET | /api/remnawave/user/{email}/live | Свежие данные из Remnawave API (subscription history, bandwidth stats, hwid devices) |

**Кэш пользователей:**

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/remnawave/users/refresh | Принудительное обновление кэша пользователей из Remnawave API |
| GET | /api/remnawave/users/cache-status | Статус кэша (last_update, updating, update_interval) |

Кэш пользователей автоматически обновляется каждые 30 минут. При успешной синхронизации удалённые из Remnawave пользователи удаляются из кеша. При ошибке запроса — retry (до 2 попыток с паузой 5 сек), если не удалось — старый кеш сохраняется. Для 20к+ пользователей страницы загружаются параллельно (до 5 одновременных запросов, page size 200). Используйте `/users/refresh` для немедленной синхронизации после добавления/удаления пользователей в Remnawave.

Параметры запросов:
- `period` — 1h, 24h, 7d, 30d, 365d, all (по умолчанию all — за всё время)
- `limit` — количество записей (1-500)
- `server_id` — фильтр по серверу
- `email` — фильтр по пользователю (ID в Remnawave)

**Схема БД (единая таблица):**

Вся статистика хранится в ОДНОЙ таблице — без нормализации, без JOIN:

1. **xray_stats** — `PK(email, source_ip, host)` → count, first_seen, last_seen
   - email: ID пользователя, source_ip: IP клиента (VARCHAR(45)), host: домен без порта
   - Заменяет 5 старых таблиц (xray_visit_stats, xray_user_ip_stats, xray_ip_destination_stats, xray_destinations, xray_source_ips)
   - Индексы: `(host)` для top-destinations, `(last_seen)` для автоочистки

2. **xray_hourly_stats** — timeline: `PK(server_id, hour) → counts`

**Summary-таблицы (pre-computed):**

Пересчитываются после каждого цикла сбора. period=all читает из них (мгновенно):

- **xray_global_summary** — 1 строка: total_visits, unique_users, unique_destinations
- **xray_destination_summary** — PK(host): total_visits, unique_users
- **xray_user_summary** — PK(email): total_visits, unique_sites, unique_client_ips (только IP с ≥100 посещениями), infrastructure_ips

4. **asn_cache** — кэш ASN-информации: `PK(ip) → asn, prefix, cached_at`
   - Данные из RIPE Stat API (`stat.ripe.net/data/network-info`)
   - TTL 7 дней, просроченные записи удаляются автоматически
   - CIDR-matching: новые IP проверяются по известным prefix перед запросом к API
   - Используется анализатором аномалий и endpoint `/stats/user/{email}`

**Автоочистка данных:**
- xray_stats: записи с last_seen > retention_days (default 365)
- xray_hourly_stats: записи старше hourly_retention (default 365)
- remnawave_user_cache: записи без обновления > 7 дней
- asn_cache: записи старше 7 дней
- VACUUM после массовых удалений

**Ручная очистка:** DELETE /api/remnawave/stats/clear — TRUNCATE всех таблиц

3. **remnawave_user_cache** — кэш пользователей (обновляется каждые 30 минут из API)

**Инфраструктурные IP:** определяются динамически при запросе (не хранятся в БД)

**Оптимизация производительности:**

Backend:
- **Одна таблица** — без JOIN, без нормализации, простые SELECT с GROUP BY
- **Summary-таблицы**: period=all читает из них без full scan
- **Batch endpoint** `/stats/batch` — summary + destinations + users в 1 HTTP-запросе
- In-memory кеш с TTL: batch/summary — **120 сек**, db-info — 5 мин
- **Pre-warm кеша** при старте и после каждого сбора
- nginx timeout для `/api/remnawave/stats/` увеличен до **120 сек**

**UX фронтенда:**
- **Toast-уведомления**: глобальная система через `sonner` — success/error для всех CRUD-операций на всех страницах
- **Skeleton-лоадеры**: все страницы при загрузке показывают структурные placeholder'ы вместо полноэкранных спиннеров (Dashboard, ServerDetails, HAProxy, Traffic, Alerts, BulkActions, Updates, Blocklist, Remnawave)
- **Локализация**: все toast-сообщения поддерживают ru/en через i18next

Frontend lazy loading (panel/frontend/src/pages/Remnawave.tsx):
- При открытии загружаются только базовые настройки (settings, nodes, collectorStatus)
- Статистика загружается через batch endpoint при переходе на overview/users/destinations
- Auto-refresh интервал: **60 сек** (попадает в кеш 120 сек — 1 из 2 рефрешей из кеша)
- Данные settings tab (db-info, infrastructure, cache status) загружаются при переходе на settings
- Analyzer данные загружаются при переходе на analyzer

**Принцип работы:**
1. Панель опрашивает только **активные** Remnawave ноды (сервер включен + нода включена)
2. При первом запросе `/api/remnawave/stats/collect` нода **лениво запускает** `XrayLogCollector`
3. Коллектор читает логи через `docker exec remnanode tail -f` и агрегирует в памяти
4. Панель каждые 60 сек вызывает `/api/remnawave/stats/collect` на активных нодах
5. Нода отдаёт данные и очищает память
6. Панель делает batch upsert в xray_stats (единая таблица) + обновляет xray_hourly_stats
7. Раз в 30 минут обновляется кэш пользователей через Remnawave API

**Ленивый запуск коллектора:**
- Коллектор НЕ запускается автоматически при старте ноды
- Запускается только при первом обращении панели за статистикой
- Ноды без Remnawave не тратят ресурсы на проверку контейнера

**Анализатор аномалий (traffic_analyzer.py + asn_lookup.py):**
- Проверки: трафик (snapshot-интервал), количество IP за 24ч (с ASN-группировкой), подозрительные HWID за 24ч (фильтр по createdAt/updatedAt)
- IP-группировка по ASN: IP из одного ASN считаются как 1 группа (не спамит при 50 IP мобильного оператора)
- ASN-данные: RIPE Stat API → кэш в PostgreSQL (asn_cache, TTL 7 дней)
- CIDR-match: новые IP проверяются по уже известным prefix без обращения к API
- Rate limiting: батчи по 5 запросов, пауза 1 сек между батчами
- На фронтенде: вкладка IPs в модалке пользователя группирует IP по ASN

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
- `panel/frontend/src/pages/Alerts.tsx` — вкладка настроек и истории

### Billing (Оплата серверов)

Отслеживание сроков оплаты серверов. Два типа: помесячная (указать кол-во дней) и ресурсная (баланс + стоимость/месяц → расчёт срока). Уведомления через Telegram бот из раздела Alerts.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/billing/servers | Список серверов |
| POST | /api/billing/servers | Добавить сервер |
| PUT | /api/billing/servers/{id} | Обновить |
| DELETE | /api/billing/servers/{id} | Удалить |
| POST | /api/billing/servers/{id}/extend | Продлить (дни) |
| POST | /api/billing/servers/{id}/topup | Пополнить баланс |
| GET | /api/billing/settings | Настройки уведомлений |
| PUT | /api/billing/settings | Обновить настройки |

**Файлы:**
- `panel/backend/app/routers/billing.py` — API роутер
- `panel/backend/app/services/billing_checker.py` — фоновая проверка сроков + Telegram
- `panel/backend/app/models.py` — `BillingServer`, `BillingSettings`
- `panel/frontend/src/pages/Billing.tsx` — вкладка оплаты

### Backup & Restore

Резервное копирование и восстановление базы данных панели. Бэкап — полный pg_dump PostgreSQL в custom format. Хранятся в `/app/data/backups/` (volume `panel-data`). Максимум 20 бэкапов, старые удаляются автоматически.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/backup/create | Создать бэкап (pg_dump в фоне) |
| GET | /api/backup/list | Список бэкапов (имя, размер, дата, версия) |
| GET | /api/backup/{filename}/download | Скачать файл бэкапа |
| DELETE | /api/backup/{filename} | Удалить бэкап |
| POST | /api/backup/restore | Загрузить и восстановить из файла (multipart) |
| GET | /api/backup/status | Статус операции (idle/creating/restoring) |

После восстановления рекомендуется перезапуск: `docker compose restart`.

**Файлы:**
- `panel/backend/app/routers/backup.py` — API роутер
- `panel/frontend/src/pages/Settings.tsx` — секция в настройках

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
