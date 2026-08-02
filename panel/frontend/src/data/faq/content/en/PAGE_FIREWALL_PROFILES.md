# Firewall profiles

One UFW rule set for many nodes. A profile defines the **entire** firewall state of a server rather than adding to it.

## How it is applied

The node does it atomically: saves the current state as a backup, resets the rules, applies the new ones and enables the firewall. If anything fails, the backup is restored automatically. Rules added manually on the node are replaced on first apply (but kept in the backup).

## Rule fields

| Field | Values |
|---|---|
| Port | 1–65535 or any |
| Protocol | TCP, UDP or any |
| Action | Allow or deny |
| Direction | Incoming or outgoing |
| Source | An IP, a subnet, or anywhere |
| Comment | For you; not stored in the firewall state |

Plus default policies for incoming and outgoing traffic.

## Lockout protection

The panel won't let you cut yourself off from a node: a new profile already contains a rule for port 9100, the interface warns if you remove it, and the node itself refuses such a profile — only an explicit confirmation overrides that. SSH access is your responsibility: add a rule for your own port yourself.

## Good to know

- Statuses match HAProxy profiles: applied, pending (offline node, re-synced automatically), failed.
- Drift is tracked by a checksum of the rules, so manual edits on a node show up as out of sync. Comments are excluded from it — UFW doesn't store them.
- Unbinding a server does **not** roll rules back: the firewall stays in its last applied state.
- The address blocklist works on top of these rules through a separate mechanism and is untouched by profiles.
