# Monitoring System

Система мониторинга серверов с веб-панелью управления. Real-time метрики, HAProxy, SSL, firewall, IP blocklist, Remnawave-интеграция и Telegram-алерты.

> **Alpha** — активная разработка, возможны breaking changes.

![Status](https://img.shields.io/badge/status-alpha-orange)
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Docker](https://img.shields.io/badge/docker-required-blue)

## Установка

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
```

После установки доступна команда `mon` — интерактивный менеджер.

<details>
<summary><b>Установочный скрипт (install.sh / mon)</b></summary>

### Меню `mon`

```
1) Установить панель          5) Удалить панель
2) Установить ноду            6) Удалить ноду
3) Обновить панель            7) Системные оптимизации
4) Обновить ноду              8) Настроить прокси
9) Установить Remnawave       w) Cloudflare WARP
l) Сменить язык               0) Выход
```

Команда `mon` — обёртка, скачивающая свежий `install.sh` с GitHub при каждом вызове (с fallback на локальные копии в `/opt/monitoring-*`). Поддерживает прокси из `/etc/monitoring/proxy.conf`.

### Установка панели (пункт 1)

| Шаг | Действие |
|-----|----------|
| Docker | Автоматическая установка Docker + Compose, если не установлены |
| Домен | Запрашивает доменное имя панели |
| SSL | Let's Encrypt через certbot standalone (требуется открытый 80 порт) |
| `.env` | Генерирует `PANEL_UID`, `PANEL_PASSWORD`, `JWT_SECRET`, `POSTGRES_PASSWORD` |
| Образы | Скачивает из GHCR (`ghcr.io/joliz1337/monitoring-panel-*`), fallback на локальную сборку |
| Запуск | `docker compose up -d` (postgres + backend + frontend + nginx) |
| Итог | Выводит URL `https://{domain}/{uid}` и пароль |

### Установка ноды (пункт 2)

| Шаг | Действие |
|-----|----------|
| Docker | Автоматическая установка Docker + Compose |
| HAProxy | Установка `haproxy` как native systemd-сервиса на хосте |
| ipset | Установка пакета `ipset` для управления блоклистами |
| UFW | Настройка: открывает только порт `9100` для IP панели и `22` для SSH |
| DNS | `configure_dns()` — systemd-resolved + netplan + `/etc/resolv.conf` + `dhclient` (1.1.1.1 + 8.8.8.8) |
| SSL | Самоподписанный сертификат для nginx ноды на порту 9100 |
| API Key | Генерация ключа авторизации `X-API-Key` |
| Образы | Скачивает из GHCR (`ghcr.io/joliz1337/monitoring-node-*`), fallback на локальную сборку |
| Итог | Выводит Server IP, Port, API Key для добавления в панель |

### Обновление (пункты 3, 4)

- Скачивает свежую версию с GitHub (commit / tag / branch)
- `docker compose pull && docker compose up -d`
- Сохраняет `.env`, данные БД и volume `traffic_data`
- Lockfile `/tmp/monitoring-*-update.lock` — защита от параллельных запусков
- Retry-логика (до 3 попыток × 5с)
- Вызывает `configure_dns()` при каждом обновлении ноды

### Удаление (пункты 5, 6)

- `docker compose down -v` — останавливает контейнеры и удаляет volumes
- Удаляет `/opt/monitoring-panel` или `/opt/monitoring-node`
- Удаляет `/usr/local/bin/mon`, если другая часть (панель/нода) не установлена

### Системные оптимизации (пункт 7)

Подменю выбора **профиля** и **режима NIC**. Применяет sysctl-настройки, лимиты, systemd-limits и скрипт тюнинга сети. Не применяется автоматически.

| Профиль | Назначение | conntrack / file-max / buffers |
|---------|-----------|-------------------------------|
| **vpn** | VPN/прокси с высокой нагрузкой | 2M / 2M / 128MB |
| **panel** | Панели, мониторинг, Remnawave | 262k / 524k / 16MB |

