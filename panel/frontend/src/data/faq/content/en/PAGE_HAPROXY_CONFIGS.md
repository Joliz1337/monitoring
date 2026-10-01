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

To add a similar rule, clone an existing one with the copy button in its row: the form opens with all of the original's settings, and you only set a new port.

When a target address belongs to a server in the panel, the IP is labelled with the server's name and which of its addresses it is: "primary" or "extra 1", "extra 2" and so on. The label appears in the rule row, in the form and on every balancer server. The primary is the address from the server's settings; the other public IPs are numbered in the order they sit on its interfaces. Removing an extra address shifts the numbers of the ones after it. Addresses of other machines stay as they are.

## Server addresses

If a server has several IPs, expand its row under "Linked Servers" and check which addresses HAProxy listens on and which it uses to reach the backends. For example, one IP only accepts clients while another only goes out. The setting covers every rule in the profile, and the profile itself stays shared: the panel builds a separate config for each server. With nothing checked, HAProxy listens on all addresses and the system picks the outgoing one.

Before rolling out, the panel checks the selected IPs against the server's addresses. If one is gone, the server gets the Failed status with a clear reason instead of silently staying on the old config.

## SNI filter

Off by default. The "SNI filter" button above the rules sets a list for the whole profile: HAProxy passes only TLS connections with these SNI to the backend. Connections with another SNI, without SNI or not using TLS (scanners, probes by IP) get no reply at all — no certificate, no refusal, the connection just hangs as if nothing were behind the port. The filter cannot hide the fact that the port is open: the system accepts the connection before the client names the SNI. An entry `*.example.com` allows `example.com` itself and all its subdomains. Every rule has its own setting: "Profile default" uses the shared list, "Own list" replaces it for that rule, "Off" accepts any connection even when the profile filter is on. Turn the filter off in rules that carry non-TLS traffic, otherwise that traffic stops getting through.

## Per-backend connection limit

A balancer physically cannot open more than ~64,000 connections to one backend address:port — each one takes a source port, and there are only 65,535 of them. So keep a balancer server's "Max Connections" around 60000: when it fills up, HAProxy gracefully moves extra clients to another server or queues them, instead of failing with errors and delays on exhausted ports. Need more on the same machine — add it as a second server with a different port (each port gets its own 64k), give the profile's server several outgoing addresses (each also gets its own 64k), or use DNAT routing, which has no such limit. Incoming client connections are unaffected — their ceiling is computed automatically from the node's RAM.

## Good to know

- Unbinding a server stops HAProxy on it; the config file stays on disk.
- Edits made directly on a node are overwritten by the next sync: change the profile instead.
- Roll non-trivial changes out one node at a time: test node first, then the whole pool.
- Passing the real client IP to the backend is enabled by the PROXY protocol option; health checks then carry the same header, otherwise the backend drops them and marks the server down.
