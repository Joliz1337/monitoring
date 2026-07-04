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
WATCHDOG_VERSION="1.0.0"

STATE_DIR="/opt/monitoring/antiddos"
STATE_FILE="$STATE_DIR/state.json"
WHITELIST_FILE="$STATE_DIR/whitelist.json"
CONFIG_FILE="$STATE_DIR/config"
RUN_DIR="$STATE_DIR/run"

CHAIN="ANTIDDOS"
ALLOW_SET="antiddos_allow"
TEMP_BLOCK_SET="blocklist_temp"   # created by ipset_manager; used if present

# --- tunables (override via $CONFIG_FILE) ---
INTERVAL=10                 # loop period, seconds
CONNLIMIT=100               # max concurrent conns per source /32 on client ports
NEWRATE=30                  # max new conns/sec per source
NEWBURST=60                 # burst for the rate limiter
NEVER_DROP_PORTS="22 9100"  # SSH + node API — always ACCEPT

# detection thresholds (conservative — must not fire on a legit evening peak)
CONNTRACK_PCT=80            # conntrack table fill % → strong signal
SYNCOOKIE_DELTA=200         # SyncookiesSent growth per cycle → strong signal (SYN flood)
PPS_THRESHOLD=150000        # rx packets/sec …
SMALL_PKT_BYTES=200         # …combined with avg packet < this → flood-of-tiny (weak)
SOFTIRQ_PCT=50              # softirq CPU% → weak signal
WEAK_HOLD=45               # weak signals must persist this long (s) before enabling
HYSTERESIS=900              # auto-disable after this many seconds with no signal

[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"

log() { echo "[antiddos] $*" >&2; }

ipt()   { iptables "$@" 2>/dev/null; }
ipt_q() { iptables "$@" >/dev/null 2>&1; }

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

# Listening TCP ports on non-loopback addresses, minus the never-drop set.
detect_client_ports() {
    local exclude
    exclude=$(printf '%s\n' $NEVER_DROP_PORTS)
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
    modprobe xt_SYNPROXY 2>/dev/null || true
    iptables -t raw -nL >/dev/null 2>&1 || return 1
    return 0
}

# ── emergency ruleset ───────────────────────────────────────────────────────

build_chain() {
    local ports; ports=$(ports_csv)

    ipt -N "$CHAIN"           # create (ignore "exists")
    ipt -F "$CHAIN"           # rebuild from scratch (ports may have changed)

    # 1. whitelist first — relays/CDN/panel bypass every limit below
    ensure_allow_set
    ipt -A "$CHAIN" -m set --match-set "$ALLOW_SET" src -j ACCEPT

    # 2. keep established traffic flowing untouched
    ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # 3. never touch SSH / node API
    local p
    for p in $NEVER_DROP_PORTS; do
        ipt -A "$CHAIN" -p tcp --dport "$p" -j ACCEPT
    done

    # 4. drop packets that belong to no valid connection (kills ACK-flood; pairs
    #    with nf_conntrack_tcp_loose=0)
    ipt -A "$CHAIN" -p tcp -m conntrack --ctstate INVALID -j DROP

    [ -z "$ports" ] && return 0

    # 5. SYNPROXY on client ports — validate the handshake before conntrack
    if synproxy_available; then
        for p in $(detect_client_ports); do
            iptables -t raw -C PREROUTING -p tcp --dport "$p" --syn -j CT --notrack 2>/dev/null \
                || iptables -t raw -A PREROUTING -p tcp --dport "$p" --syn -j CT --notrack 2>/dev/null || true
        done
        ipt -A "$CHAIN" -p tcp -m multiport --dports "$ports" -m conntrack --ctstate INVALID,UNTRACKED \
            -j SYNPROXY --sack-perm --wscale 7 --mss 1460
    fi

    # 6. per-source connection cap on client ports
    ipt -A "$CHAIN" -p tcp -m multiport --dports "$ports" \
        -m connlimit --connlimit-above "$CONNLIMIT" --connlimit-mask 32 -j DROP

    # 7. per-source new-connection rate limit on client ports
    ipt -A "$CHAIN" -p tcp -m multiport --dports "$ports" -m conntrack --ctstate NEW \
        -m hashlimit --hashlimit-above "${NEWRATE}/sec" --hashlimit-burst "$NEWBURST" \
        --hashlimit-mode srcip --hashlimit-name antiddos \
        --hashlimit-htable-max 1000000 --hashlimit-htable-expire 60000 -j DROP
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

# sets globals: SIG_STRONG SIG_WEAK SIG_REASON
sample_signals() {
    SIG_STRONG=0; SIG_WEAK=0; SIG_REASON=""

    # conntrack fill %
    local ct_count ct_max fill=0
    ct_count=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
    ct_max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)
    if [ "${ct_max:-0}" -gt 0 ] 2>/dev/null; then
        fill=$(( ct_count * 100 / ct_max ))
    fi
    if [ "$fill" -ge "$CONNTRACK_PCT" ] 2>/dev/null; then
        SIG_STRONG=1; SIG_REASON="conntrack ${fill}%"
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

        # manual pin: only self-heal, never auto-toggle
        if [ "$MODE" = "on" ] && [ "$SOURCE" = "manual" ]; then
            selfheal
            sleep "$INTERVAL"; continue
        fi

        if [ "$WATCHDOG" != "on" ]; then
            [ "$MODE" = "on" ] && selfheal
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
    version)         echo "$WATCHDOG_VERSION" ;;
    status)
        ensure_dirs; read_state
        r=$(printf '%s' "$REASON" | tr -d '"\\')
        printf '{"mode":"%s","source":"%s","since":%s,"reason":"%s","watchdog":"%s","version":"%s"}\n' \
            "$MODE" "$SOURCE" "${SINCE:-0}" "$r" "$WATCHDOG" "$WATCHDOG_VERSION" ;;
    *)               echo "usage: $0 {loop|enable-manual|disable-manual|watchdog-on|watchdog-off|apply|clear|selfheal|whitelist-sync|detect-ports|version|status}" >&2; exit 1 ;;
esac
