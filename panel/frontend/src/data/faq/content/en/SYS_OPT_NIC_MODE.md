# Network card mode

Defines how incoming packet processing is spread across cores. Untuned, interrupts often land on a single core, and it saturates long before the network does.

| Mode | When it fits |
|---|---|
| Hardware queues | The card supports multiple queues and has at least as many as there are cores. The most efficient option |
| Hybrid | Fewer queues than cores (common on desktop Intel chips). Hardware queues on some cores, software distribution on the rest |
| Software | The card has no queues at all. Works everywhere, but processing costs more |
| Auto-detect | The node checks how many queues the card supports and picks the mode |

## Good to know

- To see the queue count on a node: `ethtool -l <interface>`, the Combined line under preset maximums.
- The modes are mutually exclusive: switching removes the previous configuration automatically.
- Settings are re-applied on every boot, so they survive reboots and plan changes.
- Resizing the card's ring buffers is deliberately not used: on some providers that command resets the link and the server loses networking.
- The effect shows in per-core load: one core buried in soft interrupts before, spread out after.
