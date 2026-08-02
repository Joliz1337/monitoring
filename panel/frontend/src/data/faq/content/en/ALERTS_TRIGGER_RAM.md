# Trigger: RAM

Fires when used memory exceeds a threshold or grows sharply.

## Parameters

| Parameter | Meaning |
|---|---|
| Critical threshold | Percentage of memory in use, usually 85–90 |
| Spike | How sharply consumption grew relative to the usual level |
| Hold time | How many seconds the excess persists |
| Cooldown | Interval between repeated messages |

## Good to know

- What counts is **actually used** memory (total minus available), not the sum including cache. That's why the panel may show 40% while `top` shows 90%: the difference is disk cache, which is freed instantly.
- The real danger is falling available memory and active swap (the si/so columns in `vmstat`), not usage on its own.
- Above 95% the kernel starts killing processes, and that takes seconds — keep the hold time short or the alert arrives after the OOM.
- One-off spikes during builds or backups are handled by hold time; steady growth that never drops is a leak, find the process in server details.
