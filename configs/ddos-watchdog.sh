#!/bin/bash
#
# Anti-DDoS watchdog + emergency-mode ruleset (self-contained, runs on host).
#
# Two jobs in one script so the rule logic lives in a single place:
#   1. `loop`  — systemd service: reads /proc DDoS signals, auto-toggles the
#      emergency ruleset (conservative thresholds), self-heals the INPUT jump.
#   2. CLI verbs (enable-manual/disable-manual/watchdog-on|off/status/
#      whitelist-sync/apply/clear) — called by the node API to drive it manually.
#
# Emergency rules live in a dedicated ANTIDDOS chain jumped from INPUT only while
# active. A firewall-profile apply does `ufw --force reset` which flushes INPUT —
# the loop re-adds the jump within one cycle (self-heal). SSH is never dropped;
# the node API port 9100 is let in by the whitelist only. Client ports are auto-detected (ss), never hardcoded.
#
# Транзит — соединения, пришедшие через DNAT (опубликованные порты контейнеров
# Docker, проброс портов), — цепочку в INPUT не проходит: у него своя цепочка
# ANTIDDOS_TRANSIT в mangle/POSTROUTING с тем же лимитом новых соединений.
#

set -u

# Bumped when the script logic changes — the panel compares this against what a
# node reports and auto-reinstalls on drift, so updates roll out without clicks.
WATCHDOG_VERSION="2.6.0"

STATE_DIR="/opt/monitoring/antiddos"
STATE_FILE="$STATE_DIR/state.json"
WHITELIST_FILE="$STATE_DIR/whitelist.json"
CONFIG_FILE="$STATE_DIR/config"
AUTO_CONFIG_FILE="$STATE_DIR/config.auto"
RUN_DIR="$STATE_DIR/run"

CHAIN="ANTIDDOS"
TRANSIT_CHAIN="ANTIDDOS_TRANSIT"      # mangle/POSTROUTING, см. build_transit_chain
HANDSHAKE_CHAIN="ANTIDDOS_HANDSHAKE"  # mangle/OUTPUT, см. build_handshake_rules
TRUSTED_CHAIN="ANTIDDOS_TRUSTED"      # raw/PREROUTING, см. build_trusted_rules
TRANSIT_HASHLIMIT="ad_emg_fwd"
INTERNAL_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
# Джампы аварийного режима: таблица:встроенная цепочка:своя цепочка
EMERGENCY_JUMPS="filter:INPUT:$CHAIN raw:PREROUTING:$TRUSTED_CHAIN mangle:POSTROUTING:$TRANSIT_CHAIN mangle:OUTPUT:$HANDSHAKE_CHAIN"
ALLOW_SET="antiddos_allow"
TEMP_BLOCK_SET="blocklist_temp"   # created by ipset_manager; used if present

# --- tunables ---
# Defaults below are the small-host fallback. tune-sysctl.sh writes host-scaled
# values into config.auto, and the operator's own config wins over both — so the
# sourcing order is: defaults -> config.auto -> config.
#
# Why scaling matters: a flat PPS_THRESHOLD=150000 is ordinary evening traffic on
# a 64-core 10G node and an already-dead 2-core box. SOFTIRQ_PCT needs the
# OPPOSITE correction to what that suggests — /proc/stat's `cpu ` row is already
# normalised across CPUs, so 50% on 2 cores is one busy core (normal) while on 64
# cores it is 32 cores in softirq (catastrophic). Small hosts get a HIGHER
# threshold, not a lower one.
INTERVAL=10                 # loop period, seconds
NEWRATE=60                  # max new conns/sec per source (tier-2, emergency)
NEWBURST=240                # burst for the rate limiter
HL_MAX=262144               # hashlimit hash table size
HASHLIMIT_SRCMASK=32        # 24 helps against a /24 botnet, hurts CGNAT
NODE_API_PORT_DEFAULT=9100  # nginx mTLS; let in by the whitelist only, no ACCEPT of its own
AGENT_SOCKET=/run/monitoring-node/api.sock  # агент ноды текущей версии
AGENT_LOOPBACK_PORT=7500    # агент нод старых версий; ACCEPT только с петли
LOOPBACK_NET="127.0.0.0/8"

# detection thresholds (conservative — must not fire on a legit evening peak)
CONNTRACK_DROP_DELTA=50     # insert_failed growth/cycle → strong (table full, dropping)
CONNTRACK_PCT=90            # conntrack fill % → weak hint only (near exhaustion)
SYNCOOKIE_DELTA=200         # SyncookiesSent growth per cycle → strong signal (SYN flood)
PPS_THRESHOLD=150000        # rx packets/sec …
SMALL_PKT_BYTES=200         # …combined with avg packet < this → flood-of-tiny (weak)
SOFTIRQ_PCT=50              # aggregate softirq CPU% → weak signal
SOFTIRQ_PCT_PERCPU=90       # busiest single CPU in softirq → weak signal
SOFTNET_DROP_DELTA=200      # /proc/net/softnet_stat drops per cycle → STRONG
SOFTNET_HOLD_CYCLES=2       # ...но только если держится столько циклов подряд
LISTEN_OVERFLOW_DELTA=400   # ListenOverflows per cycle → weak, см. read_listen_overflows
WEAK_HOLD=45               # weak signals must persist this long (s) before enabling
HYSTERESIS=900              # auto-disable after this many seconds with no signal

# SYNPROXY parameters. Hardcoded --wscale 7 caps every proxied connection's
# window at 8 MB, and --mss 1460 is wrong on any tunnelled/vRack path; both are
# computed from the host's real rmem_max and MTU by tune-sysctl.sh.
SYNPROXY_WSCALE=7
SYNPROXY_MSS=1460

# Self-confirm: if the node API stops answering and no SSH session is alive this
# many cycles in a row while auto-triggered emergency mode is on, tear the chain
# down. A rule bug must not be able to leave the box unreachable until someone
# notices. A manual pin is kept — see safety_net_cleared.
SELF_CONFIRM_FAILS=3