| Режим NIC | Действие |
|-----------|----------|
| **Multiqueue HW** | `multiqueue-tune.sh` + `multiqueue-tune.service`; IRQ affinity; combined channels на max |
| **RPS/RFS/XPS** | `network-tune.sh` + `network-tune.service`; программное распределение прерываний |
| **None** | Возврат к системным настройкам по умолчанию |

Активные настройки: BBR + fq_codel, TCP/UDP буферы, conntrack auto-scaling, file descriptors (до 2M nofile), anti-DDoS (syncookies, rp_filter), отключение IPv6. Маркер активного профиля — `/opt/monitoring/configs/OPT_PROFILE`. При выборе одного режима NIC противоположный автоматически удаляется. Тонкая настройка также доступна через веб-интерфейс — страница **Оптимизации** в панели.

### Cloudflare WARP (пункт w)

| Шаг | Действие |
|-----|----------|
| Репозиторий | Добавляет официальный репозиторий Cloudflare |
| Пакет | Устанавливает `cloudflare-warp` |
| Фикс сети | `fix_warp_network()` — автодетект VPS с /32 (Aeza и т.п.) и добавление `172.30.255.1/24` |
| Регистрация | `warp-cli --accept-tos registration delete` → `registration new` |
| Режим | SOCKS5 proxy на порту **9091** (`WARP_PORT=9091`) |
| Автозапуск | `warp-auto.service` с `ExecStartPre=/usr/local/bin/warp-fix-network.sh` |
| Проверка | `curl --socks5 127.0.0.1:9091` |
| Вывод | Готовый фрагмент `outbound` для Xray конфига |

Статус отображается в главном меню `mon`: `connected` / `disconnected` / `not installed`.

### Remnawave нода (пункт 9)

Упрощённая установка VLESS-relay контейнера `remnanode`:
- Директория `/opt/remnawave`
- Самоподписанный SSL для nginx
- `docker-compose.yml` с `cap_add: NET_ADMIN` (для управления сетевыми интерфейсами)
- Nginx на unix-сокете с PROXY protocol; маскировочный шаблон
- UFW разрешает только IP панели Remnawave
- Без домена и публичного SSL

### Настройка прокси (пункт 8)

Конфигурация HTTP/HTTPS-прокси для `apt` и `docker`. Сохраняется в `/etc/monitoring/proxy.conf`, подхватывается командой `mon` при каждом запуске и обновлении.

### Строка статуса в меню

Главное меню `mon` показывает:
- Версию панели и ноды (если установлены)
- Активный профиль оптимизаций (`vpn` / `panel` / —)
- Режим NIC (`multiqueue` / `RPS` / —)
- Статус WARP (`connected` / `disconnected` / `not installed`)
- Статус прокси (`enabled` / `disabled`)
- Версию `configs/` из [configs/VERSION](configs/VERSION)

### Константы и пути

```
PANEL_DIR=/opt/monitoring-panel
NODE_DIR=/opt/monitoring-node
REMNAWAVE_DIR=/opt/remnawave
BIN_PATH=/usr/local/bin/mon
WARP_PORT=9091
LOCKFILE=/tmp/monitoring-installer.lock
REPO_URL=https://github.com/Joliz1337/monitoring.git
```

</details>

<details>
<summary><b>Возможности</b></summary>

### Dashboard и серверы

| Функция | Описание |
|---------|----------|
| Карточки серверов | Drag-and-drop, папки, три уровня детализации (стандартный / подробный / компактный), масштаб карточек |
| Статус сервера | Онлайн/офлайн-индикатор, последнее обновление, бейджи SSL и Xray |
| Выключение мониторинга | Сервер можно временно отключить без удаления — коллектор пропускает его |
| Infra Tree | Двухуровневая иерархия аккаунт → проект → серверы; встроена в страницу Servers, сворачивается, сохраняет состояние в localStorage |
| Поиск и фильтры | Поиск по имени / IP на Dashboard и в Bulk Actions |
| Кэш метрик | Кешированные метрики отдаются мгновенно; live-эндпоинт делает прямой запрос к ноде |

### Метрики и графики

