#!/bin/bash
#
# Hybrid NIC Tuning Script
# Hardware multi-queue + software RPS on remaining cores
# Use when NIC hardware queues < CPU cores (e.g. I225-V with 4 queues on 12-core host)
# Hardware dispatches to first N cores via IRQ, RPS steers softirq work to the rest
#
# Safety: skips link-down and bond/bridge slaves. Does NOT touch ring buffers
# (ethtool -G causes link reset on igb/ixgbe — on OVH edge that can kill SSH).
#

# ---------------------------------------------------------------------------
# Derived numbers come from the renderer (tune-sysctl.sh), which owns them and
# persists them into /etc/sysctl.d/99-vless-tuning.conf. This script only sets
# what a .conf file cannot express: per-queue RPS/XPS masks, IRQ affinity and
# the flow_limit CPU bitmap.
# ---------------------------------------------------------------------------
FACTS_ENV="/opt/monitoring/configs/tuning-facts.env"
# shellcheck source=/dev/null
[ -r "$FACTS_ENV" ] && . "$FACTS_ENV"

# The kernel silently rejects a non-power-of-two rps_flow_cnt. The old code did a
# plain division, so on a 40-queue NIC RFS was quietly disabled.
pow2_floor() {
    local n=${1:-0} p=1
    [ "$n" -lt 1 ] && { echo 1; return; }
    while [ $((p * 2)) -le "$n" ]; do p=$((p * 2)); done
    echo "$p"
}

# flow_limit needs a CPU bitmap, which cannot be set from a .conf file, so this
# one value legitimately stays here. Its companion net.core.flow_limit_table_len
# IS in the rendered sysctl file; without both, a single flood flow can occupy an
# entire per-CPU backlog queue.
configure_flow_limit() {
    local mask=${FLOW_LIMIT_CPU_BITMAP:-}
    [ -n "$mask" ] || return 0
    [ -e /proc/sys/net/core/flow_limit_cpu_bitmap ] || return 0
    echo "$mask" > /proc/sys/net/core/flow_limit_cpu_bitmap 2>/dev/null || true
}

get_rps_mask() {
    local cpu_count=$1
    local skip_from=$2
    awk -v c="$cpu_count" -v skip="$skip_from" 'BEGIN {
        hex=""
        for (i=0; i<c; i+=4) {
            val = 0
            if (i+0 < c && i+0 >= skip) val += 1
            if (i+1 < c && i+1 >= skip) val += 2
            if (i+2 < c && i+2 >= skip) val += 4
            if (i+3 < c && i+3 >= skip) val += 8
            hex = sprintf("%x", val) hex
        }
        print hex
    }'
}

cpu_index_mask() {
    local cpu_idx=$1
    awk -v idx="$cpu_idx" 'BEGIN {
        hex=""
        block = int(idx / 4)
        bit  = idx % 4
        val  = 0
        if (bit == 0) val = 1
        if (bit == 1) val = 2
        if (bit == 2) val = 4
        if (bit == 3) val = 8
        for (i=0; i<block; i++) hex = "0" hex
        hex = sprintf("%x", val) hex
        print hex
    }'
}

# The renderer computes nf_conntrack_max and the hashsize from one formula and
# persists the hashsize into /etc/modprobe.d/nf_conntrack.conf. This function
# used to recompute it, and the three tune scripts disagreed: network/multiqueue
# floored it at 524288 while hybrid floored it at conntrack_max/8, so the same
# profile got a different table depending only on which NIC mode was chosen.
# What remains is raising a live table that is still smaller than intended.
configure_conntrack() {
    local hashsize_file="/sys/module/nf_conntrack/parameters/hashsize"

    [ -f "$hashsize_file" ] || modprobe nf_conntrack 2>/dev/null || true
    sleep 0.5
    [ -f "$hashsize_file" ] || return 0

    local want=${CONNTRACK_HASHSIZE:-0}
    [ "$want" -gt 0 ] 2>/dev/null || return 0

    local current
    current=$(cat "$hashsize_file" 2>/dev/null || echo 0)
    if [ "${current:-0}" -lt "$want" ] 2>/dev/null; then
        echo "$want" > "$hashsize_file" 2>/dev/null || true
    fi
}

