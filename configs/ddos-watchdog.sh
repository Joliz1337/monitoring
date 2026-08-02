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
# the loop re-adds the jump within one cycle (self-heal). Ports 22 and 9100 are
# never dropped. Client ports are auto-detected (ss), never hardcoded.
#

set -u

# Bumped when the script logic changes — the panel compares this against what a
# node reports and auto-reinstalls on drift, so updates roll out without clicks.
WATCHDOG_VERSION="2.1.0"

STATE_DIR="/opt/monitoring/antiddos"
STATE_FILE="$STATE_DIR/state.json"
WHITELIST_FILE="$STATE_DIR/whitelist.json"
CONFIG_FILE="$STATE_DIR/config"
AUTO_CONFIG_FILE="$STATE_DIR/config.auto"
RUN_DIR="$STATE_DIR/run"

CHAIN="ANTIDDOS"
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
NEVER_DROP_PORTS="9100 7500"  # node API (nginx + uvicorn); SSH port auto-detected

# detection thresholds (conservative — must not fire on a legit evening peak)
CONNTRACK_DROP_DELTA=50     # insert_failed growth/cycle → strong (table full, dropping)
CONNTRACK_PCT=90            # conntrack fill % → weak hint only (near exhaustion)
SYNCOOKIE_DELTA=200         # SyncookiesSent growth per cycle → strong signal (SYN flood)
PPS_THRESHOLD=150000        # rx packets/sec …
SMALL_PKT_BYTES=200         # …combined with avg packet < this → flood-of-tiny (weak)
SOFTIRQ_PCT=50              # aggregate softirq CPU% → weak signal
SOFTIRQ_PCT_PERCPU=90       # busiest single CPU in softirq → weak signal
SOFTNET_DROP_DELTA=200      # /proc/net/softnet_stat drops per cycle → STRONG
LISTEN_OVERFLOW_DELTA=400   # ListenOverflows per cycle → weak, см. read_listen_overflows
WEAK_HOLD=45               # weak signals must persist this long (s) before enabling
HYSTERESIS=900              # auto-disable after this many seconds with no signal

# SYNPROXY parameters. Hardcoded --wscale 7 caps every proxied connection's
# window at 8 MB, and --mss 1460 is wrong on any tunnelled/vRack path; both are
# computed from the host's real rmem_max and MTU by tune-sysctl.sh.
SYNPROXY_WSCALE=7
SYNPROXY_MSS=1460

# Self-confirm: if the node API stops answering on loopback this many cycles in
# a row while emergency mode is on, tear the chain down. A rule bug must not be
# able to leave the box unreachable until someone notices.
SELF_CONFIRM_FAILS=3

[ -r "$AUTO_CONFIG_FILE" ] && . "$AUTO_CONFIG_FILE"
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"

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

