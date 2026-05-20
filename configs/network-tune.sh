#!/bin/bash
#
# Network Tuning Script - RPS/RFS/XPS/Conntrack
# Software-only path: hardware NIC has 1 queue, all RX work moved to softirq
# across all CPUs via RPS.
#
# Safety: skips link-down and bond/bridge slaves. Does NOT touch ring buffers
# (ethtool -G causes link reset on igb/ixgbe — on OVH edge that can kill SSH).
#

# CPU mask via awk (works for any core count including 64+)
get_cpu_mask() {
    local cpu_count
    cpu_count=$(nproc)
    awk -v c="$cpu_count" 'BEGIN {
        hex=""
        for (i=0; i<c; i+=4) {
            val = 0
            if (i+0 < c) val += 1
            if (i+1 < c) val += 2
            if (i+2 < c) val += 4
            if (i+3 < c) val += 8
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

configure_rps_rfs() {
    local iface=$1
    local cpu_mask
    cpu_mask=$(get_cpu_mask)
    local cpu_count
    cpu_count=$(nproc)
    local entries=32768
    local flow_entries=$(( cpu_count * entries ))

    [ $flow_entries -gt 2097152 ] && flow_entries=2097152

    echo "$flow_entries" > /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || true

    local queue_dir="/sys/class/net/$iface/queues"
    local rx_queues
    rx_queues=$(ls -d "$queue_dir"/rx-* 2>/dev/null | wc -l)
    [ "$rx_queues" -eq 0 ] && rx_queues=1
    local flow_cnt=$(( flow_entries / rx_queues ))

    for rx_dir in "$queue_dir"/rx-*; do
        [ -d "$rx_dir" ] || continue
        echo "$cpu_mask" > "$rx_dir/rps_cpus" 2>/dev/null || true
        echo "$flow_cnt" > "$rx_dir/rps_flow_cnt" 2>/dev/null || true
    done
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

    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue

        local iface
        iface=$(basename "$dev_path")

        configure_rps_rfs "$iface"
        configure_xps "$iface"
        configure_irq_affinity "$iface"
    done

    echo "=== Network Tuning Summary ==="
    echo "CPU cores: $(nproc)"
    echo "CPU mask: 0x$(get_cpu_mask)"
    echo "RPS flow entries: $(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 'N/A')"
    echo "Conntrack max: $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'N/A')"
    echo "Conntrack hashsize: $(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 'N/A')"
    echo "Configured interfaces:"
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_safe_interface "$dev_path" || continue
        echo "  - $(basename "$dev_path")"
    done
}

main "$@"
