#!/bin/bash
#
# Network Tuning Script - RPS/RFS/XPS/Conntrack
# Optimized for 50000+ VPN clients, DNAT/HAProxy relay
# Supports 64+ cores, filters virtual/bonding/bridge interfaces
#

# CPU mask via awk (works for any core count including 64+)
get_cpu_mask() {
    local cpu_count=$(nproc)
    awk -v c=$cpu_count 'BEGIN {
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

configure_conntrack() {
    local hashsize_file="/sys/module/nf_conntrack/parameters/hashsize"
    
    [ -f "$hashsize_file" ] || modprobe nf_conntrack 2>/dev/null || true
    sleep 0.5
    
    if [ -f "$hashsize_file" ]; then
        local conntrack_max=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 2097152)
        local ideal_hashsize=$(( conntrack_max / 4 ))
        
        [ $ideal_hashsize -lt 524288 ] && ideal_hashsize=524288
        [ $ideal_hashsize -gt 2097152 ] && ideal_hashsize=2097152
        
        local current=$(cat "$hashsize_file" 2>/dev/null || echo 0)
        [ "$current" -lt "$ideal_hashsize" ] && echo "$ideal_hashsize" > "$hashsize_file" 2>/dev/null || true
    fi
    
    # Re-apply sysctl conntrack params after module load (critical for boot-time)
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

configure_rps_rfs() {
    local iface=$1
    local cpu_mask=$(get_cpu_mask)
    local cpu_count=$(nproc)
    local entries=32768
    local flow_entries=$(( cpu_count * entries ))
    
    [ $flow_entries -gt 2097152 ] && flow_entries=2097152
    
    # Global RPS flow entries
    echo "$flow_entries" > /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || true
    
    # Per-queue RPS/RFS
    local queue_dir="/sys/class/net/$iface/queues"
    local rx_queues=$(ls -d "$queue_dir"/rx-* 2>/dev/null | wc -l)
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
    
    # Must have /device (excludes lo, tun, tap, veth)
    [ -d "$dev_path/device" ] || return 1
    
    # Skip bonding
    [ -f "$dev_path/bonding/slaves" ] && return 1
    
    # Skip bridge
    [ -d "$dev_path/bridge" ] && return 1
    
    return 0
}

main() {
    # Conntrack hashsize
    configure_conntrack
    
    # Process all real interfaces
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_real_interface "$dev_path" || continue
        
        local iface=$(basename "$dev_path")
        
        configure_ring_buffer "$iface"
        configure_rps_rfs "$iface"
        configure_xps "$iface"
        configure_irq_affinity "$iface"
    done
    
    # Summary
    echo "=== Network Tuning Summary ==="
    echo "CPU cores: $(nproc)"
    echo "CPU mask: 0x$(get_cpu_mask)"
    echo "RPS flow entries: $(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 'N/A')"
    echo "Conntrack max: $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'N/A')"
    echo "Conntrack hashsize: $(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo 'N/A')"
    echo "Configured interfaces:"
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        is_real_interface "$dev_path" || continue
        echo "  - $(basename "$dev_path")"
    done
}

main "$@"
