#!/bin/bash
#
# gre-tunnels.sh — DDoS-Guard protected addresses delivered to this host over GRE.
#
# DDoS-Guard filters the traffic of the protected address (CE) and sends it here
# through a GRE tunnel. Replies from CE must go back into the tunnel, to their end
# of the /30 point-to-point network (PE). The main default route is never touched:
# only traffic sourced from CE is steered, by policy routing.
#
# Desired state is $CONFIG, one tunnel per line, written by the agent:
#   <slot> <local> <remote> <pe> <ce> <health_net>
# Per tunnel: link gre-ddg<slot> (GRE local = our address, remote = DDoS-Guard)
# carrying CE/30 with loose rp_filter, and table TABLE_BASE+slot with
#   default via PE        replies from CE to clients
#   <health_net> via PE   DDoS-Guard health checks
# Rules:
#   PRIO_SUPPRESS  main without its default — docker and local subnets keep going by main
#   PRIO_SOURCE    from CE — everything else back into the tunnel
# MTU is the underlay MTU minus the GRE overhead, at most 1476: the kernel derives
# the TCP MSS of connections on CE from it, so no MSS clamping rule is needed.
#
# Owned: links gre-ddg<N>, tables TABLE_BASE+1..TABLE_LAST, priorities 1150/1151.
# The source pool owns 101-130, extra IPs 999, 1000 and tables 1001-1100,
# NIC routing 995/1100/1101 and tables 20000-29999.
#
# Verbs:
#   sync     one pass: make the kernel match $CONFIG, silent when in sync
#   watch    sync, then a pass on every link/address/route/rule event (systemd unit)
#   clear    drop our links, rules and routes
#   status   "TUN <slot> <local> <remote> <pe> <ce> <health_net> <state> <mtu|-> <foreign|->"
#            (state: ok | drift | missing | no_local | conflict), IN_SYNC=yes|no

set -u

CONFIG="${GRE_CONFIG:-/opt/monitoring/gre/tunnels.list}"
IF_PREFIX="gre-ddg"
PTP_PREFIX=30
TABLE_BASE=1200             # tunnel table = TABLE_BASE + slot
TABLE_LAST=1208
PRIO_SUPPRESS=1150
PRIO_SOURCE=1151
GRE_OVERHEAD=24             # outer IPv4 header + GRE header
MAX_MTU=1476
RESYNC_SEC=60               # a pass without events too, in case one was missed
DEBOUNCE_SEC=1              # networkd changes routes in bursts: one pass per burst

LINKS=""
REPORT=""
LAST_REPORT=""

run() {
    local out
    if out=$(ip -4 "$@" 2>&1); then
        echo "ip -4 $*"
        return 0
    fi
    echo "ip -4 $*: $out" >&2
    return 1
}

contains() { printf '%s\n' "$1" | grep -qxF -- "$2"; }

sorted() { printf '%s\n' "$1" | sed '/^$/d' | sort; }

link_exists() { ip link show dev "$1" >/dev/null 2>&1; }

# Administrative UP: the kernel refuses a route through a link that is down
link_is_up() { ip -o link show dev "$1" 2>/dev/null | grep -qE '<([^>]*,)?UP(,[^>]*)?>'; }

link_mtu() { ip -o link show dev "$1" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "mtu") {print $(i + 1); exit}}'; }

link_addresses() { ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}'; }

address_assigned() {
    ip -4 -o addr show 2>/dev/null | awk -v a="$1" '{split($4, p, "/"); if (p[1] == a) found = 1} END {exit !found}'
}

# Every GRE link: "<name> <local> <remote>"
gre_links() {
    ip -d -o link show type gre 2>/dev/null | awk '
        {
            name = $2; sub(/[:@].*$/, "", name)
            lo = "-"; re = "-"
            for (i = 3; i < NF; i++) {
                if ($i == "local") lo = $(i + 1)
                if ($i == "remote") re = $(i + 1)
            }
            print name, lo, re
        }'
}

link_endpoints() { printf '%s\n' "$LINKS" | awk -v n="$1" '$1 == n {print $2, $3; exit}'; }

