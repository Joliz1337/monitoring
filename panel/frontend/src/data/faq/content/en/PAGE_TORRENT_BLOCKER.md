# Torrent blocker

The panel polls Remnawave, finds sessions with torrent traffic and bans those addresses on the nodes.

## Parameters

| Parameter | Meaning |
|---|---|
| Poll interval | How often the panel asks Remnawave for new detections. 5 minutes by default |
| Ban duration | How long an address stays blocked |
| Where to ban | «Everywhere except selected» — every node except the list; «Only on selected» — only the nodes in the list. Each mode keeps its own list; a whole folder can be added at once |
| Webhook warning | Send a notification and give the user time to stop the client before the ban |

## Ban only ports

Click a server in the list to choose how it bans: the mode default («Don't ban» or «Ban the whole IP») or «Ban only ports». Useful on a bridge where each port leads to its own exit node: close only the port of the node where torrents are forbidden, and the client keeps every other server. Ports are comma-separated, ranges like `10000-10010` work, TCP and UDP are both closed. Requires node 10.35.0.

## How the warning works

With the webhook enabled the panel first sends a request to your endpoint (a bot that messages the user in Telegram, for example), waits the configured delay and only then bans. A failed webhook does not cancel the ban — the block happens regardless.

## Good to know

- Detection is Remnawave's job, blocking is the panel's: without a working Remnawave integration this page is useless.
- Bans use the same mechanism as the blocklist and expire automatically.
- Remnawave may also block the user on its own side with its own duration — that block is independent of the panel's.
- False positives usually come from update clients that use P2P: such nodes are easiest to exclude.
