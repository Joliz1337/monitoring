#!/bin/bash
#
# extra-ips.sh — transactional management of additional IP addresses on a host
# interface, driven by the monitoring node agent. Lives on the host (outside
# Docker) so the rollback timer and the boot guard keep working when the agent
# container is down or never comes back.
#
# The agent renders backend config files (netplan / networkd / ifupdown) and
# sends a plan; this script does the privileged, order-sensitive part:
#   backup -> write files -> apply backend -> verify -> arm rollback timer.
# The panel then confirms by reaching the node again. No confirmation before
# the deadline -> the timer restores the backup. Unconfirmed at reboot ->
# boot-guard restores the backup before the network comes up.
#
# An address with its own gateway (the hoster routes it via another subnet) gets
# policy routing instead of backend config: `from <addr> lookup <table>` and
# `default via <gw> onlink` in that table. routes.list is the desired state;
# routes_sync makes the kernel match it (apply, rollback, boot, agent self-heal).
#
# Verbs:
#   detect <iface>             backend facts (netplan/networkd/NetworkManager/ifupdown)
#   apply                      plan on stdin (KEY=value lines)
#   confirm <tx>               cancel the rollback timer, keep the change
#   rollback <tx>              manual rollback of a pending transaction
#   rollback-unconfirmed <tx>  timer target: roll back if still unconfirmed
#   boot-guard                 systemd unit: roll back a transaction left over from the previous boot
#   restore-runtime            systemd unit: re-add managed addresses and gateway routes after boot
#   sync-routes                agent self-heal: restore gateway rules/routes, print ROUTES_CHANGED=n
#   self-test                  check tooling, print SELFTEST=ok
#
# Exit codes: 0 ok, 2 bad arguments/plan/status, 3 busy (a transaction is pending),
#             4 apply failed and was rolled back, 5 apply failed AND rollback failed.

set -u

SELF="/opt/monitoring/scripts/extra-ips.sh"
# Overridable only for tests (a scratch dir inside a network namespace)
STATE_DIR="${EXTRA_IPS_STATE_DIR:-/opt/monitoring/network}"
MANAGED_FILE="$STATE_DIR/managed.list"
ROUTES_FILE="$STATE_DIR/routes.list"
ROUTES_NEXT="$STATE_DIR/routes.list.next"
TX_FILE="$STATE_DIR/transaction.env"
HISTORY_FILE="$STATE_DIR/history.log"
BACKUP_ROOT="$STATE_DIR/backups"
LOCK_FILE="$STATE_DIR/lock"
PERSIST_UNIT="mon-extra-ips.service"
BOOT_ID_FILE="/proc/sys/kernel/random/boot_id"

MAX_BACKUPS=5
HISTORY_KEEP=200
VERIFY_ATTEMPTS=20
VERIFY_SLEEP=0.5
# After this many attempts a missing static address is re-added by hand:
# networkd flushes addresses it does not own when it reconfigures a link.
READD_AFTER_ATTEMPT=6

# Gateway routes: one table per (interface, gateway) from this range (the agent
# allocates them), one `from <addr>` rule per address. The source pool owns
# tables and priorities 101-130.
GW_TABLE_MIN=1001
GW_TABLE_MAX=1100
GW_RULE_PRIORITY=1000
# `lookup main suppress_prefixlength 0` ahead of the per-address rules: every
# specific route (own subnet, docker bridges, tunnels) still wins, only the
# default route is picked by source address.
GW_SUPPRESS_PRIORITY=999
ROUTES_CHANGED=0

# Transaction fields (mirrored in transaction.env)
TX_ID=""; TX_STATUS=""; TX_IFACE=""; TX_BACKEND=""; TX_DETAIL=""
TX_ADD=""; TX_REMOVE=""; TX_STARTED_AT=""; TX_FINISHED_AT=""; TX_DEADLINE_AT=""
TX_TIMEOUT=""; TX_TIMER=""; TX_BOOT_ID=""; TX_MESSAGE=""; TX_WARNINGS=""
VERIFY_ERROR=""

log() {
    printf 'extra-ips: %s\n' "$*" >&2
    command -v logger >/dev/null 2>&1 && logger -t extra-ips -- "$*"
}

die() {
    local code="$1"; shift
    log "$*"
    exit "$code"
}

now() { date +%s; }

boot_id() { cat "$BOOT_ID_FILE" 2>/dev/null || echo unknown; }

# Tabs and newlines are field separators in history.log and transaction.env
one_line() { printf '%s' "$1" | tr '\t\n\r' '   '; }

family_of() { case "$1" in *:*) echo 6 ;; *) echo 4 ;; esac; }

valid_cidr() { [[ "$1" =~ ^[0-9A-Fa-f:.]+/[0-9]{1,3}$ ]]; }

contains_word() {
    local needle="$1" word
    shift
    for word in "$@"; do [ "$word" = "$needle" ] && return 0; done
    return 1
}

ensure_state_dir() {
    mkdir -p "$STATE_DIR" "$BACKUP_ROOT" && chmod 700 "$STATE_DIR"
}