# Перечитывается КАЖДЫЙ цикл, а не только при старте. tune-sysctl.sh считает
# пороги под конкретное железо и пишет config.auto уже после того, как сервис
# поднялся — при разовом чтении вотчдог месяцами держит в памяти дефолты выше и
# ничем этого не показывает: на 8-ядерной ноде порог остаётся 200 вместо 800,
# и аварийный режим включается на всплеске, который панель считает штатным.
load_config() {
    [ -r "$AUTO_CONFIG_FILE" ] && . "$AUTO_CONFIG_FILE"
    [ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
    return 0
}

load_config

log() { echo "[antiddos] $*" >&2; }

# DRYRUN prints the exact commands instead of running them. The emergency chain
# is inserted at INPUT position 1, so being able to read it before it goes live
# is worth the six lines.
DRYRUN=0
ipt() {
    if [ "$DRYRUN" = "1" ]; then echo "iptables $*"; return 0; fi
    iptables "$@" 2>/dev/null
}
ipt_raw() {
    if [ "$DRYRUN" = "1" ]; then echo "iptables -t raw $*"; return 0; fi
    iptables -t raw "$@" 2>/dev/null
}
ipt_mangle() {
    if [ "$DRYRUN" = "1" ]; then echo "iptables -t mangle $*"; return 0; fi
    iptables -t mangle "$@" 2>/dev/null
}

ensure_dirs() { mkdir -p "$STATE_DIR" "$RUN_DIR" 2>/dev/null || true; }

# ── state helpers ───────────────────────────────────────────────────────────

# state.json keys: mode(on|off) source(auto|manual|none) since(epoch)
#                  reason watchdog(on|off)
read_state() {
    MODE=off; SOURCE=none; SINCE=0; REASON=""; WATCHDOG=on
    [ -r "$STATE_FILE" ] || return 0
    MODE=$(grep -oE '"mode"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE_FILE" | grep -oE '[^"]*"$' | tr -d '"')
    SOURCE=$(grep -oE '"source"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE_FILE" | grep -oE '[^"]*"$' | tr -d '"')
    SINCE=$(grep -oE '"since"[[:space:]]*:[[:space:]]*[0-9]+' "$STATE_FILE" | grep -oE '[0-9]+$')
    REASON=$(grep -oE '"reason"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE_FILE" | grep -oE '[^"]*"$' | tr -d '"')
    WATCHDOG=$(grep -oE '"watchdog"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE_FILE" | grep -oE '[^"]*"$' | tr -d '"')
    [ -n "$MODE" ] || MODE=off
    [ -n "$SOURCE" ] || SOURCE=none
    [ -n "$SINCE" ] || SINCE=0
    [ -n "$WATCHDOG" ] || WATCHDOG=on
}

write_state() {
    local mode=$1 source=$2 since=$3 reason=$4 watchdog=$5
    ensure_dirs
    reason=$(printf '%s' "$reason" | tr -d '"\\')
    local tmp="$STATE_FILE.tmp"
    printf '{"mode":"%s","source":"%s","since":%s,"reason":"%s","watchdog":"%s"}\n' \
        "$mode" "$source" "${since:-0}" "$reason" "$watchdog" > "$tmp" 2>/dev/null \
        && mv "$tmp" "$STATE_FILE" 2>/dev/null || true
}

now() { date +%s; }

# ── client-port detection ───────────────────────────────────────────────────

# SSH port isn't always 22 — detect it from config + socket activation + live
# listeners, so a node on a custom SSH port never gets rate-limited or locked out.
detect_ssh_ports() {
    local ports=""
    # sshd_config Port directives
    ports="$ports $(grep -rhiE '^[[:space:]]*Port[[:space:]]+[0-9]+' \
        /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk '{print $2}')"
    # systemd socket activation (Ubuntu 24 default): ListenStream=[addr:]port
    ports="$ports $(grep -rhiE '^[[:space:]]*ListenStream=' \
        /lib/systemd/system/ssh.socket /etc/systemd/system/ssh.socket.d/*.conf 2>/dev/null \
        | grep -oE '[0-9]+[[:space:]]*$' )"
    # live sshd listeners (catches anything the above missed)
    ports="$ports $(ss -H -tlnp 2>/dev/null | awk '/sshd/{print $4}' | sed -E 's/.*:([0-9]+)$/\1/')"

    ports=$(printf '%s\n' $ports | grep -E '^[0-9]+$' | sort -un)
    [ -z "$ports" ] && ports=22
    echo $ports
}

# The node's mTLS-nginx may listen on a custom port (NODE_API_PORT in the
# node's .env) — it has to stay out of the rate-limited client ports, or
# emergency mode would throttle the panel on such nodes.
detect_node_api_port() {
    grep -s '^NODE_API_PORT=' /opt/monitoring-node/.env 2>/dev/null \
        | head -1 | cut -d= -f2 | tr -dc '0-9'
}

node_api_ports() {
    printf '%s\n' "$NODE_API_PORT_DEFAULT" $(detect_node_api_port) | grep -E '^[0-9]+$' | sort -un
}

# Node API ports + agent port + auto-detected SSH port(s) — never rate-limited
# and never SYNPROXYed. That is not the same as ACCEPT: see build_chain.
effective_never_drop() {
    printf '%s\n' $(node_api_ports) "$AGENT_LOOPBACK_PORT" $(detect_ssh_ports) | grep -E '^[0-9]+$' | sort -un
}

# Listening TCP ports on non-loopback addresses, minus the never-drop set.
detect_client_ports() {
    local exclude
    exclude=$(effective_never_drop)
    ss -H -tln 2>/dev/null | awk '
        {
            addr=$4
            n=split(addr, a, ":")
            port=a[n]
            local=substr(addr, 1, length(addr)-length(port)-1)
            if (local == "127.0.0.1" || local == "[::1]" || local == "::1") next
            if (port ~ /^[0-9]+$/) print port
        }' | sort -un | grep -vxF "$exclude" 2>/dev/null
}

ports_csv() {
    detect_client_ports | paste -sd, - 2>/dev/null
}

# Опубликованные порты контейнеров из числа клиентских: на хосте их слушает
# docker-proxy, а сами соединения уходят в контейнер через DNAT.
detect_docker_ports() {
    ss -H -tlnp 2>/dev/null | awk '/docker-proxy/ {
            n = split($4, a, ":")
            if (a[n] ~ /^[0-9]+$/) print a[n]
        }' | sort -un | grep -xF -f <(detect_client_ports)
}

# ── ipset allowlist ─────────────────────────────────────────────────────────

ensure_allow_set() {
    ipset list "$ALLOW_SET" >/dev/null 2>&1 && return 0
    ipset create "$ALLOW_SET" hash:net family inet hashsize 4096 maxelem 1000000 2>/dev/null || true
}

# Entries on stdin → `add` lines for one `ipset restore`. The whitelist grows
# with the fleet (~10k entries at 2000 nodes), and an `ipset add` process per
# entry took longer than the 30 s the node's nginx gives the sync request.
# Entries hash:net cannot hold (prefix outside 1..32, octet above 255) are left
# out: restore stops at the first rejected line, where a per-entry add skipped
# only that one. So are entries with a leading zero in an octet or the prefix:
# ipset reads those as octal — it rejects `08` and takes `010` for 8.
allow_add_lines() {
    awk -v set_name="$1" -F'[./]' '
        (NF == 4 || NF == 5 && $5 >= 1 && $5 <= 32) &&
        $1 <= 255 && $2 <= 255 && $3 <= 255 && $4 <= 255 &&
        $0 !~ "(^|[./])0[0-9]" {
            print "add", set_name, $0, "-exist"
        }'
}

restore_whitelist() {
    ensure_allow_set
    [ -r "$WHITELIST_FILE" ] || return 0
    grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?' "$WHITELIST_FILE" 2>/dev/null \
        | allow_add_lines "$ALLOW_SET" | ipset restore \
        || log "whitelist restore failed — $ALLOW_SET may be incomplete until the next sync"
}

# whitelist-sync: replace the allow set from newline/comma-separated IPs on stdin
whitelist_sync() {
    ensure_allow_set
    local incoming tmp_new
    incoming=$(tr ', ' '\n' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?' | sort -u)
    tmp_new=$(printf '%s\n' "$incoming")

    local swap="${ALLOW_SET}_swap"
    ipset destroy "$swap" 2>/dev/null || true
    # The new set goes live only if it loaded completely: a half-built one would
    # put whitelisted relays and the panel back under the emergency limits.
    {
        echo "create $swap hash:net family inet hashsize 4096 maxelem 1000000 -exist"
        echo "flush $swap"
        printf '%s\n' "$tmp_new" | allow_add_lines "$swap"
    } | ipset restore || {
        ipset destroy "$swap" 2>/dev/null || true
        log "whitelist-sync: ipset restore failed — $ALLOW_SET left unchanged"
        return 1
    }
    ipset swap "$swap" "$ALLOW_SET" 2>/dev/null || true
    ipset destroy "$swap" 2>/dev/null || true

    ensure_dirs
    {
        printf '['
        printf '%s\n' "$tmp_new" | awk 'NF{ if(c++) printf ","; printf "\"%s\"", $0 }'
        printf ']\n'
    } > "$WHITELIST_FILE.tmp" 2>/dev/null && mv "$WHITELIST_FILE.tmp" "$WHITELIST_FILE" 2>/dev/null || true

    printf '%s\n' "$tmp_new" | grep -c . 2>/dev/null || echo 0
}

# ── SYNPROXY (best-effort; skipped if kernel lacks the module) ───────────────

synproxy_available() {
    modprobe nf_synproxy_core 2>/dev/null || true
    modprobe xt_SYNPROXY     2>/dev/null || true
    iptables -t raw -nL >/dev/null 2>&1 || { log "raw table unavailable"; return 1; }

    # The old check stopped here — it only proved the raw table exists, never
    # that the SYNPROXY target does. On a kernel without xt_SYNPROXY the
    # --notrack raw rules were still installed while `-j SYNPROXY` failed
    # silently (ipt swallows stderr), which is a total blackhole of new
    # connections with no log line. Probe BOTH halves, in a throwaway chain so a
    # failed probe cannot leave a live rule behind.
    local probe="ANTIDDOS_PROBE" ok=1

    iptables -N "$probe" >/dev/null 2>&1
    iptables -F "$probe" >/dev/null 2>&1
    iptables -A "$probe" -p tcp --dport 65000 -m conntrack --ctstate INVALID,UNTRACKED \
        -j SYNPROXY --sack-perm --timestamp --wscale 7 --mss 1460 >/dev/null 2>&1 || ok=0
    iptables -F "$probe" >/dev/null 2>&1
    iptables -X "$probe" >/dev/null 2>&1
    if [ "$ok" != 1 ]; then
        log "SYNPROXY target unavailable — emergency mode will run hashlimit-only"
        return 1
    fi

    iptables -t raw -N "$probe" >/dev/null 2>&1
    iptables -t raw -F "$probe" >/dev/null 2>&1
    iptables -t raw -A "$probe" -p tcp --dport 65000 --syn -j CT --notrack >/dev/null 2>&1 || ok=0
    iptables -t raw -F "$probe" >/dev/null 2>&1
    iptables -t raw -X "$probe" >/dev/null 2>&1
    if [ "$ok" != 1 ]; then
        log "CT --notrack unavailable — emergency mode will run hashlimit-only"
        return 1
    fi

    # SYNPROXY encodes wscale/MSS/SACK in the TCP timestamp option.
    if [ "$(sysctl -n net.ipv4.tcp_timestamps 2>/dev/null || echo 0)" != "1" ]; then
        log "net.ipv4.tcp_timestamps=0 — SYNPROXY cannot carry wscale/MSS; running hashlimit-only"
        return 1
    fi
    return 0
}

# ── emergency ruleset ───────────────────────────────────────────────────────

# iptables -m multiport takes at most 15 ports per rule, and a busy Xray node
# listens on far more, so every port-scoped rule is emitted per 15-port group.
chunk_ports() {
    awk 'NF{ buf=(c?buf","$0:$0); c++; if(c==15){print buf; buf=""; c=0} } END{ if(c) print buf }'
}

port_chunks() { detect_client_ports | chunk_ports; }

build_chain() {
    local ports; ports=$(ports_csv)

    ipt -N "$CHAIN"           # create (ignore "exists")
    ipt -F "$CHAIN"           # rebuild from scratch (ports may have changed)
    ipt_mangle -N "$HANDSHAKE_CHAIN"
    ipt_mangle -F "$HANDSHAKE_CHAIN"
    ipt_raw -N "$TRUSTED_CHAIN"
    ipt_raw -F "$TRUSTED_CHAIN"

    # 1. operator blocklist, if the set exists (populated via ipset_manager)
    if ipset list "$TEMP_BLOCK_SET" >/dev/null 2>&1; then
        ipt -A "$CHAIN" -m set --match-set "$TEMP_BLOCK_SET" src -j DROP
    fi

    # 2. whitelist — relays/CDN/panel bypass every limit below
    ensure_allow_set
    ipt -A "$CHAIN" -m set --match-set "$ALLOW_SET" src -j ACCEPT

    # 3. keep established traffic flowing untouched (~99% of packets exit here)
    ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # 4. never touch SSH (auto-detected port) — BEFORE any DROP. The node API
    #    ports get no ACCEPT of their own: the panel gets in through the
    #    whitelist above, everyone else falls through to INPUT/UFW untouched
    #    (these ports are outside the rate-limited client ports). The agent port
    #    is open to loopback only.
    local p
    for p in $(detect_ssh_ports); do
        ipt -A "$CHAIN" -p tcp --dport "$p" -j ACCEPT
    done
    ipt -A "$CHAIN" -s "$LOOPBACK_NET" -p tcp --dport "$AGENT_LOOPBACK_PORT" -j ACCEPT

    [ -z "$ports" ] && return 0

    local synproxy_ok=0
    synproxy_available && synproxy_ok=1

    # 5. SYNPROXY FIRST — this ordering is the whole point of the rewrite.
    #    Previously the INVALID drop sat here instead. With SYNs untracked in the
    #    raw table, the client's handshake-completing ACK IS tracked, finds no
    #    conntrack entry, is therefore INVALID, and died on that rule before ever
    #    reaching SYNPROXY. Under nf_conntrack_tcp_loose=0 that is a total
    #    blackhole of new connections for as long as emergency mode is on.
    #    netfilter's documented order is SYNPROXY, then drop what is still
    #    invalid.
    local chunk synproxy_applied=1
    if [ "$synproxy_ok" = "1" ]; then
        while read -r chunk; do
            [ -z "$chunk" ] && continue
            ipt -A "$CHAIN" -p tcp -m multiport --dports "$chunk" \
                -m conntrack --ctstate INVALID,UNTRACKED \
                -j SYNPROXY --sack-perm --timestamp \
                --wscale "$SYNPROXY_WSCALE" --mss "$SYNPROXY_MSS" || synproxy_applied=0
        done < <(port_chunks)
        if [ "$synproxy_applied" != "1" ]; then
            log "a SYNPROXY rule was rejected — falling back to hashlimit-only"
            synproxy_ok=0
        fi
    fi

    # 6. only NOW drop what is still invalid
    ipt -A "$CHAIN" -p tcp -m conntrack --ctstate INVALID -j DROP
    # A non-SYN packet claiming to open a connection is an ACK flood; pairs with
    # nf_conntrack_tcp_loose=0.
    ipt -A "$CHAIN" -p tcp ! --syn -m conntrack --ctstate NEW -j DROP

    # 7. per-source new-connection rate limit.
    #    ONE shared hashlimit table across all chunks. The old code used a unique
    #    --hashlimit-name per chunk, which multiplied the effective limit by the
    #    number of chunks (a 60-port node got 4x NEWRATE) and burned 4x the
    #    htable memory. Every rule must carry identical htable params — the first
    #    creates the table and a later conflicting one is rejected.
    #    connlimit is gone entirely: xt_connlimit walks the source's conntrack
    #    bucket on every NEW packet, so it gets expensive exactly when it must be
    #    cheap, and --connlimit-above 100 per /32 punishes CGNAT egress IPs and
    #    every non-Mux client. hashlimit bounds the rate; conntrack timeouts
    #    bound the standing total.
    while read -r chunk; do
        [ -z "$chunk" ] && continue
        ipt -A "$CHAIN" -p tcp -m multiport --dports "$chunk" -m conntrack --ctstate NEW \
            -m hashlimit --hashlimit-above "${NEWRATE}/sec" --hashlimit-burst "$NEWBURST" \
            --hashlimit-mode srcip --hashlimit-name ad_emg \
            --hashlimit-srcmask "$HASHLIMIT_SRCMASK" \
            --hashlimit-htable-max "$HL_MAX" --hashlimit-htable-expire 60000 -j DROP
    done < <(port_chunks)

    # 8. raw --notrack LAST, and only when SYNPROXY is genuinely working.
    #    Installing --notrack without a working SYNPROXY is itself the blackhole,
    #    so the order is inverted relative to the old code and gated on success.
    if [ "$synproxy_ok" = "1" ]; then
        for p in $(detect_client_ports); do
            ipt_raw -C PREROUTING -p tcp --dport "$p" --syn -j CT --notrack \
                || ipt_raw -A PREROUTING -p tcp --dport "$p" --syn -j CT --notrack || true
        done
        build_synproxy_helpers
    else
        teardown_synproxy_raw
    fi
}

build_synproxy_helpers() {
    build_handshake_rules
    build_trusted_rules
}

teardown_synproxy_raw() {
    [ "$DRYRUN" = "1" ] && return 0
    local p rule
    for p in $(detect_client_ports); do
        iptables -t raw -D PREROUTING -p tcp --dport "$p" --syn -j CT --notrack 2>/dev/null || true
    done
    # И всё, что осталось от портов, переставших слушать за время режима: в
    # detect_client_ports такой порт уже не попадает, а его notrack без SYNPROXY —
    # потеря всех новых соединений, когда порт откроется снова.
    while read -r _ rule; do
        [ -n "$rule" ] && iptables -t raw -D $rule 2>/dev/null
    done < <(iptables -t raw -S PREROUTING 2>/dev/null \
        | grep -- '--tcp-flags FIN,SYN,RST,ACK SYN -j CT --notrack$')
    return 0
}

# notrack для SYN ломает DNAT: без записи conntrack пакет не переадресуется и
# попадает не в контейнер, а к docker-proxy на хосте. Тот соединение обслужит,
# но контейнер увидит адрес моста вместо адреса клиента. Чужие адреса от этого
# спасает сам SYNPROXY — его SYN к серверу идёт через OUTPUT, где Docker делает
# DNAT ещё раз. Адреса из белого списка в SYNPROXY не заходят, поэтому их SYN к
# портам контейнеров выходит из raw раньше notrack и идёт обычным путём.
# Только для портов контейнеров: на портах хоста белый SYN без записи conntrack
# дешевле — флуд с подставными адресами из белого списка не занимает таблицу.
build_trusted_rules() {
    local chunk
    while read -r chunk; do
        [ -z "$chunk" ] && continue
        ipt_raw -A "$TRUSTED_CHAIN" -p tcp -m tcp --syn -m multiport --dports "$chunk" \
            -m set --match-set "$ALLOW_SET" src -j ACCEPT
    done < <(detect_docker_ports | chunk_ports)
}

# SYNPROXY завершает рукопожатие с сервером собственным ACK, и его же хук
# пересчитывает в этом ACK поле TSecr второй раз. Ядра с проверкой TSecr по
# RFC 7323 (в /proc/net/netstat у них есть счётчик TSEcrRejected) такой ACK
# отбрасывают, сервер ждёт повторный SYN-ACK — и каждое новое соединение
# открывается на секунду дольше. Опция timestamp снимается только с этого
# пакета: в OUTPUT он единственный идёт с чужим адресом источника на адрес
# самого хоста. Остальные пакеты соединения несут timestamp как обычно.
build_handshake_rules() {
    local chunk
    while read -r chunk; do
        [ -z "$chunk" ] && continue
        if ! ipt_mangle -A "$HANDSHAKE_CHAIN" -p tcp -m tcp --tcp-flags SYN,ACK,FIN,RST,PSH ACK \
                -m multiport --dports "$chunk" \
                -m addrtype ! --src-type LOCAL --dst-type LOCAL \
                -j TCPOPTSTRIP --strip-options timestamp; then
            log "TCPOPTSTRIP unavailable — where the kernel validates TSecr a SYNPROXY handshake takes an extra second"
            ipt_mangle -F "$HANDSHAKE_CHAIN"
            # Непустая цепочка: selfheal не повторяет попытку каждый цикл
            ipt_mangle -A "$HANDSHAKE_CHAIN" -j RETURN
            return 0
        fi
    done < <(port_chunks)
}

# Транзит — соединения, пришедшие через DNAT: опубликованные порты контейнеров
# Docker и проброс портов. Они идут PREROUTING → FORWARD и цепочку в INPUT не
# проходят, поэтому лимит новых соединений для них стоит в mangle/POSTROUTING:
#   - там проходит и SYN, которым SYNPROXY открывает соединение с контейнером
#     (он рождается на хосте и идёт через OUTPUT, минуя FORWARD);
#   - адрес источника там ещё настоящий — MASQUERADE стоит позже, в nat;
#   - mangle не трогают ни `ufw reset`, ни Docker, ни ACCEPT'ы в filter/FORWARD.
# Через POSTROUTING идёт и весь исходящий трафик самого хоста, поэтому первые
# два правила — выходы: установленные соединения и всё, что пришло не через
# DNAT. Пакеты INVALID цепочка не трогает: таким conntrack видит SYN-ACK,
# который SYNPROXY шлёт клиенту.
# Лимит не действует на сам хост и на приватные сети: оттуда приходят свои
# контейнеры, а не клиенты, и обратный прокси в контейнере открывает к соседу
# больше NEWRATE соединений в секунду как раз под нагрузкой.
build_transit_chain() {
    local ok=1 net

    ipt_mangle -N "$TRANSIT_CHAIN"
    ipt_mangle -F "$TRANSIT_CHAIN"

    ipt_mangle -A "$TRANSIT_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN || ok=0
    ipt_mangle -A "$TRANSIT_CHAIN" -m conntrack ! --ctstate DNAT -j RETURN || ok=0

    if ipset list "$TEMP_BLOCK_SET" >/dev/null 2>&1; then
        ipt_mangle -A "$TRANSIT_CHAIN" -m set --match-set "$TEMP_BLOCK_SET" src -j DROP || ok=0
    fi
    ensure_allow_set
    ipt_mangle -A "$TRANSIT_CHAIN" -m set --match-set "$ALLOW_SET" src -j RETURN || ok=0

    ipt_mangle -A "$TRANSIT_CHAIN" -m addrtype --src-type LOCAL -j RETURN || ok=0
    for net in $INTERNAL_NETS; do
        ipt_mangle -A "$TRANSIT_CHAIN" -s "$net" -j RETURN || ok=0
    done

    ipt_mangle -A "$TRANSIT_CHAIN" -p tcp ! --syn -m conntrack --ctstate NEW -j DROP || ok=0
    ipt_mangle -A "$TRANSIT_CHAIN" -p tcp -m conntrack --ctstate NEW \
        -m hashlimit --hashlimit-above "${NEWRATE}/sec" --hashlimit-burst "$NEWBURST" \
        --hashlimit-mode srcip --hashlimit-name "$TRANSIT_HASHLIMIT" \
        --hashlimit-srcmask "$HASHLIMIT_SRCMASK" \
        --hashlimit-htable-max "$HL_MAX" --hashlimit-htable-expire 60000 -j DROP || ok=0

    # Цепочка без одного из выходов резала бы чужой трафик — лучше никакой.
    # Остаётся один RETURN: по непустой цепочке selfheal не повторяет попытку
    # каждый цикл, а self-test видит, что лимита в ней нет.
    if [ "$ok" != "1" ]; then
        ipt_mangle -F "$TRANSIT_CHAIN"
        ipt_mangle -A "$TRANSIT_CHAIN" -j RETURN
        log "transit chain rejected — container ports and port forwarding stay without the rate limit"
    fi
}

chain_has_rules() { iptables -t "$1" -S "$2" 2>/dev/null | grep -q '^-A '; }

jump_present() { iptables -t "$1" -C "$2" -j "$3" >/dev/null 2>&1; }

add_jump() {
    local jump table hook chain
    for jump in $EMERGENCY_JUMPS; do
        IFS=: read -r table hook chain <<< "$jump"
        jump_present "$table" "$hook" "$chain" \
            || iptables -t "$table" -I "$hook" 1 -j "$chain" 2>/dev/null
    done
}

remove_jump() {
    local jump table hook chain
    for jump in $EMERGENCY_JUMPS; do
        IFS=: read -r table hook chain <<< "$jump"
        while jump_present "$table" "$hook" "$chain"; do
            iptables -t "$table" -D "$hook" -j "$chain" 2>/dev/null || break
        done
    done
}

apply_rules() {
    build_chain
    build_transit_chain
    add_jump
}

clear_rules() {
    remove_jump
    teardown_synproxy_raw
    ipt -F "$CHAIN"
    ipt_mangle -F "$TRANSIT_CHAIN"
    ipt_mangle -F "$HANDSHAKE_CHAIN"
    ipt_raw -F "$TRUSTED_CHAIN"
}

# Emergency mode inserts a chain at INPUT position 1. If a rule bug there ever
# cuts the box off, nobody can log in to fix it — so the loop proves the node is
# still reachable on its own API and tears an auto-triggered chain down after
# SELF_CONFIRM_FAILS consecutive failures (safety_net_cleared). Cheap, no new
# units, self-healing.
#
# Печатает, чем подтверждена связь; код 1 — ничем. Сторож панель берёт с GitHub
# своего канала, а версия ноды может быть любой: агент текущей ноды слушает
# сокет, старой — порт на петле.
reachable_via() {
    if curl -sf --max-time 3 --unix-socket "$AGENT_SOCKET" http://localhost/health >/dev/null 2>&1; then
        echo "agent via unix socket"
        return 0
    fi
    if curl -sf --max-time 3 "http://127.0.0.1:$AGENT_LOOPBACK_PORT/health" >/dev/null 2>&1; then
        echo "agent via 127.0.0.1:$AGENT_LOOPBACK_PORT (node of an older version)"
        return 0
    fi
    # Fall back to "is any SSH session still established" — on a node where the
    # API is legitimately down we must not tear down protection for that alone.
    # Без -H ss печатает заголовок и при пустом списке соединений.
    local p
    for p in $(detect_ssh_ports); do
        if ss -Htn state established "( sport = :$p )" 2>/dev/null | grep -q .; then
            echo "live SSH session only (agent /health unreachable)"
            return 0
        fi
    done
    return 1
}

self_confirm_ok() { reachable_via >/dev/null; }

# Страховка, раз в цикл; код 0 — цепочка снята. Ручной пин не снимается: его
# ставит и снимает админ, SSH цепочка не режет никогда, а агент молчит ~30 с и
# при обычном обновлении ноды или рестарте docker — снимать из-за этого защиту
# посреди атаки нельзя. Автоматический режим детект включит снова сам.
safety_net_cleared() {
    if [ "$MODE" != "on" ] || self_confirm_ok; then
        save_prev confirm_fails 0
        return 1
    fi
    local cf; cf=$(read_prev confirm_fails)
    cf=$(( ${cf:-0} + 1 ))
    save_prev confirm_fails "$cf"
    [ "$cf" -ge "$SELF_CONFIRM_FAILS" ] 2>/dev/null || return 1
    if [ "$SOURCE" = "manual" ]; then
        # Одна запись на весь простой, а не на каждый цикл
        [ "$cf" -eq "$SELF_CONFIRM_FAILS" ] \
            && log "node unreachable for $cf cycles — manual pin kept, only the operator lifts it"
        return 1
    fi
    log "node unreachable for $cf cycles with emergency mode on — clearing rules"
    disable_mode
    save_prev confirm_fails 0
    return 0
}

# self-heal: while active, guarantee the chain+jump survive a foreign ufw reset
# Цепочка «на месте», когда в ней есть правила: сторонний `iptables -F` оставляет
# пустую цепочку с тем же именем, и по одному её существованию режим считался бы
# целым, хотя не делает ничего.
selfheal() {
    chain_has_rules filter "$CHAIN" || build_chain
    chain_has_rules mangle "$TRANSIT_CHAIN" || build_transit_chain
    # Режим мог включить сторож прежней версии: правила SYNPROXY стоят, а
    # вспомогательных цепочек к ним ещё нет
    if ! chain_has_rules mangle "$HANDSHAKE_CHAIN" && iptables -S "$CHAIN" 2>/dev/null | grep -q SYNPROXY; then
        ipt_mangle -N "$HANDSHAKE_CHAIN"
        ipt_raw -N "$TRUSTED_CHAIN"
        ipt_raw -F "$TRUSTED_CHAIN"
        build_synproxy_helpers
    fi
    add_jump
}

# ── mode transitions ────────────────────────────────────────────────────────

enable_mode() {
    local source=$1 reason=$2
    apply_rules
    write_state on "$source" "$(now)" "$reason" "$WATCHDOG"
    log "emergency ON ($source): $reason"
}

disable_mode() {
    clear_rules
    write_state off none 0 "" "$WATCHDOG"
    log "emergency OFF"
}

# ── DDoS signal sampling (loop only) ────────────────────────────────────────

read_prev() { cat "$RUN_DIR/$1" 2>/dev/null || echo 0; }
save_prev() { echo "$2" > "$RUN_DIR/$1" 2>/dev/null || true; }

# Sum the conntrack insert_failed counter across CPUs — the real "table full,
# dropping packets" signal. /proc/net/stat/nf_conntrack has a named header then
# per-CPU hex rows; parse in bash (mawk lacks strtonum).
read_insert_failed() {
    local f="/proc/net/stat/nf_conntrack" col total=0 h
    [ -r "$f" ] || { echo 0; return 0; }
    col=$(awk 'NR==1{for(i=1;i<=NF;i++) if($i=="insert_failed"){print i; exit}}' "$f")
    [ -n "$col" ] || { echo 0; return 0; }
    while read -r h; do
        [[ "$h" =~ ^[0-9a-fA-F]+$ ]] && total=$(( total + 0x$h ))
    done < <(awk -v c="$col" 'NR>1{print $c}' "$f")
    echo "$total"
}

# Пакеты, выброшенные с per-CPU очереди backlog, и отдельно — счётчик flow_limit.
# /proc/net/softnet_stat — по hex-строке на CPU; колонка 2 — dropped,
# колонка 11 — flow_limit_count. Печатает «дропы flow_limit» одной строкой.
#
# Разделять их обязательно. flow_limit — наш собственный ограничитель, он
# включается вместе с RPS и режет поток, занявший больше половины истории
# очереди; его срабатывания ядро тоже считает в dropped. На одноочередной карте
# один быстрый клиент даёт сотни таких дропов за цикл при совершенно здоровой
# ноде, и без вычитания это неотличимо от флуда.
read_softnet_counters() {
    local f="/proc/net/softnet_stat" drops=0 flow=0 d fl
    [ -r "$f" ] || { echo "0 0"; return 0; }
    while read -r d fl; do
        [[ "$d" =~ ^[0-9a-fA-F]+$ ]] && drops=$(( drops + 0x$d ))
        [[ "$fl" =~ ^[0-9a-fA-F]+$ ]] && flow=$(( flow + 0x$fl ))
    done < <(awk '{ f = "0"; if (NF >= 11) f = $11; print $2, f }' "$f")
    echo "$drops $flow"
}

# Переполнение очереди accept — единственный специфичный признак того, что
# приложение не успевает принимать соединения.
#
# Раньше здесь суммировались ListenOverflows и ListenDrops, и это оказалось
# ошибкой: ListenDrops растёт и при штатной смене слушающих сокетов (перезапуск
# Xray, переконфигурация). На живой ноде это давало фон в сотни за цикл при
# НУЛЕВЫХ ListenOverflows — и вотчдог поднимал аварийный режим на ровном месте.
# ListenOverflows инкрементируется строго при полной очереди accept.
# Пакеты, прошедшие транзитом (FORWARD), а не к локальным сокетам: на DNAT-релее
# это почти весь rx, и считать их флудом нельзя — они и есть полезная нагрузка.
read_forwarded() {
    local f="/proc/net/snmp" n
    [ -r "$f" ] || { echo 0; return 0; }
    n=$(awk '/^Ip:/ {
            if (!hdr) { for (i = 1; i <= NF; i++) if ($i == "ForwDatagrams") col = i; hdr = 1 }
            else if (col) { print $col + 0; exit }
        }' "$f" 2>/dev/null)
    [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo 0
}


read_listen_overflows() {
    local f="/proc/net/netstat" n
    [ -r "$f" ] || { echo 0; return 0; }
    n=$(awk '/^TcpExt:/ {
            if (!hdr) { for (i = 1; i <= NF; i++) if ($i == "ListenOverflows") col = i; hdr = 1 }
            else if (col) { print $col + 0; exit }
        }' "$f" 2>/dev/null)
    [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo 0
}


# Busiest single CPU in softirq. On a multi-queue NIC one pegged core is the
# early symptom long before the aggregate moves, and the aggregate is the only
# thing the old code looked at.
read_percpu_softirq() {
    awk '/^cpu[0-9]+ /{ si=$8; tot=0; for(i=2;i<=NF;i++) tot+=$i; print $1, si, tot }' \
        /proc/stat 2>/dev/null
}

max_percpu_softirq_pct() {
    local prev_file="$RUN_DIR/percpu" cur prev
    cur=$(read_percpu_softirq)
    [ -n "$cur" ] || { echo 0; return 0; }
    prev=$(cat "$prev_file" 2>/dev/null)
    printf '%s\n' "$cur" > "$prev_file" 2>/dev/null || true
    [ -n "$prev" ] || { echo 0; return 0; }

    printf '%s\n===\n%s\n' "$prev" "$cur" | awk '
        /^===$/ { second = 1; next }
        !second { psi[$1] = $2; ptot[$1] = $3; next }
        {
            dsi = $2 - psi[$1]; dtot = $3 - ptot[$1]
            if (dtot > 0 && dsi >= 0) { p = int(dsi * 100 / dtot); if (p > max) max = p }
        }
        END { print max + 0 }'
}

# sets globals: SIG_STRONG SIG_WEAK SIG_REASON
sample_signals() {
    SIG_STRONG=0; SIG_WEAK=0; SIG_REASON=""

    # conntrack: the real attack signal is inserts actually FAILING (table full,
    # dropping packets) — not a high fill on a merely-busy node. Use the delta of
    # insert_failed; a near-full table is only a weak corroborating hint.
    local cur_if prev_if dif
    cur_if=$(read_insert_failed)
    prev_if=$(read_prev insertfailed)
    save_prev insertfailed "$cur_if"
    dif=$(( cur_if - prev_if ))
    [ "$dif" -lt 0 ] && dif=0
    if [ "$dif" -ge "$CONNTRACK_DROP_DELTA" ] 2>/dev/null; then
        SIG_STRONG=1; SIG_REASON="conntrack dropping (+${dif}/cycle)"
    fi

    local ct_count ct_max fill=0
    ct_count=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
    ct_max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)
    if [ "${ct_max:-0}" -gt 0 ] 2>/dev/null; then
        fill=$(( ct_count * 100 / ct_max ))
    fi
    if [ "$fill" -ge "$CONNTRACK_PCT" ] 2>/dev/null; then
        SIG_WEAK=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }conntrack ${fill}%"
    fi

    # SyncookiesSent delta (active SYN flood). /proc/net/netstat has a TcpExt
    # header row (names) followed by a values row — find the column in the
    # header, read that field from the values row.
    local cur_sc prev_sc dsc
    cur_sc=$(awk '/^TcpExt:/ { if (!hdr) { for(i=1;i<=NF;i++) if($i=="SyncookiesSent") col=i; hdr=1 } else if (col) { print $col+0; exit } }' /proc/net/netstat 2>/dev/null)
    [ -n "$cur_sc" ] || cur_sc=0
    prev_sc=$(read_prev syncookies)
    save_prev syncookies "$cur_sc"
    dsc=$(( cur_sc - prev_sc ))
    [ "$dsc" -lt 0 ] && dsc=0
    if [ "$dsc" -ge "$SYNCOOKIE_DELTA" ] 2>/dev/null; then
        SIG_STRONG=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }syncookies +${dsc}/cycle"
    fi

    # pps + avg packet size (flood of tiny packets). /proc/net/dev rows are
    # "iface: rx_bytes rx_packets ..." — split on ':' then whitespace so leading
    # indentation doesn't shift columns. Из pps вычитаются пакеты FORWARD
    # (ForwDatagrams из /proc/net/snmp): транзит DNAT-релея — сотни тысяч мелких
    # ACK/SYN клиентов с одного интерфейса — иначе стабильно выглядит как флуд,
    # хотя это обычная работа релея, и режим включался бы на ровном месте.
    local cur_pkts cur_bytes prev_pkts prev_bytes dpkts dbytes pps avg
    local cur_fwd prev_fwd dfwd local_pkts
    read cur_pkts cur_bytes < <(awk -F: '
        NR>2 {
            iface=$1; gsub(/^ +/, "", iface)
            if (iface=="lo" || iface ~ /^(docker|veth|br-|virbr|tap|tun)/) next
            split($2, f, " ")
            bytes+=f[1]; pkts+=f[2]
        }
        END { print pkts+0, bytes+0 }' /proc/net/dev 2>/dev/null)
    [ -n "$cur_pkts" ] || cur_pkts=0
    [ -n "$cur_bytes" ] || cur_bytes=0
    prev_pkts=$(read_prev rxpkts); prev_bytes=$(read_prev rxbytes)
    save_prev rxpkts "$cur_pkts"; save_prev rxbytes "$cur_bytes"
    dpkts=$(( cur_pkts - prev_pkts )); dbytes=$(( cur_bytes - prev_bytes ))
    [ "$dpkts" -lt 0 ] && dpkts=0
    [ "$dbytes" -lt 0 ] && dbytes=0
    cur_fwd=$(read_forwarded); prev_fwd=$(read_prev fwdpkts); save_prev fwdpkts "$cur_fwd"
    dfwd=$(( cur_fwd - prev_fwd )); [ "$dfwd" -lt 0 ] && dfwd=0
    local_pkts=$(( dpkts - dfwd )); [ "$local_pkts" -lt 0 ] && local_pkts=0
    pps=$(( local_pkts / INTERVAL ))
    avg=0; [ "$dpkts" -gt 0 ] && avg=$(( dbytes / dpkts ))
    if [ "$pps" -ge "$PPS_THRESHOLD" ] 2>/dev/null && [ "$avg" -gt 0 ] && [ "$avg" -le "$SMALL_PKT_BYTES" ] 2>/dev/null; then
        SIG_WEAK=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }pps ${pps} local (fwd $(( dfwd / INTERVAL ))), avg ${avg}B"
    fi

    # softnet drops — STRONG, но с двумя оговорками.
    # Во-первых, из дельты вычитается flow_limit: это наше собственное
    # торможение жирного потока, а не потеря трафика от перегрузки.
    # Во-вторых, сигнал обязан продержаться SOFTNET_HOLD_CYCLES циклов подряд —
    # одиночный микровсплеск в пару сотен пакетов на фоне сотен тысяч
    # обработанных за тот же цикл не повод ставить правила в INPUT, а настоящий
    # флуд живёт заведомо дольше одного цикла.
    local cur_sn cur_fl prev_sn prev_fl dsn dfl sn_hits
    read cur_sn cur_fl < <(read_softnet_counters)
    prev_sn=$(read_prev softnet); prev_fl=$(read_prev softnetflow)
    save_prev softnet "$cur_sn"; save_prev softnetflow "$cur_fl"
    dsn=$(( cur_sn - prev_sn )); dfl=$(( cur_fl - prev_fl ))
    [ "$dfl" -lt 0 ] && dfl=0
    dsn=$(( dsn - dfl ))
    [ "$dsn" -lt 0 ] && dsn=0
    if [ "$dsn" -ge "$SOFTNET_DROP_DELTA" ] 2>/dev/null; then
        sn_hits=$(read_prev softnethits)
        sn_hits=$(( ${sn_hits:-0} + 1 ))
        save_prev softnethits "$sn_hits"
        if [ "$sn_hits" -ge "$SOFTNET_HOLD_CYCLES" ] 2>/dev/null; then
            SIG_STRONG=1
            SIG_REASON="${SIG_REASON:+$SIG_REASON, }softnet drops +${dsn}/cycle"
        fi
    else
        save_prev softnethits 0
    fi

    # Переполнение очереди accept — WEAK, а не STRONG: всплеск на один цикл
    # случается и под легальной нагрузкой, поэтому сигнал обязан продержаться
    # WEAK_HOLD секунд, прежде чем ставить правила в INPUT.
    local cur_lo prev_lo dlo
    cur_lo=$(read_listen_overflows)
    prev_lo=$(read_prev listenoverflows)
    save_prev listenoverflows "$cur_lo"
    dlo=$(( cur_lo - prev_lo ))
    [ "$dlo" -lt 0 ] && dlo=0
    if [ "$dlo" -ge "$LISTEN_OVERFLOW_DELTA" ] 2>/dev/null; then
        SIG_WEAK=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }accept overflow +${dlo}/cycle"
    fi


    # busiest single CPU in softirq — weak
    local percpu
    percpu=$(max_percpu_softirq_pct)
    if [ "${percpu:-0}" -ge "$SOFTIRQ_PCT_PERCPU" ] 2>/dev/null; then
        SIG_WEAK=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }cpu softirq ${percpu}%"
    fi

    # softirq CPU %
    local cur_si cur_tot prev_si prev_tot dsi dtot sipct
    read cur_si cur_tot < <(awk '/^cpu /{ si=$8; tot=0; for(i=2;i<=NF;i++) tot+=$i; print si, tot }' /proc/stat 2>/dev/null)
    [ -n "$cur_si" ] || cur_si=0
    [ -n "$cur_tot" ] || cur_tot=0
    prev_si=$(read_prev softirq); prev_tot=$(read_prev cputotal)
    save_prev softirq "$cur_si"; save_prev cputotal "$cur_tot"
    dsi=$(( cur_si - prev_si )); dtot=$(( cur_tot - prev_tot ))
    if [ "$dtot" -gt 0 ] 2>/dev/null; then
        sipct=$(( dsi * 100 / dtot ))
        if [ "$sipct" -ge "$SOFTIRQ_PCT" ] 2>/dev/null; then
            SIG_WEAK=1
            SIG_REASON="${SIG_REASON:+$SIG_REASON, }softirq ${sipct}%"
        fi
    fi
}

# ── loop ────────────────────────────────────────────────────────────────────

run_loop() {
    ensure_dirs
    restore_whitelist
    # warm up counters so the first delta is meaningful, not a cold-start spike
    sample_signals
    [ -f "$STATE_FILE" ] || write_state off none 0 "" on
    sleep "$INTERVAL"

    while true; do
        load_config
        read_state
        sample_signals

        if safety_net_cleared; then
            sleep "$INTERVAL"; continue
        fi

        # manual pin: only self-heal, never auto-toggle
        if [ "$MODE" = "on" ] && [ "$SOURCE" = "manual" ]; then
            selfheal
            sleep "$INTERVAL"; continue
        fi

        # auto-detection off: an auto-triggered emergency must not linger — clear
        # it so "disable watchdog" truly returns the node to normal. Manual pins
        # are already handled above and kept.
        if [ "$WATCHDOG" != "on" ]; then
            [ "$MODE" = "on" ] && disable_mode
            sleep "$INTERVAL"; continue
        fi

        if [ "$SIG_STRONG" = "1" ] || [ "$SIG_WEAK" = "1" ]; then
            save_prev last_active "$(now)"
        fi

        if [ "$MODE" = "off" ]; then
            if [ "$SIG_STRONG" = "1" ]; then
                enable_mode auto "$SIG_REASON"
            elif [ "$SIG_WEAK" = "1" ]; then
                local ws; ws=$(read_prev weak_since)
                if [ "${ws:-0}" -eq 0 ] 2>/dev/null; then
                    save_prev weak_since "$(now)"
                elif [ $(( $(now) - ws )) -ge "$WEAK_HOLD" ] 2>/dev/null; then
                    enable_mode auto "$SIG_REASON"
                    save_prev weak_since 0
                fi
            else
                save_prev weak_since 0
            fi
        elif [ "$MODE" = "on" ] && [ "$SOURCE" = "auto" ]; then
            selfheal
            local la; la=$(read_prev last_active)
            if [ "${la:-0}" -gt 0 ] 2>/dev/null && [ $(( $(now) - la )) -ge "$HYSTERESIS" ] 2>/dev/null; then
                disable_mode
            fi
        fi

        sleep "$INTERVAL"
    done
}

# Proves the never-drop contract on a rule listing (`iptables -S` or dry-run
# output): no ACCEPT for the node API ports, the agent port only from loopback,
# an unconditional ACCEPT for every SSH port.
audit_never_drop() {
    local rules=$1 failed=0 line port p node_ports ssh_ports ssh_open=" "
    node_ports=" $(node_api_ports | tr '\n' ' ') "
    ssh_ports=" $(detect_ssh_ports) "

    while read -r line; do
        [[ $line == *"-j ACCEPT"* && $line =~ --dport[[:space:]]+([0-9]+) ]] || continue
        port=${BASH_REMATCH[1]}
        if [[ $ssh_ports == *" $port "* ]]; then
            [[ $line == *" -s "* ]] || ssh_open+="$port "
        elif [[ $node_ports == *" $port "* ]]; then
            echo "self-test: FAIL  unconditional ACCEPT for node API port $port"
            failed=1
        elif [ "$port" = "$AGENT_LOOPBACK_PORT" ] && [[ "$line " != *" -s $LOOPBACK_NET "* ]]; then
            echo "self-test: FAIL  ACCEPT for port $port not limited to $LOOPBACK_NET"
            failed=1
        fi
    done <<< "$rules"

    for p in $ssh_ports; do
        [[ $ssh_open == *" $p "* ]] && continue
        echo "self-test: FAIL  no unconditional ACCEPT for SSH port $p"
        failed=1
    done
    [ "$failed" = 0 ] && echo "self-test: OK  never-drop: no node API ACCEPT, agent port loopback-only, SSH open"
    return $failed
}

# Транзитная цепочка стоит на пути всего исходящего трафика хоста: до первого
# DROP обязаны стоять оба выхода, а пакеты INVALID она не трогает совсем.
# Правила — вывод dry-run или `iptables -t mangle -S`.
audit_transit() {
    local rules=$1 failed=0 line total=0 drops=0 established_exit=0 dnat_gate=0
    while read -r line; do
        [[ $line == *"-A $TRANSIT_CHAIN "* ]] || continue
        total=$((total + 1))
        [[ $line == *"-j DROP"* ]] && drops=$((drops + 1))
        if [[ $line == *"INVALID"* ]]; then
            echo "self-test: FAIL  transit chain matches INVALID packets — SYNPROXY's own SYN-ACK is one"
            failed=1
        fi
        if [[ $line == *"-j RETURN"* ]]; then
            [[ $line == *"ESTABLISHED"* ]] && established_exit=1
            [[ $line == *"! --ctstate DNAT"* ]] && dnat_gate=1
        elif [[ $line == *"-j DROP"* ]] && [ "$((established_exit + dnat_gate))" -lt 2 ]; then
            echo "self-test: FAIL  transit chain drops before its exits: $line"
            failed=1
        fi
    done <<< "$rules"

    if [ "$total" = 0 ]; then
        echo "self-test: FAIL  transit chain $TRANSIT_CHAIN has no rules"
        return 1
    fi
    if [ "$drops" = 0 ]; then
        echo "self-test: WARN  transit chain was rejected by the kernel — container ports and port forwarding are not rate-limited"
        return $failed
    fi
    [ "$failed" = 0 ] && echo "self-test: OK  transit: established and non-DNAT traffic leave before any DROP, INVALID untouched"
    return $failed
}

# Structural proof that the SYNPROXY ordering fix is actually in place. A
# passing curl proves nothing here — it also passes when SYNPROXY is silently
# absent — so assert on the rule positions and on the raw/filter pairing.
do_self_test() {
    local ok=0 out

    out=$(iptables -nL "$CHAIN" --line-numbers 2>/dev/null)
    if [ -z "$out" ]; then
        echo "self-test: chain $CHAIN not present (emergency mode off) — nothing to check"
        return 0
    fi

    audit_never_drop "$(iptables -S "$CHAIN" 2>/dev/null)" || ok=1

    local transit
    transit=$(iptables -t mangle -S "$TRANSIT_CHAIN" 2>/dev/null | grep -- '^-A ')
    if [ -z "$transit" ]; then
        echo "self-test: WARN  transit chain is empty — container ports and port forwarding are not rate-limited"
    else
        audit_transit "$transit" || ok=1
        if ! jump_present mangle POSTROUTING "$TRANSIT_CHAIN"; then
            echo "self-test: FAIL  transit chain is built but not jumped from mangle POSTROUTING"
            ok=1
        fi
    fi

    local syn_line inv_line
    syn_line=$(printf '%s\n' "$out" | awk '/SYNPROXY/{print $1; exit}')
    inv_line=$(printf '%s\n' "$out" | awk '/state INVALID|ctstate INVALID/ && /DROP/{print $1; exit}')

    if [ -n "$syn_line" ] && [ -n "$inv_line" ]; then
        if [ "$syn_line" -lt "$inv_line" ]; then
            echo "self-test: OK  SYNPROXY at rule $syn_line precedes INVALID DROP at $inv_line"
        else
            echo "self-test: FAIL  SYNPROXY at $syn_line comes AFTER INVALID DROP at $inv_line"
            echo "self-test:       the handshake-completing ACK will be dropped before it reaches SYNPROXY"
            ok=1
        fi
    elif [ -z "$syn_line" ]; then
        echo "self-test: SYNPROXY not installed (hashlimit-only mode)"
    fi

    # raw --notrack must exist if and only if SYNPROXY does: one without the
    # other is the blackhole.
    local raw_count
    # grep -c сам печатает 0, когда совпадений нет
    raw_count=$(iptables -t raw -nL PREROUTING 2>/dev/null | grep -c 'CT notrack')
    if [ -z "$syn_line" ] && [ "${raw_count:-0}" -gt 0 ]; then
        echo "self-test: FAIL  $raw_count raw --notrack rules with no SYNPROXY rule"
        ok=1
    elif [ -n "$syn_line" ] && [ "${raw_count:-0}" -eq 0 ]; then
        echo "self-test: WARN  SYNPROXY present but no raw --notrack rules"
    fi

    if [ "$(sysctl -n net.ipv4.tcp_timestamps 2>/dev/null || echo 0)" != "1" ] \
       && [ -n "$syn_line" ]; then
        echo "self-test: FAIL  SYNPROXY active with tcp_timestamps=0 — wscale/MSS cannot be carried"
        ok=1
    fi

    return $ok
}

# ── CLI ─────────────────────────────────────────────────────────────────────

case "${1:-loop}" in
    loop)            run_loop ;;
    enable-manual)   read_state; WATCHDOG=${WATCHDOG:-on}; enable_mode manual "manual" ;;
    disable-manual)  read_state; disable_mode ;;
    watchdog-on)
        read_state; WATCHDOG=on
        write_state "$MODE" "$SOURCE" "$SINCE" "$REASON" on ;;
    watchdog-off)
        read_state; WATCHDOG=off
        write_state "$MODE" "$SOURCE" "$SINCE" "$REASON" off ;;
    apply)           read_state; apply_rules ;;
    clear)           read_state; clear_rules ;;
    selfheal)        read_state; [ "$MODE" = "on" ] && selfheal ;;
    whitelist-sync)  whitelist_sync ;;
    detect-ports)    ports_csv ;;
    dry-run)
        read_state; DRYRUN=1
        dry_rules=$(build_chain; build_transit_chain)
        printf '%s\n' "$dry_rules"
        echo "iptables -I INPUT 1 -j $CHAIN"
        echo "iptables -t raw -I PREROUTING 1 -j $TRUSTED_CHAIN"
        echo "iptables -t mangle -I POSTROUTING 1 -j $TRANSIT_CHAIN"
        echo "iptables -t mangle -I OUTPUT 1 -j $HANDSHAKE_CHAIN"
        audit_status=0
        audit_never_drop "$dry_rules" || audit_status=1
        audit_transit "$dry_rules" || audit_status=1
        exit "$audit_status" ;;
    audit-rules)
        audit_input=$(cat)
        audit_status=0
        audit_never_drop "$audit_input" || audit_status=1
        if [[ $audit_input == *"-A $TRANSIT_CHAIN "* ]]; then
            audit_transit "$audit_input" || audit_status=1
        fi
        exit "$audit_status" ;;
    self-test)       do_self_test ;;
    self-check)
        if via=$(reachable_via); then
            echo "self-check: OK  $via"
        else
            echo "self-check: FAIL  agent /health unreachable and no live SSH session"
            exit 1
        fi ;;
    version)         echo "$WATCHDOG_VERSION" ;;
    status)
        ensure_dirs; read_state
        r=$(printf '%s' "$REASON" | tr -d '"\\')
        printf '{"mode":"%s","source":"%s","since":%s,"reason":"%s","watchdog":"%s","version":"%s"}\n' \
            "$MODE" "$SOURCE" "${SINCE:-0}" "$r" "$WATCHDOG" "$WATCHDOG_VERSION" ;;
    *)               echo "usage: $0 {loop|enable-manual|disable-manual|watchdog-on|watchdog-off|apply|clear|selfheal|whitelist-sync|detect-ports|dry-run|audit-rules|self-test|self-check|version|status}" >&2; exit 1 ;;
esac
