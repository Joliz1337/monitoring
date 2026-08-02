# Server details

Diagnostics for a single node: charts, processes, terminal and power control. This is where you go to find out why this particular server is slow.

## What you see

- Charts for CPU, RAM, disk, network and load average over 1 hour, 24 hours, 7 days, 30 days or a year.
- Per-core CPU breakdown (on the 1-hour and 24-hour ranges) — shows whether one core is pinned or load is spread.
- Process table sortable by CPU and memory.
- Terminal, reboot and shutdown buttons, system information.

## How to read it

| What you see | What it means |
|---|---|
| One core at 100%, others idle | Single-threaded workload — adding cores won't help |
| Load average above core count | Processes are queuing. Short spikes are fine, a permanent excess is not |
| RAM nearly full but the server is fine | Linux uses free memory as cache; look at available memory, not free |
| Charts have gaps | The node was offline then — there is no history for that window |
| No temperatures | Sensors are unavailable, common on VPS and in containers |

## Good to know

- Kill a process from the terminal: `kill <PID>` politely or `kill -9 <PID>` forcibly; the PID is in the process table.
- **Reboot** brings the server back in a minute or two. **Shutdown** on a VPS without access to the provider console means it won't come back on its own.
- Empty metrics mean the agent isn't answering: check connectivity with "Test" on the Servers page.
- Metric history is stored in the panel database and thinned over time: recent points are detailed, older ones are aggregated hourly and daily.