take_lock() {
    ensure_state_dir || die 2 "cannot create $STATE_DIR"
    exec 9>"$LOCK_FILE"
    flock -w 15 9 || die 3 "busy: another extra-ips operation holds the lock"
}

# ---------------------------------------------------------------- state files

load_tx() {
    [ -f "$TX_FILE" ] || return 1
    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            TX_ID|TX_STATUS|TX_IFACE|TX_BACKEND|TX_DETAIL|TX_ADD|TX_REMOVE|TX_STARTED_AT|TX_FINISHED_AT|TX_DEADLINE_AT|TX_TIMEOUT|TX_TIMER|TX_BOOT_ID|TX_MESSAGE|TX_WARNINGS)
                printf -v "$key" '%s' "$value" ;;
        esac
    done < "$TX_FILE"
    [ -n "$TX_ID" ]
}

save_tx() {
    local tmp="$TX_FILE.tmp"
    {
        printf 'TX_ID=%s\n' "$TX_ID"
        printf 'TX_STATUS=%s\n' "$TX_STATUS"
        printf 'TX_IFACE=%s\n' "$TX_IFACE"
        printf 'TX_BACKEND=%s\n' "$TX_BACKEND"
        printf 'TX_DETAIL=%s\n' "$(one_line "$TX_DETAIL")"
        printf 'TX_ADD=%s\n' "$TX_ADD"
        printf 'TX_REMOVE=%s\n' "$TX_REMOVE"
        printf 'TX_STARTED_AT=%s\n' "$TX_STARTED_AT"
        printf 'TX_FINISHED_AT=%s\n' "$TX_FINISHED_AT"
        printf 'TX_DEADLINE_AT=%s\n' "$TX_DEADLINE_AT"
        printf 'TX_TIMEOUT=%s\n' "$TX_TIMEOUT"
        printf 'TX_TIMER=%s\n' "$TX_TIMER"
        printf 'TX_BOOT_ID=%s\n' "$TX_BOOT_ID"
        printf 'TX_MESSAGE=%s\n' "$(one_line "$TX_MESSAGE")"
        printf 'TX_WARNINGS=%s\n' "$(one_line "$TX_WARNINGS")"
    } > "$tmp" && mv -f "$tmp" "$TX_FILE"
}

print_tx() {
    printf 'TX_ID=%s\nTX_STATUS=%s\nTX_DEADLINE_AT=%s\nTX_TIMER=%s\nTX_MESSAGE=%s\nTX_WARNINGS=%s\n' \
        "$TX_ID" "$TX_STATUS" "$TX_DEADLINE_AT" "$TX_TIMER" "$(one_line "$TX_MESSAGE")" "$(one_line "$TX_WARNINGS")"
}

add_history() {
    local add="${TX_ADD// /,}" remove="${TX_REMOVE// /,}"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$TX_STARTED_AT" "${TX_FINISHED_AT:-$(now)}" "$TX_ID" "$TX_STATUS" "$TX_IFACE" "$TX_BACKEND" \
        "${add:--}" "${remove:--}" "$(one_line "$TX_MESSAGE")" >> "$HISTORY_FILE"
    if [ "$(wc -l < "$HISTORY_FILE")" -gt "$HISTORY_KEEP" ]; then
        tail -n "$HISTORY_KEEP" "$HISTORY_FILE" > "$HISTORY_FILE.tmp" && mv -f "$HISTORY_FILE.tmp" "$HISTORY_FILE"
    fi
}

finish_tx() {
    TX_STATUS="$1"; TX_MESSAGE="$2"; TX_FINISHED_AT="$(now)"
    save_tx
    add_history
}

prune_backups() {
    ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | head -n -"$MAX_BACKUPS" | while read -r dir; do
        rm -rf "$dir"
    done
}

# ------------------------------------------------------------- addresses (ip)

addr_lines() { ip -o addr show dev "$1" scope global 2>/dev/null; }

addr_list() { addr_lines "$1" | awk '$3 ~ /^inet6?$/ {print $4}'; }

static_addr_list() {
    addr_lines "$1" | awk '$3 ~ /^inet6?$/ && $0 !~ /[[:space:]]dynamic([[:space:]]|$)/ {print $4}'
}

has_addr() { addr_list "$1" | grep -qxF "$2"; }

addr_ready() {
    addr_lines "$1" | awk -v a="$2" '$4 == a && $0 !~ /tentative|dadfailed/ {found = 1} END {exit !found}'
}

addr_dadfailed() {
    addr_lines "$1" | awk -v a="$2" '$4 == a && $0 ~ /dadfailed/ {found = 1} END {exit !found}'
}

ip_add() {
    local addr="$1" iface="$2" out
    has_addr "$iface" "$addr" && return 0
    out=$(ip addr add "$addr" dev "$iface" 2>&1) && return 0
    case "$out" in *"File exists"*) return 0 ;; esac
    log "ip addr add $addr dev $iface: $out"
    return 1
}

ip_del() {
    local addr="$1" iface="$2" out
    has_addr "$iface" "$addr" || return 0
    out=$(ip addr del "$addr" dev "$iface" 2>&1) && return 0
    log "ip addr del $addr dev $iface: $out"
    return 1
}

