# Remnawave nginx

Managing nginx on Remnawave nodes from the panel. A profile is one config template for the whole fleet; nodes with an install are discovered automatically at the path from settings (default `/opt/remnawave`).

## How it works

A profile has three parts: **options** (real-IP scheme, certificates, fallback), **rules** (locations) and **linked servers** (each with its own domain). The panel renders the config, substitutes the node's domain for `{{DOMAIN}}` and sends it; the node verifies it with `nginx -t` and applies it with a graceful reload.

- Rules live between the `LOCATIONS START/END` markers. Edits **outside** the markers survive rule changes; changing options rebuilds the config from the template and drops such edits.
- Lines tagged `# auto: node` (`worker_rlimit_nofile`, `worker_connections`, `ssl_session_cache`) are recalculated by each node from its RAM and limits. Remove the tag and your value stays.
- An imported foreign config without markers works in raw mode: the rule builder stays unavailable until you press "Apply template".

## Ports

| Port | Who connects |
|---|---|
| **443** | All clients — both direct and via CDN. The CDN option doesn't change the port, only the IP source |
| **PP port** (8449) | HAProxy only. A separate port is required because nginx cannot mix PROXY-header and plain connections on one port |
| **80** | Only the HTTPS redirect and ACME — appears when the matching toggle is on |

## Rules

**gRPC → Xray** — the location for VPN traffic:
- *serviceName* must match the inbound's `grpcSettings.serviceName`, otherwise Xray rejects the request;
- *port* is the inbound's local port (check with `ss -tlnp | grep 127.0.0.1`);
- the client-IP header is always overwritten — it cannot be spoofed.

**Proxy** — a plain location to a site: path and target address.

Not allowed: duplicate rule names or paths; a rule with path `/` while fallback is set — that location already belongs to it.

## Options

**Fallback** — where everything that doesn't match a rule goes (scanners, bots, browser visits), plus Xray errors: a crashed core serves the site instead of a 502. Empty — nginx returns 404.

**Traffic via CDN** — enable if Cloudflare or similar sits in front of the node. The client-IP header is accepted only from the listed ranges; `0.0.0.0/0` means "trust everyone" — then anyone can spoof their IP with a single header.

**PROXY protocol** — for nodes behind HAProxy. Set the port; the HAProxy IP is optional (empty = accept from anyone), but then you **must** firewall the port, otherwise anyone can send a forged header.

**Redirect 80 → 443** — adds a server block on port 80. Works only if the port is open in the firewall.

**ACME (Let's Encrypt)** — adds the `/.well-known/acme-challenge/` location that Let's Encrypt uses to verify domain ownership when issuing and renewing a certificate (the HTTP-01 challenge). Needed if the certificate is issued by certbot over HTTP. **Not needed** for wildcard certificates: those use DNS-01, where verification goes through a DNS record instead of the web server.

**Reject unknown SNI** — the server stops answering with your certificate on connections with a foreign or empty SNI, i.e. on bare-IP scanning. Enable only if every client connects with the node's domain.

**Certificate paths** — with a wildcard certificate remove `{{DOMAIN}}` from them: the directory is named after the base domain (`/etc/letsencrypt/live/example.com/`), not after the node's subdomain. Keep the placeholder only when each node has its own separate certificate.

The certificate itself must already exist on the node — before applying, the node checks the files on the host and returns a clear error if they are missing, instead of raw `nginx -t` output. No need to mount the certificate directory into the container manually: if the directory (including custom paths) is not mounted yet, the node adds the volume to the installation's `docker-compose.yml` itself and recreates the container.

## Node domain

Set when linking and substituted for `{{DOMAIN}}` — in `server_name` and, if the placeholder is left there, in the certificate paths. Use the domain clients actually connect to on that node.

## For Xray to see the real IP

Without this, stats, device limits and bans all show `127.0.0.1`:

- the inbound needs `"sockopt": { "trustedXForwardedFor": ["X-Forwarded-For"] }` in `streamSettings`, next to `grpcSettings`;
- the inbound listens on `127.0.0.1` — nginx terminates TLS;
- the Xray core must be recent: for gRPC the option is correct since v26.6.22;
- edit the template **in the Remnawave panel**, not the file on the node — otherwise Remnawave sync overwrites it.

## Masquerading

- The response to outside traffic is an exact copy of the decoy's: the panel adds no headers of its own and passes the decoy's `Server` through, so the site cannot be told apart when reached directly versus through the node.
- If the decoy is down, the connection is dropped (`444`) instead of an nginx error page: regular sites don't serve 502, which would give the proxy away.
- What can leak the decoy's address is its own response: a `Location` redirect to its real domain, absolute links, `Set-Cookie` or CSP with foreign domains. Check with `curl -skI https://node-domain/` and compare to a direct request to the decoy.

## Applying and rollback

- Transaction on the node: backup → write → `nginx -t` → rollback on error → graceful reload. A broken config won't take down a running nginx.
- The profile replaces `nginx.conf` entirely. Installer-provisioned nodes mount it as a fragment — the node switches the mount to the full config and recreates the container; the unix-socket decoy block from the old fragment disappears, move it into the profile if you rely on it.
- Statuses: **synced** — applied, **pending** — queued (an offline node catches up on its own), **failed** — error, details in the sync log.