# Management ports (static) + auto-detected SSH port(s) — never rate-limited.
effective_never_drop() {
    printf '%s\n' $NEVER_DROP_PORTS $(detect_ssh_ports) | grep -E '^[0-9]+$' | sort -un
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

# ── ipset allowlist ─────────────────────────────────────────────────────────

ensure_allow_set() {
    ipset list "$ALLOW_SET" >/dev/null 2>&1 && return 0
    ipset create "$ALLOW_SET" hash:net family inet hashsize 4096 maxelem 1000000 2>/dev/null || true
}

restore_whitelist() {
    ensure_allow_set
    [ -r "$WHITELIST_FILE" ] || return 0
    local ip
    grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?' "$WHITELIST_FILE" 2>/dev/null | while read -r ip; do
        [ -n "$ip" ] && ipset add "$ALLOW_SET" "$ip" 2>/dev/null || true
    done
}

# whitelist-sync: replace the allow set from newline/comma-separated IPs on stdin
whitelist_sync() {
    ensure_allow_set
    local incoming tmp_new
    incoming=$(tr ', ' '\n' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?' | sort -u)
    tmp_new=$(printf '%s\n' "$incoming")

    local swap="${ALLOW_SET}_swap"
    ipset destroy "$swap" 2>/dev/null || true
    ipset create "$swap" hash:net family inet hashsize 4096 maxelem 1000000 2>/dev/null || true
    local ip
    printf '%s\n' "$tmp_new" | while read -r ip; do
        [ -n "$ip" ] && ipset add "$swap" "$ip" 2>/dev/null || true
    done
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
port_chunks() {
    detect_client_ports | awk 'NF{ buf=(c?buf","$0:$0); c++; if(c==15){print buf; buf=""; c=0} } END{ if(c) print buf }'
}

build_chain() {
    local ports; ports=$(ports_csv)

    ipt -N "$CHAIN"           # create (ignore "exists")
    ipt -F "$CHAIN"           # rebuild from scratch (ports may have changed)

    # 1. operator blocklist, if the set exists (populated via ipset_manager)
    if ipset list "$TEMP_BLOCK_SET" >/dev/null 2>&1; then
        ipt -A "$CHAIN" -m set --match-set "$TEMP_BLOCK_SET" src -j DROP
    fi

    # 2. whitelist — relays/CDN/panel bypass every limit below
    ensure_allow_set
    ipt -A "$CHAIN" -m set --match-set "$ALLOW_SET" src -j ACCEPT

    # 3. keep established traffic flowing untouched (~99% of packets exit here)
    ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # 4. never touch SSH (auto-detected port) / node API — BEFORE any DROP
    local p
    for p in $(effective_never_drop); do
        ipt -A "$CHAIN" -p tcp --dport "$p" -j ACCEPT
    done

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
    else
        teardown_synproxy_raw
    fi
}

teardown_synproxy_raw() {
    local p
    for p in $(detect_client_ports); do
        iptables -t raw -D PREROUTING -p tcp --dport "$p" --syn -j CT --notrack 2>/dev/null || true
    done
}

jump_present() { iptables -C INPUT -j "$CHAIN" >/dev/null 2>&1; }

add_jump()    { jump_present || ipt -I INPUT 1 -j "$CHAIN"; }
remove_jump() { while jump_present; do ipt -D INPUT -j "$CHAIN"; done; }

apply_rules() {
    build_chain
    add_jump
}

clear_rules() {
    remove_jump
    teardown_synproxy_raw
    ipt -F "$CHAIN"
}

# Emergency mode inserts a chain at INPUT position 1. If a rule bug there ever
# cuts the box off, nobody can log in to fix it — so the loop proves the node is
# still reachable on its own API and tears the chain down after
# SELF_CONFIRM_FAILS consecutive failures. Cheap, no new units, self-healing.
self_confirm_ok() {
    curl -sf --max-time 3 http://127.0.0.1:7500/health >/dev/null 2>&1 && return 0
    # Fall back to "is any SSH session still established" — on a node where the
    # API is legitimately down we must not tear down protection for that alone.
    local p
    for p in $(detect_ssh_ports); do
        if ss -tn state established "( sport = :$p )" 2>/dev/null | grep -q ':'; then
            return 0
        fi
    done
    return 1
}

# self-heal: while active, guarantee the chain+jump survive a foreign ufw reset
selfheal() {
    ipt -nL "$CHAIN" >/dev/null 2>&1 || build_chain
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

# Packets the kernel dropped off the per-CPU backlog queue. This is the direct
# measurement of "we are losing traffic in softirq" rather than the pps proxy,
# and it is exactly what the reduced netdev_max_backlog makes meaningful.
# /proc/net/softnet_stat is one hex row per CPU; column 2 is `dropped`.
read_softnet_drops() {
    local f="/proc/net/softnet_stat" total=0 h
    [ -r "$f" ] || { echo 0; return 0; }
    while read -r h; do
        [[ "$h" =~ ^[0-9a-fA-F]+$ ]] && total=$(( total + 0x$h ))
    done < <(awk '{print $2}' "$f")
    echo "$total"
}

# Переполнение очереди accept — единственный специфичный признак того, что
# приложение не успевает принимать соединения.
#
# Раньше здесь суммировались ListenOverflows и ListenDrops, и это оказалось
# ошибкой: ListenDrops растёт и при штатной смене слушающих сокетов (перезапуск
# Xray, переконфигурация). На живой ноде это давало фон в сотни за цикл при
# НУЛЕВЫХ ListenOverflows — и вотчдог поднимал аварийный режим на ровном месте.
# ListenOverflows инкрементируется строго при полной очереди accept.
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
    # indentation doesn't shift columns.
    local cur_pkts cur_bytes prev_pkts prev_bytes dpkts dbytes pps avg
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
    pps=$(( dpkts / INTERVAL ))
    avg=0; [ "$dpkts" -gt 0 ] && avg=$(( dbytes / dpkts ))
    if [ "$pps" -ge "$PPS_THRESHOLD" ] 2>/dev/null && [ "$avg" -gt 0 ] && [ "$avg" -le "$SMALL_PKT_BYTES" ] 2>/dev/null; then
        SIG_WEAK=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }pps ${pps}, avg ${avg}B"
    fi

    # softnet drops — STRONG: we are already losing packets in the kernel
    local cur_sn prev_sn dsn
    cur_sn=$(read_softnet_drops)
    prev_sn=$(read_prev softnet)
    save_prev softnet "$cur_sn"
    dsn=$(( cur_sn - prev_sn ))
    [ "$dsn" -lt 0 ] && dsn=0
    if [ "$dsn" -ge "$SOFTNET_DROP_DELTA" ] 2>/dev/null; then
        SIG_STRONG=1
        SIG_REASON="${SIG_REASON:+$SIG_REASON, }softnet drops +${dsn}/cycle"
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
        read_state
        sample_signals

        # Reachability check runs for ANY active emergency mode, manual pins
        # included: a manual pin with a bad ruleset locks you out just as hard.
        if [ "$MODE" = "on" ]; then
            if self_confirm_ok; then
                save_prev confirm_fails 0
            else
                local cf; cf=$(read_prev confirm_fails)
                cf=$(( ${cf:-0} + 1 ))
                save_prev confirm_fails "$cf"
                if [ "$cf" -ge "$SELF_CONFIRM_FAILS" ] 2>/dev/null; then
                    log "node unreachable for $cf cycles with emergency mode on — clearing rules"
                    disable_mode
                    save_prev confirm_fails 0
                    sleep "$INTERVAL"; continue
                fi
            fi
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
    raw_count=$(iptables -t raw -nL PREROUTING 2>/dev/null | grep -c 'CT notrack' || echo 0)
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
    dry-run)         read_state; DRYRUN=1; build_chain; echo "iptables -I INPUT 1 -j $CHAIN" ;;
    self-test)       do_self_test ;;
    version)         echo "$WATCHDOG_VERSION" ;;
    status)
        ensure_dirs; read_state
        r=$(printf '%s' "$REASON" | tr -d '"\\')
        printf '{"mode":"%s","source":"%s","since":%s,"reason":"%s","watchdog":"%s","version":"%s"}\n' \
            "$MODE" "$SOURCE" "${SINCE:-0}" "$r" "$WATCHDOG" "$WATCHDOG_VERSION" ;;
    *)               echo "usage: $0 {loop|enable-manual|disable-manual|watchdog-on|watchdog-off|apply|clear|selfheal|whitelist-sync|detect-ports|dry-run|self-test|version|status}" >&2; exit 1 ;;
esac
