# Monitoring Node Agent

API агент для сбора метрик сервера, отслеживания трафика и управления HAProxy.

## Возможности

- **Метрики** — CPU, RAM, диск, сеть, процессы
- **Трафик** — кумулятивные счётчики по интерфейсам и по портам (собственные цепочки iptables) в составе метрик; историю из них считает и хранит панель
- **HAProxy** — управление нативным systemd сервисом, конфигом, правилами, сертификатами
- **Firewall** — управление UFW через API
- **IPSet Blocklist** — блокировка IP/CIDR через ipset (постоянный и временный списки), отказ от приватных/служебных диапазонов, массовое применение одним `ipset restore`
- **Терминал** — выполнение произвольных команд и bash-скриптов на хосте (max 65000 символов)
- **Remnawave** — проверка доступности контейнера remnanode
- **Remnawave Nginx** — обнаружение установки Remnawave на хосте (`/opt/remnawave` по умолчанию), приём и атомарное применение nginx.conf от панели (backup → in-place запись → `nginx -t` → reload, откат при ошибке), автоподстановка host-специфичных лимитов (`worker_rlimit_nofile`/`worker_connections`/`ssl_session_cache`) под MemTotal/nofile самой ноды, проверка существования сертификатов на хосте с автомонтированием недостающих каталогов в контейнер, валидация конфига через живой или одноразовый контейнер, reload/restart сервиса
- **Синхронизация времени** — установка IANA timezone через `timedatectl`, включение NTP и принудительная синхронизация через `systemd-timesyncd`
- **SSH Security** — управление SSH-безопасностью сервера: настройки sshd, fail2ban, SSH-ключи
- **Wildcard SSL** — приём и деплой wildcard сертификатов, выпущенных панелью: разбор и валидация PEM, запись файлов на хост, бэкап, откат при ошибке reload
- **Firewall Profiles** — атомарное применение UFW-профилей от панели: backup → reset → apply → enable, авторолбэк при ошибке, node-API-port-guard (порт из `NODE_API_PORT`), drift-детекция по SHA256-хэшу
- **DNAT-маршрутизация** — проброс портов средствами netfilter (iptables nat DNAT + MASQUERADE + FORWARD ACCEPT) в собственных цепочках `MON_DNAT*`: атомарное применение набора правил от панели одним `iptables-restore --noflush`, счётчики соединений/байт по правилу, файл состояния и самолечение после ребута/`ufw reset`
- **Анти-DDoS** — многослойная защита: дежурный режим без лимитов, аварийный режим (SYNPROXY + hashlimit в отдельной iptables-цепочке `ANTIDDOS`, пороги авто-масштабируются по CPU/RAM хоста), автодетект атаки по сигналам из `/proc` (watchdog), whitelist на ipset, переживающий ребут и недоступность панели, self-check доступности ноды во время аварийного режима
- **Системные оптимизации** — sysctl/лимиты/HAProxy `maxconn` вычисляются на самой ноде из её MemTotal/nproc единым рендерером (`tune-sysctl.sh`), а не приходят готовыми от панели; авто-ре-рендер при каждой загрузке подхватывает ресайз VPS
- **Права доступа панели (NODE_CAPABILITIES)** — владелец ноды опционально сужает, что панель может делать через API, строкой в `.env`: по доменам (traffic/haproxy/firewall/ipset/ssh/ssl/antiddos/remnawave/system/exec/dnat) и уровню доступа (без доступа/только чтение/чтение и запись); пусто — полный доступ

## Быстрый старт

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
# Выберите: 2) Установить ноду
```

При установке скрипт запросит **IP-адрес панели** для настройки firewall.

## dpkg Self-Heal (deploy.sh)

На свежих серверах — особенно сразу после провижининга, до полной готовности systemd — postinst-скрипты пакетов (например `openssh-server`) падают на `systemctl restart`, оставляя dpkg в битом состоянии. После этого любая команда apt завершается ошибкой `Sub-process /usr/bin/dpkg returned an error code (1)`, и установка Docker/UFW не проходит.

`deploy.sh` решает это через два механизма:

**`enable_apt_guard()`** — вызывается перед `install_docker`:
- Создаёт `/usr/sbin/policy-rc.d` со скриптом `exit 101`, если файл ещё не существует; выставляет флаг `POLICY_RC_D_OWNED=1`
- `policy-rc.d` запрещает пакетным maintainer-скриптам трогать сервисы во время установки (читается только `invoke-rc.d`/`deb-systemd-invoke`, но **не** самим `systemctl`)
- Дожидается apt-lock (`wait_for_apt_lock`)
- Запускает `dpkg --configure -a` для донастройки застрявших пакетов

**`disable_apt_guard()`** — вызывается в `cleanup()` при любом выходе (успех/ошибка/прерывание):
- Удаляет `/usr/sbin/policy-rc.d` только если флаг `POLICY_RC_D_OWNED=1` — не трогает pre-existing файл
- Сбрасывает флаг в 0

Прямые вызовы `systemctl start docker` внутри `ensure_docker_running` гард не затрагивает — он влияет только на maintainer-скрипты пакетов.

## HAProxy

HAProxy работает как **нативный systemd сервис** на хосте (не в Docker). При установке ноды HAProxy устанавливается автоматически если не установлен.

**Конфиг**: `/etc/haproxy/haproxy.cfg`

**DNS Resolver**: В базовом конфиге включена секция `resolvers mydns` (DNS 1.1.1.1 + 8.8.8.8, hold valid 60s). Если target правила — доменное имя (а не IP), к server-линии автоматически добавляются параметры `resolvers mydns resolve-prefer ipv4 init-addr none`, что обеспечивает периодическое обновление IP домена без перезапуска HAProxy.

**Wildcard SSL**: Поле `use_wildcard: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` используется родительский домен для поиска сертификата вместо точного. Например, для правила с `cert_domain=sub.example.com` и `use_wildcard=True` нода применит сертификат `example.com` (покрывает `*.example.com`). Вспомогательные методы: `_extract_parent_domain()` — извлечение родительского домена, `_resolve_cert_domain()` — выбор итогового домена сертификата с учётом флага `use_wildcard`.

**PROXY protocol (accept_proxy)**: Поле `accept_proxy: bool` в модели `HAProxyRule` и dataclass `HAProxyRule`. При `True` добавляет `accept-proxy` к bind-строке frontend — нода принимает PROXY protocol header от вышестоящего HAProxy. Применяется к TCP и HTTPS правилам, в одиночном режиме и в режиме балансировщика. Парсинг `accept-proxy` из существующего конфига поддерживается; `update_rule()` обрабатывает изменение через пересоздание блока правила.

**Лимит соединений (maxconn) — по RAM хоста:**

Профиль конфигурации от панели один на много серверов с разной RAM, поэтому потолок `maxconn` в секции `global` вычисляет и подставляет сама нода при применении конфига, а не панель. `apply_config()` вызывает `_ensure_global_maxconn(content)`, которая вставляет расчётное значение в `global`, только если `maxconn` там ещё не задан явно (явное значение из профиля не трогается).

`_compute_maxconn()` (`haproxy_manager.py`): `MAXCONN_PER_RAM_MB = 10` соединений на МБ RAM — худший случай 2×16 КБ буфера (клиент+сервер) на соединение даёт HAProxy занять не больше ~40% памяти хоста. Дополнительно ограничен реальным лимитом дескрипторов HAProxy: `(NOFILE_LIMIT − 1024) // 3` (~2 fd на соединение с запасом). Итог зажат в `[MAXCONN_MIN = 10000, MAXCONN_MAX = 500000]`.

`_read_nofile_limit()` читает `NOFILE_LIMIT` из `/opt/monitoring/configs/tuning-facts.env` — это фактический `RLIMIT_NOFILE` HAProxy, в отличие от `/proc/sys/fs/nr_open` (потолок того, что процесс *может* установить): расчёт от `nr_open=2097152` при лимите юнита 65536 дал бы `maxconn` до 500000, и HAProxy со `strict-limits` (дефолт с 2.5) отказался бы стартовать. Рендерер (`tune-sysctl.sh`) утверждает всю цепочку файловых дескрипторов численно и пишет drop-in `LimitNOFILE` для `haproxy.service` — см. «Системные оптимизации» ниже.

**Развод по ядрам (cpu-affinity) — HAProxy и контейнеры Remnawave:**

На VPS без аппаратного multiqueue (`ethtool -l` → `Combined: 1`) весь NAPI-поллинг сетевой карты сидит на одном-двух ядрах, к которым его привязал гипервизор; процессы, работающие на этих же ядрах, отнимают у сети время, и потолок скорости задают именно они, пока остальные ядра простаивают. Номер ядра захардкодить нельзя — привязку выбирает гипервизор, и на разных хостах это разные ядра (0 и 1 на одном провайдере, 5 и 7 на другом).

