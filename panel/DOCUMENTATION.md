# Monitoring Panel

Веб-панель для мониторинга серверов. Собирает метрики с нод каждые 5 секунд и хранит историю локально.

## Возможности

- **Dashboard** — карточки серверов с drag-and-drop, статус SSL
- **Server Details** — графики CPU/RAM/Network, процессы с фильтрацией, управление питанием (перезагрузка/выключение)
- **HAProxy** — управление правилами, сертификатами, firewall (UFW)
- **Traffic** — статистика по интерфейсам и портам, TCP/UDP соединения
- **Bulk Actions** — массовое создание/удаление правил HAProxy, портов трафика и firewall

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
├── backend/           # FastAPI + SQLite
├── nginx/             # Reverse proxy с SSL
├── docker-compose.yml
├── deploy.sh
└── VERSION            # Версия панели (единственный источник)
```

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| DOMAIN | Домен панели | required |
| PANEL_UID | Секретный путь для доступа (domain.com/{uid}) | auto |
| PANEL_PASSWORD | Пароль для входа | auto |
| JWT_SECRET | Секрет для JWT | auto |
| JWT_EXPIRE_MINUTES | Время жизни токена | 1440 |

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
| GET | /api/system/version | Версии панели и нод (панель из panel/VERSION, ноды из node/VERSION на GitHub) |
| POST | /api/system/update | Обновление панели (target_ref: branch/tag/commit, по умолчанию main) |
| GET | /api/system/update/status | Статус обновления |
| GET | /api/system/certificate | Информация о SSL сертификате панели |
| POST | /api/system/certificate/renew?force=bool | Продление SSL сертификата (force=true для принудительного) |
| GET | /api/system/certificate/renew/status | Статус продления сертификата |

**Механизм обновления**:
1. API создаёт временный контейнер `panel-updater` (образ `docker:cli`)
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка)
3. Запускает `update.sh` из склонированной папки
4. `update.sh` останавливает контейнеры, копирует файлы, пересобирает образы, запускает
5. Контейнер удаляется после завершения

Проверка версий: панель скачивает `panel/VERSION` и `node/VERSION` файлы с GitHub и сравнивает с локальными. Никаких тегов не требуется.

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
