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

Вкладка панели **«Анти-DDoS»** (иконка Siren в меню) — многослойная защита от DDoS-атак на нодах, см. раздел ниже.

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

**Анти-DDoS хардening conntrack (по итогам инцидента с ACK-флудом ~294k pps):**

ACK-флуд заполнял conntrack-таблицу (262144 записей) мусорными записями без реальных TCP-сокетов, при этом реальных TCP-соединений было на порядок меньше (~19k). Во всех sysctl-профилях и всех tune-скриптах:
- Строгий conntrack: `nf_conntrack_tcp_loose=0`, `nf_conntrack_tcp_be_liberal=0` (было 1/1) — mid-stream ACK без предшествующего SYN-handshake больше не создаёт запись
- Укороченные SYN-таймауты (`syn_sent` 30→15, `syn_recv` 30→10), добавлены `nf_conntrack_checksum=0` (NIC уже проверил чексуммы) и `nf_conntrack_log_invalid=0` (не топить dmesg/journald при флуде)
- `tcp_fastopen` 3→1 — только клиентский TFO (серверный принимает data-in-SYN, чем могут злоупотреблять спуф-флуды); добавлен `tcp_invalid_ratelimit=500` — не отвечать на каждый мусорный/вне-окна пакет
- `kernel.printk_ratelimit=5` / `printk_ratelimit_burst=10` — не топить journald при атаке
- `configure_conntrack()` во всех tune-скриптах теперь пишет раннюю загрузку модуля (`/etc/modules-load.d/nf_conntrack.conf` — без неё после ребута `nf_conntrack_*` остаётся на дефолтах ядра, `max=262144`, ровно та таблица что переполнилась) и персистит hashsize через `/etc/modprobe.d/nf_conntrack.conf`
- Новая функция `configure_memory_budget()` — динамически по объёму RAM задаёт `net.ipv4.tcp_mem` (потолок ~RAM/3; дефолт ядра ~9% RAM упирается в «TCP: out of memory» на нагруженных релеях), `net.ipv4.udp_mem` и `vm.min_free_kbytes` (резерв GFP_ATOMIC, чтобы NIC-драйвер не дропал пакеты при 300k+ pps); поэтому `tcp_mem`/`udp_mem`/`min_free_kbytes` больше не хардкодятся в sysctl.conf — они зависят от размера сервера

**Файлы оптимизации сети:**

- `configs/network-tune.sh` — программный RPS/RFS: устанавливает `rps_cpus` и `rps_flow_cnt` на всех очередях сетевых интерфейсов; содержит `is_safe_interface()` и `cpu_index_mask()`; `configure_conntrack()`/`configure_memory_budget()` — анти-DDoS хардening, см. выше
- `configs/network-tune.service` — systemd-unit для `network-tune.sh`
- `configs/multiqueue-tune.sh` — аппаратный multiqueue: хелперы `parse_channels()` (awk, 6 значений за проход), `is_pos_int()`, `get_current_hw_queues()`, `cpu_index_mask()` (hex-маска для CPU с индексом >32 через awk, без переполнения bash); `is_safe_interface()` пропускает slave-интерфейсы bond (`/master`), интерфейсы с carrier=0 (link DOWN) и operstate != up; ring buffer resize (`ethtool -G`) удалён; если драйвер поддерживает combined channels — `ethtool -L combined N`, иначе (mlx4_en и подобные) — `ethtool -L rx N tx N`; число активных очередей — на stdout, логи — в stderr; настраивает XPS, IRQ affinity, conntrack и динамический бюджет памяти (`configure_conntrack()`/`configure_memory_budget()` — см. выше)
- `configs/multiqueue-tune.service` — systemd-unit для `multiqueue-tune.sh`
- `configs/hybrid-tune.sh` — гибридный режим: те же хелперы `is_safe_interface()`, `cpu_index_mask()`, `parse_channels()`; ring buffer resize удалён; число активных очередей — на stdout для расчёта RPS-маски через `configure_rps_remaining`; summary: `hw queues: ...`; тот же `configure_conntrack()`/`configure_memory_budget()`, что и в остальных tune-скриптах
- `configs/sysctl.conf` — базовый sysctl для VPN/relay-нод (fallback-копия, идентична `configs/vpn/sysctl.conf`); `disable_ipv6=1`; `arp_announce=2`/`arp_ignore=1`; анти-DDoS хардening conntrack — см. выше
- `configs/vpn/sysctl.conf` — VPN-профиль sysctl (`MON_OPT_PROFILE=vpn`): агрессивный тюнинг для VPN/прокси-нод; `disable_ipv6=1`; `file-max 2097152`; `nf_conntrack_max 2097152`; `arp_announce=2`/`arp_ignore=1`; анти-DDoS хардening conntrack — см. выше
- `configs/panel/sysctl.conf` — универсальный профиль sysctl (`MON_OPT_PROFILE=panel`): умеренный тюнинг для панелей и смешанных нагрузок; IPv6 не отключается; `file-max 524288`; `nf_conntrack_max 262144`; расслабленные conntrack-таймауты (кроме SYN — тоже сокращены); `arp_announce=2`/`arp_ignore=1`; анти-DDoS хардening conntrack — см. выше

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

