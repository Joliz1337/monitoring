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

После установки доступна команда `mon` — интерактивный менеджер:

```
1) Установить панель          5) Удалить панель
2) Установить ноду            6) Удалить ноду
3) Обновить панель             7) Системные оптимизации
4) Обновить ноду               0) Выход
```

**Panel** — скрипт установит Docker, запросит домен, получит SSL через Let's Encrypt, сгенерирует `.env` и запустит контейнеры. В конце покажет `https://{domain}/{uid}` и пароль.

**Node** — установит Docker, HAProxy (native systemd), ipset, UFW. Запросит IP панели (порт 9100 только для него). Покажет Server IP, Port и API Key.

## Возможности

| Модуль | Описание |
|--------|----------|
| **Dashboard** | Карточки серверов с drag-and-drop, статус SSL, ключевые метрики |
| **Мониторинг** | CPU, RAM, диски, сеть, TCP states, процессы — real-time |
| **Графики** | 1ч / 24ч / 7д / 30д / 365д с автоагрегацией |
| **Трафик** | По интерфейсам, портам, TCP/UDP соединениям |
| **HAProxy** | Правила, старт/стоп/reload, логи, редактор конфига (native systemd) |
| **SSL** | Let's Encrypt, загрузка своих, автопродление |
| **Firewall** | Управление UFW |
| **IP Blocklist** | ipset in/out, авто-списки из GitHub, глобальные и per-server правила |
| **Torrent Blocker** | Автоблокировка через Xray логи (по тегу + поведение) |
| **Remnawave** | Статистика посещений из Xray, анализатор аномалий, ASN-группировка |
| **Alerts** | Telegram — offline, CPU, RAM, сеть, TCP states с cooldown |
| **Bulk Actions** | Массовые операции: HAProxy, трафик, firewall |
| **Терминал** | Выполнение команд на нодах через веб-интерфейс (SSE) |

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
                              │  FastAPI (Metrics)   │
                              │         │            │
                              │  HAProxy (systemd)   │
                              └──────────────────────┘
```

**Panel** — React + FastAPI + PostgreSQL 16, образы из GHCR  
**Node** — FastAPI + psutil, HAProxy как native systemd сервис

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

**CPU** — основная нагрузка: запросы к PostgreSQL, параллельный опрос нод каждые 10 сек, активная работа с интерфейсом.  
**Диск** — retention 365 дней на 30+ серверах может занять 15–30 GB. SSD обязателен.

### Node

Нода добавляет минимальный overhead к существующему серверу.

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
- Anti-brute force: 5 попыток → бан 15 мин
- Rate limiting: 60 req/min для неавторизованных
- TLS 1.2/1.3
- Connection drop при любых ошибках авторизации — без HTTP-ответа

**Node:**
- API Key (заголовок `X-API-Key`)
- Порт 9100 только для IP панели (UFW)
- Rate limiting: 100 req/min
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
<summary><b>Системные оптимизации</b></summary>

Применяются через `mon` → пункт 7, или через панель (раздел **Обновления**). Не применяются автоматически.

Включают: BBR + fq_codel, оптимизированные TCP/UDP буферы, conntrack auto-scaling, RPS/RFS/XPS, file descriptors 10M, anti-DDoS (syncookies, rp_filter), отключение IPv6.

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
├── install.sh              # Установщик + CLI (mon)
├── panel/                  # Веб-панель
│   ├── frontend/           # React + Vite + Tailwind
│   ├── backend/            # FastAPI + PostgreSQL 16
│   ├── nginx/              # Reverse proxy + SSL
│   ├── docker-compose.yml
│   ├── deploy.sh
│   ├── update.sh
│   └── DOCUMENTATION.md
├── node/                   # Агент мониторинга
│   ├── app/                # FastAPI + psutil
│   ├── scripts/            # apply-update.sh
│   ├── nginx/              # Reverse proxy + SSL
│   ├── docker-compose.yml
│   ├── deploy.sh
│   ├── update.sh
│   └── DOCUMENTATION.md
└── configs/                # sysctl, limits, RPS/RFS, systemd limits
```

</details>

## Документация

- [Panel](panel/DOCUMENTATION.md) — API, БД, Remnawave, Blocklist, Alerts
- [Node](node/DOCUMENTATION.md) — API, метрики, HAProxy, трафик, IPSet, оптимизации

## License

MIT
