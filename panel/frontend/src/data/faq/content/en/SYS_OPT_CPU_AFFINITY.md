# Split workloads off network cores

Network card interrupts land on one or two cores. When HAProxy or Xray run on those same cores, throughput saturates there while the rest idle. This setting moves the workloads onto free cores, leaving the network cores to packet processing alone.

Off by default: the gain depends on what a given node actually saturates, and a core reserved for networking is taken away from the application entirely.

## When to enable it

Check per-core load on the node: `mpstat -P ALL 1 10`.

- One core sits well above the rest, especially in the `%soft` column — worth enabling.
- All cores loaded evenly — no effect; the application saturates the CPU as a whole.
- Load is low — the ceiling has not been reached, so there is nothing to gain.

## Good to know

- Network core numbers depend on the hypervisor and vary: 0 and 1 on one server, 5 and 7 on another. The node detects them from interrupt activity, so nothing has to be set by hand.
- Applies to HAProxy and to the Remnawave containers. Container pinning is reconfirmed regularly, so it survives their recreation and updates.
- Not applied when every core carries network interrupts — with full hardware queues there is nothing to split.
- Not applied on servers with fewer than four cores, or when networking would claim more than half the machine.
- Not applied while `irqbalance` is running: it moves interrupts between cores, so any fixed pinning drifts out of sync.
- Disabling unpins the containers immediately; HAProxy is unpinned the next time its configuration is applied.
- Measured on live nodes: network core load dropped from 65% to 24% and from 58% to 15%, with no rise in total CPU usage.
