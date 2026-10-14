#!/bin/bash
#
# nic-routing.sh — replies from the address of an extra NIC leave through that NIC.
#
# A cloud passes a VM port only packets carrying that port's own address. With a
# single default route in main, the reply from the second NIC's address leaves
# through the first NIC and the cloud drops it. Every physical NIC except the one
# holding the main default gets its own table (its subnets + default via its
# gateway) and every IPv4 address on it gets `from <addr>` rules into that table.
# The main-default NIC is never touched: the panel reaches the node through it.
#
# State comes from the kernel, not from configs: a DHCP address, a VIP or a NIC
# attached later are picked up by themselves. No netplan/networkd/cloud-init file
# is written — the rules live in the kernel only. networkd drops foreign rules
# when it restarts; `watch` (mon-nic-routing.service) puts them back on the
# kernel event, without the agent container.
#
# Rules per address:
#   PRIO_SUBNET    from <addr> to <its subnet> — neighbours through the own NIC;
#                  ahead of the extra-IPs suppress rule (999), or main would win
#   PRIO_SUPPRESS  main without its default — docker, tunnels, metadata keep
#                  going by main
#   PRIO_SOURCE    from <addr> — everything else through the NIC's gateway;
#                  after the extra-IPs rules (1000): an address with its own
#                  gateway from the panel stays with them
# Traffic not bound to an address still goes by main.
#
# Owned: tables TABLE_BASE+ifindex and priorities 995/1100/1101. The source pool
# owns 101-130, extra IPs 999, 1000 and tables 1001-1100.
#
# Verbs:
#   sync     one pass: make the kernel match the desired state, silent when in sync
#   watch    sync, then a pass on every link/address/route/rule event (systemd unit)
#   clear    drop every rule and route of ours
#   status   NIC lines "NIC <name> <role> <table> <gateway|-> <cidr,...>"
#            (role: primary | routed | no_gateway | out_of_range), IN_SYNC=yes|no, RULES=n

set -u

TABLE_BASE=20000            # NIC table = TABLE_BASE + ifindex
TABLE_LAST=29999
PRIO_SUBNET=995
PRIO_SUPPRESS=1100
PRIO_SOURCE=1101
RESYNC_SEC=60               # a pass without events too, in case one was missed
DEBOUNCE_SEC=1              # networkd changes routes in bursts: one pass per burst
# Overridable only for tests (dummy NICs in a network namespace have no device/)
SYS_NET="${NIC_ROUTING_SYS_NET:-/sys/class/net}"
LEASES_DIR="${NIC_ROUTING_LEASES_DIR:-/run/systemd/netif/leases}"

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

ip_to_int() { local IFS=.; set -- $1; echo $(( ($1 << 24) | ($2 << 16) | ($3 << 8) | $4 )); }
int_to_ip() { echo "$(( $1 >> 24 & 255 )).$(( $1 >> 16 & 255 )).$(( $1 >> 8 & 255 )).$(( $1 & 255 ))"; }
prefix_mask() { echo $(( $1 == 0 ? 0 : (0xFFFFFFFF << (32 - $1)) & 0xFFFFFFFF )); }

# 192.168.0.127/24 -> 192.168.0.0/24
network_of() {
    local len="${1#*/}"
    echo "$(int_to_ip $(( $(ip_to_int "${1%/*}") & $(prefix_mask "$len") )))/$len"
}

in_network() {
    local mask
    mask=$(prefix_mask "${2#*/}")
    [ $(( $(ip_to_int "$1") & mask )) -eq $(( $(ip_to_int "${2%/*}") & mask )) ]
}

# Administrative UP: the kernel refuses a route through a link that is down
link_is_up() { ip -o link show dev "$1" 2>/dev/null | grep -qE '<([^>]*,)?UP(,[^>]*)?>'; }

nic_ifindex() { ip -o link show dev "$1" 2>/dev/null | awk -F': ' '{print $1; exit}'; }

nic_addresses() { ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk '{print $4}'; }

# docker0, veth, br-*, lo and tunnels have no device/ — only real NICs are left
physical_nics() {
    local path nic
    for path in "$SYS_NET"/*/device; do
        [ -e "$path" ] || continue
        nic="${path%/device}"
        nic="${nic##*/}"
        link_is_up "$nic" && echo "$nic"
    done
}

main_gateway() { ip -4 route show table main default "$@" 2>/dev/null | awk '$2 == "via" {print $3; exit}'; }

