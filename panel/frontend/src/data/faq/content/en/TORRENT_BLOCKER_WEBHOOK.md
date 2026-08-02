# Webhook warning

Before banning, the panel sends a POST request (HTTPS only) to your service — so a bot can warn the user in Telegram, for example. Then it waits out the grace period and bans the address on every node.

## Request format

`POST` with `Content-Type: application/json`. If a secret is set, an `X-Signature: sha256=<hex>` header is added (HMAC-SHA256 of the request body).

```json
{
  "event": "torrent_ban_scheduled",
  "ip": "1.2.3.4",
  "user": {
    "uuid": "d6ac70b3-...",
    "short_uuid": "aB3xK9",
    "username": "user123",
    "telegram_id": 123456789
  },
  "node": {
    "name": "Germany-1",
    "country": "DE"
  },
  "detection": {
    "protocol": "bittorrent",
    "network": "tcp",
    "source": "1.2.3.4:53210",
    "destination": "198.51.100.20:6969",
    "inbound_tag": "VLESS_TCP",
    "inbound_name": "Germany VLESS",
    "outbound_tag": "DIRECT",
    "detected_at": "2026-07-25T10:00:00.000Z"
  },
  "remnawave_block": {
    "blocked": true,
    "block_duration_seconds": 600,
    "will_unblock_at": "2026-07-25T10:10:00.000Z"
  },
  "ban_duration_seconds": 1800,
  "delay_seconds": 60,
  "ban_at": "2026-07-25T10:01:00+00:00",
  "scheduled_at": "2026-07-25T10:00:00+00:00"
}
```

## Fields

- `event` — always `torrent_ban_scheduled`. A test request also carries `"test": true`.
- `ip` — the address about to be banned.
- `user` — the Remnawave user: `uuid`, `short_uuid`, `username`, `telegram_id` (pulled from the user cache, may be `null`).
- `node` — the Remnawave node where the torrent was seen: `name` and `country`.
- `detection` — detection details from xray: protocol, network, `source`/`destination` (ip:port), inbound/outbound and detection time.
- `remnawave_block` — the local tblocker ban on the Remnawave node itself: `blocked`, `block_duration_seconds`, `will_unblock_at`. Unrelated to the panel's ban and may differ in length.
- `ban_duration_seconds` — how long the panel will ban the address.
- `delay_seconds` — the grace period between webhook and ban.
- `ban_at` / `scheduled_at` — when the ban happens and when the webhook was sent (ISO 8601, UTC).

## Good to know

- Any field inside `detection` and `remnawave_block` can be `null` if Remnawave didn't provide it.
- Any response below status 400 counts as success. A delivery failure does not cancel the ban — the address is blocked after the delay regardless.
- The test button sends a request in the same format with `"test": true` — handy for checking your endpoint and signature.
