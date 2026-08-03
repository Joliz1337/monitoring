# Device anomalies

One device under many accounts, or an account hopping between devices, points to resold subscriptions, farms or leaked credentials.

## What counts as an anomaly

| Pattern | Likely cause |
|---|---|
| One device, several accounts | The subscription was shared or passed on |
| One account, constantly new devices | Credentials spread across several people |
| Connections from unusual addresses | The account is used elsewhere or through someone else's proxy |
| Consumption above threshold repeatedly | The account is used beyond its purpose |

## Check settings

Each check has its own on/off toggle; the rest keep working. IP check settings:

- **IP margin** — how many extra addresses are forgiven. Device limit 3, margin 2 — the alarm starts at 6 addresses.
- **Confirmations** — how many times in a row the excess must repeat before a notification arrives. A single random spike won't raise an alarm.
- **ASN margin** — the same, but for providers. Home Wi-Fi plus mobile data for one person is already two providers, which is normal. A notification comes only when providers exceed the device limit plus this margin.

**Known clients** — the list of apps considered normal. A connection from an app not on the list triggers the "Unknown User-Agent" alarm. Add your own app as a single line, e.g. `MyApp/`. Clear the field and save to restore the standard list.

## Good to know

- The device identifier comes from the client app, and not all clients report it the same way: reinstalling the app or updating the OS can look like a new device.
- Matching devices within a family or one office are normal, so judge by scale: ten accounts on one device and two are entirely different stories.
- False positives for specific accounts are removed by adding them to the ignore list.
