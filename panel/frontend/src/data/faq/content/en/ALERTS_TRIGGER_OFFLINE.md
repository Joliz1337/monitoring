# Trigger: server offline

The most important alert: the node stopped answering. A separate message arrives when it comes back.

## Parameters

| Parameter | Meaning |
|---|---|
| Consecutive failures | How many checks must fail before declaring the node offline. Default 3 |
| Check interval | How often the panel polls nodes — a global alerter setting, once a minute by default |
| Cooldown | Interval between repeated messages |

## Good to know

- Time to alert is simple arithmetic: check interval × failure count. With a one-minute interval and three failures the message arrives in about three minutes.
- Setting a single failure is unwise: one blip between panel and node produces a false alarm.
- The recovery message is automatic, nothing to enable.
- Server listed offline but reachable? Press "Test" on the Servers page: usually it's a dead SOCKS5 proxy, a closed port 9100 or a stopped agent container.
- A panel restart won't replay notifications for servers already down — state is restored from history.
