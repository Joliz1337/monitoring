# Exit proxy

On enabled nodes the agent runs a local SOCKS5 (127.0.0.1:port) that you point Google traffic to in Remnawave. The node tracks its own IPv4 addresses (primary and the ones added through the panel) plus WARP, checks how Google sees each exit and sends traffic only through a healthy one. The panel shows state, keeps settings and sends notifications — the proxy keeps working without it.

## How the exit is chosen

| Check | What counts as a block |
|---|---|
| Country by Google | Google places the IP in a blocked country (RU by default) — Gemini is unavailable for such an address |
| Search captcha | Search answers with the "unusual traffic" page instead of results |
| Gemini | The Gemini page says the service is not available in your region |
| Custom checks | The URL answers with a forbidden status, redirect or text matching the pattern |

The exit that passes the most checks wins. On a tie between healthy exits the first by your priority wins: WARP, unless you moved it lower, so Google never learns your own addresses from the traffic. If the current exit stops answering or Google blocks it (country, captcha, Gemini), the node leaves it at once. If it merely lost on your own checks, or another exit is equal but higher in priority, the switch waits for the next run to confirm it, so one flaky check does not cut user sessions; the log shows this as “Switch deferred”. On a switch the node drops the old exit's connections so all traffic moves at once. If no exit passes every check, traffic still goes through the best-scoring one and the panel sends a notification.

## Good to know

- The port is the same on every node, so the Remnawave snippet is shared — it sits below the node list. Route all of Google with one rule, otherwise Google sees one session from different IPs.
- A single failed probe (timeout) never switches the exit — only a confirmed block does.
- The stock Claude and ChatGPT checks talk to their APIs, not the websites: claude.ai and chatgpt.com sit behind Cloudflare bot protection and answer server IPs with a browser-check page regardless of country. Such an answer in any custom check counts as "not tested", not as a block. Reddit turns away server addresses in every country, so it is not in the stock set.
- The self-test goes through the socks itself and shows which IP the traffic really leaves from.
- The proxy is meant for Gemini, search and APIs. The first rule of the snippet sends YouTube, Android push connections and Play Store downloads through the direct outbound: they need no geo, and through the proxy they would be thousands of connections and gigabits. Do not send video into the proxy.
