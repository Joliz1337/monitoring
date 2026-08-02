# Optimisation profile

The profile selects network stack **behaviour**; the actual numbers are computed by the node from its memory and core count.

| Profile | For what | Difference |
|---|---|---|
| VPN | Nodes carrying transit traffic: Xray, WireGuard, proxies | Short connection-tracking timeouts so the table isn't filled with dead entries from thousands of short sessions |
| Panel | The panel server and ordinary applications | Long timeouts: outgoing connections are long-lived and must not be cut early |

Both profiles raise descriptor limits, socket buffers, the accept queue and connection tracking parameters alike — but proportionally to the hardware. A 4 GB node gets a far smaller connection table than a 64 GB one, because every entry costs memory.

## How to choose

Simple rule: client traffic passes through the server — take VPN. It's a panel, a database or a plain web app — take Panel.

## Good to know

- Getting it wrong is harmless: the profile can be changed at any time and values are recomputed.
- An oversized connection table on a small node is more dangerous than an undersized one: it eats memory and leads to out-of-memory conditions under load. That's why there are no fixed "maximum" values here.
- To verify what was applied, look on the node: values live in the kernel settings file and in the tuning facts, which HAProxy and nginx read when computing their own limits.
