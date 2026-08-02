# Time synchronisation

Sets one timezone across all nodes and enables network clock sync.

## How it works

Once a day the panel walks the active nodes: sets the chosen timezone, enables automatic clock synchronisation and forces a sync. The same happens right after a node is added and whenever the timezone setting changes. You can also trigger it manually.

## Why it matters

- Logs from different nodes are only comparable when their clocks agree.
- Clock drift breaks TLS certificates, one-time codes and request signatures.
- Charts and alerts are built on time: drifted clocks create phantom gaps and spikes.

## Good to know

- Some providers block network time sync and the system service silently fails — here you see its real state per node.
- Panel timezone and server timezone are different settings: the first only changes how dates are displayed.
- A few seconds of drift is invisible on charts but critical for signature and one-time-code validation, where the tolerance is tens of seconds.
