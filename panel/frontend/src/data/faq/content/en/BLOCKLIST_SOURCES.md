# Blocklist sources

External address lists the panel downloads by URL, parses and distributes to nodes. Refresh is automatic, on a schedule.

## What works

Any list with one address or subnet per line; comments and extra fields are stripped automatically. Reputation lists work well: botnet networks, known attack sources, spam networks.

## What to avoid

**Bogon lists** (collections of "networks that shouldn't exist on the internet") contain private ranges such as `10.0.0.0/8` and `127.0.0.0/8`. Applied as rules, they make a node block its own loopback and docker network, and the panel loses contact with it. The panel filters such entries automatically, but such a source brings no value anyway — don't add it.

Be careful with huge aggregators too: a list of millions of addresses inflates node memory. A few hundred thousand entries is a sane ceiling.

## Good to know

- Direction is per source: a list can be applied to incoming or outgoing traffic.
- After adding a source, check how many addresses actually landed — a big difference means entries were dropped as invalid or private.
- The allowlist outranks sources: an address in it won't be blocked even if an external list includes it.
- If a source is unreachable or returns garbage, the previous rules stay in force — nodes are never left wide open.
