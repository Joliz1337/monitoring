# DNAT routing

Kernel-level port forwarding on a node: listen port → target IP:port. Same job as HAProxy routing, but without a userspace proxy — netfilter rewrites the packets, so CPU cost is minimal and UDP is forwarded just like TCP.

## How it works

A profile is the complete forwarding rule set of a node. The node applies it atomically in three chains of its own (`nat/PREROUTING` DNAT, `nat/POSTROUTING` MASQUERADE, `filter/FORWARD` ACCEPT), remembers it and restores it after a reboot or a firewall reset. The panel checks the rule checksum and shows per-rule connection and traffic counters on the server page.

| Field | Values |
|---|---|
| Protocol | TCP, UDP or both |
| Listen port | A single port or a range (range end) |
| Target | IPv4 and port; port 0 keeps the listen port (a range is forwarded as a whole) |
| MASQUERADE | On by default: the target replies to the node, not to the client. Turn it off only if the target routes replies back through this node |

## Good to know

- The node API port (9100/tcp) cannot be forwarded, and rules with overlapping ports of the same protocol are rejected. A rule on the SSH port gets a warning.
- The target sees connections from the node address (MASQUERADE) — the real client IP does not reach it. Need the real IP — use HAProxy with PROXY protocol.
- Unlinking a server does not remove its rules: use the button on the server page.
- Permissions: the `dnat` domain in the node's NODE_CAPABILITIES.