Выключено по умолчанию — включается оператором глобальным тумблером на странице «Системные оптимизации» в панели (раздел «Развод по ядрам» в [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#система)). Выигрыш зависит от того, во что конкретно упирается нода, а ядро под сеть при включении забирается у приложения целиком, поэтому включение осталось сознательным действием, а не поведением по умолчанию.

`node/app/services/cpu_affinity.py`:

- **Состояние**: файл `/opt/monitoring/configs/cpu-affinity.env` (`STATE_FILE`, ключи `CPU_AFFINITY_ENABLED`/`CPU_AFFINITY_CONTAINERS`) — пишет панель. Каталог смонтирован в контейнер только на чтение, запись идёт через `write_host_file()` (host executor). `is_enabled()` — отсутствующий файл трактуется как «выключено». `container_names()` — список контейнеров для привязки, по умолчанию `DEFAULT_CONTAINERS = ("remnanode", "remnawave-nginx")`, переопределяется той же строкой в файле состояния. `render_state(enabled, containers)` — рендер файла для записи.
- **HAProxy**: `apply(content, cpu_count)` вызывается из `apply_config()` следом за `_ensure_global_maxconn` и вписывает `nbthread`/`cpu-map` в секцию `global` обоих шаблонов (`_generate_base_config()` на ноде, `generate_base_config()` в `panel/backend/app/services/haproxy_config.py`). Выключателем служит только настройка в панели (`is_enabled()`) — присутствие комментария-маркера `# cpu-affinity (auto)` в конфиге не обязательно, иначе конфиги на давно работающих нодах и профили, созданные прежним шаблоном (маркера не содержат), молча не получали бы привязку при включённом тумблере. Маркер найден — значение в скобках (`auto`/явный список ядер/`off`) переопределяет поведение для этой ноды, блок вставляется сразу под маркером с его отступом. Маркера нет — значения вписываются в начало секции `global` с отступом в 4 пробела и ведут себя как `auto`, точно так же, как `_ensure_global_maxconn` подставляет `maxconn` без явного значения в профиле. Секции `global` в конфиге нет вообще — предупреждение в лог, конфиг не трогается. Ранее сгенерированный блок (между `BLOCK_START`/`BLOCK_END`) вырезается на каждом применении **до** проверки `is_enabled()` — поэтому выключение настройки чистит конфиг от старой привязки при ближайшем применении, а не оставляет её навсегда.
- **Контейнеры Remnawave**: `sync_containers(executor, cpu_count)` приводит `cpuset` контейнеров из `container_names()` к расчётному набору ядер через `docker update --cpuset-cpus` — идемпотентно (сверяет текущее значение через `docker inspect`, не трогает совпадающее), контейнер, которого нет на ноде, молча пропускается (норма для ноды без Remnawave). `reset_containers(executor)` снимает cpuset при выключении настройки. `to_cpuset()` — список ядер в формат, понятный `docker --cpuset-cpus`.
  - `ContainerAffinitySync` — фоновая задача (`start()`/`stop()`, стартует в lifespan `main.py`, число ядер — `os.cpu_count()`), подтверждает привязку каждые `CONTAINER_RECHECK_INTERVAL_SEC = 300` секунд. Разово выставить нельзя: `docker compose up -d` и обновление Remnawave пересоздают контейнер с настройками из compose-файла и теряют cpuset.
- **Детект ядер (общий для HAProxy и контейнеров)**: `default_interface()` — интерфейс дефолтного маршрута из `/proc/net/route`. `detect_network_cpus()` — ядра, обслуживающие прерывания карты; имя PCI-устройства берётся из `/sys/class/net/<if>/device` (virtio-IRQ называются `virtio1-input.0` и имени интерфейса не содержат); при наличии трафика ядро определяется по максимуму счётчика в `/proc/interrupts`, иначе — по `effective_affinity_list`/`smp_affinity_list` (диапазон вместо номера = вектор не привязан, сетевым не считается). Служебные векторы отсекаются только чёрным списком по имени (`config`/`admin`/`async`/`mbox`), без встречной проверки на «rx/tx/input/output» в имени — такие суффиксы есть у virtio и многоочередных карт, но встроенные Intel PCH (`e1000e`) называют единственный вектор просто именем интерфейса, и позитивный фильтр отбрасывал бы его вместе со служебными.
- `resolve_app_cpus(value, cpu_count)` (переименована из `resolve_haproxy_cpus` — стала общей для обеих точек применения) — ядра для приложения (все, кроме сетевых) по значению `auto`/явный список (`0,1` или `0-2`)/`off`. Главное условие применимости для `auto` — `rx_queue_count(iface)` (число каталогов `rx-*` в `/sys/class/net/<if>/queues`) не больше одной очереди: при нескольких аппаратных очередях карта раскладывает нагрузку по ядрам сама, упора в одно ядро не возникает, и разводить нечего. Эту роль раньше пытался играть порог «приложению должно остаться не меньше половины ядер», но он не эквивалентен: на 8-ядерном хосте они совпадали случайно, а на 16-ядерном три аппаратные очереди занимают шесть ядер — проверка доли пропускала такой хост. Проверка очередей стоит только в ветке `auto` и использует интерфейс, полученный один раз через `default_interface()`; ручной список ядер в маркере (`# cpu-affinity (0,1)`) применяется и при нескольких очередях — это осознанное решение оператора. Кроме этого ничего не применяется, если: настройка выключена; значение `off` (маркер `# cpu-affinity (off)` — способ отключить привязку на конкретной ноде поверх включённого тумблера); ядра не определились; хост меньше `MIN_CPUS_FOR_SPLIT = 4` ядер; приложению осталось бы меньше половины ядер (запасная защита от неправдоподобно большого числа сетевых ядер — кривой парсинг и т.п., после введения проверки очередей это уже не основное условие); либо работает `irqbalance` (перемещает векторы, и статичная привязка со временем разъезжается) — ручной список ядер применяется даже при работающем `irqbalance`, в отличие от `auto`.

**API**: `GET /api/system/cpu-affinity` — текущее состояние и что нода определила у себя (`enabled`, `cpu_count`, `network_cpus`, `app_cpus`, `applicable`, `containers`). `POST /api/system/cpu-affinity` (тело `{"enabled": bool}`) — переключает настройку без полного применения оптимизаций: пишет файл состояния, затем сразу приводит контейнеры (`sync_containers` при включении, `reset_containers` при выключении); HAProxy получает привязку при ближайшем применении конфига. Отдельная лёгкая ручка нужна потому, что полный `POST /api/system/optimizations/apply` занимает минуты и перезаписывает весь sysctl. Поле `cpu_affinity: bool = False` в `ApplyOptimizationsRequest` — полное применение оптимизаций тоже несёт состояние с собой и пишет тот же файл.

Замеры на боевых нодах (только HAProxy, до расширения на контейнеры): сетевое ядро было занято на 65% при 54% простоя в среднем по машине — после развода 24% (потолок этого ядра по экстраполяции его загрузки до 100% отодвинулся примерно вдвое); на другой ноде 58%/45% → 15%/7% (примерно вчетверо). Но после развода узким местом становится уже не отдельное ядро, а суммарный CPU всех ядер, и практический потолок ниже кратности по ядру: на первой ноде расчётный потолок поднялся с 943 Мбит/с до ~1.7–1.8 Гбит/с; на второй — до ~2.5 Гбит/с (потолок по одному только сетевому ядру ушёл бы к ~5.9 Гбит/с).

Замеры на Xray-ноде (физический сервер, `e1000e`, одна RX-очередь) после расширения на контейнеры: сетевое ядро CPU0 — `busy 53.0% / usr 13.7%` → `45.1%` после увода `remnanode` → `36.3% / usr 2.6%` после увода ещё и `remnawave-nginx`; расчётный потолок по сетевому ядру вырос с ~1.6 до ~2.85 Гбит/с, суммарный расход CPU на 100k pps при этом не вырос (12.98% → 12.87%). Во всех случаях это расчётные потолки по загрузке ядра, а не измеренная скорость — реальная нагрузка на нодах до потолка не доходила.

**Тесты:** `node/tests/test_cpu_affinity.py` — 41 тест: определение ядер по счётчикам и по affinity (включая вектор без rx/tx в имени — `e1000e`), отсев служебных векторов, непривязанный вектор, ручной список/диапазон, `off`, мусорное значение, малый хост, большинство ядер под сетью, irqbalance, конфиг без маркера всё равно получает привязку (вставка внутрь `global`, идемпотентно), маркер `off` побеждает включённую настройку, конфиг без секции `global` не трогается, отступы, идемпотентность, пересчёт после смены числа ядер, выключенная настройка (ничего не меняет; выключение убирает ранее вставленный блок из конфига), разбор файла состояния (формы значений, список контейнеров, round-trip), `to_cpuset`, привязка/снятие/пропуск отсутствующего контейнера при синке, число RX-очередей как условие применимости `auto` (2/3/4/8 очередей на 16 ядрах — отказ, одна очередь — развод применяется на 4/8/16 ядрах, ручной список ядер работает даже при нескольких очередях).

**Таймауты и TCP keepalive — детект мёртвых туннелей:**

Мёртвые туннели без FIN от клиентов за NAT/ТСПУ (типично для мобильных клиентов) держали бы буферы вплоть до `timeout tunnel` и раздували бы память HAProxy, поэтому базовый шаблон конфига (`_generate_base_config()` на ноде, зеркалируется `generate_base_config()` в `panel/backend/app/services/haproxy_config.py`) задаёт:
- `tune.bufsize 16384` — вдвое меньше памяти на соединение, чем дефолтные 32768
- `timeout tunnel 1h`
- Интервалы TCP keepalive: `clitcpka-idle 60s`, `clitcpka-intvl 10s`, `clitcpka-cnt 3` и аналогично `srvtcpka-*` — без явных интервалов ядро использовало бы свой дефолт (обычно 2ч+), с ними мёртвое соединение детектится ядром за ~1.5 минуты вместо часов ожидания `timeout tunnel`.
- `hard-stop-after 1h` (равно `timeout tunnel`) — рвёт воркеров, оставшихся от seamless reload (`systemctl reload haproxy`, `-sf`): без явного значения старый воркер живёт неограниченно, пока сам не закроет все туннели, а мёртвый туннель без FIN не закрывается сам по себе. На боевой ноде без этой настройки накопилось три воркера с суммарными 3.8 ГБ RSS.

Существующий шаблон в уже применённых конфигах обновляется через «Перегенерировать конфиг» на странице профиля в панели или через `POST /api/haproxy/config/apply` с новым содержимым — сама по себе установка/обновление ноды конфиг не трогает.

**Живая статистика (stats socket):**

`GET /api/haproxy/stats` — живой срез `show stat` + `show info` через stats socket (строка `stats socket /var/run/haproxy.sock ... level admin` есть в базовом шаблоне конфига). Только чтение, admin-команды сокета не используются. Методы — в `haproxy_manager.py` (`get_stats()` и чистые парсеры).

- Путь сокета извлекается из строки `stats socket` самого конфига. Строки нет (конфиг создан старым шаблоном — `init_config()` не трогает существующий файл) — ответ `available: false, reason: socket_not_configured`; лечится перегенерацией/применением конфига и **restart** HAProxy.
- Контейнер агента работает с `pid: host` + `privileged`, поэтому сокет хоста доступен через `/proc/1/root` без bind-mount (mount на inode сокета ломался бы при рестарте haproxy). `/var/run` — absolute-симлинк на `/run` и под `/proc/1/root` может разрезолвиться в корень контейнера, поэтому кандидаты перебираются по порядку: `/proc/1/root/run/...` → `/proc/1/root/var/run/...` → сам путь из конфига.
- Ошибки — всегда HTTP 200 с `available: false` и машиночитаемым `reason`: `socket_not_configured` / `haproxy_stopped` / `socket_unavailable` (haproxy запущен без сокета — нужен restart, reload не создаёт сокет) / `timeout` / `error`. Кэш панели сохраняет только ответы 200 — состояние «недоступно» тоже должно доезжать до UI.
- Успешный ответ: `haproxy_version`, `uptime_sec`, `curr_conns` (из `show info`, его отказ не фатален) и `proxies[]` — строки CSV `show stat`, сгруппированные по `pxname` (frontend/backend/servers, классификация по `svname`). CSV парсится по заголовку (`csv.DictReader`), поэтому отсутствующие в старых haproxy колонки (например `addr`) дают `null`, а не ошибку.
- Кэш результата на ноде — 2 секунды (`_stats_cache_ttl`): гасит наложение fast-цикла панели (5 с) и авто-обновления нескольких открытых вкладок UI.

Тесты: `node/tests/test_haproxy_stats.py` — парсинг CSV/`show info`, выбор пути сокета, fallback-логика причин недоступности, кэш.

**Управление через терминал панели**:
```bash
systemctl status haproxy       # Статус
systemctl start haproxy        # Запуск
systemctl stop haproxy         # Остановка
systemctl restart haproxy      # Полный перезапуск
systemctl reload haproxy       # Reload конфига (без разрыва соединений)
haproxy -c -f /etc/haproxy/haproxy.cfg  # Проверка конфига
journalctl -u haproxy -n 100   # Логи
```

**При установке/обновлении ноды**:
- Если HAProxy уже работает — не перезапускается, конфиг не меняется
- Если не установлен — устанавливается через apt
- API адаптируется к текущему состоянию сервиса

**Миграция с контейнерной версии**:

При обновлении со старой версии (где HAProxy работал в Docker контейнере) скрипт автоматически:
1. Обнаруживает старый контейнер `monitoring-haproxy`
2. Устанавливает native HAProxy если не установлен (`apt install haproxy`)
3. Останавливает и удаляет контейнер (конфиг уже на хосте — был bind mount)
4. Включает автозапуск и запускает native HAProxy как systemd сервис

Миграция происходит автоматически при вызове `./update.sh`.

## Структура

```
node/
├── app/
│   ├── main.py           # FastAPI приложение
│   ├── config.py         # Pydantic Settings
│   ├── models/
│   │   ├── ssl.py        # Pydantic модели: WildcardDeployRequest/Response
│   │   ├── firewall_profile.py  # Pydantic модели: ProfileRule, ProfileApplyRequest/Response, ProfileStateResponse
│   │   ├── dnat.py              # Pydantic модели: DnatRule, DnatApplyRequest/Response, DnatStateResponse, DnatRuleCounters
│   │   └── remnawave_nginx.py   # Pydantic модели: NginxDiscoverResponse, NginxConfigResponse, NginxStatusResponse, NginxApplyRequest/Response, NginxActionResponse
│   ├── routers/          # API эндпоинты (metrics, haproxy, traffic, ssh, ssl, firewall, antiddos, remnawave и др.)
│   └── services/         # Сбор метрик, HAProxy, трафик, SSH менеджер
│       ├── ssl_manager.py          # Деплой wildcard сертификатов: запись на хост, бэкап, откат, валидация
│       ├── firewall_manager.py     # UFW: apply_profile, backup/restore, compute_rules_hash, get_full_state
│       ├── dnat_manager.py         # DNAT: цепочки MON_DNAT/MON_DNAT_POST/MON_DNAT_FWD, restore-скрипт, счётчики, файл состояния, самолечение
│       ├── antiddos_manager.py     # Тонкая обёртка над ddos-watchdog.sh (nsenter): enable/disable emergency, watchdog, whitelist sync
│       ├── host_files.py           # read_host_file()/write_host_file()/read_host_file_exact() — общая работа с файлами на хосте через nsenter+base64
│       ├── port_traffic_sampler.py # Цепочки учёта TRAFFIC_ACCOUNTING, фоновый съём счётчиков портов, список отслеживаемых портов
│       ├── rate_sampler.py         # Посекундный замер скоростей: per-CPU %, байт/с по интерфейсам и дискам
│       ├── legacy_traffic_store.py # Read-only доступ к старой SQLite трафика и её удаление по команде панели
│       └── remnawave_nginx_manager.py  # RemnawaveNginxManager: discover, get_config, status, logs, validate_content, apply_config, reload, restart
├── scripts/
│   └── apply-update.sh   # Логика обновления (запускается из свежего репо)
├── tests/                # Юнит-тесты на stdlib unittest (перечислены в разделах ниже)
├── nginx/                # Reverse proxy с SSL
├── docker-compose.yml
├── update.sh             # Скачивает репо и запускает apply-update.sh
└── deploy.sh
```

## Конфигурация (.env)

| Параметр | Описание | Default |
|----------|----------|---------|
| NODE_NAME | Имя ноды | node-01 |
| NODE_API_PORT | Порт mTLS-nginx, на который подключается панель. Читают трое: compose подставляет его в шаблон nginx (`nginx/templates/api.conf.template`, рендерится entrypoint'ом образа в `conf.d/api.conf`), агент — для guard'а файрвола и валидации DNAT, анти-DDoS сторож — для never-drop. Задаётся при установке (`--api-port=N` / env `NODE_API_PORT`); смена на живой ноде: правка `.env` → открыть новый порт в UFW → `docker compose up -d` → поменять порт в URL сервера на панели | 9100 |
| PANEL_IP | IP панели (для UFW) | задаётся при установке |
| PORT_SAMPLE_INTERVAL | Интервал съёма счётчиков портов из iptables (сек) | 30 |
| TRAFFIC_DB_PATH | Файл легаси-БД трафика; в его каталоге лежит и `traffic_config.json` со списком отслеживаемых портов | /var/lib/monitoring/traffic.db |
| HOST_PROC | Каталог `/proc` хоста, примонтированный в контейнер | /host/proc |
| MON_IMAGE_TAG | Тег Docker-образа api в `docker-compose.yml` (`image: ...:${MON_IMAGE_TAG:-latest}`); `deploy.sh` при установке пишет `dev`, если `MON_BRANCH=dev`, иначе `latest`; апдейтер (`apply-update.sh`) переписывает при обновлении на `main`/`dev` | latest |
| NODE_CAPABILITIES | Ограничение прав панели по доменам API, см. «Права доступа панели (NODE_CAPABILITIES)» ниже. Файл `.env` смонтирован в контейнер и перечитывается при каждом старте процесса — после правки достаточно `docker compose restart api` | пусто (полный доступ) |

## Порты

| Порт | Доступ | Описание |
|------|--------|----------|
| 9100 (`NODE_API_PORT`) | Только Panel IP | API мониторинга |
| 80 | Все | Let's Encrypt верификация |
| 22 | Все | SSH |

## Безопасность

- **mTLS**: nginx на порту API (`NODE_API_PORT`, по умолчанию 9100) требует клиентский сертификат, подписанный панельным CA (`ssl_verify_client on`); без валидного сертификата соединение обрывается на TLS-handshake, до HTTP — приложение вообще не видит запрос
- **Внутренний API изолирован**: uvicorn слушает только `127.0.0.1:7500` (`Dockerfile`), а не `0.0.0.0` — порт не проброшен наружу даже с `network_mode: host`, единственный путь на ноду снаружи — nginx с mTLS. Роутеры зарегистрированы без auth-зависимости именно поэтому: авторизация происходит раньше, на уровне TLS-хендшейка nginx
- **TLS 1.2/1.3** с сильными шифрами
- **UFW**: порт API доступен только с IP панели
- **NODE_CAPABILITIES**: mTLS решает, что панель это панель; NODE_CAPABILITIES — отдельный, более узкий вопрос: что этой конкретной панели разрешено делать на этой ноде. Прошедший TLS-хендшейк запрос может получить `403` на уровне ASGI-миддлвари, до роутера. Подробности — «Права доступа панели (NODE_CAPABILITIES)» ниже

## Права доступа панели (NODE_CAPABILITIES)

Владелец ноды может сузить то, что панель может делать через API, строкой `NODE_CAPABILITIES` в `/opt/monitoring-node/.env`. Пустая строка или отсутствие переменной — полный доступ, то есть поведение всех нод, поставленных до появления механизма, не меняется.

Агент читает строку прямо из смонтированного файла `.env` при старте процесса (значение кэшируется на время жизни процесса) — правку применяет обычный `docker compose restart api`, пересоздавать контейнер не нужно. Если сам файл не смонтирован в контейнер (нода с `docker-compose.yml` без этой строки в volumes) — агент откатывается на переменную окружения контейнера, как и раньше; в этом случае правка по-прежнему требует пересоздания (`docker compose up -d --force-recreate api`).

**Грамматика.** Одиннадцать доменов, по одному на смысловой раздел API: `traffic haproxy firewall ipset ssh ssl antiddos remnawave system exec dnat`. Слово без суффикса даёт домену чтение и запись (`rw`), с суффиксом `:ro` — только чтение. Три готовых пресета разворачиваются в набор доменов: `full` (все `rw`, то же самое, что пустая строка, но явно), `readonly` (все `ro`), `monitoring` (`traffic:ro` + `system:ro` — минимум для отображения ноды на дашборде). Слова можно комбинировать: уровень домена только повышается, порядок токенов не важен — `readonly,haproxy` и `haproxy,readonly` дают одно и то же (всё `ro`, кроме `haproxy: rw`). Регистр не важен, разделители — запятая, пробел или таб, кавычки по краям строки снимаются. Токен `metrics` принимается молча, но ни на что не влияет — метрики закрыть нельзя (см. always-allowed ниже), а слово в строке прав — не опечатка оператора. Незнакомое слово не роняет ноду и не блокирует остальную строку: оно игнорируется, попадает в `capabilities_unknown` (публикуется панели) и даёт один `WARNING` в лог ноды.

Пример: `NODE_CAPABILITIES=readonly,haproxy,exec:ro` — вся нода в режиме чтения, кроме HAProxy (полный доступ). У домена `exec` нет читающих эндпоинтов — оба его пути (`/api/system/execute`, `/api/system/execute-stream`) принимают только POST, поэтому `exec:ro` на практике закрывает терминал целиком, а не переводит его в режим просмотра.

**Домены и пути.** Путь резолвится в домен посегментно справа налево (`domain_for_path()`), а не проверкой префикса — это принципиально для `/api/system/execute` и `/api/system/execute-stream`: без выделения в отдельный домен `exec` они попали бы в `system` и делили бы уровень доступа с безобидным `/api/system/versions`. По той же причине `/api/haproxy/firewall/*` — это домен `firewall`, а не `haproxy`, хотя путь начинается с `/api/haproxy`.

**Always-allowed — 7 путей, не закрываются никаким сочетанием прав:** `/health`, `/api/version`, `/api/metrics`, `/api/system/versions`, `/api/system/update`, `/api/system/update/status`, `/api/system/replace-node-cert`. Без метрик и `/health` панель считает ноду мёртвой и шлёт ложный critical-алерт; без `/health` ещё и не поднимается сама нода — `nginx` ждёт его через `depends_on: service_healthy`, а ddos-watchdog снимает защитную цепочку при недоступности health-эндпоинта прямо во время атаки. Без `update`/`update/status`/`replace-node-cert` закрытую по ошибке ноду нельзя ни починить, ни продлить ей сертификат удалённо — только руками по SSH.

Два POST-эндпоинта ничего не меняют на хосте и поэтому проходят и под `:ro` домена: `POST /api/haproxy/validate`, `POST /api/ssh/config/test`.

**Формат публикации прав.** Нода отдаёт свою карту прав панели в каждом ответе `GET /api/metrics` (основной канал — панель и так опрашивает его каждые несколько секунд), а также в `GET /api/version` и `GET /api/system/versions`. Публикация — по принципу «всё или ничего»: `capabilities: null` означает «ограничений нет» (в том числе у любой ноды старой версии, которая про это поле вообще не знает), а если ограничения есть — публикуются **все** одиннадцать доменов сразу, каждый со значением `no`/`ro`/`rw`. Частичная карта не годится в принципе: сторона, читающая карту, трактует отсутствие ключа как разрешение, и «публикуем только явно разрешённое» тихо открыло бы всё, что забыли перечислить. Отдельно публикуется `capabilities_unknown` — список нераспознанных слов из `NODE_CAPABILITIES`, чтобы админ панели видел опечатку в конфиге ноды, не заходя на неё по SSH.

**Формат отказа.** Закрытый запрос получает `403` с телом `{detail, error: "capability_denied", domain, access, capabilities}` и заголовком `X-Capability-Denied: <домен>:<rw|ro>`. Панель это тело не пробрасывает клиенту как есть — `403` в её собственном API означает «сессия протухла», а `502`/`503`/`504` ретраятся автоматически; вместо этого панель транслирует отказ ноды в `409` (подробности — [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#права-ноды-node_capabilities)).

**Middleware (`CapabilityMiddleware`, `node/app/capabilities.py`).** Чистый ASGI-миддлварь, а не `BaseHTTPMiddleware` — тот оборачивает ответ в task group и ломает потоковый вывод `/api/system/execute-stream` (SSE). Регистрируется в `main.py` после `GZipMiddleware` и только если у ноды вообще есть ограничения — у неограниченной ноды стек обработки запроса не меняется вовсе. Повторяющиеся отказы не спамят лог: `DenyLog` пишет один `WARNING` на пару (домен, требуемый доступ) раз в 300 секунд, с числом подавленных повторов — панель опрашивает закрытые эндпоинты в своих обычных циклах (от 10 секунд), и без троттлинга журнал забился бы за сутки. Тело запроса в лог не попадает никогда: в `exec` там может быть произвольная команда, в `ssh` — пароль root.

**Fail-open во всём, что не касается самого запрета.** Разбор `NODE_CAPABILITIES` не может уронить приложение: `get_policy()` ловит любое исключение и отдаёт неограниченную политику — недоступный агент чинится только по SSH, а лишний открытый домен на ноде с битым конфигом менее опасен, чем нода, переставшая отвечать вовсе. Тот же принцип в самой проверке: путь вне известных доменов не режется никем, а домен без явного значения в разобранной строке получает `no` только на стороне ноды — на стороне панели (см. `panel/backend/app/services/node_capabilities.py`) отсутствующий ключ в чужой карте, наоборот, трактуется как разрешение, потому что панель может знать домен, которого не знает старая нода.

**Файлы:**
- `node/app/capabilities.py` — `Domain`/`Access` (Enum), `DOMAIN_PREFIXES`, `ALWAYS_ALLOWED`, `READ_ONLY_WRITES`, `PRESETS`, `domain_for_path()`, `parse_capabilities()`, `CapabilityPolicy` (`check()`/`published()`/`denial_body()`), `DenyLog`, `CapabilityMiddleware`, `ENV_FILE`/`ENV_KEY`, `read_env_file()` (построчный разбор смонтированного `.env` в обход pydantic — тот отдал бы приоритет переменной окружения, зафиксированной при создании контейнера), `get_policy()` (`lru_cache`, откат на `app.config.get_settings().node_capabilities`, если `.env` не смонтирован). Не импортирует ничего из `app.*` на верхнем уровне — панель грузит этот файл напрямую через `importlib`, чтобы структурно сверить свою карту путей с ним (см. MirrorTest в panel/DOCUMENTATION.md)
- `node/app/config.py` — поле `node_capabilities: str = ""` (строкой, не списком — `pydantic-settings` парсит «сложные» типы окружения как JSON и упал бы на обычной строке с запятыми); значение используется только как запасной путь в `get_policy()`
- `node/app/main.py` — `CAPABILITY_POLICY = get_policy()`, условная регистрация `CapabilityMiddleware`, поля `capabilities`/`capabilities_unknown` в `GET /api/version`, лог политики в `lifespan`
- `node/app/models/metrics.py` — `AllMetrics.capabilities: Optional[dict[str, str]] = None`
- `node/app/services/metrics_collector.py` — карта считается один раз при создании синглтона и отдаётся в каждом ответе `/api/metrics`
- `node/app/routers/system.py` — `capabilities`/`capabilities_unknown` в `GET /api/system/versions`
- `node/docker-compose.yml` — сервис `api` монтирует `./.env:/app/.env:ro`, чтобы `read_env_file()` видел правки без пересоздания контейнера
- `node/.env.example` — блок с грамматикой NODE_CAPABILITIES
- `node/deploy.sh` — `MON_NODE_CAPABILITIES` в `setup_env()`, валидация форматом `^[A-Za-z0-9:, ]+$`
- `node/scripts/apply-update.sh` — дописывает пустую строку `NODE_CAPABILITIES=` в уже существующий `.env`, если её там нет; значение, заданное админом, не трогает
- `install.sh` — валидация `MON_NODE_CAPABILITIES` в `run_unattended()`, переменная в `collect_firstboot_env()` (иначе провижининг через Hetzner Rescue теряет ограничение после ребута), описание в `--help`
- `node/tests/test_capabilities.py` — 39 тестов: грамматика разбора, форма публикуемой карты, неизвестные токены, резолв путей (включая `RouteCoverageTests` — все маршруты приложения обязаны быть либо в always-allowed, либо резолвиться в домен), always-allowed, уровни доступа, поведение middleware, троттлинг `DenyLog`, чтение `.env`-файла (`EnvFileTests` — значение читается, кавычки/пробелы снимаются, пустая строка отличается от отсутствия, закомментированная строка игнорируется, отсутствующий ключ и отсутствующий файл дают `None`)

## API

### Система

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/version | Версия ноды |
| GET | /api/system/versions | Объединённый endpoint: версия ноды + оптимизации |
| POST | /api/system/update | Запуск обновления (target_ref: branch/tag/commit, по умолчанию main; при вызове через панель панель подставляет выбранный канал обновлений — main/dev) |
| GET | /api/system/update/status | Статус обновления |
| GET | /api/system/optimizations/version | Версия системных оптимизаций (installed + version) |
| POST | /api/system/optimizations/apply | Применить системные оптимизации |
| POST | /api/system/optimizations/remove | Удалить все системные оптимизации |
| GET | /api/system/nic-info | Режим NIC и аппаратные возможности multiqueue |
| POST | /api/system/execute | Выполнить команду на хосте |
| POST | /api/system/execute-stream | Выполнить команду с потоковым выводом (SSE) |
| POST | /api/system/time-sync | Установить часовой пояс и синхронизировать NTP |

**`POST /api/system/time-sync`** — значение `timezone` идёт в shell-команду (`timedatectl set-timezone ...`), исполняемую на хосте от root через `nsenter`, поэтому проверяется трижды: формат ограничен паттерном `^[A-Za-z0-9_+/-]+$` (`TIMEZONE_PATTERN`, набор символов IANA-имён без точки — «..» из каталога зон не выйти), сама подстановка идёт через `shlex.quote`, и до выполнения `timezone_exists_on_host()` сверяет имя с реальным файлом в `/usr/share/zoneinfo` на хосте (`test -f`), а не только с форматом.

**`POST /api/system/update`** — оба параметра тела (`UpdateRequest`) валидируются паттерном на входе, потому что уходят в shell-команды апдейтера: `target_version` (branch/tag/commit) — `GIT_REF_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._/-]*$`, `proxy` (HTTP-прокси для git) — `PROXY_URL_PATTERN = ^[a-z][a-z0-9+.-]*://[A-Za-z0-9._%+:@/-]+$`. Тесты — `node/tests/test_update_ref_validation.py`.

**Объединённый endpoint версий** (`/api/system/versions`):
```json
{
    "node_version": "1.2.3",
    "optimizations": {
        "installed": true,
        "version": "2.0.0"
    }
}
```
Панель использует этот endpoint для получения всей информации о ноде одним запросом.

**Выполнение команд на хосте**:

Эндпоинт `/api/system/execute` позволяет выполнять произвольные shell-команды и многострочные bash-скрипты на хост-системе через `nsenter`. Работает из Docker контейнера благодаря `privileged: true` и `pid: host`. Максимальная длина поля `command` — 65000 символов.

**PATH**: Все команды выполняются с расширенным PATH (`/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), что позволяет использовать snap-пакеты и локально установленные бинарники.

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
- `command` (required) — shell-команда для выполнения
- `timeout` (optional) — таймаут в секундах, 1-600 (default: 30)
- `shell` (optional) — shell: "sh" или "bash" (default: "sh")

**Потоковое выполнение команд (SSE)**:

Эндпоинт `/api/system/execute-stream` выполняет команду с потоковым выводом через Server-Sent Events.

```
// SSE Events
event: stdout
data: {"line": "output line"}

event: stderr
data: {"line": "error line"}

event: done
data: {"exit_code": 0, "execution_time_ms": 1234, "success": true}

event: error
data: {"message": "error description"}
```

**Диагностика NIC** (`GET /api/system/nic-info`):

Определяет активный режим NIC-тюнинга и аппаратные возможности multiqueue для каждого физического интерфейса с поднятым линком. Используется панелью на вкладке «Системные оптимизации» для отображения диагностики — оператор выбирает режим самостоятельно.

```json
// Response
{
    "nic_mode": "multiqueue",
    "multiqueue_supported": true,
    "cpu_cores": 4,
    "cpu_threads": 8,
    "interfaces": [
        {
            "name": "eth0",
            "max_hw_queues": 4,
            "current_hw_queues": 4
        }
    ]
}
```

Поля:
- `nic_mode` — активный режим: `"rps"`, `"multiqueue"`, `"hybrid"` или `"none"` (определяется по enabled-статусу systemd-сервисов)
- `multiqueue_supported` — `true`, если хотя бы один интерфейс имеет `max_hw_queues > 1`
- `cpu_cores` — число физических ядер CPU (через `lscpu`)
- `cpu_threads` — число логических потоков CPU (`nproc`)
- `interfaces[].max_hw_queues` — максимальное число аппаратных очередей; при наличии `Combined` — берётся оно, иначе `max(RX, TX)`; если ethtool не поддерживает channels API — fallback на подсчёт `rx-*` в sysfs
- `interfaces[].current_hw_queues` — текущее активное число очередей

Алгоритм определения очередей (`detect_iface_hw_queues`) зеркалит `get_max_hw_queues()` из `install.sh` и корректно обрабатывает карты с `Combined: n/a` (mlx4_en, часть igb/ixgbe), которые показывают только раздельные RX/TX.

**Механизм обновления**:
1. API создаёт временный контейнер `monitoring-updater` (образ `docker:cli`)
2. Контейнер клонирует свежий код из GitHub (main или указанная ветка — панель подставляет выбранный канал обновлений, если ref не передан явно)
3. Запускает `update.sh` из склонированной папки, передавая исходный `TARGET_REF` 4-м аргументом
4. `update.sh` скачивает репо и запускает **свежий** `scripts/apply-update.sh` из скачанной версии, тоже с `TARGET_REF` 4-м аргументом
5. `apply-update.sh` выполняет обновление: копирование файлов и получение образов (pull/сборка) — до остановки контейнеров, затем миграции, `docker compose down` + `up`. Если ref — `main`/`dev`, перед `pull` в `.env` пишется `MON_IMAGE_TAG=latest|dev` (при обновлении на конкретный тег/коммит канал не меняется — используется уже сохранённое значение); если `apply-update.sh` запущен старым апдейтером без ref-аргумента — ветка вычитывается из `.git/HEAD` скачанного клона
6. Контейнер удаляется после завершения

Обновление **всегда** использует актуальную версию логики из GitHub (двойная загрузка гарантирует свежесть).

**Устойчивость к медленной сети**

Порядок обновления — «сначала скачать, потом рестартовать»: rsync файлов и `docker compose pull` выполняются **до** остановки контейнеров, нода продолжает работать на старой версии всё время скачивания; `docker compose down` + `up` — только после успешного получения образов, даунтайм сокращается до секунд рестарта. Если pull и fallback-сборка не удались — обновление отменяется **без остановки контейнеров**, нода остаётся на старой версии.

- Бюджет pull: `DOCKER_PULL_TIMEOUT` 1800с на попытку (переопределяется env) × 3 попытки — docker переиспользует уже скачанные слои между попытками; таймаут fallback-сборки 900с. Причина: на нодах с очень медленной сетью слой образа может качаться 10-15+ минут (наблюдалось 755с) — меньший бюджет обрывал бы обновление на середине.
- Если `docker compose up -d` упал на ожидании healthy у api (`depends_on`), оставив nginx в статусе `Created`, контейнеры поднимаются напрямую: `docker compose start` → `docker start monitoring-api monitoring-nginx`; тот же прямой старт выполняется и в recovery-trap.
- Ожидание updater-контейнера в node-API (`system.py`): `container.wait` с таймаутом `UPDATER_WAIT_TIMEOUT = 7200` (2 часа) — при коротком таймауте обновление дольше него ошибочно помечалось бы «failed», а повторный запуск убивал бы ещё работающий updater.
- rsync при работающих контейнерах безопасен: замена файлов идёт через rename (новый inode), bind-mounts запущенных контейнеров видят старые файлы до рестарта.
- Каждый вызов Docker SDK (`get_docker_client`, `containers.get/run/wait/logs/remove`, `images.pull`) выполняется через `asyncio.to_thread` — SDK синхронный и общается с сокетом через `requests`; без обёртки pull образа на медленной сети (десятки минут) держал бы event loop и ронял бы `/health` посреди обновления. Флаг `in_progress` роутер выставляет **до** `asyncio.create_task`, а не внутри задачи — иначе два быстрых подряд запроса на обновление успевали бы пройти проверку раньше старта первого апдейтера.

### Метрики

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/metrics | Все метрики |
| GET | /health | Health check |

**Всё для учёта трафика едет одним ответом `/api/metrics`** — отдельного опроса нода не требует, панель и так дёргает метрики каждые несколько секунд, а лишний запрос на каждую ноду умножался бы на размер парка:

- `network.interfaces[].rx_bytes`/`tx_bytes` и `network.total` — кумулятивные счётчики интерфейсов из `/proc/net/dev`.
- `network.ports[]` — кумулятивные счётчики цепочек учёта по портам: `{port, rx_bytes, tx_bytes}`. Рядом `network.ports_available` (доступен ли iptables на хосте) и `network.ports_sampled_at` (unix-время последнего замера — по нему панель отличает свежий снимок от повторно прочитанного).
- `system.boot_id` — `/proc/sys/kernel/random/boot_id`, меняется только с перезагрузкой хоста. По нему панель точно отличает ребут от сбоя счётчика: после ребута счётчики начинаются с нуля, и разница со старым значением была бы мусором.
- `agent_version` — версия агента. Панель по ней решает, поддерживает ли нода новый учёт трафика.

Дельты и историю байт из этих значений считает панель — см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#traffic).

**Скорости — за последнюю секунду, считает нода.** Фоновый посекундный семплер (`services/rate_sampler.py`, см. «Производительность») даёт в том же ответе:

- `network.interfaces[].rx_bytes_per_sec`/`tx_bytes_per_sec` — по каждому интерфейсу; `network.total.*_bytes_per_sec` — сумма по физическим (без veth/docker/br-*/bond-слейвов — их трафик уже есть на физических).
- `disk.io[<dev>].read_bytes_per_sec`/`write_bytes_per_sec` — по каждому блочному устройству; `disk.io_total` — сумма только по целым дискам (`/sys/block/<dev>` существует): раздел `sda1` уже внутри счётчика `sda`.
- `cpu.usage_percent`/`per_cpu_percent` — занятость за то же окно.
- `live_rates: {window_sec, sampled_at}` — маркер, что скорости в ответе реальные; по нему панель решает, брать их или считать дельты сама. Пока свежего замера нет (первая секунда после старта, семплер замолчал дольше `STALE_AFTER_SEC`), маркер — `null`, скорости — `0.0`, CPU — пустой список.

**Поле `antiddos` в `/api/metrics` (модель `AntiDdosInfo`, `AllMetrics.antiddos`):** режим, источник, время перехода в аварийный режим, состояние watchdog, заполнение conntrack, `insert_failed`, `SyncookiesSent`, дропы из `/proc/net/softnet_stat`, `ListenDrops`/`ListenOverflows`. Метод `MetricsCollector.get_antiddos_info()` (`metrics_collector.py`) читает `/proc` напрямую из смонтированного `/host/proc` — без `nsenter`, дешевле на каждый опрос метрик, в отличие от `antiddos_manager.py`, который дёргает `ddos-watchdog.sh` через nsenter только для управляющих команд. Хелперы: `_read_proc_int()`, `_read_hex_column_sum()` (суммирование по CPU из `/proc/net/stat/nf_conntrack`), `_read_netstat_counters()`.

### Traffic

Историю трафика ведёт панель. Нода не хранит ни бакетов, ни агрегатов: сырые кумулятивные счётчики уезжают в составе `GET /api/metrics` (см. «Метрики» выше), а этот роутер только управляет тем, за какими портами счётчики ведутся, и отдаёт легаси-историю на перенос.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/traffic/ports/tracked | Отслеживаемые порты |
| POST | /api/traffic/ports/add | Добавить порт в учёт |
| POST | /api/traffic/ports/remove | Убрать порт из учёта |
| GET | /api/traffic/legacy/export | Дневные итоги легаси-БД для одноразового импорта в панель (`max_days`, максимум 400) |
| POST | /api/traffic/legacy/purge | Удалить легаси-БД после успешного импорта |

**Счётчики по портам (`port_traffic_sampler.py`).** Учёт держится на двух собственных цепочках — `TRAFFIC_ACCOUNTING_IN` (вджамплена в `INPUT`) и `TRAFFIC_ACCOUNTING_OUT` (в `OUTPUT`). На каждый отслеживаемый порт в них добавляется по правилу-счётчику для tcp и udp: `--dport` во входящей цепочке, `--sport` в исходящей.

- Замер выполняет фоновая задача с интервалом `port_sample_interval` (30 с), а обработчик метрик только копирует готовый снимок из памяти. У `/api/metrics` жёсткий бюджет (nginx ноды — `proxy_read_timeout 10s`, столько же read timeout у панели), а дамп iptables под конкурентной блокировкой xtables съедает его целиком: медленно отвечающая нода для панели неотличима от упавшей.
- Дамп читается как `iptables-save -c` и разбирается по токенам правила (`--dport 80`). В выводе `iptables -L` подстрока `dpt:80` совпадает и внутри `dpt:8080`, из-за чего трафик 8080-го приписывался бы 80-му.
- Изменяющие команды идут с `iptables -w 5`: без ожидания блокировки команда падает, как только ufw или ipset держат xtables, и правило теряется молча.
- Цепочки перепроверяются раз в 10 минут (`CHAIN_RECHECK_INTERVAL_SEC`) — `ufw --force reset` при применении Firewall Profile сносит их целиком. Если очередной замер (`_sample()`) не находит правило для отслеживаемого порта — это сами правила пропали, а не «трафика не было»: порт выпадает из снимка вместо ложного нуля (отдать ноль означало бы шаг счётчика назад, который панель списала бы на сброс и потеряла бы накопленную дельту), а цикл сразу, не дожидаясь плановой десятиминутной перепроверки, поднимает цепочки заново и переснимает счётчики.
- Добавление и удаление порта выполняют внеочередной замер под тем же локом, что и фоновый: иначе более старый дамп лёг бы последним, панель увидела бы шаг счётчика назад, списала бы это на сброс и молча потеряла дельту за окно.
- Список отслеживаемых портов хранится в `traffic_config.json` рядом с файлом легаси-БД и переживает рестарт контейнера.
- Если iptables на хосте недоступен, `network.ports_available` равен `false`, а список счётчиков пуст — учёт по портам выключен, остальные метрики не страдают.

**Легаси-история (`legacy_traffic_store.py`).** На части нод лежит SQLite-файл с историей, накопленной до переноса учёта в панель. Доступ к ней строго на чтение: панель один раз забирает дневные итоги через `/legacy/export` и после успешного импорта подтверждает удаление через `/legacy/purge`. Сама нода файл не удаляет ни при каких обстоятельствах — иначе стал бы важен порядок обновления, и обновившаяся первой нода стёрла бы историю, которую панель ещё не забрала.

- Выгрузка идёт с `SUM` и `GROUP BY` по суткам, а не построчно: в легаси-схеме `UNIQUE`-ключ допускает NULL, поэтому UPSERT в ней не срабатывал и на один и тот же день лежит множество строк — построчное чтение отдало бы тысячи мусорных точек вместо дневных итогов.
- Нечитаемая БД отдаётся как `503`, а не как пустая выгрузка: панель не должна принять сбой чтения за «истории нет» и разрешить удаление. Подключение открывается в режиме `mode=ro`, при неудаче — повторно с `immutable=1` (БД, оставшаяся с включённым WAL без файла `-shm`, read-only подключением не открывается).
- `purge` удаляет БД вместе с `-wal`/`-shm` и оставляет тумбстоун `legacy_purged.json`, поэтому повторный вызов — no-op. `traffic_config.json` не трогается: это конфигурация правил iptables, а не история.

Витрины `GET /api/traffic/{hourly,daily,monthly,summary,ports,interfaces}` читают ту же легаси-БД, тоже только на чтение, и ничего в неё не пишут. Они существуют потому, что «Обновить всё» обновляет сначала ноды и только потом панель: в этом окне новая нода отвечает старой панели, а фолбэк на кэш у той срабатывает только по 5xx — на 404 страница трафика покраснела бы. По той же причине любая недоступность БД отдаётся здесь пустым набором, а не ошибкой.

**Файлы:**
- `node/app/services/port_traffic_sampler.py` — цепочки учёта, фоновый замер, `snapshot()` для сбора метрик, добавление/удаление портов
- `node/app/services/legacy_traffic_store.py` — read-only витрины и экспорт легаси-БД, `purge()` с тумбстоуном
- `node/app/routers/traffic.py` — API роутер
- `node/tests/test_port_counters.py` — разбор дампа `iptables-save` (80 не забирает трафик 8080, направление по имени цепочки, чужие цепочки и правила с диапазоном портов игнорируются), `snapshot()` не ходит в subprocess и отдаёт копию, поведение без iptables, валидация границ порта и round-trip списка портов через `traffic_config.json`
- `node/tests/test_legacy_export.py` — схлопывание дублей в дневные итоги, окно и потолок строк выгрузки, БД без таблицы и отсутствующий файл, подключение отказывает в записи и не меняет строки, `purge` оставляет `traffic_config.json`, пишет тумбстоун и идемпотентен

### HAProxy

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/haproxy/status | Статус сервиса |
| GET | /api/haproxy/stats | Живая статистика из stats socket (`show stat` + `show info`), read-only |
| GET | /api/haproxy/rules | Список правил |
| POST | /api/haproxy/rules | Создать правило |
| PUT | /api/haproxy/rules/{name} | Обновить правило |
| DELETE | /api/haproxy/rules/{name} | Удалить правило |
| POST | /api/haproxy/start | Запустить (systemctl start) |
| POST | /api/haproxy/stop | Остановить (systemctl stop) |
| POST | /api/haproxy/reload | Reload конфига (systemctl reload) |
| POST | /api/haproxy/restart | Restart сервиса (systemctl restart) |

При ошибке `/start`, `/stop`, `/reload`, `/restart` возвращают `HTTP 500` с полем `detail`, содержащим реальную причину от менеджера (например: `"Restart failed: ..."`, `"Config validation failed: ..."`, `"HAProxy is not installed"`, `"Failed to stop: ..."`). Панель транслирует это сообщение в результатах массовых действий по каждому серверу.
| GET | /api/haproxy/config | Получить конфиг |
| POST | /api/haproxy/config/apply | Применить конфиг; тело `ConfigApplyRequest`: `config_content`, `ensure_started: bool = False` — при `True` поднимает остановленный HAProxy через `reload(auto_start=True)` |

Изменения конфига (создание/обновление/удаление правила, apply) **и** сертификатные эндпоинты (generate, renew всех/одного, delete, upload) на роутере сериализованы общим `asyncio.Lock` (`_config_lock`) — панель шлёт такие запросы пачками (bulk-действия), а методы менеджера уходят в тред-пул, без лока параллельные правки затирали бы друг друга. На стороне менеджера снимок для отката (`_config_rollback()`) пишется в файл с уникальным именем (`haproxy.cfg.<uuid>.bak`) вместо общего `haproxy.cfg.bak` — общее имя означало бы, что откат одной операции подхватывает снимок другой, идущей параллельно, и возвращает конфиг к чужому состоянию.

Поля `target_ip`/`cert_domain`/`target_port` валидируются регулярками (`TARGET_HOST_PATTERN`, `DOMAIN_PATTERN`/`OPTIONAL_DOMAIN_PATTERN` в `models/haproxy.py`) — оба значения подставляются прямо в текст `haproxy.cfg`, а домен сертификата ещё и в путь к файлу сертификата; белый список символов отсекает пробелы, переводы строк и `../` до того, как значение попадёт в конфиг или путь.

Выпуск/продление сертификата через certbot ограничены таймаутом (`CERTBOT_ISSUE_TIMEOUT_SEC = 120`, `CERTBOT_RENEW_TIMEOUT_SEC = 300`): без него зависший certbot держал бы запрос, а при последовательных вызовах — и конфиг-лок бесконечно. `get_cert_info()` кэширует результат `openssl x509 -enddate` на час (`CERT_INFO_CACHE_TTL_SEC`) и вызывает `openssl` с таймаутом (`OPENSSL_TIMEOUT_SEC = 10`) — без кэша это форк `openssl` на каждый домен при каждом опросе метрик; кэш инвалидируется при выпуске/продлении/загрузке/удалении сертификата.

Блокирующие шаги выпуска/продления (`_release_port_80`/`_reclaim_port_80`, поиск каталога сертификата, сборка combined-файла, чтение списка доступных сертификатов, `reload`) в `generate_certificate`/`renew_certificates`/`renew_certificate` выполняются через `asyncio.to_thread` — без этого certbot и файловые операции держали бы event loop роутера на всё время выпуска, замораживая `/health` и весь остальной API ноды.

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

### Remnawave

Проверка доступности контейнера `remnanode`, плюс управление nginx-конфигом установки Remnawave на хосте (каталог по умолчанию `/opt/remnawave`).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/remnawave/status | Статус контейнера remnanode (available: true/false) |
| GET | /api/remnawave/nginx/discover?path= | Обнаружить установку: каталог/compose/nginx.conf/ssl, mount-путь конфига в контейнере, состояние `remnawave-nginx` (exists/running/image), sha256 конфига |
| GET | /api/remnawave/nginx/config?path= | Получить содержимое nginx.conf с хоста |
| GET | /api/remnawave/nginx/status?path= | Лёгкий статус: состояние контейнера + хэш конфига |
| GET | /api/remnawave/nginx/logs?tail= | Логи контейнера `remnawave-nginx` (`docker logs`) |
| POST | /api/remnawave/nginx/config/apply | Применить конфиг: `{path, content, reload_after, restart, ensure_started}` — валидация (`nginx -t`) встроена в применение, отдельного эндпоинта для неё нет |
| POST | /api/remnawave/nginx/reload | `nginx -s reload` с pre-check `nginx -t` |
| POST | /api/remnawave/nginx/restart | `docker restart remnawave-nginx` |

`GET /api/remnawave/status` используется панелью для определения поля `has_xray_node` у сервера (обновляется каждые 2 минуты). Остальные эндпоинты — для страницы «Remnawave Nginx» в панели (см. panel/DOCUMENTATION.md), путь по умолчанию `/opt/remnawave`.

**Критический инвариант: `nginx.conf` — single-file bind-mount.** Docker Compose монтирует конфиг в контейнер `remnawave-nginx` как один файл, не каталог. Любая запись или откат обязаны идти **in-place** (перезапись содержимого существующего inode) — `mv`/`rename` заменили бы файл новым inode, который перестал бы быть тем же смонтированным файлом внутри контейнера, и nginx продолжил бы читать старое содержимое до пересоздания контейнера. `RemnawaveNginxManager` (`remnawave_nginx_manager.py`) и общий `write_host_file()` (`host_files.py`) соблюдают это на каждом шаге.

**Точное чтение файлов (`read_host_file_exact`) — обязательно для nginx.conf и docker-compose.yml.** Общий `read_host_file()` (`host_files.py`) делает `.strip()` над результатом `HostExecutor.execute()` (`host_executor.py`) — для построчного парсинга в `system.py` это безобидно, но для nginx.conf терялся бы завершающий перевод строки в двух местах: откат после неудачного apply записал бы контент без финального `\n` (функционально верно, но не байт-в-байт исходный файл), а хэш, который нода отдаёт в `GET /api/remnawave/nginx/config` и `/nginx/status`, никогда не совпал бы с эталоном `Server.remnawave_nginx_node_hash` (считается от записанного контента) — reconciler видел бы мнимый drift и делал лишний ресинк при каждом переходе ноды offline→online. Поэтому `read_host_file_exact(path)` (`host_files.py`) переносит содержимое через `base64 -w0` и декодирует — у base64 нет пробельных краёв, `.strip()` его не портит; пустой файл возвращается как `""`, ошибка чтения — `None`. Используется во всех чтениях nginx.conf и docker-compose.yml (`discover`, `get_config`, `status`, `_compose_nofile`, `_prepare_compose_patch`, `apply_config`); `read_host_file()` остаётся только для `system.py`.

**Транзакция `apply_config()`:**
1. Backup текущего файла (`cp` на `.bak`)
2. In-place запись нового содержимого
3. `docker exec remnawave-nginx nginx -t` — проверка синтаксиса внутри контейнера
4. При ошибке — откат из backup, конфиг остаётся прежним
5. `nginx -s reload` (или `docker restart`, если запрошено) — при неудаче reload тоже откат + повторный reload
6. `asyncio.Lock` защищает от конкурентных apply

Если контейнер `remnawave-nginx` остановлен: валидация идёт через одноразовый `docker run --rm` с монтированием `/etc/letsencrypt` и `ssl/` вместо `docker exec` в живой контейнер; после записи опционально поднимается `docker compose up -d` через nsenter — единственная nsenter-операция во всём модуле, остальное (`docker exec`, `docker logs`, `docker restart`, `docker inspect`) идёт через Docker CLI напрямую через сокет.

**Валидация (`validate_content`):** кандидат-конфиг нельзя проверить через `docker cp` — путь на хосте не существует в файловой системе контейнера агента (агент сам работает в контейнере). Вместо этого содержимое загружается в живой контейнер `remnawave-nginx` через `docker exec -i sh -c 'cat > /tmp/... '`, затем `nginx -t -c` на этом временном файле.

Путь установки валидируется regex `^/[A-Za-z0-9._/-]+$` (`InvalidInstallPathError` при нарушении) — путь приходит от панели, и это единственная защита от произвольной записи по хосту.

**Автоподстановка host-специфичных лимитов (`# auto: node`) — прямой аналог `_ensure_global_maxconn` у HAProxy.** Профиль от панели один на много нод с разной RAM/nofile, поэтому три строки шаблона, помеченные маркером `AUTO_MARKER = "# auto: node"` (`worker_rlimit_nofile`, `worker_connections`, `ssl_session_cache`), нода пересчитывает под свой хост при каждом применении. Если оператор вручную убрал маркер — строка считается заданной явно и не трогается.

- `compute_host_limits(compose_nofile)` — `worker_rlimit_nofile` = `DOCKER_NOFILE` (иначе `NOFILE_LIMIT`) из `/opt/monitoring/configs/tuning-facts.env`; при недоступности facts — `ulimits.nofile.soft` из docker-compose.yml установки, иначе 65536. `worker_connections` = nofile/4 (2 файловых дескриптора на проксируемое соединение с запасом), clamp `[1024, 65536]`. `ssl_session_cache` = MemTotal_MB/100 МБ, clamp `[10, 100]`.
- `patch_host_limits(content, limits)` — идемпотентная regex-замена значения только у строк с `AUTO_MARKER`.
- `RemnawaveNginxManager.apply_host_limits(path, content)` вызывается и в `apply_config()` (до вычисления хэша и записи), и в `validate_content()` — валидация и реальное применение видят один и тот же итоговый контент.
- Причина: жёсткий ulimit контейнера `remnawave-nginx` задаётся `DOCKER_NOFILE` из фактов рендерера (`clamp(pow2_floor(MemMB*64), 65536, 2097152)`) — статический `worker_rlimit_nofile` (например 131072) на 1-ГБ ноде давал бы `setrlimit(...) failed (EPERM)` и молча оставлял бы старый лимит, а на 8–16 ГБ занижал бы потолок вместо использования доступного.

**Автоперевод install.sh-установок с фрагментного монтирования на полный конфиг.** Установки Remnawave через пункт меню 9 монтируют `nginx.conf` как фрагмент http-контекста (`./nginx.conf:/etc/nginx/conf.d/default.conf:ro` в docker-compose.yml установки) — внутри лежит кусок конфига (`map`, `ssl_*`, маскировочный `server` с `listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server`), без обёртки `worker_*`/`events`/`http`. Профиль панели генерирует **полный** `nginx.conf` — на такой ноде `nginx -t` падал бы (`http{}` внутри `http{}`), apply откатывался бы, сервер уходил бы в failed. При первом применении профиля нода сама переводит установку на полный конфиг.

- `FULL_CONFIG_MOUNT = "/etc/nginx/nginx.conf"` — целевая точка монтирования.
- `compose_mount_target(compose)` — читает текущую точку монтирования `nginx.conf` из docker-compose.yml установки.
- `patch_compose_mount(compose)` — переводит её на `FULL_CONFIG_MOUNT`; regex не трогает остальные тома и суффикс `:ro`; возвращает `None`, если менять нечего (конфиг уже полный или монтирования нет).
- Побочный эффект перевода: маскировочный server-блок с unix-сокетом из старого фрагмента исчезает вместе с ним — если он используется, оператор переносит его в профиль через raw-редактор панели.

**Проверка и автомонтирование путей сертификатов.** Профиль панели рендерит пути `ssl_certificate`/`ssl_certificate_key` как есть (обычно `/etc/letsencrypt/live/{{DOMAIN}}/...`), но если `docker-compose.yml` установки Remnawave не монтирует `/etc/letsencrypt` в контейнер `remnawave-nginx` — `nginx -t` внутри контейнера не видит существующий на хосте сертификат и падает с малопонятной `cannot load certificate ... BIO_new_file() failed`. Свежие установки получают маунт сразу (`install.sh` дописывает `- /etc/letsencrypt:/etc/letsencrypt:ro` в volumes сервиса), а на установках без маунта нода сама обнаруживает и чинит нехватку при первом apply:

- `config_cert_files(content)` — извлекает пути `ssl_certificate`/`ssl_certificate_key` из конфига (без дублей, только «безопасные» пути через `_SAFE_PATH_RE`).
- `host_path_for_cert(cert_file, compose, install_path)` — путь из конфига — это путь **внутри контейнера**; функция маппит его на хостовый путь через существующие тома docker-compose.yml (относительный источник `./ssl` разворачивается в `{install_path}/ssl`), если маунта нет — возвращает путь как есть (значит на хосте он лежит по тому же пути).
- `missing_certs_on_host(path, content)` — по хостовым путям проверяет существование файлов на хосте (`[ -e ... ]` через `HostExecutor`); если файла нет, `validate_content()`/`apply_config()` возвращают понятную ошибку «Сертификат не найден на хосте: `<путь>`. Выпустите сертификат ... или исправьте пути в опциях профиля» **до** запуска `nginx -t` — это самая частая причина отказа, и понятная ошибка избавляет оператора от разбора сырого вывода nginx.
- `cert_mount_dir(cert_file)` / `is_covered_by_mounts(path, targets)` / `compose_mount_targets(compose)` / `missing_cert_mounts(compose, cert_files)` — вычисляют, какие каталоги сертификатов (включая кастомные пути из опций профиля) не покрыты ни одним volume сервиса `remnawave-nginx`. Для путей под `/etc/letsencrypt` монтируется весь корень (`LETSENCRYPT_ROOT`), а не подкаталог `live/{domain}` — файлы там являются симлинками в `../../archive`, и узкий маунт дал бы контейнеру битые ссылки.
- `patch_compose_volumes(compose, dirs)` — дописывает найденные каталоги как `- <dir>:<dir>:ro` в volumes сервиса `remnawave-nginx`; точка вставки — строка монтирования `./nginx.conf`, единственная однозначно принадлежащая этому сервису (у `remnanode` её нет), поэтому `remnanode` не затрагивается.
- `_certs_not_visible_in_container(cert_files)` — отдельно от «compose не содержит нужный volume» обнаруживается «устаревший» контейнер: compose уже пропатчен (текущим или предыдущим apply), но контейнер создан **до** этого и физически не видит маунт — сверяется через `docker inspect -f '{{range .Mounts}}{{.Destination}}...'`.
- `explain_validation_error(output)` — к ошибкам `nginx -t` с `cannot load certificate`/`BIO_new_file` дописывает `CERT_HINT`: подсказку, что nginx ищет файл внутри контейнера, а не на хосте, и что контейнер, созданный до добавления volume, нужно пересоздать вручную (`docker compose up -d --force-recreate remnawave-nginx`) — на случай, если автопатч почему-то не сработал.

**Единый транзакционный путь пересборки compose (`_prepare_compose_patch`/`_apply_with_recreate`).** Механизм перевода фрагментного монтирования на полный конфиг и механизм автомонтирования сертификатов решают разные проблемы, но оба требуют одного и того же — правки `docker-compose.yml` и пересоздания контейнера — поэтому реализованы одним общим путём:

- `_prepare_compose_patch(path, cert_files)` — считает нужный патч compose: сначала `patch_compose_mount` (перевод точки монтирования), затем `missing_cert_mounts`/`patch_compose_volumes` поверх уже переведённого compose; возвращает `None`, если менять нечего, иначе `{path, current, patched, remounted, added_volumes}`.
- В `apply_config()` пересоздание запускается, если `_prepare_compose_patch` вернул патч **или** compose уже в порядке, но `_certs_not_visible_in_container` нашла невидимые контейнеру сертификаты у уже работающего контейнера.
- `_apply_with_recreate(...)` — сама транзакция: валидация кандидата одноразовым контейнером **до** любых правок (`validate_content(..., force_one_off=True)` — живому контейнеру может не хватать тех самых маунтов, поэтому проверка идёт через `docker run --rm` с добавленными каталогами) → backup+запись пропатченного `docker-compose.yml` (если патчился) → backup+запись `nginx.conf` → `docker compose up -d --force-recreate remnawave-nginx` (смена точки монтирования и новые volume не подхватываются `reload`) → повторный `nginx -t` в новом контейнере. Любая неудача на любом шаге — откат обоих файлов и обратное пересоздание контейнера.
- `_compose_up(path, force_recreate=False)` — общий helper запуска установки, флаг пересоздания используется здесь и в обычном пути (`ensure_started`).
- Ответ содержит `remounted` (перевод монтирования) и словесное перечисление изменений («монтирование переведено на полный nginx.conf», «в docker-compose.yml добавлены тома сертификатов: ...», «контейнер пересоздан») — `NginxApplyResponse.remounted` панель логирует в лог синхронизации профиля (см. panel/DOCUMENTATION.md).

**Файлы:**
- `node/app/services/remnawave_nginx_manager.py` — `RemnawaveNginxManager` (синглтон через `get_remnawave_nginx_manager()`): `discover`, `get_config`, `status`, `logs`, `validate_content`, `apply_config`, `apply_host_limits`, `missing_certs_on_host`, `reload`, `restart`; модульные `compute_host_limits()`, `patch_host_limits()`, `compose_mount_target()`, `patch_compose_mount()`, `config_cert_files()`, `cert_mount_dir()`, `is_covered_by_mounts()`, `compose_mount_targets()`, `missing_cert_mounts()`, `patch_compose_volumes()`, `host_path_for_cert()`, `explain_validation_error()`, `AUTO_MARKER`, `FULL_CONFIG_MOUNT`, `LETSENCRYPT_ROOT`, `CERT_HINT`
- `node/app/services/host_files.py` — `read_host_file()`/`read_host_file_exact()`/`write_host_file()` и его синхронный близнец `write_host_file_sync()` для менеджеров, целиком живущих в тред-пуле (обе версии строят команду и оценивают результат общими `_write_command()`/`_write_succeeded()`); общий модуль: переиспользуется системными оптимизациями (`routers/system.py`), файрволом и этим модулем
- `node/app/models/remnawave_nginx.py` — Pydantic-модели ответов/запросов, включая `NginxApplyResponse.remounted`
- `node/app/routers/remnawave.py` — API роутер
- `node/nginx/nginx.conf` — `location /api/remnawave/` с таймаутом 120с (apply может включать `docker compose up`)
- `node/tests/test_remnawave_nginx_limits.py` — тесты автоподстановки лимитов (подстановка только помеченных строк, идемпотентность, ручные правки без маркера не затираются, разумность вычисленных значений) плюс класс `ComposeMountTests` — определение фрагментного/полного монтирования, патч фрагмент→полный с сохранением прочих томов, отсутствие патча для уже полного конфига и для compose без монтирования
- `node/tests/test_remnawave_nginx_cert_mounts.py` — тесты на чистых функциях: извлечение путей сертификатов без дублей и с отсевом небезопасных путей, вычисление каталога монтирования (весь корень для `/etc/letsencrypt`, родительский каталог для кастомных путей), недостающие маунты (в т.ч. что уже смонтированный `/etc/letsencrypt` и относительный `./ssl` покрывают вложенные пути, совпадение по компоненту пути, а не по префиксу строки), вставка volume ровно в сервис `remnawave-nginx` без затрагивания `remnanode` и идемпотентно, маппинг путь-в-контейнере → путь-на-хосте (относительный и абсолютный источник маунта, немонтированный путь остаётся собой), подсказка `CERT_HINT` добавляется только к ошибкам про сертификат
- `node/tests/test_host_files.py` — `read_host_file()` теряет завершающий перевод строки, `read_host_file_exact()` возвращает контент байт-в-байт, отсутствующий файл даёт `None`; класс `WriteWithModeTests` — права выставляются `umask`'ом до записи содержимого, отказ `chmod` на файловой системе с фиксированными правами не проваливает запись при попадании в границы (владелец не меньше, «прочие» не больше запрошенного), world-readable итог и итог без бита исполнения у владельца считаются провалом, точное совпадение режима — успех, запись без `mode` не трогает umask/chmod
- `node/tests/test_haproxy_parsing.py` — чистые части `haproxy_manager`: разбор server-строк со всеми опциями (`send-proxy-v2` не выставляет заодно `send-proxy`), подстановка `resolvers` только доменным таргетам и только один раз, разбор опций балансировщика, распознавание правил в конфиге (балансировщик против одиночного таргета, backend без frontend игнорируется), расчёт `maxconn` от RAM с потолком по лимиту дескрипторов, вставка `maxconn` в `global` без затирания явного значения
- `node/tests/test_sshd_config.py` — сборка `sshd_config`: закомментированные директивы не оживают, содержимое `Match`-блоков копируется дословно, недостающие ключи встают перед первым `Match`, повторный прогон ничего не меняет; разбор конфига по правилу первого вхождения, преобразование значений туда-обратно, разбор секции fail2ban и единиц времени бана
- `node/tests/test_update_ref_validation.py` — валидация ссылки обновления и адреса прокси: пропускает ветки, теги версий и хеши коммитов (путь отката), отклоняет метасимволы shell и ведущий дефис
- Всего тестов ноды — 229 (`python -m unittest discover -s node/tests`)

### IPSet Blocklist

Блокировка IP/CIDR через ipset. Два типа списков:
- **Блок-список** — `blocklist_permanent` (постоянный) и `blocklist_temp` (временный с таймаутом); направления: in (INPUT) и out (OUTPUT).
- **Белый список (allowlist)** — доверенные IP/CIDR, которые **всегда** проходят через ноду вне зависимости от блокировок.

**Защита от приватных диапазонов:** нода самостоятельно отказывается добавлять в block-сеты IP/CIDR, пересекающиеся с приватными/служебными диапазонами (`0.0.0.0/8`, `10.0.0.0/8`, `127.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` и т.п. — полный список в `NON_PUBLIC_NETS`, `app/services/ipset_manager.py`), через `is_public_range()`. Проверка стоит в каждой точке входа: `add_ip`, `bulk_add`, `sync`, загрузка постоянного списка из `blocklist.json` при старте. Это defense-in-depth независимо от версии панели — панель фильтрует источники и ручные правила на своей стороне (см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#ip-blocklist)), но нода не доверяет входу целиком: DROP по приватному диапазону убивает loopback и docker-bridge самой ноды.

**Белый список:**

Сеты `allowlist` (in → INPUT, match src) и `allowlist_out` (out → OUTPUT, match dst), тип `hash:net`. Правило `iptables ... -j ACCEPT` всегда вставляется на позицию 1 в цепочке (выше всех DROP) — netfilter обходит цепочку сверху вниз и ACCEPT прерывает обход. Это корректно перекрывает и точечные блоки, и CIDR-перекрытия (например, разрешить `1.2.3.4` при заблокированном `1.2.3.0/24`). Белый список всегда permanent — временного режима нет.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ipset/status | Статус списков (count, timeout, allow_count) |
| GET | /api/ipset/list/{set_type} | Получить IP из списка (permanent/temp) |
| POST | /api/ipset/add | Добавить IP/CIDR |
| POST | /api/ipset/bulk-add | Массовое добавление |
| DELETE | /api/ipset/remove | Удалить IP/CIDR |
| POST | /api/ipset/bulk-remove | Массовое удаление |
| POST | /api/ipset/clear/{set_type} | Очистить список |
| PUT | /api/ipset/timeout | Изменить timeout temp списка |
| POST | /api/ipset/sync | Синхронизация блок-списка (замена всего списка, атомарный diff через `ipset restore`) |
| POST | /api/ipset/allowlist/sync | Синхронизация белого списка (замена) |

**`POST /api/ipset/allowlist/sync`** — принимает `AllowSyncRequest`:
- `ips` — массив IP/CIDR для белого списка
- `direction` — `"in"` или `"out"`

**Поля в `GET /api/ipset/status`** — `incoming.allow_count` и `outgoing.allow_count` (количество записей в allowlist).

`POST /api/ipset/sync` и `/bulk-add` дополнительно отдают `skipped_non_public` — сколько записей отброшено как приватные/служебные (см. «Защита от приватных диапазонов» выше).

**Особенности:**
- Тип ipset: `hash:net` (поддержка IP и CIDR)
- Правила iptables блок-списка: `INPUT/OUTPUT -m set --match-set blocklist_* src/dst -j DROP`
- Правила allowlist: `-I INPUT 1 ... -j ACCEPT` / `-I OUTPUT 1 ... -j ACCEPT` (позиция 1, выше DROP)
- Все постоянные правила сохраняются в `/var/lib/monitoring/blocklist.json` (ключи `in_allow`, `out_allow` для белого списка)
- При старте ноды: постоянные правила восстанавливаются, временный список пустой, allowlist загружается из персиста
- Массовые операции (`sync`, `bulk_add`, `bulk_remove`, `sync_allow`, загрузка permanent/allow из `blocklist.json` при старте) применяются одним вызовом `ipset -exist restore` вместо по-IP `ipset add`/`del` — десятки тысяч записей применяются за доли секунды; мутации сериализованы `threading.Lock` (`_mutate_lock`), взятым во всех операциях записи: `add_ip`/`remove_ip`/`clear_set`/`set_timeout`/`sync`/`sync_allow`/`bulk_add`/`bulk_remove` — параллельные запросы с панели не перемешивают друг другу diff
- Счётчики в `GET /api/ipset/status` читаются из заголовка `ipset list -t` (`Number of entries`), без выгрузки всего сета
- `_list_members(set_name)` различает «сет пуст» и «не смог прочитать» — при ошибке `ipset list` возвращает `None`, а не пустой список: `sync`/`sync_allow` в этом случае отказывают с ошибкой вместо того, чтобы посчитать diff от пустой базы и удалить всё; сохранение состояния на диск (`_save_config`) при `None` от любого из сохраняемых сетов отменяется целиком — частичный снимок стёр бы блокировки, которые ipset прямо сейчас держит. Сама запись идёт через временный файл + `os.replace` (не прямой `open(..., 'w')`) — обрыв на середине не оставляет битый JSON, который иначе читался бы только при следующем старте
- Лимит записей: сет создаётся с `maxelem 1000000` — список крупнее 1 млн записей нода принять не сможет, `ipset restore` завершится ошибкой (физический потолок ноды). Панель знает про этот лимит (константа `NODE_MAX_IPSET_ENTRIES`) и обрезает по нему источники и общий список ещё до отправки на ноду — источники сверх лимита помечаются ошибкой и исключаются из синка вместо попытки прогнать их через `ipset restore`, см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md#ip-blocklist)

### SSH Security

Управление SSH-безопасностью сервера: настройки `sshd_config`, fail2ban, authorized_keys. Все операции выполняются через `nsenter` на хост-системе.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/ssh/config | Текущие настройки sshd |
| POST | /api/ssh/config | Применить новые настройки |
| POST | /api/ssh/config/test | Валидация настроек без применения |
| GET | /api/ssh/fail2ban/status | Статус и настройки fail2ban SSH jail |
| POST | /api/ssh/fail2ban/config | Обновить конфигурацию fail2ban |
| GET | /api/ssh/fail2ban/banned | Список забаненных IP |
| POST | /api/ssh/fail2ban/unban | Разбанить IP |
| POST | /api/ssh/fail2ban/unban-all | Разбанить все IP |
| GET | /api/ssh/keys | Список SSH-ключей (authorized_keys) |
| POST | /api/ssh/keys | Добавить SSH-ключ |
| DELETE | /api/ssh/keys | Удалить SSH-ключ |
| POST | /api/ssh/password | Сменить пароль пользователя (chpasswd) |
| GET | /api/ssh/status | Общий статус SSH (sshd, fail2ban, ключи) |

**Механизмы безопасности при изменении sshd_config:**
- Автобэкап перед каждым изменением (хранятся последние 5 копий)
- `sshd -t` валидация перед применением
- Атомарная запись через temp file → `mv`
- `reload` вместо `restart` — сохраняет активные сессии
- Автовосстановление из последнего бэкапа если sshd не запустился
- Запрет отключить все методы аутентификации одновременно
- При смене порта — UFW правило открывается автоматически

Перед применением новых настроек `_clean_sshd_config_d()` вычищает из `sshd_config.d/*.conf` ключи, которые будет задавать основной конфиг (sshd берёт первое вхождение директивы, а файлы из `sshd_config.d` подключаются через `Include` раньше основного конфига — без вычистки заданное там значение молча перебивало бы применённое), и возвращает прежнее содержимое изменённых файлов. На любом пути отказа (`sshd -t` не проходит, sshd не поднялся после `reload`/`restart`, сбой на промежуточном шаге) `_rollback()` восстанавливает не только сам `sshd_config` из бэкапа, но и эти drop-in файлы (`_restore_dropins()`) — откат делает конфиг полностью таким, каким он был до попытки, а не только его основную часть.

`SSHConfigManager` целиком синхронный (subprocess-вызовы) и перенастройка sshd занимает минуты (ожидание порта, стоп/старт службы, установка fail2ban) — роутер выполняет каждый вызов через `asyncio.to_thread`, иначе один такой запрос замораживал бы event loop вместе с `/health`, и docker-healthcheck убивал бы контейнер посреди перенастройки. Изменения sshd-конфига и ключей сериализованы `asyncio.Lock` (`_sshd_lock`) — параллельное применение чередовало бы бэкап, подмену конфига и рестарт службы, а параллельное удаление ключа между проверкой «authorized_keys не пуст» и применением отрезало бы доступ к серверу; fail2ban-операции — отдельным `_fail2ban_lock` (apt-get и restart fail2ban не переживают параллельного запуска). Временные файлы (`test_sshd_config`/`write_sshd_config`) получают уникальное имя (`_unique_tmp_path`, суффикс `uuid4`) вместо фиксированного — иначе параллельный запрос мог подменить содержимое между валидацией `sshd -t` и `mv` в `/etc/ssh/sshd_config`.

**Файлы:**
- `node/app/services/ssh_config_manager.py` — работа с sshd_config, fail2ban, authorized_keys
- `node/app/routers/ssh.py` — API эндпоинты, `asyncio.to_thread` + локи (см. выше)

### Wildcard SSL

Деплой wildcard сертификатов на хост-систему ноды. Панель выпускает сертификат через certbot + Cloudflare DNS challenge и доставляет его на ноды через этот API.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/ssl/wildcard/deploy | Принять и задеплоить wildcard сертификат |

**`POST /api/ssl/wildcard/deploy`** — принимает `WildcardDeployRequest`:
- `fullchain_pem` — содержимое fullchain.pem
- `privkey_pem` — содержимое privkey.pem
- `deploy_path` — каталог на хосте для записи файлов; имена задаются `fullchain_filename`/`privkey_filename`, либо путь целиком — через `custom_fullchain_path`/`custom_privkey_path`
- `reload_command` — команда перезагрузки сервиса (например `systemctl reload nginx`)

Алгоритм деплоя:
1. Валидация сертификата через `cryptography` (`x509.load_pem_x509_certificate`) — разбор PEM происходит в процессе агента, без похода на хост
2. Целевые пути (`deploy_path`/кастомные `custom_fullchain_path`/`custom_privkey_path`) проверяются регуляркой `_SAFE_PATH_RE` (абсолютный путь, только буквы/цифры/`._/-`) перед подстановкой в shell-команды на хосте (`shlex.quote` дополнительно на каждый аргумент)
3. Бэкап текущих файлов по целевым путям (если существуют)
4. Запись новых файлов на хост через `write_host_file()` (nsenter + base64)
5. Выполнение `reload_command` (ограничен `MAX_RELOAD_COMMAND_LEN = 512` символов)
6. Откат из бэкапа при ошибке reload

PEM разбирается в процессе агента через `cryptography` (`x509.load_pem_x509_certificate`), а не внешним `openssl`: агент живёт в контейнере, а команды исполняет через `nsenter` в неймспейсах хоста, поэтому любая проверка через временный файл сверяла бы файл, которого на хосте нет. Из разобранного сертификата берутся CN и срок действия — они уходят в лог деплоя.

**Права файлов выставляет `umask` при создании, `chmod` — только доводка.** `write_host_file()` (`host_files.py`) с параметром `mode` формирует `umask` (`_umask_for()`) так, что файл создаётся сразу с целевыми правами — приватный ключ ни на миг не существует на диске в более широком доступе, чем запрошено. Отдельный `chmod` после записи идёт следом, но его отказ не проваливает деплой: итог сверяется по фактическим правам файла (`stat -c '%a'`, `_permissions_are_safe()`) по двум границам — владелец получил не меньше запрошенного (иначе скрипт с `mode=755` остался бы без бита исполнения, а конфиг — нечитаемым), «прочие» получили не больше запрошенного (иначе приватный ключ оказался бы доступен всем); биты группы между этими границами свободны. Это даёт деплою работать на `pmxcfs` (`/etc/pve` на Proxmox — FUSE-файловая система pve-cluster с фиксированными правами `0640 root:www-data`, где `chmod` всегда отвечает `Operation not permitted`, хотя сама запись проходит: `0640` укладывается в границы для запрошенного `600` — владелец `6` ⊇ `6`, «прочие» `0` ⊆ `0`).

**Файлы:**
- `node/app/models/ssl.py` — Pydantic модели
- `node/app/services/ssl_manager.py` — логика деплоя
- `node/app/routers/ssl.py` — API роутер
- `node/app/services/host_files.py` — `write_host_file()`: запись через nsenter+base64, права `umask`+best-effort `chmod` (см. выше)

### Firewall Profiles

Приём и атомарное применение UFW-профилей от панели. Защищён `asyncio.Lock` — одновременный apply невозможен.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| POST | /api/firewall/profile/apply | Применить профиль UFW |
| GET | /api/firewall/profile/state | Текущее состояние UFW + hash |

**`POST /api/firewall/profile/apply`** — принимает `ProfileApplyRequest`:
- `rules` — массив правил (`ProfileRule[]`)
- `default_incoming`, `default_outgoing` — политика UFW по умолчанию
- `force` — обойти node-API-port-guard (default: false)

Алгоритм apply:
1. Node-API-port-guard: если `default_incoming != 'allow'` и в правилах нет `allow <порт API>/tcp IN` и `force=False` — возвращает ошибку "Allow rule for node API port N/tcp missing — panel will lose connection to node. Use force=true to apply anyway". Порт берётся из `settings.node_api_port` (`NODE_API_PORT` в `.env`, по умолчанию 9100). Правило `with_from_ip` допустимо — проверяется только наличие allow-правила для порта, без требования `from any`. Тесты guard'а — `node/tests/test_firewall_api_port_guard.py`.
2. `_backup_state()` — снимок текущего UFW в `/etc/monitoring/ufw_backup_<timestamp>.json` (через nsenter); хранится максимум `MAX_BACKUPS=5`
3. `ufw reset` → установка политик → применение правил → `ufw enable`
4. При любой ошибке — `_restore_state(backup_path)` (автоматический rollback)

**Идемпотентность `add_advanced_rule()`:**

Метод `add_advanced_rule()` принимает параметр `skip_duplicate_check: bool = False`. Перед запуском ufw вызывает `_rule_already_present(port, protocol, action, from_ip, direction)` — проверяет активные правила через `list_rules`. Если идентичное правило уже существует и `skip_duplicate_check=False`, команда ufw не выполняется, возвращается `(True, "Rule already exists: ...", None)`. Результат success=True сохраняется намеренно: вызывающий код (например haproxy_manager) проверяет только флаг успеха.

`_apply_rules_list()` (путь apply_profile) вызывает `add_advanced_rule` с `skip_duplicate_check=True` — правила добавляются на чистый UFW после reset, лишние проверки `ufw status` не нужны.

Хелпер `_normalize_from(from_ip)`: нормализует источник правила — пустая строка, `any`, `anywhere` → `'anywhere'`. Используется при сравнении в `_rule_already_present`.

**`GET /api/firewall/profile/state`** — возвращает `ProfileStateResponse`:
- `rules` — активные правила UFW
- `default_incoming`, `default_outgoing` — текущие политики
- `rules_hash` — SHA256-хэш текущего состояния (для сравнения с хэшем профиля в панели)

**Node-API-port-guard:**

Три уровня защиты от потери связи панели с нодой:
1. Панель автозаполняет новый профиль правилом для порта 9100 при создании
2. Панель показывает баннер-предупреждение и индикатор-иконку в UI
3. Нода отклоняет apply, если нет `allow <NODE_API_PORT>/tcp IN` и `default_incoming != allow`, и `force=False`

**Бэкапы UFW:**

Хранятся в `/etc/monitoring/ufw_backup_<timestamp>.json` на хост-системе (через nsenter). При превышении `MAX_BACKUPS=5` старые удаляются. Запись файла идёт через общий `write_host_file_sync()` (`app/services/host_files.py`) — его результат отражает реальный код возврата записи на хосте, а не факт запуска команды; `_backup_state()` возвращает `None`, если бэкап физически не появился на диске, и в этом случае `apply_profile` не продолжает применение (откатывать после сбойного apply было бы нечем).

**Автоустановка UFW:**

Перед применением профиля `apply_profile` проверяет наличие `ufw` на хосте (`command -v ufw`). Если `ufw` не установлен — нода автоматически ставит его через `apt-get install -y -qq ufw` (сначала из кеша, при неудаче — `apt-get update` и повтор). Если установить не удалось — apply возвращает понятную ошибку «UFW недоступен на хосте: ...» вместо сообщения nsenter.

**Файлы:**
- `node/app/models/firewall_profile.py` — Pydantic модели
- `node/app/services/firewall_manager.py` — `FirewallManager`: `apply_profile`, `_ensure_ufw`, `_ufw_available`, `_install_ufw`, `_run_host`, `_backup_state`, `_restore_state`, `compute_rules_hash`, `get_full_state`, `_rule_already_present`, `_normalize_from`
- `node/app/services/container_detect.py` — `running_in_container()`: единственное определение работы в контейнере на ноде (см. ниже)
- `node/app/services/host_files.py` — `write_host_file_sync()`: синхронная запись файла на хост с проверкой реального результата
- `node/app/routers/firewall_profile.py` — API роутер (prefix `/api/firewall`)
- `node/app/main.py` — регистрация роутера (без auth-зависимости — mTLS терминируется на nginx, см. «Безопасность» выше)

**Определение работы в контейнере.** `running_in_container()` (`app/services/container_detect.py`) — единая точка правды, используемая `firewall_manager.py`, `ipset_manager.py`, `ssh_config_manager.py` и `host_executor.py`: проверяет `/.dockerenv`, а при его отсутствии (containerd, поды Kubernetes) ищет маркеры `docker`/`containerd`/`kubepods` в `/proc/1/cgroup`. До объединения существовало четыре независимые копии этой проверки, и одна из них (в sshd-менеджере) знала про containerd/kubepods, а остальные — только про `/.dockerenv`: на таком хосте sshd-менеджер шёл к хосту через `nsenter`, а файрвол и блокировки продолжали работать внутри контейнера, где нужных им правил и цепочек попросту нет.

### DNAT-маршрутизация

Проброс портов средствами netfilter — то же назначение, что у правил HAProxy (входящий порт → адрес:порт), но без userspace-прокси: пакеты переписывает ядро, CPU почти не тратится, UDP пробрасывается наравне с TCP. Обратная сторона: терминации TLS и разбора доменов нет, а цель видит адрес ноды, а не клиента (MASQUERADE).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/dnat/state | Желаемые правила из файла состояния, их наличие в ядре (`healthy`/`missing`), `ip_forward`, счётчики по правилам |
| POST | /api/dnat/apply | Атомарно заменить набор правил (`DnatApplyRequest{rules}`); ответ `success`, `message`, `rules_hash`, `error_log` |
| POST | /api/dnat/reapply | Вернуть в ядро сохранённые правила, если они потерялись (ручное самолечение) |
| POST | /api/dnat/clear | Снять джампы и цепочки, удалить файл состояния |

**Правило (`DnatRule`):** `name` (`^[a-zA-Z0-9_-]{1,64}$` — уходит в `-m comment`), `protocol` (`tcp`/`udp`/`both`), `listen_port`, `listen_port_end` (диапазон; `None` — одиночный порт, равный началу — схлопывается в `None`), `target_ip` (один или несколько unicast IPv4 через запятую, хранится как `a,b,c`, дубли схлопываются, не больше `MAX_TARGETS = 32`; DNAT не резолвит имена), `distribution` (`per_server` по умолчанию — панель уже оставила ноде один адрес, несколько адресов с этим режимом `validate_rules` отвергает; `random`/`round_robin`/`client_hash` — нода раскидывает сама, см. ниже), `target_port` (`0` — сохранить входящий порт; для диапазона порты сохраняются как есть), `masquerade` (по умолчанию `True`), `mask_ttl` (по умолчанию `False`, см. «Маскировка транзита»), `enabled`, `comment`.

**Маскировка транзита (`mask_ttl`).** Четвёртая цепочка `MON_DNAT_MANGLE` (mangle/FORWARD, джамп `-I FORWARD 1`): на каждую цель правила — `-d <ip> --dport … --ctstate DNAT -j TTL --ttl-set 64` и зеркальная `-s <ip> --sport …` (метки `<имя>:ttl-in@<ip>`/`<имя>:ttl-out@<ip>`), для tcp дополнительно `--tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu` (`<имя>:mss@<ip>`). Без этого с IP ноды уезжают TTL клиентских стеков вперемешку (50-е у мобильных, 110-е у Windows) — узнаваемый признак NAT-релея для DPI на аплинке; `MASKED_TTL = 64` совпадает с TTL собственного трафика Linux. TCP-опции клиентов (окно, wscale, MSS, timestamps) маскировка не трогает — их спрятать может только терминирующий прокси. Цепочка mangle входит в тот же `iptables-restore` (третья таблица в скрипте), в самолечение и в проверку «на месте»: у правила с `mask_ttl` `present` требует обеих TTL-строк на каждую цель. Требует `xt_TTL`/`xt_TCPMSS` (штатно в Ubuntu).

**Несколько адресов назначения (`build_restore_script`, `_selector`).** На каждую цель — своя DNAT-строка в `MON_DNAT` (плюс свои MASQUERADE и пара FORWARD-строк с `-d`/`-s` этой цели), а выбор цели для нового соединения делает матч перед `-j DNAT`:
- `random` — `-m statistic --mode random --probability p`: цель i из N получает `p = 1/(N−i)` (1/3, затем 1/2, последняя без условия) — в сумме равномерно; DNAT терминирует обход, так что до следующей строки доходят только «непойманные» соединения;
- `round_robin` — `-m statistic --mode nth --every (N−i) --packet 0` на остатке списка: каждое K-е из ещё не распределённых соединений уходит в текущую цель, итог — строгая очередь 1→2→3→1;
- `client_hash` — перед DNAT-строками одна строка `-j HMARK --hmark-tuple src --hmark-mod N --hmark-offset HMARK_OFFSET --hmark-rnd HMARK_SEED` (метка `<имя>#hash`), а цель i совпадает по `-m mark --mark HMARK_OFFSET+i`. Смещение `0x4D440000` вынесено в верхние биты fwmark, чтобы не пересечься с чужими маленькими метками (policy routing xray/WireGuard); HMARK ставит метку только первому пакету соединения (nat), на маршрутизацию транзита не влияет. Требует `xt_HMARK` (в Ubuntu-ядрах есть); без него `iptables-restore` падает и apply возвращает `error_log`.

Решение принимается на первом пакете, дальше соединение держит conntrack — живые соединения при смене списка не перекидываются. Проверки живости целей нет. Один адрес — режим не имеет значения, матч не добавляется.

**Проверка набора (`validate_rules`)** — отказ до любого касания netfilter: дубликаты имён; включённое правило, закрывающее порт API ноды (`validate_rules(rules, api_port)`, порт из `settings.node_api_port`; панель потеряла бы ноду — принудительного обхода нет); пересечение диапазонов портов у включённых правил с общим протоколом (в iptables сработало бы первое, а панель показывала бы оба как активные). Выключенные правила в проверке не участвуют — в ядро они не попадают.

**Как лежит в ядре.** Три собственные цепочки, джампы в них вставляются первыми (`-I ... 1`) в `nat/PREROUTING`, `nat/POSTROUTING` и `filter/FORWARD`:
- `MON_DNAT` — `-p <proto> --dport <port|a:b> -j DNAT --to-destination <ip>[:<port>]`;
- `MON_DNAT_POST` — при `masquerade`: `-p <proto> -d <ip> --dport <порт(ы) цели> -m conntrack --ctstate DNAT -j MASQUERADE`. Без него цель обязана маршрутизировать ответы клиенту через эту ноду;
- `MON_DNAT_FWD` — на правило две строки ACCEPT: `-d <ip> --dport … --ctstate DNAT` (клиент → цель) и `-s <ip> --sport … --ctstate DNAT` (ответы), плюс общий `--ctstate RELATED` (ICMP-ошибки, PMTUD). Цепочка нужна обязательно: и Docker, и UFW ставят политику FORWARD DROP.

Каждая строка помечена `-m comment --comment "mon-dnat:<имя>@<ip>"` (в FORWARD — `<имя>:in@<ip>`/`<имя>:out@<ip>`, HMARK-строка — `<имя>#hash`); по меткам считаются счётчики по каждой цели и проверяется наличие правила — правило «на месте», когда на месте все его цели (и HMARK-строка для `client_hash`). `--ctstate DNAT` в FORWARD и POSTROUTING — свойство соединения, а не пакета, поэтому совпадает в обе стороны и уже на первом пакете (бит выставляется при установке NAT-привязки в PREROUTING).

**Применение (`DnatManager.apply`).** `validate_rules` → проверка `iptables -t nat` → `net.ipv4.ip_forward=1` (запись в `/proc/sys/net/ipv4/ip_forward`; контейнер в сетевом namespace хоста и privileged, поэтому это хостовый sysctl; на нодах его и так держит Docker) → `-N` цепочек и `-C`/`-I` джампов → один `iptables-restore --noflush -w 5` с текстом из `build_restore_script()`: в нём `:CHAIN - [0:0]` (создать/очистить) и явный `-F CHAIN` для каждой нашей цепочки, затем строки правил, `COMMIT` на таблицу. Транзакция на таблицу означает: полусостояния не бывает, чужие правила (Docker, ufw, ANTIDDOS, учёт портов) не трогаются, а живые соединения переживают переприменение — их NAT-привязка уже в conntrack. После restore — дамп `iptables-save -c` обеих таблиц и сверка по меткам: если чего-то нет, откат к предыдущему набору из файла состояния и ошибка. Только после успешной сверки набор пишется в файл состояния (`/var/lib/monitoring/dnat_rules.json`, атомарно через `.tmp` + `replace`) и возвращается `rules_hash`.

**Хэш (`compute_rules_hash`)** — SHA256 канонического JSON: правила отсортированы по имени, поля `name/protocol/listen_port/listen_port_end/target_ip/distribution/target_port/masquerade/mask_ttl/enabled`, комментарий не входит. Формула продублирована в панели (`panel/backend/app/services/dnat_profile_sync.py`); совпадение закреплено общим «золотым» вектором в тестах обеих сторон.

**Самолечение.** Правила netfilter не переживают ребут, а `ufw --force reset` (профили firewall), `ufw disable`/`enable` вычищают джампы из встроенных цепочек. Поэтому `DnatManager.start()` в lifespan переприменяет набор из файла состояния при старте агента и запускает цикл `ensure_applied()` раз в `SELF_HEAL_INTERVAL_SEC = 30`: два дампа, сверка меток и джампов, при расхождении или сброшенном `ip_forward` — полный `apply()` из файла. Роутеры `firewall_profile.apply` и `haproxy/firewall/enable|disable` будят цикл сразу (`request_recheck()`), чтобы окно без проброса не растягивалось до следующего тика. Все операции менеджера (apply/state/ensure/clear) идут под одним `asyncio.Lock`, сами команды — в тред-пуле.

**Счётчики (`GET /api/dnat/state`).** `conns` — счётчик пакетов DNAT-строки в `MON_DNAT`: nat-таблица видит только первый пакет соединения, поэтому это число новых соединений; `bytes_in`/`packets_in` — со строки `:in` в FORWARD (клиент → цель), `bytes_out`/`packets_out` — со строки `:out` (цель → клиент). Поле `targets[]` (`DnatTargetCounters`) — то же по каждому адресу назначения, суммы на уровне правила — по всем целям. Ответ также несёт `available` (нет iptables → `False`), `ip_forward`, `healthy` и `missing` (имена потерянных правил и `jump:<цепочка>`), `applied_at`. Активные соединения не считаются — обход conntrack на нагруженной ноде слишком дорог.

**Ограничения:** только IPv4; локально сгенерированный трафик самой ноды (OUTPUT) не пробрасывается — только транзитный через PREROUTING; DNAT не терминирует TCP, поэтому в сторону цели уходят TCP-заголовки клиентов как есть (маскируются только TTL и MSS) — на плече через агрессивный DPI предпочтительнее HAProxy; у `both` строки tcp и udp несут одну метку и в счётчиках суммируются. Анти-DDoS (цепочка `ANTIDDOS` в INPUT, SYNPROXY, hashlimit) проброшенного трафика не касается — он идёт PREROUTING → FORWARD; `--notrack` watchdog ставит только на порты слушающих сокетов, DNAT-порты под него не попадают, если только не совпадают с портом локального сервиса.

**Файлы:**
- `node/app/models/dnat.py` — `DnatRule` (валидация IPv4, диапазона, `protocols()`, `covers_port()`), `DnatApplyRequest/Response`, `DnatRuleCounters`, `DnatStateResponse`, `DnatActionResponse`, `NODE_API_PORT`
- `node/app/services/dnat_manager.py` — чистые функции `normalize_rule`, `compute_rules_hash`, `validate_rules`, `build_restore_script`, `parse_dump`, `summarize`; `DnatManager` (`apply`, `clear`, `state`, `ensure_applied`, async-обёртки под замком, `start`/`stop`, `request_recheck`)
- `node/app/routers/dnat.py` — роутер (prefix `/api/dnat`)
- `node/app/main.py` — старт/остановка менеджера в lifespan, регистрация роутера
- `node/app/routers/firewall_profile.py`, `node/app/routers/haproxy.py` — `request_recheck()` после операций, перестраивающих встроенные цепочки
- `node/app/capabilities.py` — домен `dnat` (`/api/dnat`)
- `node/tests/test_dnat.py` — проверка набора (дубликаты, пересечения, порт 9100, выключенные правила, несколько целей без режима), модель (диапазон, список IPv4), restore-скрипт (одиночный порт, диапазон с сохранением портов, `both`, без MASQUERADE, выключенное правило, `random`/`round_robin`/`client_hash`, один адрес без матча), разбор дампа и сводка (счётчики по целям, потерянные правила/джампы/HMARK-строка), «золотой» хэш, файл состояния и `state()` без правил не дёргает `iptables-save`

### Лимит полосы (`bandwidth_limit.py`)

Искусственный шейпер на исходящем направлении дефолтного интерфейса хоста — ровная полка на счётчиках хостера вместо пиков; на релее ограничивает оба направления транзита (к цели и обратно к клиенту оба выходят через тот же интерфейс). Команды `tc` выполняются на хосте через `HostExecutor` (в контейнере нет iproute2). Состояние читается из `tc -j qdisc show dev X`; в JSON iproute2 печатает `bandwidth`/`rate` в байтах в секунду (в отличие от текстового вывода), `parse_tc_root` переводит в Мбит умножением на 8.

Шейпер — `tbf rate Nmbit burst <30 мс полосы> latency 100ms`; burst считает `tbf_burst_bytes()` (`BURST_SECONDS = 0.03`, пол 512 КБ — ниже tbf физически недобирает лимит, потолок 16 МБ — выше рваная полка на мгновенных скоростях). Не `cake`: на боевой VPS (kvm-clock) cake при свободном CPU недобирал ~35% полосы — лимит 950 Мбит давал фактические ~600 с 8% дропов (hrtimer-расписание шейпера на виртуализированном таймере плюс split-gso: каждая GSO-пачка по 68 КБ режется на ~45 пакетов, и каждый проходит через qdisc отдельно), а tbf на той же машине под той же нагрузкой держит 929–940 без единого дропа, пропуская GSO-пачки целиком. Уже стоящий `cake` от прежних версий распознаётся (`applied=true`, лимит работает), но `in_sync=false` — самолечение мигрирует его на tbf при первой сверке. Ниже лимита шейпер невидим (очередь пуста); снимается только собственный qdisc (`cake`/`tbf`), чужой корневой (`mq`/`fq_codel` хоста) не трогается.

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/system/bandwidth-limit | `enabled`, `mbit`, `iface`, `applied`, `applied_mbit`, `qdisc` (`tbf`, legacy `cake`), `in_sync` |
| POST | /api/system/bandwidth-limit | `{enabled, mbit}` (1–100000); `enabled=false` снимает |

Состояние — `/opt/monitoring/configs/bandwidth-limit.env` (`BANDWIDTH_LIMIT_MBIT`, `BANDWIDTH_LIMIT_IFACE`) на хосте, пишется до применения: если `tc` упал, намерение не теряется. `BandwidthLimiter.start()` в lifespan переприменяет лимит при старте агента и подтверждает раз в `BANDWIDTH_RECHECK_INTERVAL_SEC = 120` (`ensure()`: корневой qdisc не наш или не с той полосой → `_apply`) — `ethtool -L` из тюнинга и ручной `tc qdisc del` сбрасывают корневой qdisc. Тесты: `node/tests/test_bandwidth_limit.py` — разбор состояния и `tc -j`, формула burst (30 мс полосы, пол/потолок), `in_sync`/дрейф, legacy cake = applied но не in_sync, миграция cake → tbf, самолечение только при расхождении, снятие только своего qdisc.

### Анти-DDoS

Многослойная защита от DDoS-атак: дежурный режим без лимитов → аварийный режим с iptables-правилами в отдельной цепочке `ANTIDDOS` → автодетект атаки локальным watchdog-сервисом. Вся логика правил и детекции живёт в одном host-скрипте `configs/ddos-watchdog.sh` — нода лишь дёргает его CLI-команды через `nsenter`, поэтому набор правил идентичен независимо от того, кто включил режим (watchdog или панель).

Без auth-зависимости (как остальные роутеры ноды — mTLS терминируется на nginx перед uvicorn).

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | /api/antiddos/status | Текущее состояние: installed, mode (on/off), source (auto/manual/none), since, reason, watchdog (on/off), watchdog_active, client_ports, version |
| POST | /api/antiddos/emergency | Включить/выключить аварийный режим вручную (`source=manual` — автоматика не снимает) |
| POST | /api/antiddos/watchdog | Включить/выключить автодетект (сервис watchdog продолжает работать, но не трогает правила) |
| POST | /api/antiddos/whitelist/sync | Полная замена ipset-набора `antiddos_allow` (принимает `ips: string[]`) |
| POST | /api/antiddos/install | Установить/обновить `ddos-watchdog.sh` + systemd-сервис на хосте, включить (`daemon-reload` → `enable` → `restart`) |

**Аварийный режим (цепочка `ANTIDDOS`, джамп из INPUT только пока активен), порядок правил документированной схемой netfilter:**
1. DROP/ACCEPT по temp-блоклисту (`blocklist_temp`, если ipset-набор существует)
2. ACCEPT по whitelist (`antiddos_allow`, ipset `hash:net`)
3. ACCEPT established/related соединений
4. ACCEPT SSH (порт автоопределяется, см. ниже), nginx mTLS API (9100 + кастомный `NODE_API_PORT` из `.env` ноды, если задан) и внутренний uvicorn-API ноды (7500) — никогда не дропаются
5. На автоопределённые клиентские порты — **SYNPROXY** (проверка TCP-рукопожатия до создания conntrack-записи, гасит SYN-флуд со спуфнутых IP; best-effort — если `xt_SYNPROXY`/`nf_synproxy_core` недоступны, шаг пропускается). `--wscale`/`--mss` вычисляются `tune-sysctl.sh` из реальных `rmem_max` и MTU хоста — захардкоженные значения зажимали бы окно проксируемых соединений и были бы неверны на туннелированном пути
6. DROP INVALID (эффективно вместе с `nf_conntrack_tcp_loose=0` из системных оптимизаций)
7. Не-SYN пакеты в состоянии NEW — DROP
8. hashlimit на клиентские порты — лимит новых соединений/сек с одного IP

Raw-правила `--notrack` (снимают SYN с трекинга до SYNPROXY) ставятся **последними** и только если правило SYNPROXY принято в каждой группе портов; при частичном отказе снимаются. Порядок «SYNPROXY раньше DROP INVALID» критичен: стоящий первым DROP INVALID терял бы завершающий handshake ACK клиента — SYN снят с трекинга в raw-таблице, ACK не находит записи conntrack и становится INVALID, до SYNPROXY не доходя никогда; при `nf_conntrack_tcp_loose=0` это полный блэкхол новых соединений на всё время аварийного режима — ровно тогда, когда нода должна принимать легитимных клиентов. `synproxy_available()` проверяет в одноразовой цепочке обе половины — доступность raw-таблицы **и** существование таргета SYNPROXY (проверка одной raw-таблицы пропустила бы ядро без `xt_SYNPROXY`: правила `--notrack` встали бы, `-j SYNPROXY` молча упал бы — тот же блэкхол без единой строки в логе), плюс `tcp_timestamps=1` (SYNPROXY кодирует wscale/MSS/SACK в timestamp).

**`connlimit` не используется.** `xt_connlimit` обходит conntrack-бакет источника на каждом NEW-пакете — дорожает ровно во время атаки — а лимит вида `--connlimit-above 100` на `/32` карал бы CGNAT-адреса операторов и любого клиента без Mux. Скорость ограничивает hashlimit, стоячее количество — conntrack-таймауты.

**Разбивка портов на группы (`build_chain`):** `iptables -m multiport --dports` принимает не более 15 портов на правило. Busy Xray-нода может слушать 30+ клиентских инбаундов, поэтому `detect_client_ports` разбивается на группы по ≤15 портов, и для каждой группы генерируется свой набор правил SYNPROXY/hashlimit. Хэш-таблица hashlimit **одна общая** на все группы (`--hashlimit-name ad_emg`) с настраиваемой `--hashlimit-srcmask` (`HASHLIMIT_SRCMASK`, по умолчанию 32) — отдельная таблица на группу умножала бы эффективный лимит на число групп (нода с 60 портами получила бы 4×`NEWRATE` и вчетверо больше памяти htable).

**Автоопределение SSH-порта (`detect_ssh_ports()`):** захардкоженный порт 22 в never-drop оставил бы ноду с нестандартным SSH-портом без ACCEPT для реального порта — тот попал бы под hashlimit клиентских портов. Порт(ы) определяются из трёх источников и объединяются: директива `Port` в `/etc/ssh/sshd_config` и `/etc/ssh/sshd_config.d/*.conf`; `ListenStream=` в systemd socket-активации (`ssh.socket` и override'ы — дефолт Ubuntu 24); живые sshd-листенеры через `ss -H -tlnp` (грепом по `sshd`). Если ни один источник не дал результата — откат на 22. `effective_never_drop()` объединяет статические management-порты (`NEVER_DROP_PORTS="9100 7500"`), кастомный порт API из `/opt/monitoring-node/.env` (`detect_node_api_port()`, если `NODE_API_PORT` задан) и автоопределённые SSH-порты (дедуп) — используется и при исключении клиентских портов (`detect_client_ports`), и при генерации ACCEPT-правил (`build_chain`).

Джамп ставится только на время активного режима — в дежурном режиме никаких дополнительных правил и накладных расходов.

**Watchdog (автодетект, `ddos-watchdog.sh loop` — systemd-сервис `ddos-watchdog.service`):**
- Сигналы читаются из `/proc` каждые ~10 сек: рост `insert_failed` conntrack (реальные дропы), заполнение conntrack-таблицы (%, слабый намёк), рост `SyncookiesSent` за цикл, pps при малом среднем размере пакета, softirq% (суммарно и по самому загруженному ядру отдельно), дропы `/proc/net/softnet_stat` (за вычетом собственного ограничителя ядра `flow_limit_count`, см. ниже) и `ListenOverflows` из `/proc/net/netstat` (переполнение очереди accept — слабый сигнал; используется именно `ListenOverflows`, а не `ListenDrops`, потому что `ListenDrops` растёт и при штатной смене слушающих сокетов)
- Сильные сигналы (резкий рост SyncookiesSent или `insert_failed`) включают аварийный режим немедленно; дропы `/proc/net/softnet_stat` — тоже сильный сигнал, но включает режим только если превышение держится `SOFTNET_HOLD_CYCLES` циклов подряд (по умолчанию 2, ~20 сек); остальные (слабые) сигналы — после устойчивого удержания ~45 сек (защита от ложных срабатываний на вечернем пике)
- **Softnet-дропы — сигнал за вычетом `flow_limit`.** `/proc/net/softnet_stat` суммирует в колонку `dropped` и срабатывания собственного ограничителя ядра `flow_limit` (включается вместе с RPS, режет поток, занявший больше половины истории очереди с одного источника) — на одноочередной сетевой карте один быстрый клиент даёт сотни таких «дропов» за цикл при полностью здоровой ноде, и без вычитания это неотличимо от флуда. `read_softnet_counters()` читает обе колонки (`dropped` — 2-я, `flow_limit_count` — 11-я) по каждому CPU; из дельты `dropped` вычитается дельта `flow_limit`, остаток сравнивается с `SOFTNET_DROP_DELTA` и должен продержаться `SOFTNET_HOLD_CYCLES` циклов подряд (счётчик — `/opt/monitoring/antiddos/run/softnethits`, сбрасывается при любом цикле ниже порога), прежде чем поднять аварийный режим.
- **Conntrack — сигнал по реальным дропам, не по заполнению.** «Заполнение ≥ порога» само по себе — только слабый намёк при near-exhaustion (`CONNTRACK_PCT=90`); реальный сигнал атаки — рост `insert_failed` (`nf_conntrack: table full, dropping packet`), суммированного по всем CPU из `/proc/net/stat/nf_conntrack`, дельта ≥ `CONNTRACK_DROP_DELTA` (=50/цикл). Если на ноде часто держится высокое заполнение conntrack без реальных дропов — это признак отсутствия sysctl-оптимизаций; вкладка «Оптимизации» поднимает `conntrack_max` и заполнение падает до единиц процентов.
- **Пороги и конфиги — не статичны.** `tune-sysctl.sh` пишет пороги в `/opt/monitoring/antiddos/config.auto` по факту CPU/RAM хоста; порядок подключения — дефолты скрипта → `config.auto` → `/opt/monitoring/antiddos/config` (последним, выигрывает оператор). Функция `load_config()` перечитывает оба файла в начале **каждого** цикла `loop`, а не один раз при старте процесса: `tune-sysctl.sh` пишет `config.auto` уже после того, как systemd-сервис watchdog поднялся (при применении системных оптимизаций), поэтому разовое чтение держало бы вотчдог на встроенных дефолтах сколь угодно долго — например, порог softnet-дропов оставался бы 200 вместо посчитанного для 8-ядерной ноды 800. Перечитывание каждый цикл даёт подхват пересчитанных порогов за один цикл, без рестарта сервиса. `SOFTIRQ_PCT` корректируется по числу ядер в «обратную» сторону: `/proc/stat` даёт уже нормализованный по CPU агрегат, поэтому 50% на 2 ядрах — это одно занятое ядро (штатный вечерний пик), а на 64 ядрах — 32 ядра в softirq (катастрофа); малым хостам (`CPUS≤4`) порог выше (`SOFTIRQ_PCT=70`), а не ниже.
- Автовыключение — после ~15 мин без сигналов
- Ручной пин (`source=manual`, включённый через `POST /api/antiddos/emergency`) автоматика не снимает — выключить может только явный вызов `POST /api/antiddos/emergency {enabled: false}`
- Выключение автодетекта (`watchdog=off`, через `POST /api/antiddos/watchdog {enabled: false}`) в цикле `loop` снимает активный **авто**-аварийный режим (`disable_mode`) — нода возвращается в дежурный режим. Ручные пины (`source=manual`) обрабатываются раньше в цикле и этим не затрагиваются.
- **Self-heal**: если сторонний процесс (например применение Firewall Profile через `ufw --force reset`) снёс джамп в `ANTIDDOS`, watchdog восстанавливает его в течение одного цикла
- **Self-check достижимости**: пока аварийный режим активен, каждый цикл проверяет `http://127.0.0.1:7500/health` и наличие живой SSH-сессии; `SELF_CONFIRM_FAILS=3` подряд неудачи снимают цепочку — ошибка в правилах на позиции INPUT 1 не должна оставлять сервер недоступным

**Отладочные верби:** `dry-run` (печатает точные команды `iptables`, ничего не выполняя — цепочка встаёт на INPUT-позицию 1, поэтому проверить её до применения ценно) и `self-test` (структурно доказывает, что SYNPROXY стоит раньше INVALID DROP и что raw `--notrack` есть тогда и только тогда, когда есть правило SYNPROXY — штатный `curl` этого не докажет, он проходит и когда SYNPROXY молча отсутствует).

**Whitelist:** ipset-набор `antiddos_allow` (`hash:net`), хранится на диске ноды (`/opt/monitoring/antiddos/whitelist.json`) — переживает ребут и недоступность панели. ACCEPT по нему действует только в аварийном режиме (в дежурном режиме проходит весь трафик без ipset-проверок). До первого опроса панели набор засеян IP панели и `127.0.0.0/8`. Панель наполняет набор ежечасно через `POST /api/antiddos/whitelist/sync`.

**CLI-команды `ddos-watchdog.sh`** (вызываются нодой через `nsenter`, доступны и вручную на хосте): `loop`, `enable-manual`, `disable-manual`, `watchdog-on`, `watchdog-off`, `apply`, `clear`, `selfheal`, `whitelist-sync` (IP через stdin), `detect-ports`, `dry-run`, `self-test`, `version`, `status`. Состояние — `/opt/monitoring/antiddos/state.json` (mode/source/since/reason/watchdog).

**Версионирование watchdog-скрипта:** константа `WATCHDOG_VERSION` в шапке `ddos-watchdog.sh` (сейчас `"2.4.0"`) — команда `status` возвращает её полем `version`, отдельная команда `version` печатает только её. Значение растёт при изменении логики скрипта; панель сверяет его с версией, установленной на ноде (см. «Установка» ниже).

**Установка — по умолчанию.** `install.sh` вызывает `install_antiddos_watchdog()` сразу после применения системных оптимизаций — свежая нода получает watchdog без какого-либо участия панели, в состоянии покоя (`watchdog=on`, аварийный режим выключен). Существующие ноды получают его от панели — через `apply-update.sh` это не идёт, потому что rsync обновления агента исключает `configs/`: репозиторных конфигов рядом с нодой нет. Панель в фоновом опросе статуса (см. panel/DOCUMENTATION.md) видит, что нода отвечает на `/api/antiddos/status`, и сама вызывает `POST /api/antiddos/install`, если watchdog не установлен либо его `version` отличается от актуальной версии `ddos-watchdog.sh` на GitHub. Backend-эндпоинты ручной установки (`POST /antiddos/install-all` в панели, `POST /api/antiddos/install` на ноде) остаются доступны по API.

**Требования к ядру:** `xt_SYNPROXY`/`nf_synproxy_core`, `hashlimit`. На Ubuntu 24 iptables работает через nft-бэкенд (iptables-nft) — тот же стек, что UFW/Docker/ipset_manager.

**Файлы:**
- `configs/ddos-watchdog.sh` — весь host-скрипт: правила, детект, self-heal, CLI
- `configs/ddos-watchdog.service` — systemd-unit (`Type=simple`, `Restart=always`)
- `node/app/services/antiddos_manager.py` — обёртка над скриптом через `get_host_executor()` (nsenter); валидация IP/CIDR перед whitelist-sync; `install()` пишет скрипт+сервис на хост и запускает
- `node/app/routers/antiddos.py` — API роутер (prefix `/api/antiddos`)
- `node/app/main.py` — регистрация роутера

## Производительность

### Async сбор метрик

`node/app/services/metrics_collector.py`: `get_all_metrics()` выполняет 8 методов сбора (CPU, RAM, диск, сеть, процессы и др.) параллельно через `asyncio.gather()` + `asyncio.to_thread()` для блокирующих вызовов psutil. Скорости в запросе не считаются — берутся из последнего замера посекундного семплера (один `snapshot()` на весь ответ, чтобы маркер `live_rates` и сами цифры были из одного окна).

### Посекундный семплер скоростей (`services/rate_sampler.py`)

`RateSampler` — фоновая asyncio-задача (`prime()` → `start()` в `lifespan`, `stop()` на выходе), раз в `SAMPLE_INTERVAL_SEC = 1` с читает в тред-пуле `psutil.cpu_times(percpu=True)`, `/proc/net/dev` (`read_net_dev()` — общий парсер, коллектор им же берёт кумулятивные счётчики) и `psutil.disk_io_counters(perdisk=True)`, считает дельту к предыдущему чтению и подменяет неизменяемый `RateSample` целиком — `snapshot()` из тред-пула читает ссылку без лока. Панель поэтому получает нагрузку за последнюю секунду, а не среднее за свой интервал опроса (раньше CPU мерился между двумя запросами панели, а скорость сети и диска нода не считала вовсе).

- `per_cpu_percent(before, after)` — доля busy-времени каждого ядра между двумя снимками `cpu_times`; интервал берётся из самих счётчиков ядра, не из часов. Если суммарная дельта тиков любого ядра меньше `CPU_MIN_TICKS_SECONDS = 0.2` с — `None` целиком: тик ядра — 10 мс, и на дельте в единицы тиков доля busy вырождается в точные 0/33.3/50/66.7/100 («одно ядро 100%, остальные 0»). Результат ограничен `[0, 100]`, округлён до одного знака. При `None` семплер отдаёт последний валидный процент; смена числа ядер (ресайз VPS) — нули новой длины.
- `counter_rates(before, after, dt)` — байт/с по ключам, которые есть в обоих чтениях; счётчик «назад» (сброс драйвера, переподнятый интерфейс) даёт 0, а не отрицательную скорость; появившийся/исчезнувший интерфейс в этом окне рейта не имеет.
- `advance(current)` — два чтения ближе `MIN_WINDOW_SEC = 0.2` с не образуют окна: baseline не двигается, следующий тик меряет от него же на полном окне (горстка тиков и пакетов — шум, не скорость). `prime()` — стартовый блокирующий замер с паузой `CPU_PRIME_SECONDS = 0.3` с, до приёма запросов; цикл сначала спит, потом читает — первый тик не попадает на только что снятый baseline.
- `snapshot()` — `None`, пока замера нет или последнее принятое чтение старше `STALE_AFTER_SEC = 5` с: замолчавший семплер лучше не отдаст скорость вовсе, чем будет отдавать одну и ту же стухшую цифру — нода тогда уходит без маркера `live_rates`, и панель на этот цикл считает дельты сама.
- `disk_total` — сумма только по целым дискам (`is_whole_disk()`: `/sys/block/<dev>` существует — та же проверка, что у psutil для `perdisk=False`); если ни одно имя не распознано — по всем.

Зависимости (`read_counters`, `is_whole_disk`, `clock`) инжектируются в конструктор — тесты идут без psutil и без патчей.

**Тесты:** `node/tests/test_rate_sampler.py` — 18 тестов: проценты CPU (реальные значения, отбраковка коротких дельт целиком и при одном отстающем ядре, потолок 100%), рейты сети/диска по дельте и окну, счётчик «назад» → 0, появившийся и исчезнувший интерфейс, слишком близкое чтение не двигает baseline, смена числа ядер, сумма по целым дискам и фолбэк, `sampled_at`, протухание. `node/tests/test_metrics_live_rates.py` — 8 тестов: коллектор копирует рейты семплера в интерфейсы/total (только физические), диски/`io_total`, CPU; без сэмпла — нули и `live_rates: null`.

### Счётчики портов: iptables вне пути запроса метрик

`node/app/services/port_traffic_sampler.py` вынесен из обработчика `/api/metrics` целиком: фоновая задача раз в 30 секунд снимает дамп счётчиков, а `snapshot()` синхронно отдаёт копию последнего результата без единого системного вызова. Дамп iptables ждёт блокировку xtables (её держат ufw и ipset), и внутри запроса метрик это ожидание съедало бы весь бюджет ответа — панель считала бы ноду упавшей. Все вызовы `iptables`/`iptables-save` идут через `asyncio.create_subprocess_exec()` с таймаутом `COMMAND_TIMEOUT_SEC = 20`, без блокирующего `subprocess.run()`.

### IPSet: пакетное применение одним `ipset restore`

Per-IP применение (2 subprocess-вызова `ipset add`/`del` на запись) на списке в десятки тысяч записей заняло бы десятки минут, поэтому:

- `ipset_manager.py`: все массовые операции (`sync`, `bulk_add`, `bulk_remove`, `sync_allow`, загрузка permanent/allow-списков из `blocklist.json` при старте) собирают diff и применяют его одним вызовом `ipset -exist restore` (`_run_ipset_restore()`) — весь diff применяется за доли секунды независимо от размера списка.
- `routers/ipset.py`: все эндпоинты — синхронные `def`, FastAPI выполняет их в threadpool, поэтому длинная блокирующая операция не держит event loop и не замораживает остальные эндпоинты ноды (в `async def`-хендлере синхронный subprocess завесил бы **все** эндпоинты node-API — nginx отдавал бы 504 на любой запрос к ноде).
- Мутации ipset-сетов сериализованы `threading.Lock` (`_mutate_lock`) — параллельный sync с панели и ручной bulk-add не перемешивают diff-ы.

## Системные оптимизации

Установка/обновление ноды **не** применяет и не меняет оптимизации сама по себе — только через UI панели (раздел **Обновления**) или главный установщик (`monitoring` → пункт 7). После первого применения рендерер сам повторно накатывает значения на **каждой загрузке** хоста (`ExecStartPre` активного `*-tune.service`) — ресайз VPS подхватывается без повторного клика в панели.

Категории тюнинга: IPv6 (отключение), BBR + fq_codel, TCP/UDP-буферы, Busy Polling, TCP ECN, очереди (`somaxconn`/`netdev_max_backlog`), TCP performance (fastopen, no slow start after idle, MTU probing, autocorking), TIME-WAIT (tw_reuse), syncookies/rp_filter/ICMP-protection, conntrack, лимиты файловых дескрипторов. Все размерные значения из этого списка вычисляются из MemTotal/nproc/MTU/скорости линка хоста единым рендерером `tune-sysctl.sh` (`configs/`, версия формулы отдельная от `configs/VERSION` — `FORMULA_VERSION`, сейчас `1.0.0`) — не хардкод и не флат-число, одинаковое для любого сервера.

`net.ipv4.tcp_autocorking = 1` (`configs/profiles/common.base.conf`): задержка мелких записей ради их слияния в один сегмент. Замер на боевой ноде — средний исходящий TCP-сегмент 1243 байта (ровно один сегмент), то есть прокси в `mode tcp` сам не батчит и каждая запись стоила отдельного virtio-kick (0.53 на пакет); включение снизило это примерно на 19%.

Отдельный глобальный тумблер на этой же странице панели — «Развод по ядрам» (cpu-affinity): уводит HAProxy и контейнеры Remnawave с ядер, занятых прерываниями сетевой карты. Своя лёгкая ручка (`GET/POST /api/system/cpu-affinity`) в обход полного применения оптимизаций, но состояние переживается и полным применением тоже — см. «Развод по ядрам» в разделе HAProxy выше.

### Контракт с панелью

`POST /api/system/optimizations/apply` (`ApplyOptimizationsRequest`) везёт **входные данные** рендерера — сам `tune-sysctl.sh`, `profiles/common.base.conf`, `profiles/<профиль>.base.conf`, `limits.tmpl`, `systemd-limits.tmpl` (+ опционально содержимое NIC-скриптов) — а не готовый sysctl.conf: панель не знает MemTotal ноды, поэтому один отрендеренный файл не может подойти и на 4 ГБ, и на 248 ГБ. Нода пишет входные файлы на хост в `/opt/monitoring/scripts/` и `/opt/monitoring/configs/profiles/` через `write_host_file()` (запись через `nsenter` + base64, не heredoc — heredoc обрезал бы файл, если строка контента совпадёт с делимитером), затем сама вызывает `tune-sysctl.sh render <профиль>` и возвращает результат верификации в ответе. Панель отправляет этот контракт только нодам версии ≥ `10.6.0` — см. [panel/DOCUMENTATION.md](../panel/DOCUMENTATION.md).

### Верификация и дрейф

`verify_sysctl_values()` читает ожидания из `/opt/monitoring/configs/tuning-facts.json`, который пишет рендерер (`expected_from_facts()`: computed+static минус ключи, которыми в рантайме владеет Xray), а не из захардкоженной таблицы. Все значения читаются одним `nsenter`-вызовом вместо процесса на ключ; многозначные ключи нормализуются (`sysctl -n net.ipv4.tcp_mem` отдаёт поля через таб, файл — через пробел); hashsize conntrack проверяется порогом из facts, а не точным равенством. `rp_filter` и `disable_ipv6` исключены из проверки — Xray переписывает их в рантайме при поднятии WireGuard-аутбаунда.

Поскольку значения выводятся из MemTotal/nproc, у ресайзнутого VPS файл на диске может совпадать с текущей версией `configs/VERSION` и при этом быть неверным. `read_tuning_drift()` пересчитывает хеш живых фактов хоста и сравнивает с хешем, записанным при последнем рендере; `GET /api/system/versions` отдаёт `optimizations.drift`/`drift_detail`/`formula_version` — так панель узнаёт о дрейфе раньше, чем сработает следующий загрузочный ре-рендер.

### Цепочка файловых дескрипторов

Рендерер утверждает её численно и отказывается писать при нарушении: `nginx worker_rlimit_nofile ≤ container nofile ≤ NOFILE_LIMIT == limits.conf nofile == DefaultLimitNOFILE ≤ fs.nr_open == fs.file-max`, и `haproxy maxconn ≤ (RLIMIT_NOFILE HAProxy − 1024) / 3` (см. «Лимит соединений (maxconn)» в разделе HAProxy выше). `fs.nr_open` поднимается **до** записи `limits.conf` — иначе PAM ломается о значение выше текущего `nr_open`. `node/docker-compose.yml` задаёт явные `ulimits: nofile 65536` для обоих сервисов (без явного лимита наследовалось бы ~1073741816 от dockerd) и монтирует `/opt/monitoring:ro`, чтобы контейнер видел `tuning-facts.env`/`.json`.

**Тесты:** `node/tests/test_verify_sysctl.py` — 14 тестов на stdlib `unittest` (подхватываются и pytest): нормализация значений, сборка ожидаемого набора из facts, порог hashsize, отсутствующий/битый facts-файл, чтение всех ключей одним вызовом. `configs/tests/render-matrix.sh` — 252 комбинации размеров хоста × профилей, 293 проверки инвариантов на стороне самого рендерера, см. корневой [DOCUMENTATION.md](../DOCUMENTATION.md).

**Файлы:** верификация и вычистка конфликтующих sysctl/limits-конфигов вынесены из `node/app/routers/system.py` в отдельный `node/app/services/sysctl_verify.py` (`expected_from_facts`, `_normalize_sysctl_value`, `verify_sysctl_values`, `cleanup_conflicting_configs`, `_is_system_sysctl`) — это единственный кусок роутера, у которого были собственные тесты, поэтому вынос сервисного слоя проверяем ими же.

### RPS/RFS Network Tuning

При установке ноды автоматически настраивается **RPS/RFS** — распределение сетевой нагрузки по ядрам CPU:

- **RPS (Receive Packet Steering)** — распределяет входящие пакеты по всем ядрам CPU
- **RFS (Receive Flow Steering)** — оптимизирует привязку потоков к ядрам
- **XPS (Transmit Packet Steering)** — распределяет исходящие пакеты по очередям TX

**Systemd сервис**: `network-tune.service`
- Автоматически определяет основной сетевой интерфейс
- Вычисляет оптимальные значения на основе количества ядер CPU
- Запускается при каждой загрузке системы

```bash
# Статус сервиса
systemctl status network-tune

# Ручной перезапуск (после изменения железа)
systemctl restart network-tune

# Логи
journalctl -u network-tune
```

`net.core.rps_sock_flow_entries` задаётся только рендерером — NIC-скрипты (`network-tune.sh`/`multiqueue-tune.sh`/`hybrid-tune.sh`) её не трогают. `rps_flow_cnt` на каждой очереди обязан быть степенью двойки (`pow2_floor(entries / rx_queues)`) — иначе ядро молча отклоняет RFS; источник `entries` — `tuning-facts.env`.

**GRO-батчинг на одноочередных картах** (`configure_gro_batching()` в `network-tune.sh`, вызывается из `main()` для каждого безопасного интерфейса): при единственной RX-очереди выставляет `napi_defer_hard_irqs=1` и `gro_flush_timeout=50000` нс — GRO успевает собрать несколько пакетов в один skb, поэтому через единственное кольцо и стек проходит меньше буферов. На многоочередной карте не применяется — нагрузка и так разложена, задержка добавилась бы зря; на ядре < 5.10 нужных файлов в `/sys/class/net/<if>/` нет — функция тихо пропускает интерфейс.

**Примечание**: Настройки универсальны для любых машин (от 1GB RAM до 128GB+). При проблемах с сетью во время установки/обновления IPv6 отключается автоматически.

## SSL сертификаты

- Создаются через certbot (установлен в API контейнере)
- Хранятся на хосте: `/etc/letsencrypt/live/{domain}/`
- HAProxy использует combined.pem (fullchain + privkey)
- **Автопродления нет.** Сертификат, выпущенный со страницы HAProxy, истекает через 90 дней, если не продлить его вручную (кнопка «Продлить» на той же странице). Wildcard-сертификаты это не затрагивает — у них отдельный механизм на стороне панели.
- HAProxy останавливается на время выпуска/продления **только если** какое-то правило слушает порт 80 — certbot в режиме standalone требует этот порт свободным. Правила на 443 и остальных портах при продлении не рвутся.

> При обновлении агент автоматически удаляет с хоста legacy-файлы `/etc/cron.d/certbot-renew` и `/opt/monitoring-node/renew-certs.sh`, если они там есть: этот крон каждую ночь безусловно делал `systemctl stop haproxy` перед `certbot renew`, обрывая все туннели на всех портах.

## Команды

```bash
# Логи API
docker compose logs -f

# Перезапуск API
docker compose restart

# Остановка API
docker compose down

# HAProxy (нативный сервис)
systemctl status haproxy    # Статус
systemctl start haproxy     # Запуск
systemctl stop haproxy      # Остановка
systemctl restart haproxy   # Перезапуск
systemctl reload haproxy    # Reload конфига

# Логи HAProxy
journalctl -u haproxy -n 100

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
