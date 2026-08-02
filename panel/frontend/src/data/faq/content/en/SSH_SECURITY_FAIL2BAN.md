# Fail2ban

Reads the auth log, counts failed logins and temporarily blocks offending addresses.

| Parameter | Meaning |
|---|---|
| Attempts before ban | How many failures within the window trigger a block. Usually 3–5 |
| Detection window | Over what period attempts are counted, typically 10 minutes |
| Ban time | How long the address stays blocked. An hour is a sane start, a day for persistent scanners |

## Good to know

- Banned addresses are listed, and bans can be lifted individually or all at once.
- Add your own address and the panel's to the ignore list — otherwise a few password typos lock you out.
- Fail2ban won't stop a distributed attack from thousands of addresses: only keys instead of passwords help there.
- Bans live in the node's firewall rules; a bulk firewall profile rollout can wipe them — Fail2ban re-bans on the next attempt.
