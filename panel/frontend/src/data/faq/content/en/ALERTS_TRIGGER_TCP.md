# Trigger: TCP states

Watches the number of connections in each state. Every state has its own threshold and switch.

## What growth means

| State | What growth indicates |
|---|---|
| ESTABLISHED | Active connections: a client surge or DDoS. A sharp drop means the service died |
| LISTEN | Number of listening ports; rarely changes, growth means new services |
| TIME_WAIT | Recently closed. Growth by orders of magnitude means no connection reuse, no keep-alive |
| CLOSE_WAIT | The far side closed, the local one didn't. Steady growth means the app never closes sockets |
| SYN_SENT | Outgoing connection attempts. Many and persistent means an unreachable upstream or DNS trouble |
| SYN_RECV | Incoming half-open connections. A sharp rise is a classic SYN flood |
| FIN_WAIT | Connections in the process of closing |

## Good to know

- A dozen CLOSE_WAIT is normal; the worry is a number that only grows and never falls.
- Tens of thousands of TIME_WAIT are normal on busy nodes: set a high threshold or alerts will be constant.
- A SYN_RECV surge together with traffic growth is worth checking against anti-DDoS — the watchdog reacts to such attacks on its own.
- Values come from kernel statistics; on heavily stripped kernels some counters may be unavailable.
