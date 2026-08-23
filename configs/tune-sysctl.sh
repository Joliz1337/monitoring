#!/bin/bash
#
# Renderer for the kernel-tuning layer. Single owner of every derived number.
#
# The same profile must be correct on a 2 CPU / 4 GB VPS and on a 64 CPU / 248 GB
# server, so nothing here is a flat constant: every size-dependent value is
# computed from MemTotal / nproc and clamped. Values are rendered INTO
# /etc/sysctl.d/99-vless-tuning.conf rather than applied transiently with
# `sysctl -w`, because anything transient is silently reverted by the next
# `sysctl --system` (cloud-init, a package postinst, systemd-sysctl restart).
#
# Called from three places, all of which must agree:
#   * install.sh                      (host, no container yet)
#   * POST /api/system/optimizations/apply   (node agent, via nsenter)
#   * ExecStartPre of the active *-tune.service  (every boot; picks up a resize)
#
# Verbs:
#   facts      print the fact/derived set as shell assignments, write nothing
#   render     compute -> render all outputs atomically -> sysctl -e -p if changed
#   verify     compare live kernel values against tuning-facts.json, exit 0/1
#   rollback   restore the previous sysctl.d file and re-apply
#   self-test  run the invariant assertions against the current facts
#
# Testability: every host fact can be overridden, and every output redirected,
# so the whole matrix is checkable in CI with no privileges and no VM:
#   MON_FACT_MEMKB, MON_FACT_CPUS, MON_FACT_PAGESIZE, MON_FACT_LINK_MBPS,
#   MON_FACT_MTU, MON_FACT_FILENR
#   MON_RENDER_ROOT=/tmp/x   prefix for every output path
#   MON_RENDER_DRYRUN=1      skip sysctl/systemctl/modprobe entirely
#

set -u

FORMULA_VERSION="1.5.0"
FACTS_SCHEMA=1

# Гранулярность округления MemTotal вниз. Мирится с джиттером MemTotal между
# загрузками; обязана совпадать с квантованием в read_tuning_drift() на
# стороне ноды, иначе drift будет ложным.
MEM_QUANTUM_KB=16384

# ── paths ───────────────────────────────────────────────────────────────────

ROOT="${MON_RENDER_ROOT:-}"
DRYRUN="${MON_RENDER_DRYRUN:-0}"

OPT_DIR="$ROOT/opt/monitoring"
PROFILE_DIR="$OPT_DIR/configs/profiles"
CONF_DIR="$OPT_DIR/configs"

SYSCTL_OUT="$ROOT/etc/sysctl.d/99-vless-tuning.conf"
SYSCTL_PREV="$SYSCTL_OUT.prev"
LIMITS_OUT="$ROOT/etc/security/limits.d/99-nofile.conf"
SYSTEMD_OUT="$ROOT/etc/systemd/system.conf.d/limits.conf"
SYSTEMD_SLICE_OUT="$ROOT/etc/systemd/system/user-.slice.d/limits.conf"
HAPROXY_DROPIN="$ROOT/etc/systemd/system/haproxy.service.d/limits.conf"
MODPROBE_OUT="$ROOT/etc/modprobe.d/nf_conntrack.conf"
MODULES_OUT="$ROOT/etc/modules-load.d/mon-tuning.conf"
ANTIDDOS_AUTO="$OPT_DIR/antiddos/config.auto"

FACTS_JSON="$CONF_DIR/tuning-facts.json"
FACTS_ENV="$CONF_DIR/tuning-facts.env"
OVERRIDES="$CONF_DIR/local-overrides.conf"
# Extra ports to keep away from ephemeral allocation: written by the node agent
# on the panel's request, or by the operator by hand. Ports/ranges (a-b), one
# per line or comma-separated; # comments allowed.
RESERVED_EXTRA_FILE="$CONF_DIR/reserved-ports.conf"
NODE_ENV_FILE="$ROOT/opt/monitoring-node/.env"
PROFILE_MARKER="$CONF_DIR/OPT_PROFILE"
VERSION_FILE="$CONF_DIR/VERSION"

log()  { echo "[tune-sysctl] $*" >&2; }
die()  { echo "[tune-sysctl] ERROR: $*" >&2; exit 1; }

run() { if [ "$DRYRUN" = "1" ]; then log "dry-run: $*"; else "$@"; fi; }

# ── integer helpers ─────────────────────────────────────────────────────────

# Largest power of two <= n. The kernel silently rejects a non-power-of-two
# rps_flow_cnt, and hash-sized tables behave badly otherwise.
pow2_floor() {
    local n=$1 p=1
    [ "$n" -lt 1 ] && { echo 1; return; }
    while [ $((p * 2)) -le "$n" ]; do p=$((p * 2)); done
    echo "$p"
}

clamp() {
    local v=$1 lo=$2 hi=$3
    [ "$v" -lt "$lo" ] && v=$lo
    [ "$v" -gt "$hi" ] && v=$hi
    echo "$v"
}

round4k() { echo $(( ($1 / 4096) * 4096 )); }

imin() { [ "$1" -le "$2" ] && echo "$1" || echo "$2"; }
imax() { [ "$1" -ge "$2" ] && echo "$1" || echo "$2"; }

# ceil(log2(n))
clog2() {
    local n=$1 r=0 p=1
    [ "$n" -lt 1 ] && { echo 0; return; }
    while [ "$p" -lt "$n" ]; do p=$((p * 2)); r=$((r + 1)); done
    echo "$r"
}

# ── facts ───────────────────────────────────────────────────────────────────

