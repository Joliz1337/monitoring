# Backups

A full snapshot of the panel database: servers, profiles, rules, history, settings and the keys used to reach nodes.

## What's inside matters

A backup contains the mTLS keys the panel authenticates to nodes with. That makes the file a secret: whoever holds it can control the whole fleet. On the upside, restoring on a new server needs no node reinstall — connectivity comes back on its own.

## Using it

- Create — one button; the file stays on the panel server, and the last 20 copies are kept, older ones deleted automatically.
- Download — take the file off the server: a copy sitting on the same machine won't survive losing it.
- Restore — upload the file (up to 100 MB). The database is wiped and refilled from the copy, so the current state is lost.

Restart the panel afterwards: `docker compose restart`.

## Good to know

- Metric history lives in the same database and is restored along with everything else.
- Take a copy before risky changes (mass profile rollouts, blocklist experiments): rolling back takes a minute.
- The file is an ordinary PostgreSQL dump — keep it in your own storage and restore it on another machine if needed.
