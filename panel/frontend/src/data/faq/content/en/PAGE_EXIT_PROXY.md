# Exit proxy

On enabled nodes the agent runs a local SOCKS5 (127.0.0.1:port) that you point Google traffic to in Remnawave. The node tracks its own IPv4 addresses (primary and the ones added through the panel) plus WARP, checks how Google sees each exit and sends traffic only through a healthy one. The panel shows state, keeps settings and sends notifications — the proxy keeps working without it.

## How the exit is chosen

| Check | What counts as a block |
|---|---|
| Country by Google | Google places the IP in a blocked country (RU by default) — Gemini is unavailable for such an address |
| Search captcha | Search answers with the "unusual traffic" page instead of results |
| Gemini | The Gemini page says the service is not available in your region |
| Custom checks | The URL answers with a forbidden status, redirect or text matching the pattern |

Selection is sticky: the current exit stays while it passes checks and changes only when it goes bad — the node then takes the first healthy one by your priority and drops the old exit's connections so all traffic moves at once. With no healthy exits the first by priority is used and the panel sends a notification.

## Good to know

- The port is the same on every node, so the Remnawave snippet is shared — it sits below the node list. Route all of Google with one rule, otherwise Google sees one session from different IPs.
- A single failed probe (timeout) never switches the exit — only a confirmed block does.
- The self-test goes through the socks itself and shows which IP the traffic really leaves from.
- The proxy is meant for Gemini, search and APIs — do not send YouTube or video into this outbound.