collect_facts() {
    if [ -n "${MON_FACT_MEMKB:-}" ]; then
        MEM_KB="$MON_FACT_MEMKB"
    else
        MEM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
    fi
    [[ "${MEM_KB:-}" =~ ^[0-9]+$ ]] || die "cannot determine MemTotal"
    [ "$MEM_KB" -ge 65536 ] || die "MemTotal $MEM_KB kB is implausibly small"

    # MemTotal гуляет между загрузками на десятки-сотни килобайт (баллунинг у
    # гипервизора, резервы ядра, размер initrd). Величины на pow2_floor это
    # переживают, а tcp_mem/udp_mem считаются прямым делением числа страниц —
    # и менялись на пару страниц каждую загрузку. Последствия были не
    # косметические: файл переписывался вхолостую, а facts_hash от сырого
    # MEM_KB взводил drift после каждого ребута, из-за чего панель требовала
    # бы переприменения на всём парке. Квантование по 16 МиБ поглощает джиттер
    # с запасом в два порядка и не сдвигает ни одно вычисленное значение.
    MEM_KB_RAW=$MEM_KB
    MEM_KB=$(( MEM_KB / MEM_QUANTUM_KB * MEM_QUANTUM_KB ))

    if [ -n "${MON_FACT_CPUS:-}" ]; then
        CPUS="$MON_FACT_CPUS"
    else
        CPUS=$(nproc 2>/dev/null || echo 1)
    fi
    [[ "${CPUS:-}" =~ ^[0-9]+$ ]] && [ "$CPUS" -ge 1 ] || CPUS=1

    if [ -n "${MON_FACT_PAGESIZE:-}" ]; then
        PAGE="$MON_FACT_PAGESIZE"
    else
        PAGE=$(getconf PAGE_SIZE 2>/dev/null)
    fi
    [[ "${PAGE:-}" =~ ^[0-9]+$ ]] || PAGE=4096

    MEM_MB=$(( MEM_KB / 1024 ))
    PAGES=$(( MEM_KB * 1024 / PAGE ))

    # Fastest link speed among physical interfaces. Only used to size the
    # anti-DDoS pps threshold, so a wrong guess is not load-bearing.
    if [ -n "${MON_FACT_LINK_MBPS:-}" ]; then
        LINK_MBPS="$MON_FACT_LINK_MBPS"
    else
        LINK_MBPS=0
        local d s
        for d in /sys/class/net/*; do
            [ -e "$d/device" ] || continue
            s=$(cat "$d/speed" 2>/dev/null || echo 0)
            [[ "$s" =~ ^[0-9]+$ ]] || continue
            [ "$s" -gt "$LINK_MBPS" ] && LINK_MBPS=$s
        done
    fi
    [ "${LINK_MBPS:-0}" -ge 1 ] 2>/dev/null || LINK_MBPS=1000

    # MTU of the default-route interface: SYNPROXY --mss must match the real path.
    if [ -n "${MON_FACT_MTU:-}" ]; then
        MTU="$MON_FACT_MTU"
    else
        local iface
        iface=$(ip route show default 2>/dev/null | awk '/^default/{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
        MTU=$(cat "/sys/class/net/${iface:-lo}/mtu" 2>/dev/null || echo 1500)
    fi
    [[ "${MTU:-}" =~ ^[0-9]+$ ]] || MTU=1500

    # Currently allocated file handles. fs.file-max must never drop below twice
    # this, or open() starts returning ENFILE process-wide and Xray dies during
    # the apply itself.
    # fs.nr_open понижать нельзя. Это бесплатный потолок того, что процесс
    # ВПРАВЕ себе выставить — памяти он не занимает, а вот юниты со своим
    # LimitNOFILE выше него падают с 205/LIMITS. Так на живой ноде слёг
    # systemd-logind (LimitNOFILE=524288 против нашего 131072): сессии,
    # loginctl и reboot перестали работать.
    # Реальное потребление ограничивает NOFILE_LIMIT — вот его и масштабируем.
    if [ -n "${MON_FACT_NROPEN:-}" ]; then
        NR_OPEN_CURRENT="$MON_FACT_NROPEN"
    else
        NR_OPEN_CURRENT=$(cat /proc/sys/fs/nr_open 2>/dev/null || echo 0)
    fi
    [[ "${NR_OPEN_CURRENT:-}" =~ ^[0-9]+$ ]] || NR_OPEN_CURRENT=0

    # Самый большой LimitNOFILE среди установленных юнитов. Закрывает весь класс
    # поломки, а не только известные нам сервисы.
    if [ -n "${MON_FACT_UNITNOFILE:-}" ]; then
        UNIT_NOFILE_MAX="$MON_FACT_UNITNOFILE"
    else
        UNIT_NOFILE_MAX=$(grep -rhoE '^LimitNOFILE=[0-9]+' \
            /usr/lib/systemd/system /etc/systemd/system 2>/dev/null \
            | cut -d= -f2 | sort -n | tail -1)
    fi
    [[ "${UNIT_NOFILE_MAX:-}" =~ ^[0-9]+$ ]] || UNIT_NOFILE_MAX=0

    if [ -n "${MON_FACT_FILENR:-}" ]; then
        FILE_NR="$MON_FACT_FILENR"
    else
        FILE_NR=$(awk '{print $1}' /proc/sys/fs/file-nr 2>/dev/null || echo 0)
    fi
    [[ "${FILE_NR:-}" =~ ^[0-9]+$ ]] || FILE_NR=0
}

# ── formulas ────────────────────────────────────────────────────────────────

compute() {
    # conntrack: 196 entries/MB ~= 6% of RAM at 320 B per nf_conn. This is a
    # worst-case-under-attack ceiling, not a steady allocation. The floor is
    # 64k entries (~20 MB) — still 4-8x the kernel's own default on a small box,
    # while 128k would have been 7.8% of a 512 MB host.
    CONNTRACK_MAX=$(clamp "$(pow2_floor $(( MEM_MB * 196 )))" 65536 4194304)
    CONNTRACK_HASHSIZE=$(( CONNTRACK_MAX / 4 ))

    # netdev_max_backlog is PER CPU, so divide by CPUS to keep the fleet-wide
    # total bounded: <=1% of RAM if every CPU queue is full at ~2 KB/skb.
    # Depth beyond one softirq budget is bufferbloat; netdev_budget is the drain.
    NETDEV_MAX_BACKLOG=$(clamp "$(pow2_floor $(( MEM_MB * 1024 / 200 / CPUS )))" 1024 16384)
    FLOW_LIMIT_TABLE_LEN=$(clamp "$(pow2_floor $(( 4096 * CPUS )))" 4096 65536)
    NETDEV_BUDGET=$(clamp $(( 150 * CPUS )) 300 1200)
    NETDEV_BUDGET_USECS=$(clamp $(( 1000 * CPUS )) 2000 8000)

    TCP_MAX_ORPHANS=$(clamp "$(pow2_floor $(( MEM_MB * 8 )))" 8192 262144)
    TCP_MAX_TW_BUCKETS=$(clamp "$(pow2_floor $(( MEM_MB * 16 )))" 16384 1048576)

    # NOFILE_LIMIT — то, что процессы реально получают (limits.conf,
    # DefaultLimitNOFILE, ulimits контейнеров, потолок maxconn у HAProxy).
    # Вот эта величина и обязана масштабироваться по RAM.
    NOFILE_LIMIT=$(clamp "$(pow2_floor $(( MEM_MB * 64 )))" 65536 2097152)

    # fs.nr_open / fs.file-max — потолок, который ничего не стоит. Берём
    # максимум из: нужного нам лимита, того что уже стоит на хосте, запросов
    # чужих юнитов и дистрибутивного дефолта Ubuntu. Понижать нельзя никогда.
    FD_MAX=$(imax "$NOFILE_LIMIT" "$NR_OPEN_CURRENT")
    FD_MAX=$(imax "$FD_MAX" "$UNIT_NOFILE_MAX")
    FD_MAX=$(imax "$FD_MAX" 1048576)

    # file-max не должен опускаться ниже удвоенного числа уже выданных хендлов,
    # иначе open() начнёт возвращать ENFILE в масштабе системы.
    FD_FLOOR=$(( FILE_NR * 2 ))
    if [ "$FD_FLOOR" -gt "$FD_MAX" ]; then
        FD_MAX=$FD_FLOOR
        FD_FLOOR_APPLIED=1
    else
        FD_FLOOR_APPLIED=0
    fi
    NPROC_LIMIT=$(clamp "$(pow2_floor $(( 4096 * CPUS )))" 16384 262144)
    DOCKER_NOFILE=$(imin "$NOFILE_LIMIT" 1048576)

    # Accept-queue depth tracks drain rate, i.e. CPUs. Keeping somaxconn and
    # tcp_max_syn_backlog equal means overflow happens in exactly one place.
    SOMAXCONN=$(clamp "$(pow2_floor $(( 4096 * CPUS )))" 4096 65536)
    TCP_MAX_SYN_BACKLOG=$SOMAXCONN

    # 64 MB ceiling = 2.5 Gbps at 200 ms RTT. One number so setsockopt cannot
    # ask for more than autotuning can reach. The 8 MB floor is not arbitrary:
    # quic-go asks for ~7 MB of SO_RCVBUF for Hysteria2/XHTTP-h3 and logs a
    # warning plus degrades if rmem_max cannot cover it.
    SOCK_MAX=$(clamp "$(pow2_floor $(( MEM_MB * 16384 )))" 8388608 67108864)
    # The per-socket default is the dangerous one: it is charged on truesize
    # against tcp_mem/udp_mem for EVERY socket, not just the busy ones.
    SOCK_DEFAULT=$(clamp "$(pow2_floor $(( MEM_MB * 64 )))" 131072 1048576)
    # Guaranteed floor per UDP socket that survives udp_mem pressure — a flat
    # 256 KB would be an udp_mem bypass of N_sockets * 256 KB on a small box.
    UDP_MIN=$(clamp "$(pow2_floor $(( MEM_MB * 16 )))" 16384 262144)

    TCP_RMEM="4096 $SOCK_DEFAULT $SOCK_MAX"
    TCP_WMEM="4096 $SOCK_DEFAULT $SOCK_MAX"

    # tcp high + udp high = 25% of RAM. The old RAM/3 + RAM/6 = 50% meant a 4 GB
    # node could put 2 GB into socket memory alongside Xray+Docker+HAProxy.
    TCP_MEM="$(( PAGES * 3 / 32 )) $(( PAGES / 8 )) $(( PAGES * 5 / 32 ))"
    UDP_MEM="$(( PAGES / 16 )) $(( PAGES * 5 / 64 )) $(( PAGES * 3 / 32 ))"

    # GFP_ATOMIC reserve for driver allocations in IRQ context. Raising it also
    # raises the reclaim watermarks, so it must stay a small share of RAM — but
    # it also must not drop below what the kernel would pick on its own.
    # The floor is therefore RAM-aware: a flat 64 MB floor is 1.6% of a 4 GB box
    # and 12.5% of a 512 MB one.
    MIN_FREE_FLOOR=$(round4k "$(imin 65536 $(( MEM_KB * 3 / 100 )))")
    [ "$MIN_FREE_FLOOR" -lt 4096 ] && MIN_FREE_FLOOR=4096
    MIN_FREE_KBYTES=$(clamp "$(round4k $(( MEM_KB / 128 )))" "$MIN_FREE_FLOOR" 1048576)

    # RFS needs ~2x the concurrently active flow count. 4 B per entry.
    RPS_SOCK_FLOW_ENTRIES=$(clamp "$(pow2_floor $(( CPUS * 4096 )))" 4096 262144)

    TCP_NOTSENT_LOWAT=$(clamp $(( SOCK_DEFAULT / 2 )) 65536 262144)

    # 20% of 248 GB is 49 GB of dirty pages before writers block.
    DIRTY_RATIO=$(clamp $(( 20 * 16384 / MEM_MB )) 2 20)
    DIRTY_BACKGROUND_RATIO=$(imax $(( DIRTY_RATIO / 4 )) 1)

    # All-CPU hex mask, applied by the NIC tune scripts (a bitmap is not
    # reliably settable from a .conf file) but recorded here so verify can see it.
    FLOW_LIMIT_CPU_BITMAP=$(cpu_all_mask "$CPUS")

    HAPROXY_MAXCONN=$(clamp "$(imin $(( MEM_MB * 10 )) $(( (NOFILE_LIMIT - 1024) / 3 )))" 10000 500000)

    compute_reserved_ports

    # SYNPROXY encodes wscale/MSS/SACK in the TCP timestamp; a hardcoded
    # --wscale 7 caps every proxied connection's window at 8 MB.
    SYNPROXY_WSCALE=$(clamp "$(clog2 $(( SOCK_MAX / 65535 )))" 0 14)
    SYNPROXY_MSS=$(clamp $(( MTU - 40 )) 536 1460)

    compute_antiddos
}

# All-CPU affinity mask as comma-grouped hex (kernel wants 32-bit groups).
cpu_all_mask() {
    local cpus=$1
    # Separate statement: under `set -u` a $(( )) referencing a name being
    # declared in the same `local` sees it as unset.
    local groups=$(( (cpus + 31) / 32 ))
    local i bits out="" grp
    for (( i = 0; i < groups; i++ )); do
        bits=$(( cpus - i * 32 ))
        [ "$bits" -gt 32 ] && bits=32
        grp=$(printf '%x' $(( (1 << bits) - 1 )))
        if [ -z "$out" ]; then out="$grp"; else out="$grp,$out"; fi
    done
    echo "${out:-1}"
}

# ── reserved ports ──────────────────────────────────────────────────────────

# Keep the ephemeral allocator away from service ports. With the vpn floor at
# 1024 any managed port sits inside ip_local_port_range, and a service restart
# opens a window where an outbound connection can steal the port as its source
# — the service then fails to bind. Reserved ports are excluded from AUTOMATIC
# allocation only; an explicit bind() still works, so the reservation is free.

detect_node_api_port() {
    local port=""
    if [ -n "${MON_FACT_NODE_API_PORT:-}" ]; then
        port="$MON_FACT_NODE_API_PORT"
    else
        port=$(awk -F= '/^NODE_API_PORT=/ {gsub(/[^0-9]/, "", $2); print $2}' \
            "$NODE_ENV_FILE" 2>/dev/null | tail -1)
    fi
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || port=9100
    echo "$port"
}

compute_reserved_ports() {
    NODE_API_PORT=$(detect_node_api_port)
    # 7500 — внутренний uvicorn ноды, 7501-7504 — локальные socks-порты проверки
    # прокси-конфигураций из панели, 2222 — SSH-порт контейнера Remnawave.
    local tokens="7500 7501-7504 $NODE_API_PORT 2222"
    if [ -f "$RESERVED_EXTRA_FILE" ]; then
        tokens="$tokens $(grep -vE '^[[:space:]]*#' "$RESERVED_EXTRA_FILE" 2>/dev/null | tr ',;' '  ')"
    fi

    # Canonical form MUST match what the kernel prints back from
    # /proc/sys/net/ipv4/ip_local_reserved_ports (sorted ascending, overlapping
    # and adjacent entries merged, runs of two or more as "a-b") — otherwise
    # verify would compare our string against the kernel's and always mismatch.
    local result
    result=$(echo "$tokens" | awk '
        function seg(a, b) { return (a == b ? a "" : a "-" b) "," }
        {
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^[0-9]+$/) { s = $i + 0; e = s }
                else if ($i ~ /^[0-9]+-[0-9]+$/) {
                    split($i, p, "-"); s = p[1] + 0; e = p[2] + 0
                } else continue
                if (s < 1 || e > 65535 || s > e) continue
                n++; starts[n] = s; ends[n] = e
            }
        }
        END {
            if (!n) { print "|0"; exit }
            for (i = 2; i <= n; i++) {
                s = starts[i]; e = ends[i]; j = i - 1
                while (j >= 1 && starts[j] > s) {
                    starts[j+1] = starts[j]; ends[j+1] = ends[j]; j--
                }
                starts[j+1] = s; ends[j+1] = e
            }
            out = ""; cs = starts[1]; ce = ends[1]; total = 0
            for (i = 2; i <= n; i++) {
                if (starts[i] <= ce + 1) { if (ends[i] > ce) ce = ends[i]; continue }
                out = out seg(cs, ce); total += ce - cs + 1
                cs = starts[i]; ce = ends[i]
            }
            out = out seg(cs, ce); total += ce - cs + 1
            sub(/,$/, "", out)
            print out "|" total
        }
    ')
    RESERVED_PORTS="${result%|*}"
    RESERVED_PORTS_COUNT="${result#*|}"
    [ -n "$RESERVED_PORTS" ] || die "reserved ports list rendered empty"
}

compute_antiddos() {
    # 700 B average frame is the pessimistic case for a flood of small packets.
    NIC_PPS_CAP=$(( LINK_MBPS * 1000000 / 8 / 700 ))
    PPS_THRESHOLD=$(imax 20000 "$(imin $(( NIC_PPS_CAP * 60 / 100 )) $(( CPUS * 60000 )))")
    SYNCOOKIE_DELTA=$(imax 200 $(( CPUS * 200 )))
    CONNTRACK_DROP_DELTA=$(imax 20 $(( CONNTRACK_MAX / 8192 )))
    CONNTRACK_PCT=90
    # /proc/stat's `cpu ` row is already normalised across CPUs, so 50% on a
    # 2-core box is one busy core (a normal evening peak) while on 64 cores it
    # is 32 cores in softirq. Small boxes therefore need a HIGHER threshold.
    if [ "$CPUS" -le 4 ]; then SOFTIRQ_PCT=70; else SOFTIRQ_PCT=50; fi
    SOFTIRQ_PCT_PERCPU=90
    SOFTNET_DROP_DELTA=$(( CPUS * 100 ))
    # Только ListenOverflows (полная очередь accept). ListenDrops сюда больше
    # не входит: он растёт при штатной смене слушающих сокетов и на живой
    # ноде держал фон в сотни за цикл при нулевых overflow.
    LISTEN_OVERFLOW_DELTA=$(imax 200 $(( CPUS * 100 )))
    NEWRATE=$(imax 60 $(( CPUS * 30 )))
    NEWBURST=$(( NEWRATE * 4 ))
    HL_MAX=$(clamp "$(pow2_floor $(( MEM_MB * 128 )))" 65536 2097152)
    HASHLIMIT_SRCMASK=32
}

# ── invariants ──────────────────────────────────────────────────────────────

# Refuse to write a config that cannot be correct. Each of these has a concrete
# failure mode attached; none is cosmetic.
verify_invariants() {
    local errs=0
    fail() { log "INVARIANT: $*"; errs=$((errs + 1)); }

    # A limits.conf nofile above fs.nr_open makes PAM fail, which can make login
    # itself fail. This is the one that locks you out of the box.
    [ "$NOFILE_LIMIT" -le "$FD_MAX" ] || fail "NOFILE_LIMIT $NOFILE_LIMIT > FD_MAX $FD_MAX"
    # Понижение fs.nr_open ломает любой юнит со своим LimitNOFILE выше нового
    # значения — ровно так слёг systemd-logind.
    [ "$UNIT_NOFILE_MAX" -le "$FD_MAX" ] 2>/dev/null \
        || fail "fs.nr_open $FD_MAX ниже LimitNOFILE=$UNIT_NOFILE_MAX, запрошенного systemd-юнитом"
    [ "$NR_OPEN_CURRENT" -le "$FD_MAX" ] 2>/dev/null \
        || fail "fs.nr_open понижается с $NR_OPEN_CURRENT до $FD_MAX"
    [ "$DOCKER_NOFILE" -le "$NOFILE_LIMIT" ] || fail "DOCKER_NOFILE $DOCKER_NOFILE > NOFILE_LIMIT $NOFILE_LIMIT"
    [ "$HAPROXY_MAXCONN" -le $(( (NOFILE_LIMIT - 1024) / 3 )) ] \
        || fail "HAPROXY_MAXCONN $HAPROXY_MAXCONN exceeds the FD budget"

    [ "$CONNTRACK_HASHSIZE" -eq $(( CONNTRACK_MAX / 4 )) ] || fail "hashsize != conntrack_max/4"

    # Socket memory must not be able to eat the box. 26% leaves room for Xray,
    # Docker, HAProxy, nginx and the agent.
    local tcp_hi udp_hi sock_bytes budget
    tcp_hi=$(echo "$TCP_MEM" | awk '{print $3}')
    udp_hi=$(echo "$UDP_MEM" | awk '{print $3}')
    sock_bytes=$(( (tcp_hi + udp_hi) * PAGE / 1024 ))
    budget=$(( MEM_KB * 26 / 100 ))
    [ "$sock_bytes" -le "$budget" ] || fail "tcp_mem+udp_mem high ${sock_bytes} kB > 26% of RAM (${budget} kB)"

    # A full conntrack table must not be an OOM by itself. 320 B per nf_conn.
    local ct_kb
    ct_kb=$(( CONNTRACK_MAX / 1024 * 320 ))
    [ "$ct_kb" -le $(( MEM_KB * 7 / 100 )) ] || fail "conntrack_max costs ${ct_kb} kB > 7% of RAM"

    # netdev_max_backlog is per-CPU: bound the aggregate at ~2 KB/skb.
    # Skipped when the value sits at the floor, because the floor is the kernel's
    # own default (1000) — at that point we are not making anything worse than
    # stock, and an absurd RAM/CPU ratio should not block the whole render.
    if [ "$NETDEV_MAX_BACKLOG" -gt 1024 ]; then
        local nb_kb
        nb_kb=$(( NETDEV_MAX_BACKLOG * CPUS * 2048 / 1024 ))
        [ "$nb_kb" -le $(( MEM_KB * 12 / 1000 )) ] \
            || fail "netdev backlog aggregate ${nb_kb} kB > 1.2% of RAM"
    fi

    [ "$SOCK_DEFAULT" -le "$SOCK_MAX" ] || fail "SOCK_DEFAULT > SOCK_MAX"
    [ "$MIN_FREE_KBYTES" -le $(( MEM_KB * 4 / 100 )) ] || fail "min_free_kbytes > 4% of RAM"

    # A runaway reserved-ports.conf (say 1024-65535) would leave the ephemeral
    # allocator nothing to hand out and every outbound connect() would fail.
    [ "${RESERVED_PORTS_COUNT:-0}" -le 4096 ] \
        || fail "reserved ports total $RESERVED_PORTS_COUNT > 4096 — check reserved-ports.conf"

    [ "$errs" -eq 0 ] || die "$errs invariant violation(s) — refusing to write"
}

# ── token substitution ──────────────────────────────────────────────────────

# Every computed key appears in the base files as @@TOKEN@@. Substitution keeps
# the section comments exactly where the author put them, and an unresolved
# token is a hard failure rather than a silently broken config.
#
# Deliberately NOT "base file + appended computed block": sysctl takes the LAST
# occurrence of a duplicated key, so a key present in both would flip meaning
# depending on file order.
emit_token_table() {
    cat <<EOF
CONNTRACK_MAX=$CONNTRACK_MAX
NETDEV_MAX_BACKLOG=$NETDEV_MAX_BACKLOG
FLOW_LIMIT_TABLE_LEN=$FLOW_LIMIT_TABLE_LEN
NETDEV_BUDGET=$NETDEV_BUDGET
NETDEV_BUDGET_USECS=$NETDEV_BUDGET_USECS
TCP_MAX_ORPHANS=$TCP_MAX_ORPHANS
TCP_MAX_TW_BUCKETS=$TCP_MAX_TW_BUCKETS
FD_MAX=$FD_MAX
SOMAXCONN=$SOMAXCONN
TCP_MAX_SYN_BACKLOG=$TCP_MAX_SYN_BACKLOG
SOCK_MAX=$SOCK_MAX
SOCK_DEFAULT=$SOCK_DEFAULT
UDP_MIN=$UDP_MIN
TCP_RMEM=$TCP_RMEM
TCP_WMEM=$TCP_WMEM
TCP_MEM=$TCP_MEM
UDP_MEM=$UDP_MEM
MIN_FREE_KBYTES=$MIN_FREE_KBYTES
RPS_SOCK_FLOW_ENTRIES=$RPS_SOCK_FLOW_ENTRIES
TCP_NOTSENT_LOWAT=$TCP_NOTSENT_LOWAT
DIRTY_RATIO=$DIRTY_RATIO
DIRTY_BACKGROUND_RATIO=$DIRTY_BACKGROUND_RATIO
NOFILE_LIMIT=$NOFILE_LIMIT
NPROC_LIMIT=$NPROC_LIMIT
RESERVED_PORTS=$RESERVED_PORTS
EOF
}

substitute() {
    local src=$1
    awk -v tokens="$(emit_token_table)" '
        BEGIN {
            n = split(tokens, lines, "\n")
            for (i = 1; i <= n; i++) {
                if (lines[i] == "") continue
                eq = index(lines[i], "=")
                k = substr(lines[i], 1, eq - 1)
                v = substr(lines[i], eq + 1)
                val["@@" k "@@"] = v
            }
        }
        {
            while (match($0, /@@[A-Z0-9_]+@@/)) {
                t = substr($0, RSTART, RLENGTH)
                if (!(t in val)) break
                $0 = substr($0, 1, RSTART - 1) val[t] substr($0, RSTART + RLENGTH)
            }
            print
        }
    ' "$src"
}

# ── rendering ───────────────────────────────────────────────────────────────

# Ставит файл побайтово, без подстановки команды. `$(cat ...)` срезает
# завершающие переводы строк, из-за чего хеш временного файла никогда не совпадал
# с установленным и рендер переписывал конфиг на каждой загрузке впустую.
atomic_install_file() {
    local src=$1 dest=$2 mode=${3:-644}
    mkdir -p "$(dirname "$dest")" 2>/dev/null || true
    cp -f "$src" "$dest.tmp" || die "cannot write $dest.tmp"
    chmod "$mode" "$dest.tmp" 2>/dev/null || true
    mv -f "$dest.tmp" "$dest" || die "cannot move $dest.tmp -> $dest"
}

atomic_write() {
    local dest=$1 content=$2 mode=${3:-644}
    mkdir -p "$(dirname "$dest")" 2>/dev/null || true
    printf '%s' "$content" > "$dest.tmp" || die "cannot write $dest.tmp"
    chmod "$mode" "$dest.tmp" 2>/dev/null || true
    mv -f "$dest.tmp" "$dest" || die "cannot move $dest.tmp -> $dest"
}

read_profile() {
    PROFILE="${1:-}"
    if [ -z "$PROFILE" ]; then
        PROFILE=$(cat "$PROFILE_MARKER" 2>/dev/null | tr -d '[:space:]')
    fi
    case "$PROFILE" in
        vpn|panel) : ;;
        *) PROFILE="vpn" ;;
    esac
    CONTENT_VERSION=$(cat "$VERSION_FILE" 2>/dev/null | tr -d '[:space:]')
    [ -n "$CONTENT_VERSION" ] || CONTENT_VERSION="unknown"
}

# Reject a duplicated key outright: with sysctl's last-wins semantics a
# duplicate is a config whose meaning depends on line order.
assert_no_duplicate_keys() {
    local file=$1 dupes
    dupes=$(grep -oE '^[[:space:]]*[a-z][a-z0-9_.]*[[:space:]]*=' "$file" 2>/dev/null \
        | tr -d ' \t=' | sort | uniq -d)
    if [ -n "$dupes" ]; then
        log "duplicate keys in rendered output:"
        printf '  %s\n' $dupes >&2
        die "refusing to install a config with duplicate keys"
    fi
}

# A key must have exactly one home: shared or profile, never both.
assert_no_profile_overlap() {
    local common=$1 profile=$2 both
    both=$(comm -12 \
        <(grep -oE '^[[:space:]]*[a-z][a-z0-9_.]*[[:space:]]*=' "$common" | tr -d ' \t=' | sort -u) \
        <(grep -oE '^[[:space:]]*[a-z][a-z0-9_.]*[[:space:]]*=' "$profile" | tr -d ' \t=' | sort -u))
    if [ -n "$both" ]; then
        log "keys present in BOTH common.base.conf and $(basename "$profile"):"
        printf '  %s\n' $both >&2
        die "each key must have exactly one home"
    fi
}

# Record keys the running kernel does not have, so verify does not assert on
# them. This is what makes one config work across 5.15 (no tcp_migrate_req) and
# 6.8 without a per-distro config.
collect_unsupported() {
    local file=$1 key path out="" total=0 missing=0
    # No /proc/sys at all (CI, a container without /proc, a non-Linux host):
    # probing would mark EVERY key unsupported and verification would then
    # silently assert nothing. Better to report none and let verify complain.
    [ -d /proc/sys ] || { echo ""; return 0; }
    while read -r key; do
        [ -n "$key" ] || continue
        total=$((total + 1))
        path="/proc/sys/${key//.//}"
        if [ ! -e "$path" ]; then
            out="$out $key"
            missing=$((missing + 1))
        fi
    done < <(grep -oE '^[[:space:]]*[a-z][a-z0-9_.]*[[:space:]]*=' "$file" | tr -d ' \t=')
    # A real kernel is missing a handful of keys, not most of them. Anything
    # above half means the probe itself is broken (nf_conntrack not yet loaded,
    # /proc mounted read-only oddly) — don't let that disable verification.
    if [ "$total" -gt 0 ] && [ $(( missing * 100 / total )) -gt 50 ]; then
        log "probe found $missing/$total keys missing — treating as a broken probe, not a limited kernel"
        echo ""
        return 0
    fi
    echo "${out# }"
}

do_render() {
    local profile_arg="${1:-}"
    read_profile "$profile_arg"
    collect_facts
    compute
    verify_invariants

    local common="$PROFILE_DIR/common.base.conf"
    local pfile="$PROFILE_DIR/$PROFILE.base.conf"
    [ -f "$common" ] || die "missing $common"
    [ -f "$pfile" ]  || die "missing $pfile"
    assert_no_profile_overlap "$common" "$pfile"

    local body
    body=$(
        echo "# Generated by tune-sysctl.sh — DO NOT EDIT."
        echo "# formula=$FORMULA_VERSION content=$CONTENT_VERSION profile=$PROFILE"
        echo "# facts: mem=${MEM_MB}MB cpus=$CPUS page=$PAGE link=${LINK_MBPS}Mbps mtu=$MTU"
        echo "# Operator overrides belong in $OVERRIDES (appended last, wins)."
        echo
        substitute "$common"
        echo
        substitute "$pfile"
    )

    local ov_present="false" ov_sha="null" ov_keys=""
    if [ -f "$OVERRIDES" ]; then
        ov_present="true"
        ov_sha=$(sha256sum "$OVERRIDES" 2>/dev/null | awk '{print $1}')
        ov_keys=$(grep -oE '^[[:space:]]*[a-z][a-z0-9_.]*[[:space:]]*=' "$OVERRIDES" 2>/dev/null \
            | tr -d ' \t=' | sort -u | tr '\n' ' ')
        # Перекрытые ключи вырезаются из базы, иначе получается дубль, а
        # `sysctl` берёт последнее вхождение — страж дублей ниже отвергал бы
        # весь рендер. Без этого аварийный откат через local-overrides.conf,
        # обещанный в документации, попросту не работал.
        if [ -n "$ov_keys" ]; then
            body=$(printf '%s\n' "$body" | awk -v keys="$ov_keys" '
                BEGIN { n = split(keys, a, " "); for (i = 1; i <= n; i++) if (a[i] != "") drop[a[i]] = 1 }
                {
                    if (match($0, /^[ \t]*[a-z][a-z0-9_.]*[ \t]*=/)) {
                        k = substr($0, RSTART, RLENGTH)
                        gsub(/[ \t=]/, "", k)
                        if (k in drop) next
                    }
                    print
                }')
        fi
        body="$body
# ---- operator overrides from $OVERRIDES ----
$(cat "$OVERRIDES")"
    fi

    # Guard: never install a half-rendered file. Keeping the previous config is
    # always better than applying one with literal placeholder lines in it.
    # Comment lines are exempt — the base files legitimately describe the
    # placeholder syntax in their header.
    local leftover
    leftover=$(printf '%s\n' "$body" | grep -nE '^[[:space:]]*[^#[:space:]].*@@[A-Z0-9_]+@@' || true)
    if [ -n "$leftover" ]; then
        printf '%s\n' "$leftover" >&2
        die "unresolved token(s) in rendered output"
    fi

    local tmp
    tmp=$(mktemp)
    printf '%s\n' "$body" > "$tmp"
    assert_no_duplicate_keys "$tmp"

    local unsupported
    unsupported=$(collect_unsupported "$tmp")
    [ -n "$unsupported" ] && log "kernel lacks: $unsupported"

    local new_sha old_sha changed=1
    new_sha=$(sha256sum "$tmp" | awk '{print $1}')
    old_sha=$(sha256sum "$SYSCTL_OUT" 2>/dev/null | awk '{print $1}')
    [ "$new_sha" = "$old_sha" ] && changed=0

    if [ "$changed" = "1" ]; then
        [ -f "$SYSCTL_OUT" ] && cp -f "$SYSCTL_OUT" "$SYSCTL_PREV" 2>/dev/null || true
        atomic_install_file "$tmp" "$SYSCTL_OUT"
        log "rendered $SYSCTL_OUT (profile=$PROFILE mem=${MEM_MB}MB cpus=$CPUS)"
    else
        log "no change ($SYSCTL_OUT already current)"
    fi
    rm -f "$tmp"

    render_limits
    render_facts "$unsupported" "$ov_present" "$ov_sha" "$ov_keys" "$new_sha"
    render_antiddos

    if [ "$changed" = "1" ]; then
        apply_live
    fi
    return 0
}

render_limits() {
    local ltmpl="$PROFILE_DIR/limits.tmpl"
    local stmpl="$PROFILE_DIR/systemd-limits.tmpl"
    [ -f "$ltmpl" ] || die "missing $ltmpl"
    [ -f "$stmpl" ] || die "missing $stmpl"

    local limits systemd
    limits=$(substitute "$ltmpl")
    systemd=$(substitute "$stmpl")
    if printf '%s\n%s\n' "$limits" "$systemd" \
        | grep -qE '^[[:space:]]*[^#[:space:]].*@@[A-Z0-9_]+@@'; then
        die "unresolved token in limits templates"
    fi

    atomic_write "$LIMITS_OUT" "$limits
"
    atomic_write "$SYSTEMD_OUT" "$systemd
"
    # Same content with [Manager] -> [Slice] so user sessions inherit the limit.
    # This substitution used to be duplicated in install.sh and system.py.
    atomic_write "$SYSTEMD_SLICE_OUT" "$(printf '%s\n' "$systemd" | sed 's/^\[Manager\]$/[Slice]/')
"

    # HAProxy is a native systemd unit: its own RLIMIT_NOFILE is what bounds
    # maxconn, and with strict-limits (default since 2.5) a maxconn it cannot
    # back with FDs makes it refuse to start outright.
    atomic_write "$HAPROXY_DROPIN" "# Generated by tune-sysctl.sh — DO NOT EDIT.
[Service]
LimitNOFILE=$NOFILE_LIMIT
"

    atomic_write "$MODPROBE_OUT" "# Generated by tune-sysctl.sh — DO NOT EDIT.
options nf_conntrack hashsize=$CONNTRACK_HASHSIZE
"
    # nf_conntrack must be loaded before systemd-sysctl runs, otherwise every
    # net.netfilter.* key silently does nothing on boot.
    atomic_write "$MODULES_OUT" "# Generated by tune-sysctl.sh — DO NOT EDIT.
nf_conntrack
tcp_bbr
"
}

render_antiddos() {
    atomic_write "$ANTIDDOS_AUTO" "# Generated by tune-sysctl.sh — DO NOT EDIT.
# Sourced by ddos-watchdog.sh BEFORE its own config file, so operator overrides
# in /opt/monitoring/antiddos/config still win.
PPS_THRESHOLD=$PPS_THRESHOLD
SYNCOOKIE_DELTA=$SYNCOOKIE_DELTA
CONNTRACK_DROP_DELTA=$CONNTRACK_DROP_DELTA
CONNTRACK_PCT=$CONNTRACK_PCT
SOFTIRQ_PCT=$SOFTIRQ_PCT
SOFTIRQ_PCT_PERCPU=$SOFTIRQ_PCT_PERCPU
SOFTNET_DROP_DELTA=$SOFTNET_DROP_DELTA
LISTEN_OVERFLOW_DELTA=$LISTEN_OVERFLOW_DELTA
NEWRATE=$NEWRATE
NEWBURST=$NEWBURST
HL_MAX=$HL_MAX
HASHLIMIT_SRCMASK=$HASHLIMIT_SRCMASK
SYNPROXY_WSCALE=$SYNPROXY_WSCALE
SYNPROXY_MSS=$SYNPROXY_MSS
"
}

# ВАЖНО: только квантованный MEM_KB. Сырое значение здесь означало бы новый
# хеш после каждой перезагрузки и, как следствие, вечный drift.
# Формат строки обязан совпадать с probe в read_tuning_drift()
# (node/app/routers/system.py).
facts_hash() {
    printf '%s|%s|%s|%s|%s' "$MEM_KB" "$CPUS" "$PAGE" "$LINK_MBPS" "$MTU" \
        | sha256sum | awk '{print $1}'
}

json_list() {
    local out="" w
    for w in $1; do
        if [ -z "$out" ]; then out="\"$w\""; else out="$out, \"$w\""; fi
    done
    echo "[$out]"
}

# Static keys worth verifying. Values are read back OUT of the rendered file
# rather than repeated here, so there is still exactly one source of truth.
# Without this, verification would only cover the computed keys and would no
# longer notice that e.g. BBR failed to load.
VERIFY_STATIC_KEYS="net.ipv4.tcp_congestion_control net.core.default_qdisc \
net.ipv4.tcp_timestamps net.ipv4.tcp_tw_reuse net.netfilter.nf_conntrack_tcp_loose \
net.netfilter.nf_conntrack_tcp_be_liberal net.ipv4.tcp_retries2 \
net.ipv4.tcp_syncookies net.ipv4.tcp_autocorking net.ipv4.tcp_fastopen \
net.netfilter.nf_conntrack_tcp_timeout_established"

emit_static_json() {
    local file=$1 key val first=1
    for key in $VERIFY_STATIC_KEYS; do
        val=$(grep -E "^${key//./\\.}[[:space:]]*=" "$file" 2>/dev/null \
            | tail -1 | sed 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//')
        [ -n "$val" ] || continue
        if [ "$first" = 1 ]; then first=0; else printf ',\n'; fi
        printf '    "%s": "%s"' "$key" "$val"
    done
    [ "$first" = 1 ] || printf '\n'
}

render_facts() {
    local unsupported=$1 ov_present=$2 ov_sha=$3 ov_keys=$4 rendered_sha=$5
    local fh
    fh=$(facts_hash)

    local ov_sha_json="null"
    [ "$ov_sha" != "null" ] && [ -n "$ov_sha" ] && ov_sha_json="\"$ov_sha\""

    atomic_write "$FACTS_JSON" "{
  \"schema\": $FACTS_SCHEMA,
  \"formula_version\": \"$FORMULA_VERSION\",
  \"content_version\": \"$CONTENT_VERSION\",
  \"profile\": \"$PROFILE\",
  \"rendered_sha256\": \"$rendered_sha\",
  \"facts\": {
    \"mem_kb\": $MEM_KB,
    \"mem_kb_raw\": $MEM_KB_RAW,
    \"mem_mb\": $MEM_MB,
    \"cpus\": $CPUS,
    \"page_size\": $PAGE,
    \"pages\": $PAGES,
    \"link_mbps\": $LINK_MBPS,
    \"mtu\": $MTU,
    \"file_nr\": $FILE_NR,
    \"nr_open_current\": $NR_OPEN_CURRENT,
    \"unit_nofile_max\": $UNIT_NOFILE_MAX
  },
  \"facts_hash\": \"$fh\",
  \"computed\": {
    \"net.netfilter.nf_conntrack_max\": \"$CONNTRACK_MAX\",
    \"net.core.netdev_max_backlog\": \"$NETDEV_MAX_BACKLOG\",
    \"net.core.flow_limit_table_len\": \"$FLOW_LIMIT_TABLE_LEN\",
    \"net.core.netdev_budget\": \"$NETDEV_BUDGET\",
    \"net.core.netdev_budget_usecs\": \"$NETDEV_BUDGET_USECS\",
    \"net.ipv4.tcp_max_orphans\": \"$TCP_MAX_ORPHANS\",
    \"net.ipv4.tcp_max_tw_buckets\": \"$TCP_MAX_TW_BUCKETS\",
    \"fs.file-max\": \"$FD_MAX\",
    \"fs.nr_open\": \"$FD_MAX\",
    \"net.core.somaxconn\": \"$SOMAXCONN\",
    \"net.ipv4.tcp_max_syn_backlog\": \"$TCP_MAX_SYN_BACKLOG\",
    \"net.core.rmem_max\": \"$SOCK_MAX\",
    \"net.core.wmem_max\": \"$SOCK_MAX\",
    \"net.core.rmem_default\": \"$SOCK_DEFAULT\",
    \"net.core.wmem_default\": \"$SOCK_DEFAULT\",
    \"net.ipv4.tcp_rmem\": \"$TCP_RMEM\",
    \"net.ipv4.tcp_wmem\": \"$TCP_WMEM\",
    \"net.ipv4.udp_rmem_min\": \"$UDP_MIN\",
    \"net.ipv4.udp_wmem_min\": \"$UDP_MIN\",
    \"net.ipv4.tcp_mem\": \"$TCP_MEM\",
    \"net.ipv4.udp_mem\": \"$UDP_MEM\",
    \"vm.min_free_kbytes\": \"$MIN_FREE_KBYTES\",
    \"net.core.rps_sock_flow_entries\": \"$RPS_SOCK_FLOW_ENTRIES\",
    \"net.ipv4.ip_local_reserved_ports\": \"$RESERVED_PORTS\",
    \"net.ipv4.tcp_notsent_lowat\": \"$TCP_NOTSENT_LOWAT\",
    \"vm.dirty_ratio\": \"$DIRTY_RATIO\",
    \"vm.dirty_background_ratio\": \"$DIRTY_BACKGROUND_RATIO\"
  },
  \"static\": {
$(emit_static_json "$SYSCTL_OUT")  },
  \"derived\": {
    \"conntrack_hashsize\": $CONNTRACK_HASHSIZE,
    \"fd_max\": $FD_MAX,
    \"fd_floor_applied\": $FD_FLOOR_APPLIED,
    \"nofile_limit\": $NOFILE_LIMIT,
    \"nproc_limit\": $NPROC_LIMIT,
    \"docker_nofile\": $DOCKER_NOFILE,
    \"haproxy_maxconn\": $HAPROXY_MAXCONN,
    \"rps_sock_flow_entries\": $RPS_SOCK_FLOW_ENTRIES,
    \"flow_limit_cpu_bitmap\": \"$FLOW_LIMIT_CPU_BITMAP\",
    \"synproxy_mss\": $SYNPROXY_MSS,
    \"synproxy_wscale\": $SYNPROXY_WSCALE
  },
  \"unsupported_keys\": $(json_list "$unsupported"),
  \"local_overrides\": {
    \"present\": $ov_present,
    \"sha256\": $ov_sha_json,
    \"keys\": $(json_list "$ov_keys")
  }
}
"

    atomic_write "$FACTS_ENV" "# Generated by tune-sysctl.sh — DO NOT EDIT.
FORMULA_VERSION=$FORMULA_VERSION
CONTENT_VERSION=$CONTENT_VERSION
OPT_PROFILE=$PROFILE
FACTS_HASH=$fh
MEM_KB=$MEM_KB
MEM_KB_RAW=$MEM_KB_RAW
MEM_MB=$MEM_MB
CPUS=$CPUS
PAGE_SIZE=$PAGE
LINK_MBPS=$LINK_MBPS
MTU=$MTU
CONNTRACK_MAX=$CONNTRACK_MAX
CONNTRACK_HASHSIZE=$CONNTRACK_HASHSIZE
FD_MAX=$FD_MAX
NR_OPEN_CURRENT=$NR_OPEN_CURRENT
NOFILE_LIMIT=$NOFILE_LIMIT
NPROC_LIMIT=$NPROC_LIMIT
DOCKER_NOFILE=$DOCKER_NOFILE
HAPROXY_MAXCONN=$HAPROXY_MAXCONN
RPS_SOCK_FLOW_ENTRIES=$RPS_SOCK_FLOW_ENTRIES
FLOW_LIMIT_CPU_BITMAP=$FLOW_LIMIT_CPU_BITMAP
FLOW_LIMIT_TABLE_LEN=$FLOW_LIMIT_TABLE_LEN
SYNPROXY_WSCALE=$SYNPROXY_WSCALE
SYNPROXY_MSS=$SYNPROXY_MSS
NODE_API_PORT=$NODE_API_PORT
RESERVED_PORTS=$RESERVED_PORTS
"
}

# fs.nr_open must be raised BEFORE limits.conf is honoured by PAM, and
# systemd-sysctl runs long before network-online.target, so the freshly
# rendered file has to be applied here or it only takes effect next boot.
apply_live() {
    [ "$DRYRUN" = "1" ] && { log "dry-run: skipping live apply"; return 0; }
    [ "$(id -u)" = "0" ] || { log "not root: skipping live apply"; return 0; }

    run modprobe nf_conntrack 2>/dev/null || true
    run modprobe tcp_bbr 2>/dev/null || true

    sysctl -qw fs.nr_open="$FD_MAX" 2>/dev/null || log "could not raise fs.nr_open"
    # -e: ignore unknown keys instead of failing the whole file on a kernel
    # that lacks one of them.
    sysctl -e -p "$SYSCTL_OUT" >/dev/null 2>&1 || log "sysctl -e -p reported errors"

    local cur
    cur=$(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 0)
    if [ "${cur:-0}" -lt "$CONNTRACK_HASHSIZE" ] 2>/dev/null; then
        echo "$CONNTRACK_HASHSIZE" > /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || true
    fi

    run systemctl daemon-reload >/dev/null 2>&1 || true
}

# ── verify ──────────────────────────────────────────────────────────────────

# Compares the live kernel against the facts file rather than a hardcoded table,
# so a per-host computed value cannot produce a false failure. Reads every key
# in ONE pass instead of one process per key.
do_verify() {
    [ -f "$FACTS_JSON" ] || {
        echo '{"success": false, "failed": ["tuning-facts.json missing — node predates the renderer"], "checked": {}}'
        return 1
    }

    # Инициализация обязательна: `local x` оставляет переменную UNSET, а не
    # пустой, и под `set -u` обращение к ней падает. unsupported присваивается
    # только в fallback-ветке — на хосте с python3 (обычный случай) её иначе
    # никогда не существует.
    local expected="" unsupported=""
    expected=$(python3 - "$FACTS_JSON" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
skip = set(d.get("unsupported_keys") or [])
for k, v in {**d.get("computed", {}), **d.get("static", {})}.items():
    if k not in skip:
        print(f"{k}\t{v}")
PY
    )
    if [ -z "$expected" ]; then
        # No python3 (bare host during install): fall back to grep/sed on the
        # flat "key": "value" pairs inside the computed block.
        expected=$(sed -n '/"computed": {/,/^  }/p' "$FACTS_JSON" \
            | grep -oE '"[a-z][a-z0-9_.\-]*": "[^"]*"' \
            | sed 's/^"//; s/": "/\t/; s/"$//')
        unsupported=$(sed -n 's/.*"unsupported_keys": \[\(.*\)\].*/\1/p' "$FACTS_JSON" | tr -d '", ')
    fi

    local failed=0 checked=0 out="" key want got
    while IFS=$'\t' read -r key want; do
        [ -n "$key" ] || continue
        case " $unsupported " in *" $key "*) continue ;; esac
        got=$(sysctl -n "$key" 2>/dev/null) || got="MISSING"
        checked=$((checked + 1))
        # sysctl -n returns multi-value keys tab-separated while the file uses
        # single spaces, so compare token lists, not raw strings.
        local wn gn
        wn=$(echo "$want" | tr -s ' \t' ' ' | sed 's/^ //; s/ $//')
        gn=$(echo "$got"  | tr -s ' \t' ' ' | sed 's/^ //; s/ $//')
        if [ "$wn" != "$gn" ]; then
            failed=$((failed + 1))
            out="$out $key(want=$wn got=$gn)"
            log "MISMATCH $key: want '$wn' got '$gn'"
        fi
    done <<< "$expected"

    # Threshold, not equality: the module may already have a larger table.
    local want_hs cur_hs
    want_hs=$(sed -n 's/.*"conntrack_hashsize": \([0-9]*\).*/\1/p' "$FACTS_JSON")
    cur_hs=$(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 0)
    if [ -n "$want_hs" ] && [ "${cur_hs:-0}" -lt "$want_hs" ] 2>/dev/null; then
        failed=$((failed + 1))
        out="$out conntrack_hashsize(want>=$want_hs got=$cur_hs)"
    fi

    if [ "$failed" -eq 0 ]; then
        echo "{\"success\": true, \"checked_count\": $checked, \"failed\": []}"
        return 0
    fi
    echo "{\"success\": false, \"checked_count\": $checked, \"failed\": \"$(echo "$out" | sed 's/^ //')\"}"
    return 1
}

