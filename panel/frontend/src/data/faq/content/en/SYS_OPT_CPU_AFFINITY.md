# Split workloads off network cores

The network card announces incoming packets through interrupts, and those land on one or two cores rather than all of them. Those cores process the entire inbound stream. When HAProxy or Xray run on the same cores, they set the throughput ceiling while the rest of the machine idles at half load.

This setting keeps workloads off the network cores. Networking gets them to itself, the application gets everything else.

Off by default: the gain depends on what a given node actually saturates, and an enabled setting takes a core away from the application entirely.

## What happens when you enable it

Nothing has to be edited by hand — neither the HAProxy configuration nor any container files.

The node detects its network cores from interrupt activity and takes it from there:

- **HAProxy** gets `nbthread` and `cpu-map` lines in its `global` section — they define how many threads to run and which cores to keep them on. The node writes them the next time the HAProxy configuration is applied, and removes them the same way when the setting is turned off.
- **Remnawave containers** (`remnanode`, `remnawave-nginx`) get a `cpuset` — the list of cores they may use. This applies immediately and is reconfirmed every few minutes, so it survives container recreation and Remnawave updates.

Core numbers are never stored or entered by hand: the hypervisor picks them, and they differ between servers — 0 and 1 on one, 5 and 7 on another.

## When to enable it

Check per-core load on the node: `mpstat -P ALL 1 10`.

- One or two cores sit well above the rest, especially in the `%soft` column — worth enabling.
- All cores loaded evenly — no effect: the application saturates the CPU as a whole, not a single core.
- Load is low — the ceiling has not been reached, so there is nothing to gain.

## Confirming it works

Run `mpstat -P ALL 1 10` again after enabling: the network core's `%usr` column should drop — the application has left it, and only packet processing remains. Total load barely changes, since the work did not disappear, it moved to other cores.

Container pinning is visible via `docker inspect -f '{{.HostConfig.CpusetCpus}}' remnanode`, and HAProxy threads via `ps -eLo psr,comm | grep haproxy`.

## When the setting is not applied

- Every core carries network interrupts — with full hardware queues there is nothing to split.
- The application would be left with fewer than half the cores.
- The server has fewer than four cores.
- `irqbalance` is running: it moves interrupts between cores, so any fixed pinning drifts out of sync.
- No cores could be identified — for instance, the interface has no interrupts of its own.

## Fine-tuning

The `# cpu-affinity (auto)` line in the HAProxy `global` section overrides the behaviour on a single node. Instead of `auto` you may list the network cores explicitly — `0,1` or `0-2` — or write `off` to disable pinning on that server only. Without the line the node works the cores out on its own, and in normal use it needs no attention.
