# Servers

Every node under monitoring: adding, connectivity checks, SSH auto-install and organisation into a tree. The panel↔node channel is encrypted with mTLS.

## Two ways to add a node

**Manually** — if the agent is already installed or you install it yourself:

1. **Add server** — an **Installer Token** appears at the top of the form, copy it.
2. Fill in the name, address (IP or domain) and port (`9100` by default) → **Create**.
3. On the node: `mon` → `Install Node`, paste the token.
4. Press **Test** — the status should turn green.

**SSH auto-install** — the panel logs into the server and does everything itself:

1. Enable "Install node over SSH" in the form, provide the port, login and either a password or a private key.
2. Tick what else to install: system optimisations (with profile and NIC mode), Cloudflare WARP, a Remnawave node, an HTTP proxy for restricted environments.
3. Start it — the install log streams live.

The install runs on the backend: closing the tab won't stop it, and you can return to the log for 10 minutes after it finishes. Only restarting the panel container aborts it. The install timeout is 25 minutes; the SSH password is never stored.

## Single-server key

When someone else installs the node — the owner of a server you rent — the shared **Installer Token** must not be handed over: it carries the private key of a certificate identical across your whole fleet, enough to impersonate the panel towards any of your nodes. The form has a **Single-server key** block for exactly this.

- Such a key carries its own certificate, good only for acting as a node: it grants no access to your other servers.
- The lifetime is chosen at issue time, from 30 to 1095 days. Once it ends the panel stops seeing that node — you'll need a fresh key and a reinstall.
- It installs like any token: `mon` → `Install Node`. The **Install command** button copies a ready one-liner you can simply forward.
- An issued key cannot be revoked from an already installed node — deleting it in the panel only removes it from the list. Limit the lifetime instead of counting on revocation.

## What the node hands over

By default a node is fully open to the panel. If the server belongs to someone else, or you simply don't want to hand over everything, trim the permissions on the node itself: the file `/opt/monitoring-node/.env`, the line `NODE_CAPABILITIES=`. Empty, or no line at all, means everything is allowed, exactly as before.

Words are comma-separated; the `:ro` suffix leaves viewing only: `haproxy:ro` — rules are visible but cannot be changed.

| Word | What it opens |
|---|---|
| `traffic` | Port tracking and the migration of old traffic history |
| `haproxy` | The balancer and its certificates |
| `firewall` | UFW rules and firewall profiles |
| `ipset` | IP blocking: blocklists, allowed addresses, torrent bans |
| `ssh` | SSH settings, fail2ban, keys, root password |
| `ssl` | Wildcard SSL deployment |
| `antiddos` | Emergency mode, watchdog, anti-DDoS whitelist |
| `remnawave` | Nginx on Remnawave nodes |
| `system` | Optimisations and time sync |
| `exec` | The terminal and the "Reboot" / "Shutdown" buttons |

Instead of listing words you can take a ready-made set: `monitoring` — view traffic and system data, `readonly` — everything visible, nothing changeable, `full` — same as no line at all. A set adds up with individual words: `readonly,haproxy` — view everything plus full access to the balancer. Common recipes: `monitoring` — watch and don't touch; `readonly` — someone else's server under full observation, no interference; `readonly,haproxy,firewall` — the balancer and the firewall are yours, the rest is view-only.

An unknown word is silently skipped: a typo never opens more than intended, but it won't give you what you meant either — `readonly,haproxi` leaves just `readonly`, while `redonly` closes everything. The `:ro` suffix only works with the words from the table; `monitoring`, `readonly` and `full` must never carry it. The **Test** button on the server card shows what the node actually understood.

After editing, reload the line: `cd /opt/monitoring-node && docker compose restart api`. Updating the node never overwrites it.

Five things can never be closed: metrics, the health check, the agent version, updates and certificate rotation. Otherwise the panel would treat the node as dead, fire false "server offline" alerts and be unable to update it — you'd have to restore the permissions by hand over SSH. Metrics therefore always flow in full: load, disks, per-port counters, certificate expiry and the anti-DDoS state stay visible even for closed sections. The flip side: if you close `antiddos` and the watchdog turns emergency mode on, you can no longer turn it off from the panel — only on the server.

Here is what it looks like in the panel: the server card gets a **Restricted** badge whose tooltip lists what is still allowed, and closed sections show a padlock notice instead of their tables. The panel sends no requests there at all — no waiting, no red errors.

## Good to know

- The **Installer Token** is shared by all nodes, is not single-use and doesn't rotate. Closed the form? Open it again — same token. Hand it to your own servers only.
- Port `9100` carries the mTLS handshake. Open it in the node firewall for the panel IP only, otherwise scanners will fill your logs.
- The **SOCKS5 proxy** field is for nodes behind NAT or blocked by IP. Everything goes through it: metrics, commands, blocklist sync, even SSH during auto-install. Format `ip:port` or `ip:port@login:password`. If the proxy dies, the node shows offline with a "Proxy connection error" — that distinguishes a dead proxy from a dead node.
- The **Old key** badge means the node still uses a per-server certificate: migrate it with the button. Very old nodes on `X-API-Key` need a reinstall.
- The **Restricted** badge means the node did not hand the panel everything. The badge tooltip lists what is still allowed. It is changed on the node itself; there is no such setting in the panel.
- The **"Account → Project → Servers" tree** is a second, independent way to organise nodes by cloud account and cluster. Deleting an account or project never deletes servers.
- Disabling monitoring keeps the server in the list but stops polling and alerts for it.
- Deleting a server erases its history and rules in the panel but doesn't touch the node itself — its containers keep running.

## When "Test" fails

Work bottom-up: ping the address → `nc -zv <ip> 9100` → `openssl s_client -connect <ip>:9100`.

| Response | Meaning |
|---|---|
| `certificate required` | Normal: the node is alive and wants the panel's client certificate |
| `unknown ca` | The node was installed with a different token — reinstall with the current one |
| Timeout, port closed | Node or provider firewall, or the agent isn't running |
| `Proxy connection error` | The SOCKS5 proxy is down, not the node |