has_default_route() {
    ip "-$1" route show default 2>/dev/null | grep -q .
}

# ------------------------------------------------- gateway routes (policy routing)

# Lines of a routes file (`<iface> <addr> <gateway> <table>`) for one family
routes_of_family() {
    [ -f "$1" ] || return 0
    awk -v fam="$2" -v lo="$GW_TABLE_MIN" -v hi="$GW_TABLE_MAX" '
        NF == 4 && $4 ~ /^[0-9]+$/ && $4 + 0 >= lo + 0 && $4 + 0 <= hi + 0 {
            six = index($2, ":") > 0
            if ((fam == 6) == six) print
        }' "$1"
}

# "<addr> <table>" of our per-address rules; other priorities and tables are not ours
gw_rules() {
    ip "-$1" rule show 2>/dev/null | awk -v p="$GW_RULE_PRIORITY:" -v lo="$GW_TABLE_MIN" -v hi="$GW_TABLE_MAX" '
        $1 == p {
            f = ""; t = ""
            for (i = 2; i < NF; i++) { if ($i == "from") f = $(i + 1); if ($i == "lookup") t = $(i + 1) }
            if (f != "" && t ~ /^[0-9]+$/ && t + 0 >= lo + 0 && t + 0 <= hi + 0) print f, t
        }'
}

has_suppress_rule() {
    ip "-$1" rule show 2>/dev/null | awk -v p="$GW_SUPPRESS_PRIORITY:" \
        '$1 == p && / lookup main / && / suppress_prefixlength 0/ {found = 1} END {exit !found}'
}

# Tables of our range that hold any route
gw_tables() {
    ip "-$1" route show table all 2>/dev/null | awk -v lo="$GW_TABLE_MIN" -v hi="$GW_TABLE_MAX" '
        { for (i = 1; i < NF; i++) if ($i == "table" && $(i + 1) ~ /^[0-9]+$/ && $(i + 1) + 0 >= lo + 0 && $(i + 1) + 0 <= hi + 0) print $(i + 1) }' \
        | sort -u
}

gw_route_ok() {
    local family="$1" table="$2" gateway="$3" iface="$4"
    ip "-$family" route show table "$table" 2>/dev/null | awk -v g="$gateway" -v d="$iface" \
        '$1 == "default" && $2 == "via" && $3 == g && $4 == "dev" && $5 == d {found = 1} END {exit !found}'
}

route_cmd() {
    local family="$1" out
    shift
    if out=$(ip "-$family" "$@" 2>&1); then
        ROUTES_CHANGED=$((ROUTES_CHANGED + 1))
        return 0
    fi
    case "$out" in *"File exists"*) return 0 ;; esac
    log "ip -$family $*: $out"
    return 1
}

# Make the kernel match a routes file: add what is missing, drop our rules and
# tables the file no longer lists. Writes nothing when everything is in place.
routes_sync() {
    local file="$1" family desired current iface addr gateway table failed=0
    for family in 4 6; do
        desired=$(routes_of_family "$file" "$family")
        current=$(gw_rules "$family")
        if [ -n "$desired" ] && ! has_suppress_rule "$family"; then
            route_cmd "$family" rule add lookup main suppress_prefixlength 0 priority "$GW_SUPPRESS_PRIORITY" || failed=1
        fi
        while read -r iface addr gateway table; do
            [ -n "$table" ] || continue
            if ! gw_route_ok "$family" "$table" "$gateway" "$iface"; then
                route_cmd "$family" route replace default via "$gateway" dev "$iface" onlink table "$table" || failed=1
            fi
            if ! printf '%s\n' "$current" | grep -qxF "$addr $table"; then
                route_cmd "$family" rule add from "$addr" lookup "$table" priority "$GW_RULE_PRIORITY" || failed=1
            fi
        done <<< "$desired"
        while read -r addr table; do
            [ -n "$table" ] || continue
            printf '%s\n' "$desired" | awk -v a="$addr" -v t="$table" '$2 == a && $4 == t {found = 1} END {exit !found}' && continue
            route_cmd "$family" rule del from "$addr" lookup "$table" priority "$GW_RULE_PRIORITY" || failed=1
        done <<< "$current"
        for table in $(gw_tables "$family"); do
            printf '%s\n' "$desired" | awk -v t="$table" '$4 == t {found = 1} END {exit !found}' && continue
            route_cmd "$family" route flush table "$table" || failed=1
        done
        if [ -z "$desired" ] && has_suppress_rule "$family"; then
            route_cmd "$family" rule del lookup main suppress_prefixlength 0 priority "$GW_SUPPRESS_PRIORITY" || failed=1
        fi
    done
    return "$failed"
}

# Space-separated list of what a routes file wants but the kernel lacks
routes_missing() {
    local file="$1" family rules iface addr gateway table
    for family in 4 6; do
        rules=$(gw_rules "$family")
        while read -r iface addr gateway table; do
            [ -n "$table" ] || continue
            printf '%s\n' "$rules" | grep -qxF "$addr $table" || printf ' rule:%s' "$addr"
            gw_route_ok "$family" "$table" "$gateway" "$iface" || printf ' route:%s' "$addr"
        done <<< "$(routes_of_family "$file" "$family")"
    done
}

