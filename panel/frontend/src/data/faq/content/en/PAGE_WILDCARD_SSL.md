# Wildcard SSL

`*.example.com` certificates for the fleet: the panel issues them, renews them and distributes them to nodes itself. You can keep several certificates for different domains.

## How it works

1. The panel issues the certificate through Let's Encrypt, proving domain ownership with a DNS record in Cloudflare — no open ports on the servers needed.
2. Every node with delivery enabled receives the certificates chosen for it.
3. The node validates the files, keeps a copy of the current ones, writes the new ones and runs the configured reload command.
4. Once a day the panel checks the expiry date and renews ahead of time.

## Per-node setup

For each node you choose which certificates it needs — one or several at once, each goes into its own folder named after the domain. You also specify where to put the files and which command reloads the service — an nginx or HAProxy reload, for example; the command runs once, after all files are written. Non-standard file names or full paths can be set if the service expects them somewhere specific; with full paths a node can hold only one certificate.

A new certificate isn't assigned to any node on its own: tick it in the cards of the nodes that need it, or select it for several nodes at once with bulk editing.

## Good to know

- You need a Cloudflare API token allowed to edit the domains' DNS records. The domains must be served by Cloudflare, and the token needs access to all their zones.
- A wildcard covers one level only: `*.example.com` fits `node1.example.com` but not `a.b.example.com`.
- The certificate directory is named after the base domain, not the subdomain — this matters when certificate paths are written into configs by hand.
- If renewal or delivery to some nodes fails, the panel sends a Telegram notification listing the problem servers.
- This differs from certificates on the HAProxy page: those are ordinary single-server certificates issued via HTTP validation.
