# Updates

Panel and node versions in one place, updates in one click.

## What you can do

- See the installed and available panel version, update it with an automatic restart.
- See every node's version and update any of them — one by one or all at once.
- Select nodes — one by one, by dashboard folder, or all — and update the selected ones the regular way or over SSH.
- Update everything with one button: the panel starts updating all nodes, then updates itself — the page reloads automatically.
- Upgrade HAProxy on nodes to the newest official LTS version for their OS: when a newer version exists — a newer branch or a fresh fix in the current one — a badge such as "↑ 3.4.6" appears next to the HAProxy version on the card. Works for one node or for the selected ones.
- Check, change or remove the download proxy: when one is set, the node card shows "Proxy", and the globe button opens a window with every place it was found. Changing it restarts Docker on the node — VPN drops for 10–30 seconds.
- Force a check for new versions.

## Update order

Panel first, nodes second. That way a new node is less likely to answer the panel with something it doesn't understand yet; backward compatibility is maintained, but "panel first" is the safer order.

Updating pulls ready-made images, keeps configuration and data, and restarts the services itself.

## Good to know

- Updating the panel restarts it: the interface is unavailable for a few seconds and background jobs (bulk operations, node installs) are aborted.
- Updating a node restarts only the monitoring agent. VPN services, HAProxy and nginx on the node keep running — client traffic is not interrupted.
- A node that won't update: check that it is online and can reach the image registry; behind a SOCKS5 proxy, the proxy needs that access too.
- If the node can't reach the registry, update it over SSH: the panel uploads the image itself. This runs in the background — you can close the log window, the status stays on the node's card. Up to 5 nodes update this way at once, the rest wait in a queue.
- While a node updates, its card shows a yellow line with the step, for example "Updating: pulling images · 3/4". An SSH update shows its own steps and the image upload percentage the same way. The panel keeps this status, so it stays visible after a page reload.
- The panel follows how a regular node update ends. If the node couldn't update itself, the panel updates it over SSH when the server has saved SSH access. If there is no access or SSH fails too, you get a Telegram notification and an entry in the alert history.
- If a server behind censorship filtering (TSPU) can't reach the HAProxy repository, tick "Download everything through the panel" in the upgrade window: the upgrade runs over SSH and the server downloads the packages through the panel. Docker and VPN are not restarted.
- Some panel features require a minimum node version and will say so plainly if the agent is too old.
- HAProxy switches to the new version without dropping connections: current clients finish on the old one. If that doesn't work, HAProxy restarts and clients reconnect; if the new version rejects the config, the server rolls back to the previous one.
