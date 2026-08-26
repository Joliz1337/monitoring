# Settings

Global panel parameters, organised into tabs. Everything is saved as soon as it changes; the open tab is kept in the page address.

| Tab | What's there |
|---|---|
| Interface | Language, server list layout, how often the page pulls fresh data, panel timezone, how charts are drawn |
| Nodes | How often the panel polls nodes, time synchronisation on servers, Remnawave install path |
| Sections | Which sections appear in the menu |
| System | Panel host resources, the panel's own SSL certificate, update channel (stable / dev) |
| Backups | Database backups and automatic backups to Telegram |

## Good to know

- The metrics interval ("Nodes") affects database size the most: five seconds instead of thirty is six times more points. History is thinned automatically, but recent data is kept as is.
- Polling too often across a large fleet strains the network and the nodes rather than the panel.
- The panel timezone ("Interface") is presentation only. To align times in server logs, use time synchronisation ("Nodes").
