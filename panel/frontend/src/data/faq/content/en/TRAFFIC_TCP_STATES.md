# TCP states

A connection passes through several states from setup to teardown. Their counts tell you what's happening to a service.

| State | Meaning |
|---|---|
| LISTEN | The port is open and waiting for connections |
| ESTABLISHED | Active connections — the main working mass |
| SYN_SENT | The server is trying to connect somewhere and waiting for a reply |
| SYN_RECV | A connection request arrived, the handshake isn't finished |
| FIN_WAIT | The connection is closing, initiated by the server |
| CLOSE_WAIT | The client closed, the application hasn't |
| TIME_WAIT | Closed and cooling down for a couple of minutes before the port is freed |

## What imbalances mean

- Growing **CLOSE_WAIT** that never falls means the application doesn't close sockets; only fixing or restarting it helps.
- Tens of thousands of **TIME_WAIT** are normal for a busy proxy: a consequence of many short connections, not a leak.
- Lots of **SYN_RECV** during a traffic spike looks like a SYN flood; anti-DDoS handles those.
- Lots of **SYN_SENT** means the server can't reach the outside: an unreachable upstream or DNS failure.