# Device of the main default the kernel uses (the first one; a multipath default
# has it on its first nexthop line)
primary_nic() {
    ip -4 route show table main default 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

# The NIC's own default in main, else ROUTER of its DHCP lease (networkd sets no
# default with `use-routes: false`), else the main gateway when it is inside the
# NIC's subnet (a second NIC in the same network without a default of its own)
nic_gateway() {
    local nic="$1" gateway cidr
    gateway=$(main_gateway dev "$nic")
    [ -n "$gateway" ] || gateway=$(awk -F= '$1 == "ROUTER" {split($2, r, " "); print r[1]; exit}' \
        "$LEASES_DIR/$(nic_ifindex "$nic")" 2>/dev/null)
    if [ -n "$gateway" ]; then
        echo "$gateway"
        return
    fi
    gateway=$(main_gateway)
    [ -n "$gateway" ] || return
    for cidr in $(nic_addresses "$nic"); do
        in_network "$gateway" "$(network_of "$cidr")" && { echo "$gateway"; return; }
    done
}

# WANT_ROUTES: "<table> <dest> <via> <dev> <src>", WANT_RULES: "<prio> <from> <to> <table>";
# "-" stands for an absent field. REPORT: status lines of the NICs with addresses.
build_desired() {
    local primary nic table gateway addrs cidr addr network role
    WANT_ROUTES=""
    WANT_RULES=""
    REPORT=""
    primary=$(primary_nic)
    for nic in $(physical_nics); do
        addrs=$(nic_addresses "$nic")
        [ -n "$addrs" ] || continue
        table=$((TABLE_BASE + $(nic_ifindex "$nic")))
        gateway=$(nic_gateway "$nic")
        role=routed
        if [ "$nic" = "$primary" ]; then
            role=primary
        elif [ "$table" -gt "$TABLE_LAST" ]; then
            role=out_of_range
        elif [ -z "$gateway" ]; then
            role=no_gateway
        fi
        REPORT+="NIC $nic $role $table ${gateway:--} $(printf '%s' "$addrs" | tr '\n' ',')"$'\n'
        [ "$role" = routed ] || continue
        WANT_ROUTES+="$table default $gateway $nic -"$'\n'
        for cidr in $addrs; do
            addr="${cidr%/*}"
            WANT_RULES+="$PRIO_SOURCE $addr - $table"$'\n'
            [ "${cidr#*/}" -lt 32 ] || continue
            network=$(network_of "$cidr")
            WANT_RULES+="$PRIO_SUBNET $addr $network $table"$'\n'
            # One route per subnet, its src is the NIC's first address there
            case "$WANT_ROUTES" in
                *"$table $network - $nic "*) ;;
                *) WANT_ROUTES+="$table $network - $nic $addr"$'\n' ;;
            esac
        done
    done
    [ -z "$WANT_RULES" ] || WANT_RULES+="$PRIO_SUPPRESS - - main"$'\n'
}

current_rules() {
    ip -4 rule show 2>/dev/null | awk -v a="$PRIO_SUBNET:" -v b="$PRIO_SUPPRESS:" -v c="$PRIO_SOURCE:" '
        $1 == a || $1 == b || $1 == c {
            from = "-"; to = "-"; table = "-"
            for (i = 2; i < NF; i++) {
                if ($i == "from" && $(i + 1) != "all") from = $(i + 1)
                if ($i == "to") to = $(i + 1)
                if ($i == "lookup") table = $(i + 1)
            }
            sub(":", "", $1)
            print $1, from, to, table
        }'
}

current_routes() {
    ip -4 route show table all 2>/dev/null | awk -v lo="$TABLE_BASE" -v hi="$TABLE_LAST" '
        {
            table = ""; via = "-"; dev = "-"; src = "-"
            for (i = 2; i < NF; i++) {
                if ($i == "table") table = $(i + 1)
                if ($i == "via") via = $(i + 1)
                if ($i == "dev") dev = $(i + 1)
                if ($i == "src") src = $(i + 1)
            }
            if (table ~ /^[0-9]+$/ && table + 0 >= lo + 0 && table + 0 <= hi + 0) print table, $1, via, dev, src
        }'
}

# <add|del> <prio> <from> <to> <table>
rule_cmd() {
    local args="priority $2"
    [ "$3" = "-" ] || args+=" from $3"
    [ "$4" = "-" ] || args+=" to $4"
    args+=" lookup $5"
    [ "$2" != "$PRIO_SUPPRESS" ] || args+=" suppress_prefixlength 0"
    run rule "$1" $args
}

# <table> <dest> <via> <dev> <src>
route_replace() {
    local args="$2"
    [ "$3" = "-" ] || args+=" via $3 onlink"
    args+=" dev $4"
    [ "$5" = "-" ] || args+=" src $5"
    run route replace $args table "$1"
}

route_del() { run route del "$2" table "$1"; }

# Writes nothing when the kernel already matches: `watch` would otherwise wake
# up on its own changes forever
sync_all() {
    local line have
    build_desired
    if [ "$REPORT" != "$LAST_REPORT" ]; then
        printf '%s' "$REPORT"
        LAST_REPORT="$REPORT"
    fi
    # Routes before rules, rules go before routes: a rule looking into an empty
    # table would send the reply through main for a moment
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
    local line
    while read -r line; do
        [ -z "$line" ] || rule_cmd del $line
    done <<< "$(current_rules)"
    while read -r line; do
        [ -z "$line" ] || route_del $line
    done <<< "$(current_routes)"
}

show_status() {
    local rules
    build_desired
    rules=$(current_rules)
    printf '%s' "$REPORT"
    if [ "$(sorted "$WANT_RULES")" = "$(sorted "$rules")" ] \
        && [ "$(sorted "$WANT_ROUTES")" = "$(sorted "$(current_routes)")" ]; then
        echo "IN_SYNC=yes"
    else
        echo "IN_SYNC=no"
    fi
    echo "RULES=$(sorted "$rules" | wc -l)"
}

# A rule gone missing (networkd restart), a new DHCP address, a new NIC — a pass
# right on the kernel event; without events — every RESYNC_SEC
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
