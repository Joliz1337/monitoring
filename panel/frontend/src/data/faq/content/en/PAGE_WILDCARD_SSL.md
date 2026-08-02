# Wildcard SSL

One `*.example.com` certificate for the whole fleet: the panel issues it, renews it and distributes it to nodes itself.

## How it works

1. The panel issues the certificate through Let's Encrypt, proving domain ownership with a DNS record in Cloudflare — no open ports on the servers needed.
2. The certificate is pushed to every node with delivery enabled.
3. The node validates the files, keeps a copy of the current ones, writes the new ones and runs the configured reload command.
4. Once a day the panel checks the expiry date and renews ahead of time.

## Per-node setup

For each node you specify where to put the files and which command reloads the service — an nginx or HAProxy reload, for example. Non-standard file names or full paths can be set if the service expects them somewhere specific.

## Good to know

- You need a Cloudflare API token allowed to edit the domain's DNS records, and the domain must be served by Cloudflare.
- A wildcard covers one level only: `*.example.com` fits `node1.example.com` but not `a.b.example.com`.
- The certificate directory is named after the base domain, not the subdomain — this matters when certificate paths are written into configs by hand.
- If renewal or delivery to some nodes fails, the panel sends a Telegram notification listing the problem servers.
- This differs from certificates on the HAProxy page: those are ordinary single-server certificates issued via HTTP validation.
