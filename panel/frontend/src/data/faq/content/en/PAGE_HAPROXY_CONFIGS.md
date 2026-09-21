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

## Per-backend connection limit

A balancer physically cannot open more than ~64,000 connections to one backend address:port — each one takes a source port, and there are only 65,535 of them. So keep a balancer server's "Max Connections" around 60000: when it fills up, HAProxy gracefully moves extra clients to another server or queues them, instead of failing with errors and delays on exhausted ports. Need more on the same machine — add it as a second server with a different port (each port gets its own 64k), or use DNAT routing, which has no such limit. Incoming client connections are unaffected — their ceiling is computed automatically from the node's RAM.

## Good to know

- Unbinding a server stops HAProxy on it; the config file stays on disk.
- Edits made directly on a node are overwritten by the next sync: change the profile instead.
- Roll non-trivial changes out one node at a time: test node first, then the whole pool.
- Passing the real client IP to the backend is enabled by the PROXY protocol option; health checks then carry the same header, otherwise the backend drops them and marks the server down.
