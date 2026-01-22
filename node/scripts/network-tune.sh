#!/bin/bash
#
# Network Tuning Script - RPS/RFS Configuration
# Distributes network load across all CPU cores
#
# This script automatically:
# - Detects the main network interface
# - Configures RPS (Receive Packet Steering) to spread packets across CPUs
# - Configures RFS (Receive Flow Steering) for flow-aware packet distribution
#
# Should run at system startup via systemd service
#

set -e

LOG_TAG="network-tune"

log_info() {
    echo "[INFO] $1"
    logger -t "$LOG_TAG" "INFO: $1" 2>/dev/null || true
}

log_error() {
    echo "[ERROR] $1" >&2
    logger -t "$LOG_TAG" "ERROR: $1" 2>/dev/null || true
}

log_success() {
    echo "[OK] $1"
    logger -t "$LOG_TAG" "OK: $1" 2>/dev/null || true
}

# Get main network interface (the one with default route)
get_main_interface() {
    local iface
    
    # Method 1: Get interface with default route
    iface=$(ip route show default 2>/dev/null | awk '/default/ {print $5}' | head -1)
    
    if [ -n "$iface" ] && [ -d "/sys/class/net/$iface" ]; then
        echo "$iface"
        return 0
    fi
    
    # Method 2: First non-lo interface that is UP
    for iface in $(ls /sys/class/net/ 2>/dev/null); do
        if [ "$iface" != "lo" ] && [ -d "/sys/class/net/$iface" ]; then
            local state=$(cat "/sys/class/net/$iface/operstate" 2>/dev/null)
            if [ "$state" = "up" ]; then
                echo "$iface"
                return 0
            fi
        fi
    done
    
    # Method 3: First non-lo interface (even if down)
    for iface in $(ls /sys/class/net/ 2>/dev/null); do
        if [ "$iface" != "lo" ] && [ -d "/sys/class/net/$iface" ]; then
            echo "$iface"
            return 0
        fi
    done
    
    return 1
}

# Calculate CPU mask for RPS (all CPUs)
get_cpu_mask() {
    local cpu_count=$(nproc)
    local mask=$(( (1 << cpu_count) - 1 ))
    printf "%x\n" $mask
}

# Get number of RX queues for interface
get_rx_queues() {
    local iface=$1
    local queues_dir="/sys/class/net/$iface/queues"
    
    if [ -d "$queues_dir" ]; then
        ls -d "$queues_dir"/rx-* 2>/dev/null | wc -l
    else
        echo "1"
    fi
}

# Configure RPS/RFS for a single interface
configure_interface() {
    local iface=$1
    local cpu_count=$(nproc)
    local cpu_mask=$(get_cpu_mask)
    local flow_entries=$(( cpu_count * 4096 ))
    local rx_queues=$(get_rx_queues "$iface")
    
    log_info "Configuring $iface: CPUs=$cpu_count, mask=0x$cpu_mask, queues=$rx_queues"
    
    # Set global RPS flow entries
    if [ -f /proc/sys/net/core/rps_sock_flow_entries ]; then
        echo "$flow_entries" > /proc/sys/net/core/rps_sock_flow_entries
        log_info "Set rps_sock_flow_entries=$flow_entries"
    fi
    
    # Configure each RX queue
    local flow_cnt=$(( flow_entries / rx_queues ))
    local queue_dir="/sys/class/net/$iface/queues"
    
    for queue in "$queue_dir"/rx-*; do
        if [ -d "$queue" ]; then
            local queue_name=$(basename "$queue")
            
            # Set RPS CPU mask
            if [ -f "$queue/rps_cpus" ]; then
                echo "$cpu_mask" > "$queue/rps_cpus"
                log_info "Set $queue_name/rps_cpus=$cpu_mask"
            fi
            
            # Set RFS flow count per queue
            if [ -f "$queue/rps_flow_cnt" ]; then
                echo "$flow_cnt" > "$queue/rps_flow_cnt"
                log_info "Set $queue_name/rps_flow_cnt=$flow_cnt"
            fi
        fi
    done
    
    log_success "Interface $iface configured for RPS/RFS"
}

# Configure XPS (Transmit Packet Steering) if available
configure_xps() {
    local iface=$1
    local cpu_count=$(nproc)
    local queue_dir="/sys/class/net/$iface/queues"
    
    # XPS: assign each TX queue to corresponding CPU
    local queue_num=0
    for queue in "$queue_dir"/tx-*; do
        if [ -d "$queue" ] && [ -f "$queue/xps_cpus" ]; then
            # Assign to CPU round-robin
            local cpu_idx=$(( queue_num % cpu_count ))
            local xps_mask=$(( 1 << cpu_idx ))
            printf "%x" $xps_mask > "$queue/xps_cpus"
            queue_num=$((queue_num + 1))
        fi
    done
    
    if [ $queue_num -gt 0 ]; then
        log_info "XPS configured for $queue_num TX queues"
    fi
}

# Enable IRQ affinity spreading (if irqbalance not running)
configure_irq_affinity() {
    local iface=$1
    
    # Skip if irqbalance is active (it handles this)
    if pgrep -x irqbalance > /dev/null 2>&1; then
        log_info "irqbalance is running, skipping manual IRQ affinity"
        return 0
    fi
    
    # Find IRQs for this interface
    local irqs=$(grep "$iface" /proc/interrupts 2>/dev/null | awk -F: '{print $1}' | tr -d ' ')
    
    if [ -z "$irqs" ]; then
        return 0
    fi
    
    local cpu_count=$(nproc)
    local cpu_idx=0
    
    for irq in $irqs; do
        if [ -f "/proc/irq/$irq/smp_affinity" ]; then
            local mask=$(( 1 << cpu_idx ))
            printf "%x" $mask > "/proc/irq/$irq/smp_affinity" 2>/dev/null || true
            cpu_idx=$(( (cpu_idx + 1) % cpu_count ))
        fi
    done
    
    log_info "IRQ affinity configured for $iface"
}

# Main function
main() {
    log_info "Starting network tuning..."
    
    # Get main interface
    local main_iface=$(get_main_interface)
    
    if [ -z "$main_iface" ]; then
        log_error "Could not detect main network interface"
        exit 1
    fi
    
    log_info "Detected main interface: $main_iface"
    log_info "CPU count: $(nproc)"
    
    # Configure RPS/RFS
    configure_interface "$main_iface"
    
    # Configure XPS (optional, for TX side)
    configure_xps "$main_iface"
    
    # Configure IRQ affinity (if irqbalance not running)
    configure_irq_affinity "$main_iface"
    
    log_success "Network tuning complete!"
    
    # Show summary
    echo ""
    echo "=== Network Tuning Summary ==="
    echo "Interface: $main_iface"
    echo "CPU cores: $(nproc)"
    echo "RPS CPU mask: 0x$(get_cpu_mask)"
    echo "RPS flow entries: $(cat /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || echo 'N/A')"
    echo ""
}

# Run
main "$@"
