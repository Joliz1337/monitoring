# Split workloads off network cores

The network card announces packets through interrupts, and those land on one or two cores rather than all of them. When HAProxy or Xray run on those same cores, they set the throughput ceiling while the rest of the machine idles. This setting keeps workloads off the network cores: networking gets them to itself, the application gets everything else.

Worth enabling when network cards run in **software** mode — that is where there is a single queue and everything funnels through one core. It only matters on VPN nodes that actually carry traffic.

## When to enable it

The switch is global and takes effect on every server at once, but each node decides on its own whether to apply it, based on how its own card spreads interrupts. Where there is nothing to split, nothing happens: if the card has more than one queue, the load is already spread across cores by the hardware and the node leaves the configuration alone. The same goes for servers with fewer than four cores and while `irqbalance` is running.

To check whether a given server needs it, run `mpstat -P ALL 1 10`: if one or two cores sit well above the rest in the `%soft` column, the setting will help; if load is even across all cores, the bottleneck is the CPU as a whole and there is nothing to gain.

## Good to know

- Neither the HAProxy configuration nor container files need editing by hand: the node writes `nbthread`/`cpu-map` and `cpuset` itself, and removes them the same way when the setting is turned off.
- Core numbers are chosen by the hypervisor and differ between servers — 0 and 1 on one, 5 and 7 on another. They never have to be entered manually.
- Remnawave containers are pinned immediately. HAProxy is pinned the next time its configuration reaches the node: that happens automatically on any profile or rule change, and manually via the “Sync All” button on the HAProxy Configs page. Online nodes receive the configuration on every sync, even when the profile itself has not changed.
- The `# cpu-affinity (auto)` line in the `global` section overrides the behaviour on a single node: instead of `auto` you may list the network cores (`0,1`) or write `off` to disable pinning on that server only.
