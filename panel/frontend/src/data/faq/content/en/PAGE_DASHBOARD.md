# Dashboard

The whole fleet on one screen: what is online, where load is rising, what went down.

## What you see

- **Fleet summary on top** — total CPU, RAM and network speed across online servers. CPU is weighted by core count, so 100% on an eight-core node counts for more than on a dual-core one. Offline servers are excluded so stale numbers don't inflate the totals. Click a tile to expand a chart of that metric across the whole fleet — over an hour, a day, a week, a month or a year; click again to collapse.
- **Server card** — status, CPU, RAM, disk, network speed, load average, IP (click copies it).
- **Folders** — grouping for cards, collapsible to save space.
- Clicking a card opens server details with charts, processes and a terminal.

## Reading the indicators

| Indicator | What to look at |
|---|---|
| Green dot | Agent is online and sending metrics |
| Red dot | Node unreachable: network, dead agent, firewall or a dead SOCKS5 proxy |
| Load average | Above the core count means processes are queuing. Short spikes are fine |
| Network speed | Counted on physical interfaces only; docker bridges and veth are excluded, otherwise traffic would be double-counted |

## Good to know

- Cards are drag-and-drop, both between folders and inside one. The order is stored on the server and is the same for every administrator.
- The view mode (grid or list, compact or detailed) is kept in the browser, per administrator.
- The metrics refresh interval is set in panel settings, 10 seconds by default.
- A red status with stale numbers is the last successful reading: the panel keeps it so you can see the state at the moment of failure.
