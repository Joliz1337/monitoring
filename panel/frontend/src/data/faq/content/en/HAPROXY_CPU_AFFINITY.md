# Cores reserved for networking

On a server without hardware queues, every network card interrupt lands on one or two cores. If HAProxy threads occupy those same cores, throughput saturates there while the rest idle. The `# cpu-affinity (auto)` line in the `global` section tells the node to keep them apart.

| Value | What it does |
|---|---|
| `auto` | The node finds the cores handling card interrupts and gives HAProxy all the others |
| `0,1` or `0-2` | Manual list of cores to leave for networking |
| `off` | Change nothing |

## Good to know

- Core numbers depend on the hypervisor: 0 and 1 on one server, 5 and 7 on another. That is why the template ships `auto` rather than fixed numbers.
- The node reads actual interrupt counters from `/proc/interrupts`, and on a freshly created server falls back to vector affinity — so it works right after installation.
- Delete the line entirely and the node leaves core distribution alone.
- Not applied on servers with fewer than four cores, or when networking would claim more than half the machine.
- Not applied while `irqbalance` is running: it moves interrupts between cores, so any fixed pinning drifts out of sync.
- To see per-core load on a node: `mpstat -P ALL 1 10` — one core sits well above the rest before tuning, and the gap disappears after.