| Функция | Описание |
|---------|----------|
| Real-time метрики | CPU (ядра, частота, per-cpu, температуры), RAM + swap, диски (partitions + IO), сеть (интерфейсы + TCP states), процессы, hostname, uptime, Load Average 1/5/15 |
| История метрик | 5 периодов: 1ч / 24ч / 7д / 30д / 365д с автоагрегацией (raw → hourly → daily) |
| SSE live-обновления | Страница деталей сервера обновляется в реальном времени без перезагрузки |
| Load Average | Отображается на карточках и в деталях сервера; алерт при превышении порога (cpu_count + offset) |
| Фильтрация виртуальных интерфейсов | Трафик Docker/veth не задваивает скорость сети на карточках |
| Speedtest | Ookla CLI / iperf3 / авто-выбор по гео; быстрый и полный режим; периодический запуск; бейдж на карточке; Telegram-уведомления |
| Синхронизация времени | Автоустановка IANA timezone и NTP-синхронизация на всех нодах каждые 24ч |

### Трафик

| Функция | Описание |
|---------|----------|
| Статистика по интерфейсам | Входящий / исходящий трафик по физическим интерфейсам |
| Статистика по портам | Отслеживание трафика по конкретным TCP/UDP портам через iptables accounting |
| Периоды | Почасовая / дневная / месячная агрегация; хранение 90 дней |
| Bulk управление портами | Добавление/удаление отслеживаемых портов сразу на группе серверов |

### HAProxy

| Функция | Описание |
|---------|----------|
| Native systemd | HAProxy работает как системный сервис на хосте (не в Docker) |
| Правила TCP/HTTPS | Создание, редактирование, удаление правил; PROXY protocol; wildcard SSL |
| Load Balancing | Несколько backend-серверов на одно правило; алгоритмы roundrobin/leastconn/source/uri и др.; health checks (TCP/HTTP); sticky sessions (cookie / stick-table) |
| DNS Resolver | Доменные backend-серверы разрешаются HAProxy без перезапуска |
| Сертификаты | Let's Encrypt (standalone / webroot), загрузка своих, автопродление cron |
| Логи | Последние N строк через journalctl |
| Конфиг-профили | Централизованные профили в PostgreSQL; синхронизация на несколько нод; импорт конфига с существующего сервера; лог синхронизаций |
| Bulk операции | Запуск/остановка/перезапуск/правила/конфиг на группе серверов; find-and-replace в конфиге |

### SSL сертификаты

| Функция | Описание |
|---------|----------|
| Let's Encrypt | HTTP-01 через certbot standalone/webroot при установке панели |
| Cloudflare DNS | DNS-01 challenge — не нужен порт 80; поддержка wildcard `*.domain.com` |
| Wildcard деплой | Панель выпускает сертификат и доставляет его на все настроенные ноды через API |
| Загрузка своих | Загрузка произвольных PEM/key через веб-интерфейс |
| Автопродление | Cron каждые 24ч; определяет метод (LE или Cloudflare) по конфигурации |
| HAProxy SSL | Certbot standalone прямо на ноде; автопродление (3:00 AM daily) |

### Firewall

| Функция | Описание |
|---------|----------|
| UFW | Создание/удаление правил allow/deny через API; работает и при выключенном UFW |
| Bulk | Массовое добавление/удаление правил firewall на группе серверов |

### IP Blocklist

| Функция | Описание |
|---------|----------|
| ipset in/out | Четыре списка: permanent/temp входящие и исходящие; тип `hash:net` (IP и CIDR) |
| Постоянные и временные | Temporary с настраиваемым TTL; permanent сохраняются после перезагрузки ноды |
| Per-request timeout | Torrent Blocker и Blocklist могут задавать разное время бана в одном запросе |
| Автоматические списки | Источники из GitHub с автообновлением каждые 24ч; включены AntiScanner и Government Networks |
| Глобальные правила | Применяются ко всем серверам; правила по конкретному серверу |
| Bulk sync | Параллельная синхронизация всех нод через asyncio.gather; hot-apply при изменении правил |

### Torrent Blocker