## Анти-DDoS защита

Многослойная защита от DDoS-атак на нодах, независимая от `network-tune`/`multiqueue-tune`/`hybrid-tune` (conntrack-харденинг оттуда остаётся первой линией обороны). Три слоя:

1. **Дежурный режим** — никаких лимитов по IP в мирное время, нулевые накладные расходы и ложные срабатывания.
2. **Аварийный режим («под атакой»)** — набор iptables-правил в отдельной цепочке `ANTIDDOS` (не в raw INPUT — переживает `ufw --force reset` от Firewall Profiles): whitelist ACCEPT первым → established ACCEPT → SSH (автоопределяется)/nginx mTLS API(9100)/внутренний uvicorn-API ноды(7500) ACCEPT (никогда не дропаются) → drop INVALID (в связке с `nf_conntrack_tcp_loose=0`) → на автоопределённые клиентские порты: SYNPROXY (проверка TCP-рукопожатия до conntrack — от SYN-флуда со спуфнутых IP), connlimit (лимит одновременных соединений с IP) и hashlimit (лимит новых соединений/сек с IP). `iptables -m multiport` принимает не более 15 портов на правило, поэтому клиентские порты разбиваются на группы по ≤15 — на busy Xray-ноде с 30+ инбаундами иначе часть портов осталась бы без лимитов; у каждой группы своя hashlimit-таблица (`antiddos1`, `antiddos2`, ...). Джамп из INPUT в `ANTIDDOS` ставится только пока режим активен.

**Автоопределение SSH-порта** — never-drop покрывает SSH независимо от того, сменён ли порт с дефолтного 22: `detect_ssh_ports()` в `ddos-watchdog.sh` вычисляет его из директивы `Port` в `sshd_config`/`sshd_config.d/*.conf`, `ListenStream=` в systemd socket-активации (`ssh.socket`, дефолт Ubuntu 24) и живых sshd-листенеров (`ss`), откат на 22 если ничего не найдено. Never-drop = статические management-порты (`9100`, `7500`) + автоопределённые SSH-порты.
3. **Автодетект (watchdog)** — локальный сторож на ноде читает сигналы из `/proc` (реальные дропы conntrack, заполнение conntrack как слабый намёк, всплеск SyncookiesSent, pps при мелком среднем пакете, softirq%) с консервативными порогами: сильный сигнал включает режим сразу, слабые — после удержания ~45 с; авто-выключение после ~15 мин тишины. Ручной пин (`source=manual`) автоматика не снимает — только админ. Self-heal: watchdog переставляет джамп в INPUT после стороннего `ufw reset`. Выключение автодетекта (`watchdog=off`) снимает активный авто-аварийный режим — нода полностью возвращается в дежурный режим (исправлено в `WATCHDOG_VERSION` 1.1.0; раньше такой режим только самолечился и оставался включённым).

   **Сигнал conntrack — рост дропов, а не просто заполнение.** Раньше «заполнение ≥ 80%» само по себе считалось сигналом атаки — это ложно срабатывало на просто занятой ноде или ноде без применённых sysctl-оптимизаций (дефолтный `conntrack_max` ~262k вместо 2M делает 80%+ нормальной нагрузкой), а аварийный режим conntrack не уменьшает, поэтому нода флапала по кругу без пользы. С `WATCHDOG_VERSION` 1.4.0 реальный сигнал атаки — рост счётчика `insert_failed` (таблица реально дропает пакеты, `nf_conntrack: table full, dropping packet`), суммированного по всем CPU из `/proc/net/stat/nf_conntrack`. Заполнение таблицы теперь только слабый намёк при near-exhaustion (порог поднят до 95%). Если на ноде часто держится высокое заполнение conntrack без реальных дропов — это признак отсутствия sysctl-оптимизаций; применение вкладки «Оптимизации» поднимает `conntrack_max` до 2M и заполнение падает до единиц процентов.

