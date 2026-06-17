# Monitoring — Документация проекта

Система мониторинга серверов с веб-панелью и агентами на нодах.

## Состав проекта

```
monitiring/
├── panel/             # Веб-панель (FastAPI + React + PostgreSQL)
│   ├── DOCUMENTATION.md
│   ├── backend/
│   ├── frontend/
│   ├── nginx/
│   └── docker-compose.yml
├── node/              # API-агент на каждой ноде (FastAPI + SQLite)
│   ├── DOCUMENTATION.md
│   ├── app/
│   ├── nginx/
│   └── docker-compose.yml
├── scripts/           # Вспомогательные скрипты CLI
├── configs/           # Версионированные конфиги
├── install.sh         # Главный установщик
└── CLAUDE.md          # Правила разработки
```

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
```

Меню установщика:
- **1** — Установить панель
- **2** — Установить ноду
- **7** — Применить системные оптимизации (с выбором режима NIC)
- **9** — Установить Remnawave ноду (без домена/SSL); контейнер `remnanode` запускается с `cap_add: NET_ADMIN` для управления сетевыми интерфейсами

### Пункт 7 — Системные оптимизации

При выборе пункта 7 перед подменю выводится блок автоматической детекции аппаратного multiqueue — для каждого реального интерфейса показывается поддерживается ли multiqueue и максимальное число очередей. Далее появляется подменю выбора режима NIC:

| Режим | Действие |
|-------|----------|
| **1 — Multi-queue NIC (аппаратный)** | Устанавливает `multiqueue-tune.sh` и включает `multiqueue-tune.service`; удаляет RPS-конфигурацию если была |
| **2 — Обычная NIC (программный RPS)** | Устанавливает `network-tune.sh` и включает `network-tune.service`; удаляет multiqueue-конфигурацию если была |
| **0 — Назад** | Возврат в главное меню |

При выборе любого режима противоположный режим автоматически удаляется. Строка статуса в меню отображает активный режим: `NIC: multiqueue (применены)` или `NIC: RPS (применены)`.

**Безопасность применения tune-скриптов**

На ряде хостеров (OVH и аналогичных с port-security на свитчах) `ethtool -G` (resize ring buffers) на интерфейсах `igb`/`ixgbe`/`i40e` вызывает hard link reset, что может лишить доступа к серверу. По этой причине:
- `ethtool -G` полностью удалён из всех tune-скриптов.

**Файлы оптимизации сети:**

- `configs/network-tune.sh` — программный RPS/RFS: устанавливает `rps_cpus` и `rps_flow_cnt` на всех очередях сетевых интерфейсов; содержит `is_safe_interface()` и `cpu_index_mask()`
- `configs/network-tune.service` — systemd-unit для `network-tune.sh`
- `configs/multiqueue-tune.sh` — аппаратный multiqueue: хелперы `parse_channels()` (awk, 6 значений за проход), `is_pos_int()`, `get_current_hw_queues()`, `cpu_index_mask()` (hex-маска для CPU с индексом >32 через awk, без переполнения bash); `is_safe_interface()` пропускает slave-интерфейсы bond (`/master`), интерфейсы с carrier=0 (link DOWN) и operstate != up; ring buffer resize (`ethtool -G`) удалён; если драйвер поддерживает combined channels — `ethtool -L combined N`, иначе (mlx4_en и подобные) — `ethtool -L rx N tx N`; число активных очередей — на stdout, логи — в stderr; настраивает XPS, IRQ affinity, conntrack
- `configs/multiqueue-tune.service` — systemd-unit для `multiqueue-tune.sh`
- `configs/hybrid-tune.sh` — гибридный режим: те же хелперы `is_safe_interface()`, `cpu_index_mask()`, `parse_channels()`; ring buffer resize удалён; число активных очередей — на stdout для расчёта RPS-маски через `configure_rps_remaining`; summary: `hw queues: ...`
- `configs/sysctl.conf` — базовый sysctl для VPN/relay-нод; `disable_ipv6=1`; `arp_announce=2`/`arp_ignore=1`
- `configs/vpn/sysctl.conf` — VPN-профиль sysctl (`MON_OPT_PROFILE=vpn`): агрессивный тюнинг для VPN/прокси-нод; `disable_ipv6=1`; `file-max 2097152`; `nf_conntrack_max 2097152`; `arp_announce=2`/`arp_ignore=1`
- `configs/panel/sysctl.conf` — универсальный профиль sysctl (`MON_OPT_PROFILE=panel`): умеренный тюнинг для панелей и смешанных нагрузок; IPv6 не отключается; `file-max 524288`; `nf_conntrack_max 262144`; расслабленные conntrack-таймауты; `arp_announce=2`/`arp_ignore=1`

**Быстрая установка ноды (one-liner):**

```bash
# Установить только ноду
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh) <NODE_SECRET>

