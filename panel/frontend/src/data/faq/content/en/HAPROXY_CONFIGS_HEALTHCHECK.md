# Health checks

The balancer probes pool servers regularly and pulls the unresponsive ones out of rotation.

## Check types

| Type | What it does | When to use |
|---|---|---|
| TCP | Opens a connection to the port and closes it | Any protocol: databases, VPN inbounds, arbitrary TCP |
| HTTP | Requests a path and inspects the status code | Web applications, where the port alone proves nothing |

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
