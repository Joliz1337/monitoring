# Bulk actions

One action across dozens of nodes: restart HAProxy, add a firewall rule, run a command.

## What you can do

- Select servers by checkbox, by whole folder or all at once, with search by name and address.
- HAProxy: start, stop, restart; replace the config entirely or do a find-and-replace inside it.
- Firewall: add or delete a rule.
- Traffic: add or remove a tracked port.
- Terminal: an arbitrary command with per-server results.

## How it runs

The operation runs on the panel server and doesn't depend on your browser: close the tab or lose connectivity, and progress is picked up again when you return. Nodes are processed in parallel, but no more than 20 at a time, so a hundred servers won't saturate the network.

Offline nodes are skipped instantly with a note instead of hanging until timeout. Disabled servers don't take part at all, even if selected.

## Good to know

- A running operation cannot be cancelled. Double-check the selection before `rm`, `iptables -F`, `systemctl stop` or `reboot` — a mistake repeats everywhere at once.
- Partial success is a normal outcome: one node offline, another past its timeout, the rest fine. Retry only the problematic ones.
- Progress and results live in panel memory for about 10 minutes after completion. Restarting the panel container aborts the operation and loses the results.
- Different results on different nodes usually mean different load or disk speed — raise the timeout.
