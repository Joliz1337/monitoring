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

The whole chain gets verified: the port is open, the core accepted the connection and managed to reach the masking site.

- **SNI** must be a domain from the inbound's `serverNames`. With a foreign name the server drops the connection and the check fails every time.
- **Interval.** Every check is a full TLS handshake plus a real outbound request from the core. A single rule uses 30 seconds; in a pool it is set per server, and at 5 seconds one node alone sends tens of thousands of requests a day to the masking site.
- **TCP rules only.** HTTPS rules terminate TLS in the panel, so a plain HTTP check works there.

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