| Функция | Описание |
|---------|----------|
| Автоблокировка | Фоновый воркер с настраиваемым интервалом (по умолчанию 5 мин) опрашивает Remnawave Torrent Blocker plugin |
| Блокировка через ipset | IP из отчётов добавляются на все активные ноды с заданным временем бана |
| Настройки | Включение/отключение, интервал опроса, длительность бана, список исключённых серверов |
| Статистика | Статус воркера, количество заблокированных IP, время последнего опроса |

### Remnawave Integration

| Функция | Описание |
|---------|----------|
| Статистика Xray | Пользователи, их IP-адреса и счётчики посещений из Remnawave Panel API |
| HWID-устройства | Список устройств пользователей (platform, OS version, model) |
| Обнаружение аномалий | IP превышают лимит устройств; неизвестный User-Agent; трафик-аномалии; некорректные данные устройства |
| ASN-фильтрация | IP аномалии подавляются, если все адреса принадлежат одному провайдеру |
| EMA-подтверждение | Уведомления только после N подряд подтверждений (5 для IP, настраиваемо для трафика) |
| Игнор-списки | Игнорировать пользователя / конкретный IP / конкретный HWID |
| Telegram-алерты | Группировка IP по ASN/провайдеру; inline-кнопки игнора прямо в сообщении |
| Кэш пользователей | Обновляется каждые 30 минут; экспорт и просмотр |
| Фильтрация | По статусу (ACTIVE/DISABLED/LIMITED/EXPIRED), по IP, по email |
| Ноды | Список нод получается автоматически из Remnawave API |

### Xray Monitor

| Функция | Описание |
|---------|----------|
| Мониторинг подписок | Парсинг URI-ключей, base64, JSON xray-конфигов; тест через proxychains4 + xray-core |
| Speedtest | Ookla CLI через SOCKS5-прокси Xray; только download (`--no-upload`) |
| Проверка канала панели | Перед каждым циклом тестируется прямая скорость панели; при просадке цикл пропускается |
| История | История проверок для каждого сервера (download, upload) |
| Telegram-уведомления | offline / recovery / низкая скорость; кастомный бот |
| Игнор-список | Серверы, исключённые из мониторинга |

### Alerts (Telegram-уведомления)

| Функция | Описание |
|---------|----------|
| Офлайн-детект | Активный пробинг: API-попытки → ICMP ping; разные уведомления для полного и частичного офлайна |
| CPU / RAM | Критический порог; адаптивное EMA-отслеживание скачков |
| Сеть | Спайк/падение трафика относительно EMA baseline |
| TCP States | Отдельные триггеры для Established / Listen / Time Wait / Close Wait / SYN / FIN Wait |
| Load Average | Порог = cpu_count + offset; N последовательных проверок до алерта |
| Cooldown | Настраиваемый период между повторными алертами (default 30 мин) |
| Исключения | Список серверов, исключённых из мониторинга полностью или из отдельных триггеров |
| История | Сохраняется в БД; фильтрация по серверу и типу алерта |

### Billing (Оплата серверов)

| Функция | Описание |
|---------|----------|
| Помесячная модель | Дата следующей оплаты, уведомление за N дней до истечения |
| Ресурсная модель | Баланс + стоимость/мес → автоматический расчёт оставшегося срока |
| Yandex Cloud | Автосинхронизация баланса через YC Billing API; OAuth-токен (бессрочный); IAM кэшируется 58 мин; EMA дневного потребления |
| Telegram-уведомления | Об истечении срока и низком балансе |
| Продление и пополнение | Кнопки extend / topup прямо в интерфейсе |

### Bulk Actions

| Функция | Описание |
|---------|----------|
| HAProxy | Запуск/остановка/перезапуск; создание/удаление правил; применение конфига; find-replace в конфиге |
| Firewall | Создание/удаление UFW-правил на группе серверов |
| Трафик | Добавление/удаление отслеживаемых портов |
| Терминал | Выполнение произвольной команды или bash-скрипта на одном или нескольких серверах параллельно |
| Выбор серверов | Группировка по папкам; tri-state чекбоксы; поиск; состояние сворачивания в localStorage |

### SSH Security

