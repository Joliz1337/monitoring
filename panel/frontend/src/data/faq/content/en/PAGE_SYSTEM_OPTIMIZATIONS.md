# System optimisations

Kernel and network tuning: values are computed for each specific server rather than taken from a fixed list.

## What's on the page

- Per node: the applied profile, the network card mode and the tuning version.
- Applying a profile to one node or to all of them.
- Switching the network card mode.
- Removing optimisations, reverting to distribution defaults.

## How values are computed

Nodes differ: one has 2 GB of memory, another 248. So connection limits, buffer sizes and descriptor limits are calculated on the node itself from its memory, core count and network parameters. The profile defines behaviour, not numbers.

Recalculation also happens on every boot — resize the VPS and the limits catch up after a reboot without the panel's involvement.

## Good to know

- The previous configuration is preserved, so removing optimisations restores the original state.
- Applying requires a recent agent: an outdated node is refused with a clear message — update it first.
- Resizing network card ring buffers is deliberately avoided: on some providers that operation drops the link and the server becomes unreachable.
- Values you want to override manually go into a separate file on the node that re-applying never overwrites.
