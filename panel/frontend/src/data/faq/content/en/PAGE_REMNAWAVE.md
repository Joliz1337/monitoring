# Remnawave

Integration with the Remnawave panel: where clients connect from, their devices and anomaly detection. Data comes straight from the Remnawave API; node agents aren't involved.

## What's on the page

- **Overview** — summary, nodes, the client map and the IP table: activity, user, node, city, provider. The table shows what's currently visible on the map; clicking a node keeps only its clients.
- **Map** — countries from afar, cities closer in. Circle size is how many clients are online now, color compares with the same hour yesterday: yellow — noticeably fewer, red — far fewer, red outline — there were clients yesterday and none now. Clicking a city fans out its clients, clicking a dot opens the IP card.
- **Users** — the user card with a mini map: where they connect from now and before, through which nodes, which providers, and their traffic over the last 24 hours.
- **Route** — a button next to the IP in the table and in the card. The node the client is connected to traces the route to their address, and the map draws the traffic path through the cities where the routers are. A leg whose latency is higher than the distance explains is highlighted yellow or red — that shows where time is lost on the way. The Remnawave node has to be added to the panel as a server.
- Connection settings and the ignore list.

## Anomalies

- **By device** — one device used by several accounts, or an account constantly changing devices.
- **By address** — connections from unusual addresses, which looks like a shared subscription.
- **By traffic** — consumption above a threshold, confirmed several times in a row so a one-off spike doesn't raise a false alarm.

Notifications go to Telegram: either through the shared alerts bot or a dedicated one if configured.

## Check settings

Each check has its own on/off toggle; the rest keep working. IP check settings:

- **IP margin** — how many extra addresses are forgiven. Device limit 3, margin 2 — the alarm starts at 6 addresses.
- **Confirmations** — how many times in a row the excess must repeat before a notification arrives. A single random spike won't raise an alarm.
- **ASN margin** — the same, but for providers. Home Wi-Fi plus mobile data for one person is already two providers, which is normal. A notification comes only when providers exceed the device limit plus this margin.
- **Smart detection** — before raising an alarm the panel checks how much the user downloaded over the last day. Below the threshold there is no alarm: someone travelling changes addresses and providers, but one person doesn't pull that much traffic, while a subscription shared by several people does. The threshold sits right next to it, 20 GB per day by default. Turn it off and notifications come purely by the number of addresses, as before.

The device data check has the same **smart detection**: the "invalid device data" notification arrives only when the user downloaded more than the threshold over the last day. Broken device data alone is not worth an alarm — it matters when the subscription pushes serious traffic. It has its own threshold, set under the check's toggle.

**Known clients** — the list of apps considered normal. Every device introduces itself with a User-Agent string, e.g. `Happ/1.6.2 iPhone`. The panel compares the start of that string against each line of the list — if none matches, the "Unknown User-Agent" alarm fires.

The rules are simple, one line — one app:

- a line matches from the start: `Happ/` covers `Happ/1.6.2`, `Happ/2.0` and any other version;
- `*` stands for any number of characters: `*https*` allows "https" anywhere in the string, `v2raytun/*` — any platform;
- `?` stands for exactly one character;
- letter case doesn't matter.

Add your own app as a single line like `MyApp/`. Clear the field and save to restore the standard list.

## Good to know

- The user list is cached and refreshed roughly every half hour; "online" is the latest collection snapshot, while IP history is kept for as many days as set in the settings (30 by default).
- Cities come from the free DB-IP database, which the panel updates monthly on its own. Accuracy is city level, sometimes only region; mobile carriers often place a client in another city. Clients connecting through your relays show up with the relay's address, so their location is unknown.
- Empty statistics? Verify the API address and token with the connection test button.
- Users who shouldn't raise anomalies (your test accounts, shared subscriptions) belong in the ignore list.
- This page only observes: torrent bans are handled by the blocker page.