| Функция | Описание |
|---------|----------|
| sshd_config | Управление настройками SSH: порт, root login, методы аутентификации, allowed users |
| Fail2ban | Настройки jail, список забаненных IP, разбан, разбан всех |
| SSH-ключи | Управление authorized_keys через API |
| Смена пароля | Генератор безопасного пароля (20+ символов), смена на одном или всех серверах |
| Bulk операции | Применение настроек sshd/fail2ban/пресетов на группе серверов |
| Пресеты | Встроенные (recommended / maximum) и кастомные (сохраняются в панели) |
| Безопасность | Автобэкап перед изменением; `sshd -t` валидация; автовосстановление при ошибке; `reload` вместо `restart` |

### Страница «Оптимизации»

| Функция | Описание |
|---------|----------|
| Карточки нод | Профиль (VPN / Panel), режим NIC (Multiqueue / RPS / None), версия оптимизаций, аппаратные возможности |
| Детали интерфейсов | Имя интерфейса и количество очередей по каждой ноде |
| Раскладка | VPN-ноды слева, Panel-ноды справа; неназначенные ноды (без оптимизаций) — сверху |
| Применение | Выбор профиля и режима NIC из выпадающего списка; противоположный NIC-режим удаляется автоматически |
| Удаление | Полный откат оптимизаций с диалогом подтверждения |
| Применить ко всем | Кнопка пакетного обновления всех нод одним профилем и режимом |

### Терминал

| Функция | Описание |
|---------|----------|
| Веб-терминал | Интерактивный терминал на странице деталей сервера |
| SSE streaming | Вывод stdout/stderr в реальном времени через Server-Sent Events |
| История команд | Сохраняется в localStorage |
| Таймаут и shell | Настраиваемый таймаут 30с–10м; выбор sh или bash |
| Режим скрипта | Многострочный bash в Bulk Actions |
| Параллельное выполнение | Команда отправляется на группу серверов одновременно через Bulk Actions |

### Shared Notes & Tasks

| Функция | Описание |
|---------|----------|
| Совместный блокнот | Единый документ для всех пользователей панели |
| Список задач | Создание, выполнение, удаление задач с сортировкой (выполненные внизу) |
| Real-time синхронизация | SSE-поток; дебаунсированное сохранение 500 мс; версионирование (OCC) против конфликтов |
| Плавающий таб | Открывается через amber-кнопку на правом крае экрана; не мешает основному интерфейсу |

### Backup & Restore

| Функция | Описание |
|---------|----------|
| Создание бэкапа | Полный `pg_dump` PostgreSQL в custom format; хранится до 20 файлов |
| Восстановление | Загрузка файла (до 100 MB); сброс схемы + `pg_restore`; без конфликтов FK |
| Скачивание | Скачать любой бэкап из списка через веб-интерфейс |

### Безопасность панели

| Функция | Описание |
|---------|----------|
| Секретный URL | Панель доступна только по `domain.com/{UID}`; все остальные пути → nginx 444 |
| JWT | HttpOnly Secure SameSite=strict cookie |
| Anti-brute | Двухуровневая (память + БД): 5 попыток → бан 15 мин |
| Timing-safe | Сравнение пароля и UID через `secrets.compare_digest` |
| Connection drop | Ошибки авторизации не возвращают HTTP-ответа — соединение разрывается |
| TLS | 1.2/1.3 с сильными шифрами |
| Rate limiting | 60 req/min для неавторизованных |

### Безопасность ноды

| Функция | Описание |
|---------|----------|
| X-API-Key | Timing-safe сравнение; 10 попыток → бан 1 час |
| UFW | Порт 9100 открыт только для IP панели |
| Connection drop | Ошибки авторизации без HTTP-ответа |
| TLS | 1.2/1.3 на порту 9100 (self-signed SSL) |
| Rate limiting | 100 req/min |

### Дополнительно

