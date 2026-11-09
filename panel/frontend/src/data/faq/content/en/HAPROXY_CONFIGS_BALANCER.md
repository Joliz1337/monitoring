# Balancing algorithms

The algorithm decides which pool server takes the next connection.

| Algorithm | When to use |
|---|---|
| roundrobin | Round the circle. For short uniform requests: APIs, static content |
| leastconn | To the server with the fewest active connections. For long-lived ones: WebSocket, gRPC, databases |
| source | The server is picked by hashing the client IP — cheap stickiness without cookies |
| first | Everything to the first live server, the rest idle. That's "primary plus standby", not balancing |
| static-rr | Like roundrobin but without dynamic weighting; slightly faster and simpler |

## Weights and standby

- Weight sets the share of traffic: a server with weight 2 gets twice as much as one with weight 1. That's how you level out nodes of different capacity.
- A backup server only receives traffic when every primary is down.
- "Load-based weights" (roundrobin, leastconn, random) — the panel adjusts weights every 10 seconds: while a server's link and CPU load stay under 70% its weight follows its link, by 95% it drops to zero and new clients go to free servers. Connected clients stay on their server, nothing is dropped.
- The link is entered once per server in the "Server links" block above the backend list — shared by all its IPs and all rules. With sticky sessions the weights only spread new clients.

## Good to know

- With very fast responses (single-digit milliseconds) the connection counter can't keep up — roundrobin spreads load more evenly than leastconn.
- IP-based stickiness holds only while the pool composition is stable: add or remove a server and some clients move. For real stickiness use cookie-based affinity.
- Changing the algorithm takes effect on config reload and doesn't drop existing connections.
- Uneven load? Check health checks first: part of the pool is probably marked down.
