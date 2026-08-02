# Telegram notifications

The single delivery channel for alerts. Takes a couple of minutes to set up.

## Setup

1. In Telegram open **@BotFather**, send `/newbot`, pick a name.
2. Paste the resulting **Bot Token** (looks like `1234567890:AaBbCc...`) into the token field.
3. Create a group and add the bot — or just use a private chat with it.
4. Find the **Chat ID**: via **@userinfobot**, or open `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id": ...}`.
5. Paste the Chat ID and run the test — a message should arrive.

## Good to know

- A group Chat ID is negative (`-1001234...`), a private chat ID is positive.
- The bot must be allowed to post in the group; if the group uses topics, it must see the right one.
- The token is a secret: anyone holding it can post as your bot. Leaked? Revoke it in @BotFather.
- One chat serves the whole panel. For several recipients, use a group.

## Test errors

| Response | Cause |
|---|---|
| `401 Unauthorized` | Wrong or revoked token |
| `chat not found` | Wrong Chat ID, or the bot isn't in the chat |
| `bot was blocked by the user` | The private chat blocked the bot |
| Timeout | The panel server can't reach `api.telegram.org` |
