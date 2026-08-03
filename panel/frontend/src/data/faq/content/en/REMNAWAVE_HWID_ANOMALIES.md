# Device anomalies

One device under many accounts, or an account hopping between devices, points to resold subscriptions, farms or leaked credentials.

## What counts as an anomaly

| Pattern | Likely cause |
|---|---|
| One device, several accounts | The subscription was shared or passed on |
| One account, constantly new devices | Credentials spread across several people |
| Connections from unusual addresses | The account is used elsewhere or through someone else's proxy |
| Consumption above threshold repeatedly | The account is used beyond its purpose |

## Good to know

- The device identifier comes from the client app, and not all clients report it the same way: reinstalling the app or updating the OS can look like a new device.
- Matching devices within a family or one office are normal, so judge by scale: ten accounts on one device and two are entirely different stories.
- The traffic threshold and confirmation count are configurable: raising them cuts the noise from one-off spikes.
- Checks you don't need can be switched off individually in settings — the rest keep working.
- False positives for specific accounts are removed via the ignore list or the whitelist by name mask (e.g. `vip-*`).
