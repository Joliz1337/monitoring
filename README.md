# 🖥️ Monitoring System

Система мониторинга серверов с веб-панелью. Собирает метрики в реальном времени, отслеживает трафик, управляет HAProxy и SSL сертификатами.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-green.svg)
![Docker](https://img.shields.io/badge/docker-required-blue.svg)

## 🚀 Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
```

После установки команда `monitoring` доступна глобально для управления.

## 📋 Возможности

| Модуль | Описание |
|--------|----------|
| **Dashboard** | Карточки серверов с drag-and-drop, статус SSL, ключевые метрики |
| **Мониторинг** | CPU, RAM, диски, сеть, процессы — в реальном времени |
| **Графики** | История за 1ч / 24ч / 7д / 30д / 365д с автоматической агрегацией |
| **Трафик** | По интерфейсам, портам, TCP/UDP соединениям |
| **HAProxy** | Создание правил, старт/стоп, логи, редактор конфига |
| **SSL** | Let's Encrypt автоматически, загрузка своих, автопродление |
| **Firewall** | Управление UFW через панель |
| **Bulk Actions** | Массовые операции на нескольких серверах |

<details>
<summary><b>🏗️ Архитектура</b></summary>

```
┌─────────────────────────────────────────────────────────────┐
│                         PANEL                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Nginx     │  │  Frontend   │  │      Backend        │  │
│  │   (SSL)     │──│  (React)    │──│  (FastAPI + SQLite) │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         │                                    │               │
└─────────│────────────────────────────────────│───────────────┘
          │                                    │
          │ HTTPS :443                         │ HTTPS :9100
          ▼                                    ▼
     ┌─────────┐                    ┌─────────────────────┐
     │  User   │                    │        NODE         │
     │ Browser │                    │  ┌───────────────┐  │
     └─────────┘                    │  │    Nginx      │  │
                                    │  │    (SSL)      │  │
                                    │  └───────┬───────┘  │
                                    │          │          │
                                    │  ┌───────▼───────┐  │
                                    │  │   FastAPI     │  │
                                    │  │   (Metrics)   │  │
                                    │  └───────────────┘  │
                                    │          │          │
                                    │  ┌───────▼───────┐  │
                                    │  │   HAProxy     │  │
                                    │  │  (optional)   │  │
                                    │  └───────────────┘  │
                                    └─────────────────────┘
```

**Компоненты:**
- **Panel** — центральная веб-панель (React + FastAPI + SQLite)
- **Node** — агенты на серверах (FastAPI + psutil + Docker SDK)

</details>

## 📦 Установка

### Требования
- Ubuntu 20.04+ / Debian 11+
- Docker (устанавливается автоматически)
- Минимум 1 GB RAM
- Домен для панели (указывающий на IP сервера)

### Установка панели

1. Убедитесь, что DNS домена указывает на IP сервера
2. Запустите `monitoring` → выберите **"1) Установить панель"**
3. Введите домен — **SSL сертификат получится автоматически**
4. Сохраните сгенерированный пароль

### Установка ноды

1. Запустите `monitoring` → выберите **"2) Установить ноду"**
2. Введите IP-адрес панели (для настройки firewall)
3. Сохраните сгенерированный API Key
4. В панели добавьте сервер: `https://IP_СЕРВЕРА:9100` + API Key

<details>
<summary><b>⚙️ Конфигурация</b></summary>

### Panel (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| `DOMAIN` | Домен панели | required |
| `PANEL_UID` | Уникальный ID для URL | auto |
| `PANEL_PASSWORD` | Пароль для входа | auto |
| `JWT_SECRET` | Секрет для JWT | auto |
| `JWT_EXPIRE_MINUTES` | Время жизни токена | 1440 (24ч) |
| `MAX_FAILED_ATTEMPTS` | Попыток до бана | 5 |
| `BAN_DURATION_SECONDS` | Время бана | 900 (15 мин) |

### Node (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| `API_KEY` | Ключ авторизации | auto |
| `NODE_NAME` | Имя ноды | node-01 |
| `PANEL_IP` | IP панели для UFW | required |
| `TRAFFIC_COLLECT_INTERVAL` | Интервал сбора трафика (сек) | 60 |
| `TRAFFIC_RETENTION_DAYS` | Хранение данных трафика | 90 |

</details>

<details>
<summary><b>🔒 Безопасность</b></summary>

### Panel
- JWT токены в httpOnly cookie (secure, samesite=strict)
- Anti-brute force: 5 попыток = бан на 15 минут
- Rate limiting: 10 req/s для API, 1 req/s для login
- TLS 1.2/1.3 с сильными шифрами
- Security headers (HSTS, X-Frame-Options, CSP)

### Node
- API Key авторизация (заголовок `X-API-Key`)
- Порт 9100 доступен только с IP панели (UFW)
- Rate limiting: 100 запросов/минуту
- Anti-brute force: 10 попыток = бан на 1 час
- TLS 1.2/1.3, API docs отключены в production

### Порты

| Порт | Компонент | Доступ | Описание |
|------|-----------|--------|----------|
| 443 | Panel | Все | HTTPS интерфейс |
| 80 | Panel/Node | Все | HTTP → HTTPS / Let's Encrypt |
| 9100 | Node | Только Panel IP | API мониторинга |

</details>

<details>
<summary><b>🛠️ Управление</b></summary>

### Panel

```bash
cd /opt/monitoring-panel

docker compose logs -f          # Логи
docker compose restart          # Перезапуск
docker compose down             # Остановка

certbot certificates            # Статус SSL
certbot renew --force-renewal && docker compose restart nginx  # Обновить SSL
```

### Node

```bash
cd /opt/monitoring-node

docker compose logs -f                     # Логи
docker compose restart                     # Перезапуск
docker compose down                        # Остановка

docker compose --profile haproxy up -d     # Включить HAProxy
docker compose --profile haproxy down      # Выключить HAProxy

# Изменить IP панели
ufw delete allow from OLD_IP to any port 9100 proto tcp
ufw allow from NEW_IP to any port 9100 proto tcp
```

</details>

<details>
<summary><b>🔄 Обновление</b></summary>

### Через веб-интерфейс (рекомендуется)
1. Откройте панель → раздел **Обновления**
2. Нажмите "Обновить панель" или "Обновить" для отдельных нод

### Через скрипт

```bash
# Panel
cd /opt/monitoring-panel && ./update.sh

# Node
cd /opt/monitoring-node && ./update.sh

# До конкретной версии
./update.sh v1.1.0
```

### Вручную

```bash
docker compose down
git pull
docker compose build --no-cache
docker compose up -d
```

</details>

## 📁 Структура проекта

```
monitoring/
├── install.sh              # Универсальный установщик
├── panel/                  # Веб-панель
│   ├── frontend/           # React + Vite + Tailwind
│   ├── backend/            # FastAPI + SQLite
│   └── DOCUMENTATION.md
└── node/                   # Агент мониторинга
    ├── app/                # FastAPI + psutil + Docker SDK
    └── DOCUMENTATION.md
```

## 📖 Документация

- [Panel Documentation](panel/DOCUMENTATION.md) — API панели, эндпоинты, настройка
- [Node Documentation](node/DOCUMENTATION.md) — API ноды, системные оптимизации, HAProxy

## 📝 License

MIT License
