# Blocklist

Address filtering across all nodes at once: blacklists, an allowlist and automatic sources. It runs through ipset on the nodes, so a million addresses don't slow packet processing down.

## What it consists of

| Type | Scope | Purpose |
|---|---|---|
| Global rule | All servers | The main way to block |
| Server rule | One node | A targeted addition to the global set |
| Allowlist | All servers | Addresses that always pass, above any block |
| URL source | All servers | A ready-made list the panel downloads and refreshes itself |

**Incoming** blocks connections to the server, **outgoing** blocks the server's own connections. Blocks can be permanent or temporary with automatic expiry.

## Self-lockout protection

- The allowlist is always evaluated before blocks and, besides your entries, automatically contains the panel address and the addresses of all active nodes — control traffic can't be lost.
- Private and reserved ranges (`10.0.0.0/8`, `127.0.0.0/8`, `192.168.0.0/16` and friends) are rejected from blocks, both manual and from sources: such a list once took down every node at once by blocking loopback and docker bridges.

## Good to know

- Rules reach nodes in the background; a freshly added node gets the current lists on the next sync.
- The same address can sit in both the blocklist and the allowlist — the allowlist wins.
- Blocking a subnet is cheaper than a hundred addresses from it: `1.2.3.0/24` is a single entry.
- This isn't the same as firewall rules: those describe ports and services, the blocklist only addresses — but across the whole fleet.
