# Monitoring Panel

Веб-панель для мониторинга серверов. Собирает метрики с нод с настраиваемым интервалом (по умолчанию 10 сек) и хранит историю локально.

## Возможности

- **Dashboard** — карточки серверов с drag-and-drop, статус SSL
- **Server Details** — графики CPU/RAM/Network, процессы с фильтрацией, управление питанием (перезагрузка/выключение)
- **HAProxy** — управление правилами, сертификатами, firewall (UFW)
- **Traffic** — статистика по интерфейсам и портам, TCP/UDP соединения
- **Bulk Actions** — массовое создание/удаление правил HAProxy, портов трафика и firewall
- **IP Blocklist** — блокировка IP/CIDR через ipset с автообновлением списков из GitHub
- **Remnawave** — интеграция с Remnawave Panel, статистика посещений из Xray логов

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
├── backend/           # FastAPI + PostgreSQL
├── nginx/             # Reverse proxy с SSL
├── docker-compose.yml # Включает postgres контейнер
├── deploy.sh
└── VERSION            # Версия панели (единственный источник)
```

## База данных

Панель использует **PostgreSQL 16** для хранения данных:
- Метрики серверов (история 24ч raw + 30 дней hourly + 365 дней daily)
- Remnawave статистика (xray_visit_stats, xray_hourly_stats)
- Кэш пользователей, blocklist правила, настройки

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
| GET | /api/system/optimizations/version | Версии системных оптимизаций (устаревший, данные уже в /version) |
| POST | /api/proxy/{id}/system/optimizations/apply | Применить системные оптимизации на ноду |

**Механизм обновления**:
1. API создаёт временный контейнер `panel-updater` (образ `docker:cli`)
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка)
3. Запускает `update.sh` из склонированной папки
4. `update.sh` останавливает контейнеры, копирует файлы, пересобирает образы, запускает
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

Все bulk-эндпоинты принимают `server_ids: list[int]` и возвращают результат для каждого сервера.
При удалении выполняется проверка наличия правила перед удалением.

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
| GET | /api/blocklist/settings | Текущие настройки |
| PUT | /api/blocklist/settings | Обновить настройки |
| POST | /api/blocklist/sync | Синхронизировать все ноды (оба направления) |
| POST | /api/blocklist/sync/{id} | Синхронизировать одну ноду (оба направления) |

**Дефолтные списки (включены по умолчанию, направление: входящие):**
- AntiScanner: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list`
- Government Networks: `https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/government_networks.list`

Списки автоматически обновляются каждые 24 часа. При обнаружении изменений блоклисты синхронизируются со всеми активными нодами. Синхронизация отправляет оба направления (in + out) отдельными запросами.

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

Сайты из этого списка полностью исключаются из сбора статистики.
По умолчанию добавлены: `www.google.com:443`, `1.1.1.1:53`

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
| GET | /api/remnawave/stats/summary | Общая сводка (total_visits, unique_users, unique_destinations) |
| GET | /api/remnawave/stats/top-destinations | Топ посещаемых сайтов |
| GET | /api/remnawave/stats/top-users | Топ активных пользователей |
| GET | /api/remnawave/stats/user/{email} | Детальная статистика пользователя |
| GET | /api/remnawave/stats/destination/users | Пользователи посещавшие сайт |
| GET | /api/remnawave/stats/timeline | Временной график посещений |
| GET | /api/remnawave/stats/db-info | Информация о БД (количество записей и размер в байтах) |
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

Кэш пользователей автоматически обновляется каждые 30 минут. Используйте `/users/refresh` для немедленной синхронизации после добавления/удаления пользователей в Remnawave.

Параметры запросов:
- `period` — 1h, 24h, 7d, 30d, 365d, all (по умолчанию all — за всё время)
- `limit` — количество записей (1-500)
- `server_id` — фильтр по серверу
- `email` — фильтр по пользователю (ID в Remnawave)

**Оптимизированная схема БД:**

Данные хранятся компактно с двойной нормализацией (домены + IP) и составными первичными ключами:

1. **xray_destinations** — справочник уникальных доменов
   - destination (полный адрес: google.com:443), host (хост без порта: google.com)
   - host кеширован для быстрого GROUP BY без regexp_replace

2. **xray_source_ips** — справочник уникальных IP-адресов клиентов
   - Нормализация: VARCHAR(45) → INTEGER FK, экономия ~30 байт на строку

3. **xray_visit_stats** — счётчики: `PK(server_id, destination_id, email) → visit_count`
   - Составной PK вместо суррогатного id (экономия 4 байта + 1 индекс на строку)

4. **xray_hourly_stats** — timeline: `PK(server_id, hour) → counts`

5. **xray_user_ip_stats** — IP пользователей: `PK(server_id, email, source_ip_id)`

6. **xray_ip_destination_stats** — IP → destination: `PK(server_id, email, source_ip_id, destination_id)`
   - first_seen убран (не используется в запросах)

**Автоочистка данных (настраиваемая):**
- xray_visit_stats: записи с last_seen > visit_stats_retention_days (default 365)
- xray_hourly_stats: записи старше hourly_stats_retention_days (default 365)
- xray_user_ip_stats: записи с last_seen > ip_stats_retention_days (default 90)
- xray_ip_destination_stats: записи с last_seen > ip_destination_retention_days (default 90)
- remnawave_user_cache: записи без обновления > 7 дней
- xray_destinations, xray_source_ips: orphaned записи удаляются автоматически
- После массовых удалений выполняется VACUUM для возврата дискового пространства

**Ручная очистка:** DELETE /api/remnawave/stats/clear — удаляет всю статистику

7. **remnawave_user_cache** — кэш пользователей (обновляется каждые 30 минут из API)

**Оптимизация производительности:**

Backend-кеширование:
- In-memory кеш с TTL: summary/top-destinations/top-users — 30 сек, db-info — 5 мин
- IP counts объединены в один SQL запрос с conditional aggregation
- GROUP BY по кешированному host вместо regexp_replace на каждой строке

Frontend lazy loading (panel/frontend/src/pages/Remnawave.tsx):
- При открытии загружаются только базовые настройки (settings, nodes, collectorStatus)
- Статистика загружается при переходе на вкладки overview/users/destinations
- Данные settings tab (db-info, infrastructure, cache status) загружаются при переходе на settings
- Analyzer данные загружаются при переходе на analyzer

**Принцип работы:**
1. Панель опрашивает только **активные** Remnawave ноды (сервер включен + нода включена)
2. При первом запросе `/api/remnawave/stats/collect` нода **лениво запускает** `XrayLogCollector`
3. Коллектор читает логи через `docker exec remnanode tail -f` и агрегирует в памяти
4. Панель каждые 60 сек вызывает `/api/remnawave/stats/collect` на активных нодах
5. Нода отдаёт данные и очищает память
6. Панель инкрементирует счётчики в xray_visit_stats и добавляет запись в xray_hourly_stats
7. Раз в 30 минут обновляется кэш пользователей через Remnawave API

**Ленивый запуск коллектора:**
- Коллектор НЕ запускается автоматически при старте ноды
- Запускается только при первом обращении панели за статистикой
- Ноды без Remnawave не тратят ресурсы на проверку контейнера

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
monitoring
```

## Обновления

Панель поддерживает автоматическое обновление через веб-интерфейс:

1. Перейдите в раздел **Обновления** в меню
2. Просмотрите текущие версии панели и нод
3. Нажмите "Обновить" для обновления

При обновлении:
- Сохраняется конфигурация (.env)
- Пересобираются Docker контейнеры
- Сервисы перезапускаются автоматически
