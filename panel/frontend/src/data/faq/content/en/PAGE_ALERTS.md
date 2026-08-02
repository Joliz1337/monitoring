# Alerts

Telegram notifications when something is wrong with a server: it vanished, is overloaded, has odd traffic or piled-up connections.

## How it works

A background check runs every minute across all active servers. Each trigger has its own thresholds, hold time and repeat pause. An event goes to Telegram and into history, filterable by server, type and date.

| Setting | Meaning |
|---|---|
| Threshold | The value above which a metric counts as a problem |
| Hold time | How many seconds the metric must stay above the threshold — protection from short spikes |
| Cooldown | Minimum interval between repeated messages about the same problem, 30 minutes by default |
| Exclusions | Servers the check skips entirely |

## What is monitored

- **Availability** — the server missed several checks in a row; a separate message arrives when it returns.
- **CPU, RAM, load average** — threshold breaches and unusual growth.
- **Network** — spikes and, conversely, suspicious silence.
- **TCP states** — each state with its own threshold.
- **Anti-DDoS** — three separate signals: emergency mode lasting over 30 minutes; the watchdog firing three or more times an hour (the threshold doesn't suit this node's traffic); the connection table over 80% full.

## Good to know

- Alerter state survives a panel restart: cooldowns and statuses are restored from the last day of history, so a restart doesn't replay notifications for servers that are already down.
- No alert despite an obvious problem? Check in order: the trigger is on, the server isn't excluded, the metric stayed above the threshold longer than the hold time, the cooldown has expired.
- The node itself also notifies when it enters anti-DDoS emergency mode — the panel adds duration, repetition and a shared cooldown on top.