# Нода + системные оптимизации (автоопределение NIC)
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh) <NODE_SECRET> --optimize

# Нода + оптимизации с явным профилем sysctl
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh) <NODE_SECRET> --optimize --profile=vpn

# То же через именованный флаг
bash install.sh --node=<NODE_SECRET> [--optimize] [--profile=vpn|panel]
```

Аргументы командной строки `main()`:

| Аргумент | Описание |
|----------|----------|
| `<NODE_SECRET>` (позиционный) | Экспортирует `NODE_SECRET`, `MON_INSTALL_NODE=1` и вызывает `run_unattended` |
| `--node=<NODE_SECRET>` | Эквивалент позиционного аргумента |
| `--optimize` | Дополнительно ставит `MON_INSTALL_OPTIMIZATIONS=1` и `MON_NIC_MODE=auto` |
| `--profile=vpn\|panel` | Задаёт `MON_OPT_PROFILE`; применяется только вместе с `--optimize` |
| `--unattended` | Старый env-driven режим, поведение не изменилось |
| `-h`, `--help` | Печатает справку и выходит |

При передаче `NODE_SECRET` скрипт вычитывает `panel_ip` из самого токена — дополнительно `PANEL_IP` задавать не нужно.

**Режим Hetzner Rescue System (автоматическая установка ОС):**

Если установщик запущен внутри Hetzner Rescue System (временный Linux в RAM до установки настоящей ОС), `detect_rescue_system()` обнаруживает это и переключается в режим провижининга ОС — обычная установка ноды в этом режиме невозможна.

Сценарии использования:

```bash
# Установить Ubuntu 24.04 + после ребута автоматически поставить ноду
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh) <NODE_SECRET> [--optimize] [--profile=vpn|panel]