# Underlay MTU minus the GRE overhead; MAX_MTU when the underlay is unknown or larger
tunnel_mtu() {
    local dev mtu
    dev=$(ip -4 route get "$2" from "$1" 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
    [ -n "$dev" ] && mtu=$(link_mtu "$dev")
    if [[ ${mtu:-} =~ ^[0-9]+$ ]] && [ $((mtu - GRE_OVERHEAD)) -lt "$MAX_MTU" ]; then
        echo $((mtu - GRE_OVERHEAD))
    else
        echo "$MAX_MTU"
    fi
}

rp_filter_path() { echo "/proc/sys/net/ipv4/conf/$1/rp_filter"; }

# Loose: packets from DDoS-Guard's health network arrive on the tunnel, not on the
# link main would choose for that source
rp_filter_loose() { [ "$(cat "$(rp_filter_path "$1")" 2>/dev/null)" = 2 ]; }

set_rp_filter_loose() {
    if echo 2 > "$(rp_filter_path "$1")" 2>/dev/null; then
        echo "rp_filter $1=2"
    else
        echo "rp_filter $1: cannot write" >&2
    fi
}

# <name> <local> <remote> <ce> <mtu>
link_in_sync() {
    [ "$(link_endpoints "$1")" = "$2 $3" ] || return 1
    link_is_up "$1" && [ "$(link_mtu "$1")" = "$5" ] || return 1
    [ "$(link_addresses "$1")" = "$4/$PTP_PREFIX" ] || return 1
    rp_filter_loose "$1"
}

# WANT_LINKS: "<slot> <local> <remote> <ce> <mtu>", WANT_ROUTES: "<table> <dest> <via> <dev>",
# WANT_RULES: "<prio> <from> <table>" ("-" — from all). REPORT: status lines.
build_desired() {
    local slot local_ remote pe ce health _ name table mtu foreign state
    WANT_LINKS=""
    WANT_ROUTES=""
    WANT_RULES=""
    REPORT=""
    LINKS=$(gre_links)
    [ -r "$CONFIG" ] || return 0
    while read -r slot local_ remote pe ce health _; do
        [[ $slot =~ ^[1-9]$ ]] && [ $((TABLE_BASE + slot)) -le "$TABLE_LAST" ] || continue
        name="$IF_PREFIX$slot"
        table=$((TABLE_BASE + slot))
        mtu="-"
        # The kernel keeps one GRE link per address pair: a foreign one blocks ours.
        # Our own link of another slot is not foreign — the pass drops it as stale
        foreign=$(printf '%s\n' "$LINKS" | awk -v p="^$IF_PREFIX[0-9]+$" -v l="$local_" -v r="$remote" \
            '$1 !~ p && $2 == l && $3 == r {print $1; exit}')
        if ! address_assigned "$local_"; then
            state=no_local
        elif [ -n "$foreign" ]; then
            state=conflict
        else
            mtu=$(tunnel_mtu "$local_" "$remote")
            WANT_LINKS+="$slot $local_ $remote $ce $mtu"$'\n'
            WANT_ROUTES+="$table default $pe $name"$'\n'
            WANT_ROUTES+="$table $health $pe $name"$'\n'
            WANT_RULES+="$PRIO_SOURCE $ce $table"$'\n'
            if ! link_exists "$name"; then
                state=missing
            elif link_in_sync "$name" "$local_" "$remote" "$ce" "$mtu"; then
                state=ok
            else
                state=drift
            fi
        fi
        REPORT+="TUN $slot $local_ $remote $pe $ce $health $state $mtu ${foreign:--}"$'\n'
    done < "$CONFIG"
    [ -z "$WANT_RULES" ] || WANT_RULES+="$PRIO_SUPPRESS - main"$'\n'
}

# Our links that are not wanted (removed from the config, GRE SRC gone, conflict)
stale_links() {
    local name
    for name in $(printf '%s\n' "$LINKS" | awk -v p="^$IF_PREFIX[0-9]+$" '$1 ~ p {print $1}'); do
        printf '%s\n' "$WANT_LINKS" | awk -v s="${name#"$IF_PREFIX"}" '$1 == s {found = 1} END {exit !found}' \
            || echo "$name"
    done
}

# <slot> <local> <remote> <ce> <mtu>
sync_link() {
    local name="$IF_PREFIX$1" cidr
    if [ "$(link_endpoints "$name")" != "$2 $3" ]; then
        ! link_exists "$name" || run link del "$name"
        # ip_gre is a module on stock kernels; nothing to load when it is built in
        modprobe ip_gre 2>/dev/null
        run link add "$name" type gre local "$2" remote "$3" || return
    fi
    if ! link_is_up "$name" || [ "$(link_mtu "$name")" != "$5" ]; then
        run link set dev "$name" mtu "$5" up
    fi
    for cidr in $(link_addresses "$name"); do
        [ "$cidr" = "$4/$PTP_PREFIX" ] || run addr del "$cidr" dev "$name"
    done
    contains "$(link_addresses "$name")" "$4/$PTP_PREFIX" || run addr add "$4/$PTP_PREFIX" dev "$name"
    rp_filter_loose "$name" || set_rp_filter_loose "$name"
}

current_rules() {
    ip -4 rule show 2>/dev/null | awk -v a="$PRIO_SUPPRESS:" -v b="$PRIO_SOURCE:" '
        $1 == a || $1 == b {
            from = "-"; table = "-"
            for (i = 2; i < NF; i++) {
                if ($i == "from" && $(i + 1) != "all") from = $(i + 1)
                if ($i == "lookup") table = $(i + 1)
            }
            sub(":", "", $1)
            print $1, from, table
        }'
}

current_routes() {
    ip -4 route show table all 2>/dev/null | awk -v lo="$((TABLE_BASE + 1))" -v hi="$TABLE_LAST" '
        {
            table = ""; via = "-"; dev = "-"
            for (i = 2; i < NF; i++) {
                if ($i == "table") table = $(i + 1)
                if ($i == "via") via = $(i + 1)
                if ($i == "dev") dev = $(i + 1)
            }
            if (table ~ /^[0-9]+$/ && table + 0 >= lo + 0 && table + 0 <= hi + 0) print table, $1, via, dev
        }'
}

# <add|del> <prio> <from> <table>
rule_cmd() {
    local args="priority $2"
    [ "$3" = "-" ] || args+=" from $3"
    args+=" lookup $4"
    [ "$2" != "$PRIO_SUPPRESS" ] || args+=" suppress_prefixlength 0"
    run rule "$1" $args
}

# <table> <dest> <via> <dev>
route_replace() { run route replace "$2" via "$3" dev "$4" onlink table "$1"; }

route_del() { run route del "$2" table "$1"; }

# Writes nothing when the kernel already matches: `watch` would otherwise wake
# up on its own changes forever
sync_all() {
    local line have name
    build_desired
    if [ "$REPORT" != "$LAST_REPORT" ]; then
        printf '%s' "$REPORT"
        LAST_REPORT="$REPORT"
    fi
    for name in $(stale_links); do
        run link del "$name"
    done
    while read -r line; do
        [ -z "$line" ] || sync_link $line
    done <<< "$WANT_LINKS"
    # Routes before rules: a rule looking into an empty table would send the
    # reply through main for a moment
    have=$(current_routes)
    while read -r line; do
        [ -z "$line" ] || contains "$have" "$line" || route_replace $line
    done <<< "$WANT_ROUTES"
    have=$(current_rules)
    while read -r line; do
        [ -z "$line" ] || contains "$have" "$line" || rule_cmd add $line
    done <<< "$WANT_RULES"
    while read -r line; do
        [ -z "$line" ] || contains "$WANT_RULES" "$line" || rule_cmd del $line
    done <<< "$have"
    # Re-read: `replace` may have swapped a route the old snapshot showed as stale
    have=$(current_routes)
    while read -r line; do
        [ -z "$line" ] || contains "$WANT_ROUTES" "$line" || route_del $line
    done <<< "$have"
}

clear_all() {
    local line name
    while read -r line; do
        [ -z "$line" ] || rule_cmd del $line
    done <<< "$(current_rules)"
    for name in $(gre_links | awk -v p="^$IF_PREFIX[0-9]+$" '$1 ~ p {print $1}'); do
        run link del "$name"
    done
    # Deleting a link takes its routes along; whatever is left goes here
    while read -r line; do
        [ -z "$line" ] || route_del $line
    done <<< "$(current_routes)"
}

show_status() {
    build_desired
    printf '%s' "$REPORT"
    if [ -z "$(printf '%s' "$REPORT" | awk '$8 == "drift" || $8 == "missing"')" ] \
        && [ -z "$(stale_links)" ] \
        && [ "$(sorted "$WANT_RULES")" = "$(sorted "$(current_rules)")" ] \
        && [ "$(sorted "$WANT_ROUTES")" = "$(sorted "$(current_routes)")" ]; then
        echo "IN_SYNC=yes"
    else
        echo "IN_SYNC=no"
    fi
}

# A rule gone missing (networkd restart), GRE SRC back after DHCP, a deleted
# link — a pass right on the kernel event; without events — every RESYNC_SEC
watch_kernel() {
    local status
    sync_all
    ip -4 monitor link address route rule | while :; do
        read -r -t "$RESYNC_SEC" _
        status=$?
        # End of stream: ip monitor died — exit and let systemd restart us
        [ "$status" -eq 0 ] || [ "$status" -gt 128 ] || exit 1
        while read -r -t "$DEBOUNCE_SEC" _; do :; done
        sync_all
    done
}

case "${1:-}" in
    sync) sync_all ;;
    watch) watch_kernel ;;
    clear) clear_all ;;
    status) show_status ;;
    *) echo "usage: $0 sync|watch|clear|status" >&2; exit 2 ;;
esac