do_rollback() {
    [ -f "$SYSCTL_PREV" ] || die "no $SYSCTL_PREV to roll back to"
    cp -f "$SYSCTL_PREV" "$SYSCTL_OUT" || die "rollback copy failed"
    log "restored $SYSCTL_OUT from .prev"
    [ "$DRYRUN" = "1" ] || sysctl -e -p "$SYSCTL_OUT" >/dev/null 2>&1 || true
}

do_facts() {
    read_profile "${1:-}"
    collect_facts
    compute
    # Also assert here, not just in render: `facts` is what the test matrix and
    # any human debugging reads, and it must never print a set of numbers that
    # the renderer would refuse to install.
    verify_invariants
    echo "# facts"
    for v in MEM_KB MEM_KB_RAW MEM_MB CPUS PAGE PAGES LINK_MBPS MTU FILE_NR \
             NR_OPEN_CURRENT UNIT_NOFILE_MAX; do
        eval "echo \"$v=\$$v\""
    done
    echo "# computed"
    emit_token_table
    echo "# derived"
    for v in CONNTRACK_HASHSIZE DOCKER_NOFILE HAPROXY_MAXCONN FLOW_LIMIT_CPU_BITMAP \
             SYNPROXY_WSCALE SYNPROXY_MSS NODE_API_PORT RESERVED_PORTS_COUNT; do
        eval "echo \"$v=\$$v\""
    done
    echo "# antiddos"
    for v in PPS_THRESHOLD SYNCOOKIE_DELTA CONNTRACK_DROP_DELTA CONNTRACK_PCT \
             SOFTIRQ_PCT SOFTNET_DROP_DELTA LISTEN_OVERFLOW_DELTA NEWRATE NEWBURST HL_MAX; do
        eval "echo \"$v=\$$v\""
    done
}

do_self_test() {
    read_profile "${1:-}"
    collect_facts
    compute
    verify_invariants
    log "self-test OK (mem=${MEM_MB}MB cpus=$CPUS profile=$PROFILE)"
}

case "${1:-render}" in
    facts)     do_facts "${2:-}" ;;
    render)    do_render "${2:-}" ;;
    verify)    do_verify ;;
    rollback)  do_rollback ;;
    self-test) do_self_test "${2:-}" ;;
    *)
        echo "usage: $0 {facts|render|verify|rollback|self-test} [profile]" >&2
        exit 1
        ;;
esac
