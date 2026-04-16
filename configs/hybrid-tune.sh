#!/bin/bash
#
# Hybrid NIC Tuning Script
# Hardware multi-queue + software RPS on remaining cores
# Use when NIC hardware queues < CPU cores (e.g. I225-V with 4 queues on 12-core host)
# Hardware dispatches to first N cores via IRQ, RPS steers softirq work to the rest
#

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

configure_conntrack() {
    local hashsize_file="/sys/module/nf_conntrack/parameters/hashsize"

    [ -f "$hashsize_file" ] || modprobe nf_conntrack 2>/dev/null || true
    sleep 0.5

    if [ -f "$hashsize_file" ]; then
        local conntrack_max
        conntrack_max=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 2097152)
        local ideal_hashsize=$(( conntrack_max / 4 ))

        local min_hashsize=$(( conntrack_max / 8 ))
        [ $min_hashsize -lt 16384 ] && min_hashsize=16384
        [ $ideal_hashsize -lt $min_hashsize ] && ideal_hashsize=$min_hashsize
        [ $ideal_hashsize -gt 2097152 ] && ideal_hashsize=2097152

        local current
        current=$(cat "$hashsize_file" 2>/dev/null || echo 0)
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

# Sets combined channels to hardware max and returns the applied value on stdout
configure_multiqueue() {
    local iface=$1
    if ! command -v ethtool &>/dev/null; then
        echo 0
        return 0
    fi

    local max_combined current_combined
    max_combined=$(ethtool -l "$iface" 2>/dev/null | awk '/Pre-set maximums/,/Current/ { if (/Combined:/) print $2 }' | head -1)
    if [ -z "$max_combined" ] || ! [ "$max_combined" -gt 1 ] 2>/dev/null; then
        echo "${max_combined:-0}"
        return 0
    fi

    current_combined=$(ethtool -l "$iface" 2>/dev/null | awk '/Current hardware/,0 { if (/Combined:/) print $2 }' | head -1)

    if [ -n "$current_combined" ] && [ "$current_combined" -lt "$max_combined" ] 2>/dev/null; then
        ethtool -L "$iface" combined "$max_combined" 2>/dev/null || true
        echo "  $iface: combined channels $current_combined -> $max_combined" >&2
    else
        echo "  $iface: combined channels already at max ($current_combined)" >&2
    fi

    echo "$max_combined"
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
        local xps_mask=$(( 1 << cpu_idx ))
        printf "%x" $xps_mask > "$tx_dir/xps_cpus" 2>/dev/null || true
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

        local iface
        iface=$(basename "$dev_path")
        local hw_queues

        configure_ring_buffer "$iface"
        hw_queues=$(configure_multiqueue "$iface")
        configure_xps "$iface"
        configure_irq_affinity "$iface"
        configure_rps_remaining "$iface" "${hw_queues:-0}"
    done

    echo "=== Hybrid NIC Tuning Summary ==="
    echo "CPU cores: $(nproc)"
    echo "Conntrack max: $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'N/A')"
    echo "Conntrack hashsize: $(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 'N/A')"
    echo "RPS flow entries: $(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 'N/A')"
    echo "Configured interfaces:"
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_real_interface "$dev_path" || continue
        local iface
        iface=$(basename "$dev_path")
        local channels
        channels=$(ethtool -l "$iface" 2>/dev/null | awk '/Current hardware/,0 { if (/Combined:/) print $2 }' | head -1)
        local queue_dir="/sys/class/net/$iface/queues"
        local first_rps=""
        for rx_dir in "$queue_dir"/rx-*; do
            [ -d "$rx_dir" ] && first_rps=$(cat "$rx_dir/rps_cpus" 2>/dev/null) && break
        done
        echo "  - $iface (combined: ${channels:-N/A}, rps_cpus: 0x${first_rps:-0})"
    done
}

main "$@"
