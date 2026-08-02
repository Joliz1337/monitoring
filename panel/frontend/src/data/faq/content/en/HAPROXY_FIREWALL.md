# Node firewall

UFW rules on one server: what is allowed, from where and in which direction.

## Rule fields

| Field | Values |
|---|---|
| Port | 1–65535 or any |
| Protocol | TCP, UDP or any |
| Action | Allow or deny |
| Direction | Incoming or outgoing |
| Source | A single IP, a subnet like `10.0.0.0/24`, or `0.0.0.0/0` (anywhere) |

A typical VPN node set: SSH from your address only, 443 from anywhere, port 9100 from the panel IP only.

## Good to know

- These rules apply to one node. For the same set across the fleet use firewall profiles: rules are defined once and rolled out with validation and rollback.
- Port open but the service unreachable? Make sure something is actually listening: `ss -tlnp | grep :443`.
- Locked yourself out of SSH? Only the provider console helps: `ufw disable`, then restore rules from the panel.
- The address blocklist is a different mechanism: it works through ipset across all nodes and is unrelated to these rules.