| Функция | Описание |
|---------|----------|
| FAQ встроенный | 44 статьи на русском; drawer с анимацией; иконки справки на каждой странице и сложном разделе |
| i18n | RU / EN; LanguageDetector из localStorage → navigator |
| Темная тема | Единственная тема; проектная цветовая схема (dark-800/700/200) |
| Framer Motion | Плавные анимации переходов, модалок, тултипов, drawer |
| Обновления | Автообновление через GHCR; fallback на локальную сборку; обновление всех нод из панели |
| ASN lookup | RIPE Stat API + ip-api.com; кэш 7 дней; используется в Remnawave аномалиях и Blocklist |
| GeoResolver | Определение страны и города ноды по IP; авто-выбор iperf3-серверов поблизости |
| Tooltip | Кастомный компонент: createPortal, Framer Motion, auto-flip, a11y, задержка 300 мс |

</details>

## Архитектура

```
┌──────────────────────────────────────────────────────────┐
│                        PANEL                              │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐  │
│  │  Nginx   │  │ Frontend │  │  Backend    PostgreSQL  │  │
│  │  (SSL)   │──│ (React)  │──│ (FastAPI)     (v16)    │  │
│  └──────────┘  └──────────┘  └────────────────────────┘  │
│       │                               │                   │
└───────│───────────────────────────────│───────────────────┘
        │                               │
        │ HTTPS :443                    │ HTTPS :9100
        ▼                               ▼
   ┌─────────┐                ┌──────────────────────┐
   │  User   │                │        NODE          │
   │ Browser │                │  Nginx (SSL) :9100   │
   └─────────┘                │         │            │
                              │  FastAPI v8.0.0      │
                              │  psutil + SQLite      │
                              │         │            │
                              │  HAProxy (systemd)   │
                              └──────────────────────┘
```

**Panel** — React 18 + FastAPI + PostgreSQL 16, образы из GHCR  
**Node** — FastAPI + psutil, HAProxy как native systemd сервис, версия 8.0.0

## Обновление

**Через веб-интерфейс** — раздел **Обновления** в меню панели (обновляет и панель, и ноды).

**Через CLI:**
```bash
mon  # пункты 3 и 4 в меню
```

**Через скрипт напрямую:**
```bash
cd /opt/monitoring-panel && ./update.sh           # последняя версия из main
cd /opt/monitoring-node && ./update.sh            # аналогично для ноды
./update.sh some-branch                           # конкретная ветка/тег/коммит
```

Конфигурация `.env` сохраняется. Образы скачиваются из GHCR, при недоступности — fallback на локальную сборку.

<details>
<summary><b>Системные требования</b></summary>

### ОС и софт

- **OS**: Ubuntu 20.04+ / Debian 11+ (amd64)
- **Docker**: 20.10+ (устанавливается автоматически)

### Panel

| Серверов | Модули | Минимум | Рекомендуемые |
|----------|--------|---------|---------------|
| 1–5 | Мониторинг, алерты | 1 vCPU / 512 MB / 5 GB | 1 vCPU / 1 GB / 10 GB |
| 5–15 | + Remnawave, Blocklist | 1 vCPU / 1 GB / 10 GB | 2 vCPU / 1 GB / 20 GB |
| 15–30 | Все модули | 2 vCPU / 1 GB / 20 GB | 4 vCPU / 1 GB / 40 GB |
| 30–50+ | Все + длительное хранение | 4 vCPU / 1 GB / 40 GB | 4–6 vCPU / 2 GB / 60+ GB |

**CPU** — основная нагрузка: запросы к PostgreSQL, параллельный опрос нод каждые 10 сек.  
**Диск** — retention 365 дней на 30+ серверах может занять 15–30 GB. SSD обязателен.

### Node

| Сценарий | RAM | CPU | Описание |
|----------|-----|-----|----------|
| Базовый | ~100–150 MB | < 1% | Мониторинг + HAProxy + Firewall + Traffic |
| + Remnawave | ~300–700 MB | 1–3% | Xray Log Collector (буфер до 100 MB, stats до 512 MB) |
| + Torrent Blocker | +50 MB | < 1% | Парсинг логов + ipset блокировка |

</details>

<details>
<summary><b>Конфигурация (.env)</b></summary>

**Panel:**

