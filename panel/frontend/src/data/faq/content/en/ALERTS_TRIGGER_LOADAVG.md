# Trigger: load average

Load average is the number of processes waiting to run. The threshold is a multiplier of the core count, not an absolute number.

## Parameters

| Parameter | Meaning |
|---|---|
| Multiplier | Threshold = cores × multiplier. With 1.5 on 4 cores the alert fires from LA 6 |
| Hold time | How many seconds LA stays above the threshold |
| Cooldown | Interval between repeated messages |

## Good to know

- A multiplier beats an absolute value because LA 8 is a catastrophe on two cores and half the capacity on sixteen.
- Rule of thumb: LA within the core count is fine, above means queuing. Production nodes usually use 1.2–1.5.
- LA is jumpy, so a hold time under a minute almost always produces false alarms.
- LA is not CPU usage: the processor can be idle while LA grows because processes wait on disk or network.
