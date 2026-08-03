# Remnawave

Integration with the Remnawave panel: connection statistics, user devices and anomaly detection. Data comes straight from the Remnawave API; node agents aren't involved.

## What's on the page

- Summary: unique users, addresses and devices.
- Top users, filterable by subscription status and by a specific address.
- User card: which addresses they connected from and which devices are registered.
- Connection settings and the ignore list.

## Anomalies

- **By device** — one device used by several accounts, or an account constantly changing devices.
- **By address** — connections from unusual addresses, which looks like a shared subscription.
- **By traffic** — consumption above a threshold, confirmed several times in a row so a one-off spike doesn't raise a false alarm.

Each check has its own toggle, and thresholds are adjustable: IP and ASN margin above the device limit, consecutive confirmations, and the known-clients registry for the User-Agent check. Notifications go to Telegram: either through the shared alerts bot or a dedicated one if configured.

## Good to know

- The user list is cached and refreshed roughly every half hour; connection statistics are rebuilt on their own interval and fully replaced — it's a snapshot of "now", not accumulated history.
- Empty statistics? Verify the API address and token with the connection test button.
- Users who shouldn't raise anomalies (your test accounts, shared subscriptions) belong in the ignore list.
- This page only observes: torrent bans are handled by the blocker page.
