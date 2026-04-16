#!/bin/bash
#
# Multi-queue Network Tuning Script
# Enables hardware multiqueue (combined channels), IRQ affinity, XPS, ring buffers, conntrack
# For NICs with hardware multi-queue support — RPS not needed
#

configure_conntrack() {
    local hashsize_file="/sys/module/nf_conntrack/parameters/hashsize"

    [ -f "$hashsize_file" ] || modprobe nf_conntrack 2>/dev/null || true
    sleep 0.5

    if [ -f "$hashsize_file" ]; then
        local conntrack_max=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 2097152)
        local ideal_hashsize=$(( conntrack_max / 4 ))

        local min_hashsize=$(( conntrack_max / 8 ))
        [ $min_hashsize -lt 16384 ] && min_hashsize=16384
        [ $ideal_hashsize -lt $min_hashsize ] && ideal_hashsize=$min_hashsize
        [ $ideal_hashsize -gt 2097152 ] && ideal_hashsize=2097152

        local current=$(cat "$hashsize_file" 2>/dev/null || echo 0)
        [ "$current" -lt "$ideal_hashsize" ] && echo "$ideal_hashsize" > "$hashsize_file" 2>/dev/null || true
    fi

    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        sysctl -p /etc/sysctl.d/99-vless-tuning.conf >/dev/null 2>&1 || true
    fi
}

configure_ring_buffer() {
    local iface=$1
    command -v ethtool &>/dev/null || return 0
    ethtool -g "$iface" &>/dev/null || return 0
    ethtool -G "$iface" rx 4096 2>/dev/null || true
    ethtool -G "$iface" tx 4096 2>/dev/null || true
}

configure_multiqueue() {
    local iface=$1
    command -v ethtool &>/dev/null || return 0

    local max_combined current_combined
    max_combined=$(ethtool -l "$iface" 2>/dev/null | awk '/Pre-set maximums/,/Current/ { if (/Combined:/) print $2 }' | head -1)
    [ -z "$max_combined" ] || [ "$max_combined" -le 1 ] 2>/dev/null && return 0

    current_combined=$(ethtool -l "$iface" 2>/dev/null | awk '/Current hardware/,0 { if (/Combined:/) print $2 }' | head -1)

    if [ -n "$current_combined" ] && [ "$current_combined" -lt "$max_combined" ] 2>/dev/null; then
        ethtool -L "$iface" combined "$max_combined" 2>/dev/null || true
        echo "  $iface: combined channels $current_combined -> $max_combined"
    else
        echo "  $iface: combined channels already at max ($current_combined)"
    fi
}

configure_xps() {
    local iface=$1
    local cpu_count=$(nproc)
    local queue_dir="/sys/class/net/$iface/queues"
    local queue_num=0

    for tx_dir in "$queue_dir"/tx-*; do
        [ -d "$tx_dir" ] && [ -f "$tx_dir/xps_cpus" ] || continue
        local cpu_idx=$(( queue_num % cpu_count ))
        local xps_mask=$(( 1 << cpu_idx ))
        printf "%x" $xps_mask > "$tx_dir/xps_cpus" 2>/dev/null || true
        queue_num=$((queue_num + 1))
    done
}

configure_irq_affinity() {
    local iface=$1
    pgrep -x irqbalance &>/dev/null && return 0

    local irqs=$(grep "$iface" /proc/interrupts 2>/dev/null | awk -F: '{print $1}' | tr -d ' ')
    [ -z "$irqs" ] && return 0

    local cpu_count=$(nproc)
    local cpu_idx=0

    for irq in $irqs; do
        if [ -f "/proc/irq/$irq/smp_affinity" ]; then
            local mask=$(( 1 << cpu_idx ))
            printf "%x" $mask > "/proc/irq/$irq/smp_affinity" 2>/dev/null || true
            cpu_idx=$(( (cpu_idx + 1) % cpu_count ))
        fi
    done
}

is_real_interface() {
    local dev_path=$1
    [ -d "$dev_path/device" ] || return 1
    [ -f "$dev_path/bonding/slaves" ] && return 1
    [ -d "$dev_path/bridge" ] && return 1
    return 0
}

main() {
    configure_conntrack

    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_real_interface "$dev_path" || continue

        local iface=$(basename "$dev_path")

        configure_ring_buffer "$iface"
        configure_multiqueue "$iface"
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
        is_real_interface "$dev_path" || continue
        local iface=$(basename "$dev_path")
        local channels
        channels=$(ethtool -l "$iface" 2>/dev/null | awk '/Current hardware/,0 { if (/Combined:/) print $2 }' | head -1)
        echo "  - $iface (combined: ${channels:-N/A})"
    done
}

main "$@"