| Параметр | Описание | Default |
|----------|----------|---------|
| `DOMAIN` | Домен панели | задаётся при установке |
| `PANEL_UID` | Секретный путь `domain.com/{uid}` | auto |
| `PANEL_PASSWORD` | Пароль для входа | auto |
| `JWT_SECRET` | Секрет для JWT | auto |
| `JWT_EXPIRE_MINUTES` | Время жизни токена | 1440 |
| `MAX_FAILED_ATTEMPTS` | Попыток до бана | 5 |
| `BAN_DURATION_SECONDS` | Время бана (сек) | 900 |
| `POSTGRES_USER` | Пользователь PostgreSQL | panel |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL | auto |
| `POSTGRES_DB` | Имя базы | panel |

**Node:**

| Параметр | Описание | Default |
|----------|----------|---------|
| `API_KEY` | Ключ авторизации | auto |
| `NODE_NAME` | Имя ноды | node-01 |
| `PANEL_IP` | IP панели (для UFW) | задаётся при установке |
| `TRAFFIC_COLLECT_INTERVAL` | Интервал сбора трафика (сек) | 60 |
| `TRAFFIC_RETENTION_DAYS` | Хранение данных трафика (дни) | 90 |

</details>

<details>
<summary><b>Безопасность</b></summary>

**Panel:**
- Секретный URL: `domain.com/{PANEL_UID}` — все остальные пути → nginx 444 (connection drop)
- Двойная проверка UID: nginx + API (timing-safe)
- JWT в httpOnly cookie (secure, samesite=strict)
- Anti-brute force: двухуровневая (память + БД), 5 попыток → бан 15 мин
- TLS 1.2/1.3
- Connection drop при ошибках авторизации — без HTTP-ответа

**Node:**
- API Key (заголовок `X-API-Key`)
- Порт 9100 только для IP панели (UFW)
- Anti-brute force: 10 попыток → бан 1 час
- Connection drop без HTTP-ответа

**Порты:**

| Порт | Компонент | Доступ |
|------|-----------|--------|
| 443 | Panel | Все |
| 80 | Panel / Node | Все (Let's Encrypt) |
| 9100 | Node | Только Panel IP |
| 22 | Node | Все (SSH) |

</details>

<details>
<summary><b>Управление (CLI)</b></summary>

```bash
mon                             # Менеджер установки/обновления

# Panel (/opt/monitoring-panel)
docker compose logs -f          # Логи
docker compose restart          # Перезапуск
docker compose down             # Остановка
certbot certificates            # Статус SSL

# Node (/opt/monitoring-node)
docker compose logs -f          # Логи API
docker compose restart          # Перезапуск API
systemctl status haproxy        # Статус HAProxy
systemctl reload haproxy        # Reload конфига HAProxy
journalctl -u haproxy -n 100   # Логи HAProxy

# Сменить IP панели на ноде
ufw delete allow from OLD_IP to any port 9100 proto tcp
ufw allow from NEW_IP to any port 9100 proto tcp
```

</details>

<details>
<summary><b>Структура проекта</b></summary>

```
monitoring/
├── install.sh              # Установщик + CLI (mon), ~2413 строк
├── panel/                  # Веб-панель
│   ├── frontend/           # React 18 + Vite + TypeScript + TailwindCSS
│   ├── backend/            # FastAPI + PostgreSQL 16
│   ├── nginx/              # Reverse proxy + SSL
│   ├── docker-compose.yml
│   ├── deploy.sh
│   ├── update.sh
│   └── DOCUMENTATION.md
├── node/                   # Агент мониторинга (v8.0.0)
│   ├── app/                # FastAPI + psutil + SQLite
│   ├── scripts/            # apply-update.sh
│   ├── nginx/              # Reverse proxy + SSL
│   ├── docker-compose.yml
│   ├── deploy.sh
│   ├── update.sh
│   └── DOCUMENTATION.md
├── configs/                # sysctl, limits, RPS/RFS, systemd limits (v4.4.0)
└── scripts/                # install-mon-cli.sh
```

</details>

## Документация

- [DOCUMENTATION.md](DOCUMENTATION.md) — полная техническая документация проекта
- [Panel](panel/DOCUMENTATION.md) — API, БД, Remnawave, Blocklist, Alerts
- [Node](node/DOCUMENTATION.md) — API, метрики, HAProxy, трафик, ipset, оптимизации

## License

MIT
