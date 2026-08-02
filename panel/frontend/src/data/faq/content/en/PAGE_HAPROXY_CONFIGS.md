# HAProxy profiles

One configuration for many nodes: rules live in a profile, bound servers receive them by sync.

## How it works

1. Create a profile and add rules — either with the builder or by editing the raw config.
2. Bind servers. Binding starts HAProxy on the node automatically.
3. Sync rolls the config out to every bound node in parallel.

Before saving, the panel validates the config with real HAProxy: certificate paths are swapped for dummies, since the real files don't exist on the panel. A config with a syntax error is never saved and never reaches the nodes.

## Server statuses

| Status | Meaning |
|---|---|
| Synced | The config is applied and matches the profile |
| Pending | The node is offline or changes haven't arrived yet. The panel re-syncs a revived node about every half minute |
| Failed | The node rejected the config or is unreachable; the reason is in the sync log |

## Rules

- **Single target** — one destination address. Supports PROXY protocol to the backend, accepting PROXY protocol from an upstream balancer, TLS to the backend and wildcard certificates.
- **Balancer** — a pool of servers with a distribution algorithm, health checks, weights and client stickiness.

## Good to know

- Unbinding a server stops HAProxy on it; the config file stays on disk.
- Edits made directly on a node are overwritten by the next sync: change the profile instead.
- Roll non-trivial changes out one node at a time: test node first, then the whole pool.
- Passing the real client IP to the backend is enabled by the PROXY protocol option; health checks then carry the same header, otherwise the backend drops them and marks the server down.
