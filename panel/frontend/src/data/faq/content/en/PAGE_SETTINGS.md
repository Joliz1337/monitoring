# Settings

Global panel parameters: intervals, time, certificate, backups.

| Setting | Meaning |
|---|---|
| Interface refresh interval | How often the page pulls fresh data. Lower means livelier charts and more requests |
| Metrics collection interval | How often the panel polls nodes. Lower means finer history but more load and a faster growing database |
| HAProxy collection interval | How often balancer statistics are refreshed |
| Panel timezone | Which timezone dates are displayed in. Doesn't change server time |
| Server timezone | Target timezone for time synchronisation on nodes |
| Remnawave install path | Where to look for Remnawave on nodes to manage its nginx |

Separate blocks cover the panel's own SSL certificate, time synchronisation and database backups.

## Good to know

- The metrics interval affects database size the most: five seconds instead of thirty is six times more points. History is thinned automatically, but recent data is kept as is.
- Polling too often across a large fleet strains the network and the nodes rather than the panel.
- The panel timezone is presentation only. To align times in server logs, use time synchronisation.