parse_channels() {
    local iface=$1
    local out_cb_max=$2 out_rx_max=$3 out_tx_max=$4
    local out_cb_cur=$5 out_rx_cur=$6 out_tx_cur=$7

    local parsed
    parsed=$(ethtool -l "$iface" 2>/dev/null | awk '
        /Pre-set maximums:/ { sect="max"; next }
        /Current hardware/  { sect="cur"; next }
        sect=="max" && /^RX:/       { mrx=$2 }
        sect=="max" && /^TX:/       { mtx=$2 }
        sect=="max" && /^Combined:/ { mcb=$2 }
        sect=="cur" && /^RX:/       { crx=$2 }
        sect=="cur" && /^TX:/       { ctx=$2 }
        sect=="cur" && /^Combined:/ { ccb=$2 }
        END { print mcb"|"mrx"|"mtx"|"ccb"|"crx"|"ctx }
    ')
    IFS='|' read -r _cb_max _rx_max _tx_max _cb_cur _rx_cur _tx_cur <<<"$parsed"

    eval "$out_cb_max=\$_cb_max"
    eval "$out_rx_max=\$_rx_max"
    eval "$out_tx_max=\$_tx_max"
    eval "$out_cb_cur=\$_cb_cur"
    eval "$out_rx_cur=\$_rx_cur"
    eval "$out_tx_cur=\$_tx_cur"
}

is_pos_int() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -gt 0 ]; }

configure_multiqueue() {
    local iface=$1
    if ! command -v ethtool &>/dev/null; then
        echo 0; return 0
    fi

    local cb_max rx_max tx_max cb_cur rx_cur tx_cur
    parse_channels "$iface" cb_max rx_max tx_max cb_cur rx_cur tx_cur

    if is_pos_int "$cb_max" && [ "$cb_max" -gt 1 ]; then
        if is_pos_int "$cb_cur" && [ "$cb_cur" -lt "$cb_max" ]; then
            if ethtool -L "$iface" combined "$cb_max" 2>/dev/null; then
                echo "  $iface: combined channels $cb_cur -> $cb_max" >&2
            fi
        else
            echo "  $iface: combined channels already at max (${cb_cur:-?})" >&2
        fi
        echo "$cb_max"; return 0
    fi

    if is_pos_int "$rx_max" && is_pos_int "$tx_max" && { [ "$rx_max" -gt 1 ] || [ "$tx_max" -gt 1 ]; }; then
        local target=$rx_max
        [ "$tx_max" -lt "$target" ] && target=$tx_max

        local need_apply=false
        is_pos_int "$rx_cur" && [ "$rx_cur" -lt "$target" ] && need_apply=true
        is_pos_int "$tx_cur" && [ "$tx_cur" -lt "$target" ] && need_apply=true

        if $need_apply && ethtool -L "$iface" rx "$target" tx "$target" 2>/dev/null; then
            echo "  $iface: rx/tx channels ${rx_cur:-?}/${tx_cur:-?} -> $target/$target" >&2
        else
            echo "  $iface: rx/tx channels already at max (${rx_cur:-?}/${tx_cur:-?})" >&2
        fi
        echo "$target"; return 0
    fi

    local rx_count
    rx_count=$(find "/sys/class/net/$iface/queues" -maxdepth 1 -name 'rx-*' -type d 2>/dev/null | wc -l)
    [ "$rx_count" -gt 1 ] && { echo "$rx_count"; return 0; }

    echo 0
}

configure_rps_remaining() {
    local iface=$1
    local hw_queues=$2
    local cpu_count
    cpu_count=$(nproc)

    if [ "$cpu_count" -le "$hw_queues" ] 2>/dev/null; then
        echo "  $iface: RPS skipped (cpu_count=$cpu_count <= hw_queues=$hw_queues)" >&2
        return 0
    fi

    local rps_mask
    rps_mask=$(get_rps_mask "$cpu_count" "$hw_queues")
    [ -z "$rps_mask" ] && return 0

    # Single owner: net.core.rps_sock_flow_entries lives in the rendered
    # sysctl file. This script used to recompute it as nproc*32768 and
    # overwrite the value at boot, so the file and the script disagreed.
    local flow_entries=${RPS_SOCK_FLOW_ENTRIES:-}
    if [ -z "$flow_entries" ]; then
        flow_entries=$(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 0)
    fi
    [ "${flow_entries:-0}" -gt 0 ] 2>/dev/null || return 0

    local queue_dir="/sys/class/net/$iface/queues"
    local rx_queues
    rx_queues=$(ls -d "$queue_dir"/rx-* 2>/dev/null | wc -l)
    [ "$rx_queues" -eq 0 ] && rx_queues=1
    local flow_cnt
    flow_cnt=$(pow2_floor $(( flow_entries / rx_queues )))

    for rx_dir in "$queue_dir"/rx-*; do
        [ -d "$rx_dir" ] || continue
        echo "$rps_mask" > "$rx_dir/rps_cpus" 2>/dev/null || true
        echo "$flow_cnt" > "$rx_dir/rps_flow_cnt" 2>/dev/null || true
    done

    echo "  $iface: RPS mask 0x$rps_mask (CPUs $hw_queues-$((cpu_count - 1)))" >&2
}

