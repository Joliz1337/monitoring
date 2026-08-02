# Вебхук-предупреждение

Перед баном панель отправляет POST-запрос (только HTTPS) на ваш сервис — например, чтобы бот предупредил пользователя в Telegram. Затем выжидает грейс-период («Задержка перед баном») и банит IP на всех нодах.

## Формат запроса

`POST` с заголовком `Content-Type: application/json`. Если задан секрет — добавляется `X-Signature: sha256=<hex>` (HMAC-SHA256 от тела запроса).

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

## Поля

- `event` — всегда `torrent_ban_scheduled`. В тестовом запросе дополнительно приходит `"test": true`.
- `ip` — IP-адрес, который будет забанен.
- `user` — пользователь Remnawave: `uuid`, `short_uuid`, `username`, `telegram_id` (подтягивается из кэша пользователей, может быть `null`).
- `node` — нода Remnawave, где замечен торрент: `name` и `country`.
- `detection` — детали детекта из xray: протокол, сеть, `source`/`destination` (ip:порт), инбаунд/аутбаунд и время обнаружения.
- `remnawave_block` — локальный бан tblocker на самой ноде Remnawave: `blocked`, `block_duration_seconds`, `will_unblock_at`. С баном панели не связан и может отличаться по длительности.
- `ban_duration_seconds` — на сколько секунд панель забанит IP.
- `delay_seconds` — грейс-период между вебхуком и баном.
- `ban_at` / `scheduled_at` — когда IP будет забанен и когда отправлен вебхук (ISO 8601, UTC).

## Полезно знать

- Любое поле внутри `detection` и `remnawave_block` может быть `null`, если Remnawave его не передал.
- Успехом считается ответ со статусом меньше 400. Сбой доставки бан не отменяет — IP всё равно банится после задержки.
- Кнопка «Тестовый вебхук» отправляет запрос в этом же формате с `"test": true` — удобно проверить эндпоинт и подпись.
