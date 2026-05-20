#!/bin/bash
#
# Multi-queue Network Tuning Script
# Enables hardware multiqueue (combined channels), XPS, IRQ affinity, conntrack
#
# Safety notes:
#   - Skips interfaces without carrier (link DOWN) and bond/bridge slaves
#   - Does NOT touch ring buffers (ethtool -G): on igb/ixgbe/i40e it causes a
#     hard link reset which on OVH edge can trigger port-security and kill SSH.
#     Resize ring buffers manually if you really need it.
#

configure_conntrack() {
    local hashsize_file="/sys/module/nf_conntrack/parameters/hashsize"

    [ -f "$hashsize_file" ] || modprobe nf_conntrack 2>/dev/null || true
    sleep 0.5

    [ -f "$hashsize_file" ] || return 0

    local conntrack_max ideal_hashsize current
    conntrack_max=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 2097152)
    ideal_hashsize=$(( conntrack_max / 4 ))

    [ $ideal_hashsize -lt 524288 ] && ideal_hashsize=524288
    [ $ideal_hashsize -gt 2097152 ] && ideal_hashsize=2097152

    current=$(cat "$hashsize_file" 2>/dev/null || echo 0)
    [ "$current" -lt "$ideal_hashsize" ] && echo "$ideal_hashsize" > "$hashsize_file" 2>/dev/null || true
}

# Compute hex mask for a single CPU index (safe for >32 CPUs)
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
        # Build hex right-to-left: leading zeros for higher blocks, then the active block
        for (i=0; i<block; i++) hex = "0" hex
        hex = sprintf("%x", val) hex
        print hex
    }'
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

# Prints applied hw-queue count on stdout
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
                echo "$cb_max"; return 0
            fi
        fi
        echo "  $iface: combined channels already at max (${cb_cur:-?})" >&2
        echo "${cb_cur:-$cb_max}"; return 0
    fi

    if is_pos_int "$rx_max" && is_pos_int "$tx_max" && { [ "$rx_max" -gt 1 ] || [ "$tx_max" -gt 1 ]; }; then
        local target=$rx_max
        [ "$tx_max" -lt "$target" ] && target=$tx_max

        local need_apply=false
        is_pos_int "$rx_cur" && [ "$rx_cur" -lt "$target" ] && need_apply=true
        is_pos_int "$tx_cur" && [ "$tx_cur" -lt "$target" ] && need_apply=true

        if $need_apply; then
            if ethtool -L "$iface" rx "$target" tx "$target" 2>/dev/null; then
                echo "  $iface: rx/tx channels ${rx_cur:-?}/${tx_cur:-?} -> $target/$target" >&2
                echo "$target"; return 0
            fi
        fi
        echo "  $iface: rx/tx channels already at max (${rx_cur:-?}/${tx_cur:-?})" >&2
        echo "$target"; return 0
    fi

    local rx_count
    rx_count=$(find "/sys/class/net/$iface/queues" -maxdepth 1 -name 'rx-*' -type d 2>/dev/null | wc -l)
    [ "$rx_count" -gt 1 ] && { echo "$rx_count"; return 0; }

    echo 0
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

# Process only physical, link-up, non-slave interfaces
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

get_current_hw_queues() {
    local iface=$1
    local cb_max rx_max tx_max cb_cur rx_cur tx_cur
    parse_channels "$iface" cb_max rx_max tx_max cb_cur rx_cur tx_cur

    if is_pos_int "$cb_cur" && [ "$cb_cur" -gt 0 ]; then
        echo "$cb_cur"; return 0
    fi
    local eff=0
    is_pos_int "$rx_cur" && [ "$rx_cur" -gt "$eff" ] && eff=$rx_cur
    is_pos_int "$tx_cur" && [ "$tx_cur" -gt "$eff" ] && eff=$tx_cur
    [ "$eff" -gt 0 ] && { echo "$eff"; return 0; }

    local rx_count
    rx_count=$(find "/sys/class/net/$iface/queues" -maxdepth 1 -name 'rx-*' -type d 2>/dev/null | wc -l)
    [ "$rx_count" -gt 0 ] && { echo "$rx_count"; return 0; }
    echo 0
}

main() {
    configure_conntrack

    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue

        local iface
        iface=$(basename "$dev_path")

        configure_multiqueue "$iface" >/dev/null
        configure_xps "$iface"
        configure_irq_affinity "$iface"
    done

    echo "=== Multi-queue Tuning Summary ==="
    echo "CPU cores: $(nproc)"
    echo "Conntrack max: $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'N/A')"
    echo "Conntrack hashsize: $(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 'N/A')"
    echo "Configured interfaces:"
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue
        local iface
        iface=$(basename "$dev_path")
        local q
        q=$(get_current_hw_queues "$iface")
        echo "  - $iface (hw queues: ${q:-N/A})"
    done
}

main "$@"
