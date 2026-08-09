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

## Good to know

- The **Installer Token** is shared by all nodes, is not single-use and doesn't rotate. Closed the form? Open it again — same token. Hand it to your own servers only.
- Port `9100` carries the mTLS handshake. Open it in the node firewall for the panel IP only, otherwise scanners will fill your logs.
- The **SOCKS5 proxy** field is for nodes behind NAT or blocked by IP. Everything goes through it: metrics, commands, blocklist sync, even SSH during auto-install. Format `ip:port` or `ip:port@login:password`. If the proxy dies, the node shows offline with a "Proxy connection error" — that distinguishes a dead proxy from a dead node.
- The **Old key** badge means the node still uses a per-server certificate: migrate it with the button. Very old nodes on `X-API-Key` need a reinstall.
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