# ------------------------------------------------------------------- backends

# Prints facts for every backend that is present; the agent picks the one that
# really owns the interface (netplan wins when it defines it, because the lower
# layers it generates would be overwritten on the next `netplan apply`).
cmd_detect() {
    local iface="$1" network_file state con st uuid keyfile
    [ -d "/sys/class/net/$iface" ] || die 2 "interface $iface not found"

    if command -v netplan >/dev/null 2>&1 && ls /etc/netplan/*.yaml >/dev/null 2>&1; then
        echo "NETPLAN=yes"
        echo "NETPLAN_GET_B64=$(netplan get network 2>/dev/null | base64 -w0)"
    else
        echo "NETPLAN=no"
    fi

    if command -v networkctl >/dev/null 2>&1 && systemctl is-active --quiet systemd-networkd 2>/dev/null; then
        network_file=$(networkctl status "$iface" 2>/dev/null | sed -n 's/^[[:space:]]*Network File: *//p' | head -1)
        echo "NETWORKD_FILE=$network_file"
    fi

    if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager 2>/dev/null; then
        state=$(nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION device show "$iface" 2>/dev/null)
        st=$(printf '%s\n' "$state" | sed -n 's/^GENERAL.STATE://p')
        con=$(printf '%s\n' "$state" | sed -n 's/^GENERAL.CONNECTION://p' | sed 's/\\:/:/g')
        if [[ "$st" == *connected* ]] && [ -n "$con" ] && [ "$con" != "--" ]; then
            uuid=$(nmcli -g connection.uuid connection show "$con" 2>/dev/null)
            keyfile=""
            [ -n "$uuid" ] && keyfile=$(grep -ls "^uuid=$uuid" /etc/NetworkManager/system-connections/* 2>/dev/null | head -1)
            echo "NM_CONNECTION=$con"
            echo "NM_KEYFILE=$keyfile"
            echo "NM_IPV4_METHOD=$(nmcli -g ipv4.method connection show "$con" 2>/dev/null)"
            echo "NM_IPV6_METHOD=$(nmcli -g ipv6.method connection show "$con" 2>/dev/null)"
        fi
    fi

    # Our own ifupdown file must not count as "the hoster configured it here"
    if [ -f /etc/network/interfaces ] && grep -qsE "^[[:space:]]*iface[[:space:]]+$iface([[:space:]]|$)" \
            /etc/network/interfaces $(ls /etc/network/interfaces.d/* 2>/dev/null | grep -v monitoring-extra-ips); then
        echo "IFUPDOWN=yes"
        if grep -qsE '^[[:space:]]*source(-directory)?[[:space:]]' /etc/network/interfaces; then
            echo "IFUPDOWN_SOURCED=yes"
        else
            echo "IFUPDOWN_SOURCED=no"
        fi
    fi

    if command -v systemd-run >/dev/null 2>&1; then echo "SYSTEMD_RUN=yes"; else echo "SYSTEMD_RUN=no"; fi
}

nm_modify() {
    local sign="$1" addr="$2"
    if [ "$(family_of "$addr")" = 6 ]; then
        nmcli connection modify "$NM_CONNECTION" "${sign}ipv6.addresses" "$addr"
    else
        nmcli connection modify "$NM_CONNECTION" "${sign}ipv4.addresses" "$addr"
    fi
}

backend_apply() {
    local addr out
    case "$TX_BACKEND" in
        netplan)
            out=$(netplan generate 2>&1) || { log "netplan generate: $out"; return 1; }
            out=$(netplan apply 2>&1) || { log "netplan apply: $out"; return 1; }
            ;;
        networkd)
            out=$(networkctl reload 2>&1) || { log "networkctl reload: $out"; return 1; }
            ;;
        networkmanager)
            for addr in $TX_ADD; do out=$(nm_modify + "$addr" 2>&1) || { log "nmcli: $out"; return 1; }; done
            for addr in $TX_REMOVE; do out=$(nm_modify - "$addr" 2>&1) || { log "nmcli: $out"; return 1; }; done
            out=$(nmcli device reapply "$TX_IFACE" 2>&1) || { log "nmcli device reapply: $out"; return 1; }
            ;;
        ifupdown)
            out=$(ifquery --list --allow=auto 2>&1) || { log "ifquery: $out"; return 1; }
            ;;
        fallback)
            ;;
        *)
            log "unknown backend $TX_BACKEND"; return 1 ;;
    esac
}

# Live rollback of the backend after the files are restored. Failures are logged
# but not fatal: the addresses themselves are already put back by `ip`.
backend_restore() {
    local out
    case "$TX_BACKEND" in
        netplan)
            netplan generate >/dev/null 2>&1 && netplan apply >/dev/null 2>&1 || log "netplan re-apply after rollback failed"
            ;;
        networkd)
            networkctl reload >/dev/null 2>&1 || log "networkctl reload after rollback failed"
            ;;
        networkmanager)
            if ! out=$(nmcli connection reload 2>&1); then
                log "nmcli connection reload: $out — restoring properties by hand"
                nmcli connection modify "$NM_CONNECTION" ipv4.addresses "$NM_IPV4_ADDRESSES" ipv6.addresses "$NM_IPV6_ADDRESSES" >/dev/null 2>&1
            fi
            nmcli device reapply "$TX_IFACE" >/dev/null 2>&1 || log "nmcli device reapply after rollback failed"
            ;;
    esac
}

# The unit re-adds fallback addresses and gateway routes after boot. restore-runtime
# is idempotent (addresses a backend already set are skipped), so the unit is
# simply enabled while there is anything to restore.
sync_persist_unit() {
    command -v systemctl >/dev/null 2>&1 || return 0
    if [ -s "$MANAGED_FILE" ] || [ -s "$ROUTES_FILE" ]; then
        systemctl enable "$PERSIST_UNIT" >/dev/null 2>&1
    else
        systemctl disable "$PERSIST_UNIT" >/dev/null 2>&1
    fi
}

# ------------------------------------------------------------------ transaction

PLAN_FILES=()
PLAN_ABSENT=()
PLAN_MANAGED_B64=""
PLAN_ROUTES_B64=""
NM_CONNECTION=""; NM_KEYFILE=""; NM_IPV4_ADDRESSES=""; NM_IPV6_ADDRESSES=""
BEFORE_ALL=""; BEFORE_STATIC=""; DEFAULT4=no; DEFAULT6=no

read_plan() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            TX_ID=*) TX_ID="${line#*=}" ;;
            IFACE=*) TX_IFACE="${line#*=}" ;;
            BACKEND=*) TX_BACKEND="${line#*=}" ;;
            DETAIL=*) TX_DETAIL="${line#*=}" ;;
            TIMEOUT=*) TX_TIMEOUT="${line#*=}" ;;
            ADD=*) TX_ADD="${line#*=}" ;;
            REMOVE=*) TX_REMOVE="${line#*=}" ;;
            MANAGED_B64=*) PLAN_MANAGED_B64="${line#*=}" ;;
            ROUTES_B64=*) PLAN_ROUTES_B64="${line#*=}" ;;
            NM_CONNECTION=*) NM_CONNECTION="${line#*=}" ;;
            NM_KEYFILE=*) NM_KEYFILE="${line#*=}" ;;
            FILE=*) PLAN_FILES+=("${line#FILE=}") ;;
            ABSENT=*) PLAN_ABSENT+=("${line#ABSENT=}") ;;
        esac
    done
}

allowed_path() {
    case "$1" in
        *..*) return 1 ;;
        /etc/netplan/*|/etc/systemd/network/*|/etc/network/*) return 0 ;;
    esac
    return 1
}

validate_plan() {
    local entry addr path
    [[ "$TX_ID" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$ ]] || die 2 "bad transaction id"
    [ -d "/sys/class/net/$TX_IFACE" ] || die 2 "interface $TX_IFACE not found"
    [[ "$TX_TIMEOUT" =~ ^[0-9]+$ ]] && [ "$TX_TIMEOUT" -ge 30 ] && [ "$TX_TIMEOUT" -le 600 ] || die 2 "bad timeout"
    case "$TX_BACKEND" in netplan|networkd|networkmanager|ifupdown|fallback) ;; *) die 2 "bad backend" ;; esac
    [ -n "$TX_ADD$TX_REMOVE" ] || die 2 "nothing to apply"
    for addr in $TX_ADD $TX_REMOVE; do valid_cidr "$addr" || die 2 "bad address $addr"; done
    for entry in "${PLAN_FILES[@]}"; do
        path=$(printf '%s' "$entry" | awk '{print $2}')
        allowed_path "$path" || die 2 "path not allowed: $path"
    done
    for path in "${PLAN_ABSENT[@]}"; do allowed_path "$path" || die 2 "path not allowed: $path"; done
    if [ "$TX_BACKEND" = networkmanager ]; then
        [ -n "$NM_CONNECTION" ] && [ -n "$NM_KEYFILE" ] || die 2 "NetworkManager connection is not known"
    fi
}

snapshot_before() {
    BEFORE_ALL=$(addr_list "$TX_IFACE" | tr '\n' ' ')
    BEFORE_STATIC=$(static_addr_list "$TX_IFACE" | tr '\n' ' ')
    has_default_route 4 && DEFAULT4=yes
    has_default_route 6 && DEFAULT6=yes
    if [ "$TX_BACKEND" = networkmanager ]; then
        NM_IPV4_ADDRESSES=$(nmcli -g ipv4.addresses connection show "$NM_CONNECTION" 2>/dev/null)
        NM_IPV6_ADDRESSES=$(nmcli -g ipv6.addresses connection show "$NM_CONNECTION" 2>/dev/null)
    fi
}

BACKUP_COUNT=0

backup_path() {
    local dir="$1" path="$2"
    if [ -e "$path" ]; then
        BACKUP_COUNT=$((BACKUP_COUNT + 1))
        cp -p "$path" "$dir/files/$BACKUP_COUNT" || return 1
        printf '%s\tfiles/%s\n' "$path" "$BACKUP_COUNT" >> "$dir/manifest"
    else
        printf '%s\tABSENT\n' "$path" >> "$dir/manifest"
    fi
}

backup_transaction() {
    local dir="$BACKUP_ROOT/$TX_ID" entry path
    rm -rf "$dir" && mkdir -p "$dir/files" || return 1
    : > "$dir/manifest"
    BACKUP_COUNT=0
    for entry in "${PLAN_FILES[@]}"; do
        path=$(printf '%s' "$entry" | awk '{print $2}')
        backup_path "$dir" "$path" || return 1
    done
    for path in "${PLAN_ABSENT[@]}"; do backup_path "$dir" "$path" || return 1; done
    if [ -n "$NM_KEYFILE" ]; then backup_path "$dir" "$NM_KEYFILE" || return 1; fi
    if [ -f "$MANAGED_FILE" ]; then cp -p "$MANAGED_FILE" "$dir/managed.list.bak" || return 1; fi
    if [ -f "$ROUTES_FILE" ]; then cp -p "$ROUTES_FILE" "$dir/routes.list.bak" || return 1; fi
    {
        printf 'BACKEND=%s\nIFACE=%s\nADD=%s\nREMOVE=%s\n' "$TX_BACKEND" "$TX_IFACE" "$TX_ADD" "$TX_REMOVE"
        printf 'BEFORE_ALL=%s\nBEFORE_STATIC=%s\nDEFAULT4=%s\nDEFAULT6=%s\n' "$BEFORE_ALL" "$BEFORE_STATIC" "$DEFAULT4" "$DEFAULT6"
        printf 'NM_CONNECTION=%s\nNM_KEYFILE=%s\nNM_IPV4_ADDRESSES=%s\nNM_IPV6_ADDRESSES=%s\n' \
            "$NM_CONNECTION" "$NM_KEYFILE" "$NM_IPV4_ADDRESSES" "$NM_IPV6_ADDRESSES"
    } > "$dir/meta.env"
}

load_backup_meta() {
    local dir="$BACKUP_ROOT/$1" key value
    [ -f "$dir/meta.env" ] || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            BACKEND) TX_BACKEND="$value" ;;
            IFACE) TX_IFACE="$value" ;;
            ADD) TX_ADD="$value" ;;
            REMOVE) TX_REMOVE="$value" ;;
            BEFORE_ALL|BEFORE_STATIC|DEFAULT4|DEFAULT6|NM_CONNECTION|NM_KEYFILE|NM_IPV4_ADDRESSES|NM_IPV6_ADDRESSES)
                printf -v "$key" '%s' "$value" ;;
        esac
    done < "$dir/meta.env"
}

write_plan_files() {
    local entry mode path b64 tmp
    for entry in "${PLAN_FILES[@]}"; do
        mode=$(printf '%s' "$entry" | awk '{print $1}')
        path=$(printf '%s' "$entry" | awk '{print $2}')
        b64=$(printf '%s' "$entry" | awk '{print $3}')
        tmp="$path.tmp-extraips"
        mkdir -p "$(dirname "$path")" || return 1
        if ! { printf '%s' "$b64" | base64 -d > "$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$path"; }; then
            rm -f "$tmp"
            return 1
        fi
    done
    for path in "${PLAN_ABSENT[@]}"; do rm -f "$path" || return 1; done
}

restore_files() {
    local dir="$BACKUP_ROOT/$1" path copy
    [ -f "$dir/manifest" ] || return 1
    while IFS=$'\t' read -r path copy; do
        [ -n "$path" ] || continue
        if [ "$copy" = ABSENT ]; then
            rm -f "$path"
        else
            mkdir -p "$(dirname "$path")" && cp -p "$dir/$copy" "$path" || return 1
        fi
    done < "$dir/manifest"
    if [ -f "$dir/managed.list.bak" ]; then
        cp -p "$dir/managed.list.bak" "$MANAGED_FILE"
    else
        rm -f "$MANAGED_FILE"
    fi
    if [ -f "$dir/routes.list.bak" ]; then
        cp -p "$dir/routes.list.bak" "$ROUTES_FILE"
    else
        rm -f "$ROUTES_FILE"
    fi
    rm -f "$ROUTES_NEXT"
}

readd_lost_static() {
    local addr
    for addr in $BEFORE_STATIC; do
        contains_word "$addr" $TX_REMOVE && continue
        has_addr "$TX_IFACE" "$addr" && continue
        if ip_add "$addr" "$TX_IFACE"; then
            log "runtime address $addr was dropped by the network stack and re-added"
            TX_WARNINGS="${TX_WARNINGS:+$TX_WARNINGS; }runtime address $addr re-added after reload"
        fi
    done
}

# Every added address is up (IPv6 past DAD), nothing that was there before is
# gone, removed ones are gone, default routes that existed still exist, every
# gateway rule and route is in place.
verify_apply() {
    local attempt=1 addr missing routes_gap
    while :; do
        routes_gap=$(routes_missing "$ROUTES_NEXT")
        missing="$routes_gap"
        for addr in $TX_ADD; do
            if addr_dadfailed "$TX_IFACE" "$addr"; then
                VERIFY_ERROR="$addr failed duplicate address detection (already used in the segment)"
                return 1
            fi
            addr_ready "$TX_IFACE" "$addr" || missing="$missing $addr"
        done
        for addr in $BEFORE_ALL; do
            contains_word "$addr" $TX_REMOVE && continue
            has_addr "$TX_IFACE" "$addr" || missing="$missing $addr(previous)"
        done
        for addr in $TX_REMOVE; do
            has_addr "$TX_IFACE" "$addr" && missing="$missing $addr(still present)"
        done
        [ "$DEFAULT4" = yes ] && ! has_default_route 4 && missing="$missing default-route-v4"
        [ "$DEFAULT6" = yes ] && ! has_default_route 6 && missing="$missing default-route-v6"
        [ -z "$missing" ] && return 0
        if [ "$attempt" -ge "$VERIFY_ATTEMPTS" ]; then
            VERIFY_ERROR="not settled after verification:$missing"
            return 1
        fi
        [ "$attempt" -eq "$READD_AFTER_ATTEMPT" ] && readd_lost_static
        # networkd drops foreign routes and rules while it reconfigures the link after a reload
        [ -n "$routes_gap" ] && routes_sync "$ROUTES_NEXT"
        attempt=$((attempt + 1))
        sleep "$VERIFY_SLEEP"
    done
}

arm_timer() {
    local unit="mon-extra-ips-rollback-$TX_ID"
    if command -v systemd-run >/dev/null 2>&1 && systemd-run --unit="$unit" --on-active="$TX_TIMEOUT" \
            --timer-property=AccuracySec=1s --quiet "$SELF" rollback-unconfirmed "$TX_ID" 2>/dev/null; then
        TX_TIMER="unit:$unit"
        return 0
    fi
    # 9>&- — the lock fd must not outlive this call in the sleeper, or confirm
    # and rollback would find the lock taken until the timer fires
    nohup setsid sh -c "sleep $TX_TIMEOUT; $SELF rollback-unconfirmed $TX_ID" >/dev/null 2>&1 9>&- &
    TX_TIMER="pid:$!"
}

cancel_timer() {
    case "$TX_TIMER" in
        unit:*) systemctl stop "${TX_TIMER#unit:}.timer" >/dev/null 2>&1; systemctl stop "${TX_TIMER#unit:}.service" >/dev/null 2>&1 ;;
        pid:*) kill "${TX_TIMER#pid:}" >/dev/null 2>&1 ;;
    esac
    TX_TIMER=""
}

# restore_transaction <tx> <live|offline> <reason> [final status]
restore_transaction() {
    local tx="$1" mode="$2" reason="$3" status="${4:-rolled_back}" addr failed=0
    load_backup_meta "$tx" || { log "no backup for $tx"; return 1; }
    restore_files "$tx" || failed=1
    if [ "$mode" = live ]; then
        for addr in $TX_ADD; do ip_del "$addr" "$TX_IFACE" || failed=1; done
        for addr in $TX_REMOVE; do ip_add "$addr" "$TX_IFACE" || failed=1; done
        backend_restore
        readd_lost_static
        routes_sync "$ROUTES_FILE" || failed=1
    elif [ "$TX_BACKEND" = netplan ]; then
        # The generator already ran before the guard: regenerate from the restored files
        netplan generate >/dev/null 2>&1 || log "netplan generate during boot rollback failed"
    fi
    sync_persist_unit
    cancel_timer
    finish_tx "$status" "$reason"
    return "$failed"
}

fail_apply() {
    local error="$1"
    log "$error — rolling back"
    if restore_transaction "$TX_ID" live "$error" failed; then
        print_tx
        exit 4
    fi
    TX_MESSAGE="$error; rollback incomplete, check the interface by hand"
    save_tx; print_tx
    exit 5
}

cmd_apply() {
    local addr error=""
    take_lock
    if load_tx; then
        case "$TX_STATUS" in
            pending|applying) die 3 "busy: transaction $TX_ID is $TX_STATUS" ;;
        esac
    fi
    TX_ID=""; TX_STATUS=""; TX_IFACE=""; TX_BACKEND=""; TX_DETAIL=""; TX_ADD=""; TX_REMOVE=""
    TX_STARTED_AT=""; TX_FINISHED_AT=""; TX_DEADLINE_AT=""; TX_TIMEOUT=""; TX_TIMER=""; TX_MESSAGE=""; TX_WARNINGS=""
    read_plan
    validate_plan
    snapshot_before
    backup_transaction || die 2 "backup failed"

    TX_STATUS=applying; TX_STARTED_AT="$(now)"; TX_BOOT_ID="$(boot_id)"
    save_tx

    if ! write_plan_files; then
        fail_apply "cannot write configuration files"
    fi
    if ! backend_apply; then
        fail_apply "backend apply failed ($TX_BACKEND)"
    fi
    for addr in $TX_ADD; do ip_add "$addr" "$TX_IFACE" || fail_apply "ip addr add $addr failed"; done
    for addr in $TX_REMOVE; do ip_del "$addr" "$TX_IFACE" || fail_apply "ip addr del $addr failed"; done
    if ! printf '%s' "$PLAN_ROUTES_B64" | base64 -d > "$ROUTES_NEXT"; then
        fail_apply "cannot write gateway routes"
    fi
    # A failed `ip` here is logged; verification decides — a rule networkd removed
    # a moment ago makes `ip rule del` fail harmlessly
    routes_sync "$ROUTES_NEXT"
    if ! verify_apply; then
        fail_apply "$VERIFY_ERROR"
    fi

    if ! { printf '%s' "$PLAN_MANAGED_B64" | base64 -d > "$MANAGED_FILE.tmp" && mv -f "$MANAGED_FILE.tmp" "$MANAGED_FILE" \
            && mv -f "$ROUTES_NEXT" "$ROUTES_FILE"; }; then
        fail_apply "cannot write managed list"
    fi
    sync_persist_unit

    TX_STATUS=pending; TX_DEADLINE_AT=$(( $(now) + TX_TIMEOUT )); save_tx
    if ! arm_timer; then
        fail_apply "cannot arm the rollback timer"
    fi
    save_tx
    print_tx
}

cmd_confirm() {
    local tx="$1"
    take_lock
    load_tx || die 2 "no transaction"
    [ "$TX_ID" = "$tx" ] || die 2 "unknown transaction $tx (current: $TX_ID $TX_STATUS)"
    if [ "$TX_STATUS" != pending ]; then
        print_tx
        return 0
    fi
    cancel_timer
    finish_tx confirmed "confirmed by the panel"
    prune_backups
    print_tx
}

cmd_rollback() {
    local tx="$1" reason="$2" quiet="$3"
    take_lock
    if ! load_tx; then
        [ "$quiet" = yes ] && exit 0
        die 2 "no transaction"
    fi
    if [ "$TX_ID" != "$tx" ] || { [ "$TX_STATUS" != pending ] && [ "$TX_STATUS" != applying ]; }; then
        [ "$quiet" = yes ] && exit 0
        print_tx
        die 2 "transaction $tx is not pending (current: $TX_ID $TX_STATUS)"
    fi
    if restore_transaction "$tx" live "$reason"; then
        print_tx
        return 0
    fi
    print_tx
    exit 5
}

cmd_boot_guard() {
    take_lock
    load_tx || exit 0
    case "$TX_STATUS" in pending|applying) ;; *) exit 0 ;; esac
    [ "$TX_BOOT_ID" != "$(boot_id)" ] || exit 0
    log "transaction $TX_ID was left $TX_STATUS across a reboot — restoring the backup"
    restore_transaction "$TX_ID" offline "unconfirmed at reboot, restored by boot guard"
}

cmd_restore_runtime() {
    local iface addr
    take_lock
    if [ -f "$MANAGED_FILE" ]; then
        while read -r iface addr; do
            [ -n "$iface" ] && [ -n "$addr" ] || continue
            [ -d "/sys/class/net/$iface" ] || { log "restore-runtime: $iface is missing"; continue; }
            ip_add "$addr" "$iface"
        done < "$MANAGED_FILE"
    fi
    routes_sync "$ROUTES_FILE"
}

cmd_sync_routes() {
    local status=0
    ensure_state_dir || die 2 "cannot create $STATE_DIR"
    exec 9>"$LOCK_FILE"
    # Non-blocking: a running transaction holds the lock and syncs routes itself
    flock -n 9 || die 3 "busy: another extra-ips operation holds the lock"
    routes_sync "$ROUTES_FILE" || status=1
    echo "ROUTES_CHANGED=$ROUTES_CHANGED"
    return "$status"
}

cmd_self_test() {
    local tool
    for tool in ip flock base64 awk sed mkdir; do
        command -v "$tool" >/dev/null 2>&1 || die 2 "missing tool: $tool"
    done
    ensure_state_dir || die 2 "cannot create $STATE_DIR"
    echo "SELFTEST=ok"
    if command -v systemd-run >/dev/null 2>&1; then echo "SYSTEMD_RUN=yes"; else echo "SYSTEMD_RUN=no"; fi
}

case "${1:-}" in
    detect) [ -n "${2:-}" ] || die 2 "usage: $0 detect <iface>"; cmd_detect "$2" ;;
    apply) cmd_apply ;;
    confirm) [ -n "${2:-}" ] || die 2 "usage: $0 confirm <tx>"; cmd_confirm "$2" ;;
    rollback) [ -n "${2:-}" ] || die 2 "usage: $0 rollback <tx>"; cmd_rollback "$2" "rolled back manually from the panel" no ;;
    rollback-unconfirmed) [ -n "${2:-}" ] || die 2 "usage: $0 rollback-unconfirmed <tx>"; cmd_rollback "$2" "not confirmed by the panel in time, restored from backup" yes ;;
    boot-guard) cmd_boot_guard ;;
    restore-runtime) cmd_restore_runtime ;;
    sync-routes) cmd_sync_routes ;;
    self-test) cmd_self_test ;;
    *) die 2 "usage: $0 detect <iface> | apply | confirm <tx> | rollback <tx> | rollback-unconfirmed <tx> | boot-guard | restore-runtime | sync-routes | self-test" ;;
esac