**Мастер-переключатель «Автодетект атак» (`AntiDdosSettings.enabled`)** — управляет только watchdog (автодетектом) на всех активных нодах разом: вкл/выкл вызывает `set_watchdog_all(enabled)` и больше ничего. Аварийный режим — независимый контрол: глобальные кнопки «Включить/выключить аварийный на всех» и per-node тумблер «Аварийный» им не затрагиваются ни в одну сторону, ручной пин оператора не сбрасывается при обычном выключении автодетекта. Тонкая настройка остаётся на уровне отдельных нод: тумблеры «Автодетект»/«Аварийный», ручная кнопка «Я под атакой».

**Whitelist** — отдельный ipset-набор `antiddos_allow` на диске ноды (переживает ребут и недоступность панели). Панель наполняет его ежечасно из трёх частей: авто (IP всех активных нод + IP панели), ручная (CIDR-подсети CDN и т.п., настраиваются в панели) и авто-источники по URL (произвольные списки IP/CIDR, например Cloudflare/Yandex Cloud — панель парсит IPv4/CIDR из ответа независимо от формата). ACCEPT по нему работает только в аварийном режиме.

**Файлы:**
- `configs/ddos-watchdog.sh` — самодостаточный host-скрипт (не зависит от Docker/панели). CLI: `loop` (systemd-сервис — детект + self-heal), `enable-manual`/`disable-manual`, `watchdog-on`/`watchdog-off`, `apply`/`clear`, `selfheal`, `whitelist-sync` (IP через stdin), `detect-ports`, `version`, `status`. Состояние — `/opt/monitoring/antiddos/state.json` (mode/source/since/reason/watchdog). Тюнинг-пороги в шапке скрипта, переопределяются через `/opt/monitoring/antiddos/config`.
- `configs/ddos-watchdog.service` — systemd-unit (`Type=simple`, `Restart=always`, `ExecStart=... loop`).

**Установка — zero-touch:** панель раскатывает watchdog на ноды сама, без ручных действий и без кнопки в интерфейсе. Фоновый опрос статуса (раз в ~60 сек) сверяет версию установленного на ноде скрипта с версией `ddos-watchdog.sh` на GitHub (константа `WATCHDOG_VERSION`, сейчас `1.4.0`) и автоматически ставит/обновляет его через `POST /api/antiddos/install`, если он не установлен или устарел. Единственное условие — на ноде уже должен стоять образ node-api с эндпоинтом `/api/antiddos` (обычное обновление ноды). Backend-эндпоинты ручной установки (`POST /antiddos/install-all`, `POST /proxy/{id}/antiddos/install`) остаются в API — доступны программно, но кнопка «Установить watchdog на все» из интерфейса панели убрана как избыточная.

**Требования к ядру:** SYNPROXY использует `xt_SYNPROXY`/`nf_synproxy_core` (best-effort — если модули недоступны, SYNPROXY пропускается, connlimit/hashlimit/drop-invalid остаются), плюс `connlimit` и `hashlimit`. На Ubuntu 24 iptables = iptables-nft (тот же стек, что UFW/Docker/ipset_manager).

Подробности API и полей БД — в [panel/DOCUMENTATION.md](panel/DOCUMENTATION.md#анти-ddos-защита) и [node/DOCUMENTATION.md](node/DOCUMENTATION.md#анти-ddos).

## Компоненты

Подробная документация по каждому компоненту:

- [panel/DOCUMENTATION.md](panel/DOCUMENTATION.md) — веб-панель: API, БД, конфигурация, безопасность (v10.23.1)
- [node/DOCUMENTATION.md](node/DOCUMENTATION.md) — нода-агент: API, метрики, HAProxy, трафик, Remnawave, Firewall Profiles, Анти-DDoS

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

### Пул соединений PostgreSQL

Бэкенд использует SQLAlchemy async pool (`pool_size=40`, `max_overflow=80`). Ключевой паттерн масштабируемости: сессия БД закрывается (`await db.commit()`) **до** любых сетевых запросов к нодам — медленные или зависшие ноды не удерживают коннекты пула. Неограниченные fan-out по нодам ограничены `asyncio.Semaphore`. PostgreSQL настроен на `max_connections=200` (задаётся в `panel/docker-compose.yml`).

Следующий архитектурный шаг при дальнейшем росте (когда bounded-семафоров и `max_connections=200` станет мало) — PgBouncer в режиме transaction pooling. На текущем масштабе не требуется.

## CI/CD

Docker-образы собираются GitHub Actions (`.github/workflows/docker-publish.yml`) при пуше в `main` и публикуются в GHCR:
- `ghcr.io/joliz1337/monitoring-panel-frontend:latest`
- `ghcr.io/joliz1337/monitoring-panel-backend:latest`
- `ghcr.io/joliz1337/monitoring-node-api:latest`

Установка и обновление: `docker compose pull` → `docker compose up -d`.

