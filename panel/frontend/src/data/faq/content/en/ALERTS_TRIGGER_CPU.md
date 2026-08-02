# Trigger: CPU

Fires when processor load stays above a threshold or jumps well above the node's normal level.

## Parameters

| Parameter | Meaning |
|---|---|
| Critical threshold | Absolute percentage, usually 80–90 |
| Spike | Growth relative to the node's usual level: sits at 30%, spike 40% — alert at 70% |
| Hold time | How many seconds load stays above the threshold |
| Cooldown | Interval between repeated messages |

## Good to know

- Both modes can run together: the threshold catches disasters, the spike catches unexpected growth on a normally quiet node.
- The usual level is a moving average, so after moving workloads onto a node it needs time to adapt.
- Short bursts during backups and log rotation are normal: raise the hold time rather than the threshold.
- To see what is actually burning CPU, open server details: the process table and per-core breakdown show whether it's one thread or everything.
