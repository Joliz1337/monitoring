# Health checks

The balancer probes pool servers regularly and pulls the unresponsive ones out of rotation.

## Check types

| Type | What it does | When to use |
|---|---|---|
| TCP | Opens a connection to the port and closes it | Any protocol: databases, VPN inbounds, arbitrary TCP |
| HTTP | Requests a path and inspects the status code | Web applications, where the port alone proves nothing |
| TLS + masking site | Handshakes with the server's SNI and requests a page | Inbounds behind REALITY, where a TCP check misleads |

## The masking-site check

A TCP check proves exactly one thing: the port accepted a connection. It cannot tell a throttled or crashed inbound behind a live port from a healthy one — the server stays in rotation while no traffic passes through it.

REALITY has a fallback: a client that does not match gets the real masking site. So a live server must answer a plain TLS handshake with its own SNI. That is what this check does — handshake, `GET /`, and the reply has to be sane HTTP: 2xx, a redirect, even 403/404 all count, because each proves a server is working behind the port. Silence or 5xx pulls the server out of rotation.

- **SNI** — a domain from the inbound's `serverNames`.
- **Interval.** Every check is a full TLS handshake plus a real outbound request from the core. A single rule uses 30 seconds; in a pool it is set per server, and at 5 seconds one node alone sends tens of thousands of requests a day to the masking site.
- **TCP rules only.** HTTPS rules terminate TLS in the panel, so a plain HTTP check works there.

### What this check cannot see

A green status means "the core on that server is alive and answering", not "the keys work". Four cases slip past it:

- **A block on a path it does not travel.** The check runs from the node hosting HAProxy. If the filtering sits at the client's operator while the node-to-server path is clean, the status stays green — the check never crosses the filtered segment. Settle it with an Xray test run from the location in question.
- **A wrong SNI.** REALITY hands any unrecognised client to the masking site, and the check is not a client with a key — so a domain missing from `serverNames` still answers.
- **Neighbouring inbounds.** Only the address and port named in the rule get checked. Another port on the same server may be throttled without showing up here.
- **Throttling after the handshake.** The check finishes inside the first few kilobytes. A filter that lets the connection open and then strangles the flow stays invisible to it.

What it does catch reliably: a crashed core, a closed port, a server unreachable from this node at all. That is considerably more than a TCP check gives.

## Parameters

- **Interval** — how often to probe. More often means faster reaction but more noise.
- **Failures before removal** — how many consecutive failures pull a server out.
- **Successes before return** — how many successful probes bring it back; prevents flapping.
- **Slow start** — a returning server ramps up its share of traffic instead of taking it all at once.

## Good to know

- Server alive but marked down? Check that the probe targets the right port, that the firewall allows it, and that the health path returns 200.
- If PROXY protocol is enabled towards the backend, health checks must carry it too, otherwise the backend drops them and the server stays down forever. The panel sets this automatically.
- Frequent probes across a large pool create noticeable background traffic — a few seconds is enough for dozens of servers.
- An HTTP health path should be lightweight and avoid database calls, or the check itself becomes a load source.
