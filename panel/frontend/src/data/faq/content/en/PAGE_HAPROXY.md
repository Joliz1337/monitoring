# HAProxy on a server

The state of the load balancer on one node: service, active rules, certificates and firewall.

## What's on the page

- **Service** — whether HAProxy runs, whether the config is valid, buttons to start, stop and reload.
- **Rules** — which port forwards where. They are read-only here: rules are created and edited in config profiles and arrive by sync.
- **Certificates** — issue via Let's Encrypt, upload your own, renew, delete.
- **Firewall (UFW)** — rules for this node only.
- A button to view the raw `haproxy.cfg`.

## Reload or restart

| Action | What happens |
|---|---|
| Reload config | Graceful: new settings apply, active connections survive. The normal choice |
| Restart process | Full stop and start: all connections drop. Only when the service is stuck |

The config is always validated before being applied, and an invalid one is never applied — a typo can't take the service down.

## Good to know

- If the server is bound to a config profile, a banner shows its name: edit the profile, otherwise the next sync overwrites your changes.
- A stopped service almost always means a broken config; the reason is in the config check block.
- A rule that didn't take effect usually just needs a config reload.
- The firewall here applies to one node. For identical rules across dozens of nodes use firewall profiles, and for address blacklists use the blocklist.
