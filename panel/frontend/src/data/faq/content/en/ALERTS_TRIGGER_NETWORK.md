# Trigger: network

Watches traffic rate and catches both spikes and suspicious silence.

## Parameters

| Parameter | Meaning |
|---|---|
| Spike | Growth over the usual level. Catches loud problems: DDoS, leaks, a looping client |
| Drop | Decline below the usual level. Catches quiet ones: a dead service, clients gone |
| Minimum traffic | Below this, traffic counts as noise — protects quiet nodes from percentage swings on nothing |
| Hold time | How many seconds the deviation persists |
| Cooldown | Interval between repeated messages |

## Good to know

- The usual level is a moving average rather than a fixed number, so daily seasonality is handled automatically.
- A 100% spike means traffic doubled. Values of 50–200% are typical, over 500% is very sharp.
- Constant false alarms are cured by raising minimum traffic and hold time; genuinely quiet nodes are easier to exclude.
- To catch DDoS sooner, drop the hold time to 5–10 seconds — but expect false positives during update bursts.
- Speed is measured on physical interfaces only, docker bridges excluded, so numbers match the dashboard.
