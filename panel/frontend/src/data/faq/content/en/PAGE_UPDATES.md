# Updates

Panel and node versions in one place, updates in one click.

## What you can do

- See the installed and available panel version, update it with an automatic restart.
- See every node's version and update any of them — one by one or all at once.
- Select nodes — one by one, by dashboard folder, or all — and update the selected ones the regular way or over SSH.
- Update everything with one button: the panel starts updating all nodes, then updates itself — the page reloads automatically.
- Upgrade HAProxy on nodes to the newest official LTS version for their OS: when one exists, a badge with the branch, e.g. "↑ 3.2", appears next to the HAProxy version on the card. Works for one node or for the selected ones.
- Force a check for new versions.

## Update order

Panel first, nodes second. That way a new node is less likely to answer the panel with something it doesn't understand yet; backward compatibility is maintained, but "panel first" is the safer order.

Updating pulls ready-made images, keeps configuration and data, and restarts the services itself.

## Good to know

- Updating the panel restarts it: the interface is unavailable for a few seconds and background jobs (bulk operations, node installs) are aborted.
- Updating a node restarts only the monitoring agent. VPN services, HAProxy and nginx on the node keep running — client traffic is not interrupted.
- A node that won't update: check that it is online and can reach the image registry; behind a SOCKS5 proxy, the proxy needs that access too.
- If the node can't reach the registry, update it over SSH: the panel uploads the image itself. This runs in the background — you can close the log window, the status stays on the node's card. Up to 5 nodes update this way at once, the rest wait in a queue.
- Some panel features require a minimum node version and will say so plainly if the agent is too old.
- HAProxy switches to the new version without dropping connections: current clients finish on the old one. If that doesn't work, HAProxy restarts and clients reconnect; if the new version rejects the config, the server rolls back to the previous one.