configure_xps() {
    local iface=$1
    local cpu_count
    cpu_count=$(nproc)
    local queue_dir="/sys/class/net/$iface/queues"
    local queue_num=0

    for tx_dir in "$queue_dir"/tx-*; do
        [ -d "$tx_dir" ] && [ -f "$tx_dir/xps_cpus" ] || continue
        local cpu_idx=$(( queue_num % cpu_count ))
        cpu_index_mask "$cpu_idx" > "$tx_dir/xps_cpus" 2>/dev/null || true
        queue_num=$((queue_num + 1))
    done
}

configure_irq_affinity() {
    local iface=$1
    pgrep -x irqbalance &>/dev/null && return 0

    local irqs
    irqs=$(grep "$iface" /proc/interrupts 2>/dev/null | awk -F: '{print $1}' | tr -d ' ')
    [ -z "$irqs" ] && return 0

    local cpu_count
    cpu_count=$(nproc)
    local cpu_idx=0

    for irq in $irqs; do
        if [ -f "/proc/irq/$irq/smp_affinity" ]; then
            cpu_index_mask "$cpu_idx" > "/proc/irq/$irq/smp_affinity" 2>/dev/null || true
            cpu_idx=$(( (cpu_idx + 1) % cpu_count ))
        fi
    done
}

is_safe_interface() {
    local dev_path=$1
    [ -d "$dev_path/device" ] || return 1
    [ -f "$dev_path/bonding/slaves" ] && return 1
    [ -e "$dev_path/master" ] && return 1
    [ -d "$dev_path/bridge" ] && return 1
    [ "$(cat "$dev_path/operstate" 2>/dev/null)" = "up" ] || return 1
    [ "$(cat "$dev_path/carrier" 2>/dev/null)" = "1" ] || return 1
    return 0
}

main() {
    configure_conntrack
    configure_flow_limit

    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue

        local iface
        iface=$(basename "$dev_path")
        local hw_queues

        hw_queues=$(configure_multiqueue "$iface")
        configure_xps "$iface"
        configure_irq_affinity "$iface"
        configure_rps_remaining "$iface" "${hw_queues:-0}"
    done

    echo "=== Hybrid NIC Tuning Summary ==="
    echo "CPU cores: $(nproc)"
    echo "Conntrack max: $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'N/A')"
    echo "Conntrack hashsize: $(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 'N/A')"
    echo "tcp_mem (pages): $(sysctl -n net.ipv4.tcp_mem 2>/dev/null || echo 'N/A')  [set by tune-sysctl.sh]"
    echo "min_free_kbytes: $(sysctl -n vm.min_free_kbytes 2>/dev/null || echo 'N/A')"
    echo "RPS flow entries: $(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 'N/A')"
    echo "Configured interfaces:"
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue
        local iface
        iface=$(basename "$dev_path")

        local cb_max rx_max tx_max cb_cur rx_cur tx_cur
        parse_channels "$iface" cb_max rx_max tx_max cb_cur rx_cur tx_cur
        local hw="N/A"
        if is_pos_int "$cb_cur"; then
            hw=$cb_cur
        elif is_pos_int "$rx_cur"; then
            hw=$rx_cur
            is_pos_int "$tx_cur" && [ "$tx_cur" -lt "$hw" ] && hw=$tx_cur
        fi

        local queue_dir="/sys/class/net/$iface/queues"
        local first_rps=""
        for rx_dir in "$queue_dir"/rx-*; do
            [ -d "$rx_dir" ] && first_rps=$(cat "$rx_dir/rps_cpus" 2>/dev/null) && break
        done
        echo "  - $iface (hw queues: $hw, rps_cpus: 0x${first_rps:-0})"
    done
}

main "$@"