# Только чистая Ubuntu 24.04 (с подтверждением), без автопродолжения
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
```

Поведение в rescue:

1. `find_ubuntu2404_image()` — находит образ `Ubuntu-2404-noble-<arch>-base.tar.{zst,gz}` в `/root/images` или `/root/.oldroot/nfs/images`; предпочитает `.tar.zst`.
2. Диски определяются через `lsblk`. При наличии двух дисков одинакового размера — RAID1 (`mdraid`), иначе один диск без RAID.
3. Разметка: `/boot ext4 1024 МБ`, `swap 8 ГБ`, `/` ext4 остаток.
4. Hostname задаётся через `MON_OS_HOSTNAME` (по умолчанию `ubuntu`).
5. Если переданы параметры установки (`NODE_SECRET`, `MON_INSTALL_*`) — генерируется post-install скрипт (`-x`): он создаёт `/etc/monitoring/firstboot.env` (chmod 600) и one-shot systemd-сервис `mon-firstboot.service`. После первого старта Ubuntu сервис скачивает `install.sh` и запускает его в режиме `--unattended` с сохранёнными параметрами, затем удаляет себя (идемпотентно).
6. Запускается `installimage -a ...`; при успехе — автоматический `reboot`.
7. Root-пароль новой системы совпадает с паролем Rescue System (поведение `installimage`).

Новая env-переменная установщика:

| Переменная | Описание |
|------------|----------|
| `MON_OS_HOSTNAME` | Hostname новой системы (по умолчанию `ubuntu`) |

**Неинтерактивный режим (`--unattended`):**

```bash
bash install.sh --unattended
```

Запускает установку без меню и подтверждений. Компоненты выбираются через env-переменные:

| Переменная | Описание |
|------------|----------|
| `MON_INSTALL_NODE=1` | Установить ноду мониторинга |
| `MON_INSTALL_WARP=1` | Установить Cloudflare WARP |
| `MON_INSTALL_REMNAWAVE=1` | Установить ноду Remnawave |
| `MON_INSTALL_OPTIMIZATIONS=1` | Установить системные оптимизации |
| `MON_OPT_PROFILE` | Профиль sysctl-оптимизаций: `vpn` (по умолчанию) или `panel` |
| `MON_NIC_MODE` | NIC-режим оптимизаций: `auto` (по умолчанию, автоопределение через `auto_detect_nic_mode()`), `multiqueue`, `hybrid`, `rps`; если задан и не равен `auto`, режим применяется без автодетекта |
| `MON_PROXY_URL` | HTTP-прокси для скачивания компонентов |
| `NODE_SECRET` | Ключ API ноды мониторинга |
| `PANEL_IP` | IP панели (для UFW ноды мониторинга) |
| `REMNAWAVE_CERT` | Сертификат ноды Remnawave (не читается из /dev/tty) |

Глобальный флаг `UNATTENDED=1` — все запросы подтверждения переустановки (нода/WARP/Remnawave) автоматически принимаются. Функция `run_unattended()` выполняет компоненты в порядке: HTTP-прокси установщика → нода мониторинга → оптимизации (если `MON_INSTALL_OPTIMIZATIONS=1`) → WARP → нода Remnawave. Интерактивное меню без изменений.

**Автоматический выбор NIC-режима (`auto_detect_nic_mode()`):**

При запуске оптимизаций в режиме `--unattended` NIC-режим выбирается автоматически на основе числа аппаратных очередей и ядер CPU:

| Условие | Режим |
|---------|-------|
| Очередей ≥ числа CPU или ≥ 3 | `multiqueue` |
| 1 < очередей < 3 и < числа CPU | `hybrid` |
| Нет аппаратного multiqueue | `rps` |

`apply_system_optimizations()` принимает опциональные аргументы `(profile, nic_mode)` — при их наличии интерактивное меню пропускается. Без аргументов (пункт 7 меню) работает как раньше. Временные конфиги из репозитория доступны на шаге оптимизаций: `cleanup_temp` перенесён в конец после оптимизаций.

**Функции `install.sh`:**

- `install_warp()` — устанавливает Cloudflare WARP; маппит кодовое имя дистрибутива на поддерживаемый Cloudflare релиз (bullseye/bookworm/jammy/noble) — неподдерживаемые кодовые имена (например Ubuntu 25.04 "plucky") автоматически подменяются на `noble`; после `apt-get install cloudflare-warp` проверяет успешность установки: если пакет не установился или `warp-cli` недоступен — выводит `[ERROR]`, удаляет нерабочий файл `/etc/apt/sources.list.d/cloudflare-client.list` и возвращает код 1; добавлены ключи локализации `warp_codename_fallback` и `warp_install_failed` (MSG_EN и MSG_RU)
- `detect_multiqueue_support()` — перебирает реальные сетевые интерфейсы (фильтрует виртуальные, bridge, bond), через `ethtool -l` и `get_max_hw_queues()` определяет максимальное число аппаратных очередей; awk-парсер: Combined + RX + TX из секций `Pre-set maximums` и `Current hardware settings` за один проход; если `Combined > 0` — используется он, иначе `max(RX, TX)`; fallback — подсчёт `rx-*` каталогов в `/sys/class/net/$iface/queues/`; корректно обрабатывает карты с `Combined: n/a` (mlx4_en, часть igb/ixgbe); та же логика реализована в `detect_iface_hw_queues()` ноды (`node/app/routers/system.py`) для эндпоинта `GET /api/system/nic-info`
- `remove_rps()` — останавливает `network-tune.service`, удаляет скрипт и service-файл, сбрасывает `rps_cpus`/`rps_flow_cnt`
- `remove_multiqueue()` — останавливает `multiqueue-tune.service`, удаляет скрипт и service-файл
- `install_nic_tune()` — универсальная установка: копирует скрипт и service-файл для выбранного режима
- `enable_tune_service()` — активирует и запускает tune-сервис: снимает зависший таймер `mon-tune-rollback` от старых версий установщика (`systemctl stop`/`reset-failed` для `.timer` и `.service`), затем выполняет `daemon-reload`, `enable`, `restart`; если `restart` не удался — fallback на прямой запуск скрипта; подтверждения сети и авто-отката нет, оптимизации применяются сразу и навсегда

## Компоненты

Подробная документация по каждому компоненту:

- [panel/DOCUMENTATION.md](panel/DOCUMENTATION.md) — веб-панель: API, БД, конфигурация, безопасность (v10.14.0)
- [node/DOCUMENTATION.md](node/DOCUMENTATION.md) — нода-агент: API, метрики, HAProxy, трафик, Remnawave, Firewall Profiles

## Архитектура

```
Browser → nginx (SSL) → panel frontend (React)
                      → panel backend (FastAPI)
                              ↓
                         PostgreSQL
                              ↓ proxy
                         node API (FastAPI)
                              ↓
                         SQLite + host system
```

Панель собирает метрики с нод через proxy-роутер (`/api/proxy/{id}/...`). Ноды хранят данные локально в SQLite. Панель агрегирует и хранит историю в PostgreSQL.

## CI/CD

Docker-образы собираются GitHub Actions (`.github/workflows/docker-publish.yml`) при пуше в `main` и публикуются в GHCR:
- `ghcr.io/joliz1337/monitoring-panel-frontend:latest`
- `ghcr.io/joliz1337/monitoring-panel-backend:latest`
- `ghcr.io/joliz1337/monitoring-node-api:latest`

Установка и обновление: `docker compose pull` → `docker compose up -d`.

