# DNAT routing

Kernel-level port forwarding on a node: listen port → target IP:port. Same job as HAProxy routing, but without a userspace proxy — netfilter rewrites the packets, so CPU cost is minimal and UDP is forwarded just like TCP.

## What it is and how it differs from HAProxy

HAProxy **terminates** the connection: it accepts TCP from the client, opens its own connection to the target and shuffles bytes. DNAT accepts nothing — the kernel rewrites the destination address in the packet header and sends it on; the reply comes back to the client via conntrack. The node opens no sockets and runs no process.

| | DNAT | HAProxy |
|---|---|---|
| CPU per 1 Gbit/s | a few percent (softirq) | a good share of a core: two TCP stacks + userspace copies |
| UDP (Hysteria, WireGuard, QUIC) | yes | no |
| Ephemeral ports / TIME_WAIT on the node | not used | one per connection to the target |
| Processes on the node | none — iptables rules only | haproxy |
| Real client IP at the target | no (MASQUERADE) — only if the target routes replies back through the node | no, but PROXY protocol is available |
| TLS/SNI/domains, backend health checks | no | yes |
| What DPI sees between node and target | the clients' packets as they are: their TCP options, window, MSS, source ports, TTL (TTL and MSS can be disguised) | the node's own connections — a normal server stack |
| Balancing | across servers / random / round-robin / by client IP, no health checks | leastconn, weights, health checks |

Rule of thumb: DNAT where you need cheap transit and UDP and the path between node and target is "quiet" (your own nodes, same country, a tunnel); HAProxy where an aggressive DPI sits between node and target and looking like a server matters, or you need TLS/PROXY protocol/health checks.

## What DPI sees on the path and what disguising does

A DPI on the node's uplink sees DNAT transit as a stream of connections from one IP to one IP:port carrying the **clients' TCP headers**: different windows/MSS/wscale (phones, Windows, iOS), client-range source ports and — without disguise — mixed TTLs (50s from mobiles, 110s from Windows). To a DPI that is a NAT-relay tell. Plus the SNI inside TLS: a well-known domain's SNI towards a foreign IP (`vk.com`, `max.ru`, `google.com` at a hoster's IP) is dropped for the domain↔IP mismatch; only the SNI of a domain that really points at this IP passes (self-steal with your own domain).

The **"Disguise transit"** checkbox rewrites the TTL of forwarded packets to 64 in both directions (like a Linux server's own traffic) and clamps MSS to the MTU. The TTL mix disappears; client TCP options cannot be hidden — a fundamental limit of forwarding, only termination (HAProxy) hides them.

Sign of a "learned" and throttled endpoint: the rule's connections and bytes "from clients" grow while "to clients" stays near zero; TCP from the node to the target succeeds every other time with 0 % ICMP loss. Fix: change the target port (one field in the rule, clients untouched), keep several ports/IPs ready in advance and distribute by client IP.

A profile is the complete forwarding rule set of a node. The node applies it atomically, remembers it and restores it after a reboot or a firewall reset. The panel checks the rule checksum and shows per-rule connection and traffic counters on the server page.

## Listen port, range end, target port

Three fields describe one rule: "what we listen on → where we send it". Range end turns the listen port into a span of ports; target port decides what happens to the port number on the target: `0` — keep the one the client used, any number — collapse everything into that port.

| Listen | Range end | Target | Result |
|---|---|---|---|
| 443 | — | 10.0.0.2 : 8443 | `node:443 → 10.0.0.2:8443` — one port to another |
| 443 | — | 10.0.0.2 : 0 | `node:443 → 10.0.0.2:443` — same port |
| 20000 | 30000 | 10.0.0.2 : 0 | `node:20000–30000 → 10.0.0.2:20000–30000` — each port to the same port on the target (Hysteria port hopping when the server listens on the whole range) |
| 20000 | 30000 | 10.0.0.2 : 443 | `node:20000–30000 → 10.0.0.2:443` — the whole range collapses into one port (port hopping when the server listens on 443 only) |

Several different inputs to one target are several rules (or one range). A range mapped to a range with different numbers (`20000–30000 → 30000–40000`) is not possible. Rules of the same protocol cannot overlap on ports — only the first would fire while both would look active, so the panel refuses to save that.

## Several target IPs — balancing

The "Target IP" field accepts several comma-separated addresses: `10.0.0.2, 10.0.0.3, 10.0.0.4`. The "Distribution" field (shown once there is more than one address) decides how they are used:

| Mode | Who distributes | How |
|---|---|---|
| **Across servers** (default) | Panel | Each profile node gets one address by link order: #1 → first IP, #2 → second, then round-robin. A new server joins the end, the others keep their addresses. The assignment is shown on the Servers tab |
| **Random** | Node | Each node gets the whole list and spreads new connections across the addresses randomly and evenly |
| **Round-robin** | Node | Strictly in turn: 1st connection → first IP, 2nd → second, … |
| **By client IP** | Node | The address is picked by hashing the client IP: one client always lands on the same target while the list is unchanged. For protocols where a client opens several connections that must hit the same server. Requires the `xt_HMARK` kernel module (present in Ubuntu) |

In every mode the choice is made on the first packet; the connection then lives on the chosen address until it closes — nothing is moved on the fly. DNAT has no health checks: a dead server keeps receiving its share of new connections until you remove it from the list. On the server page, counters for random/round-robin/client-IP modes are shown per address. Node-side modes require agent 10.24.0 or newer.

## MASQUERADE — rewriting the source address

Client `1.2.3.4` connects to `node:443`. The node always rewrites the destination to the target; the MASQUERADE checkbox decides what happens to the sender address.

- **On (default):** the sender is rewritten to the node address too. The target sees a connection *from the node*, replies to the node, the node passes the reply back to the client. Always works, nothing to configure on the target. Downside — the target does not know the real client IP.
- **Off:** the sender stays `1.2.3.4`. The target sees the real client IP but sends the reply straight to it over its usual route — bypassing the node. The client receives a packet from an unexpected address and drops it: the connection never establishes. This works only when the target routes replies **back through the node** — e.g. it sits in a private network or tunnel and the node is its default gateway.

Turn it off only if such return routing is already in place. Need the real client IP without it — that is a job for HAProxy with PROXY protocol; DNAT cannot do it.

## Good to know

- The node API port (9100/tcp) cannot be forwarded. A rule on the SSH port gets a warning: after apply, SSH to the node goes to the target.
- The “Rule enabled” checkbox is a pause without deleting: a disabled rule stays in the profile but is removed from the nodes and no traffic is forwarded through it; tick it again and it is re-applied.
- Unlinking a server from a profile (and deleting a profile) removes all DNAT rules from the node; an offline node gets this once it is back online. Rules can also be removed manually with the button on the server page.
- Permissions: the `dnat` domain in the node's NODE_CAPABILITIES.
- An artificial bandwidth limit for the node (a flat ceiling on the hoster's counters) is on the server page, "Bandwidth limit" card.
