# Xray test

Checks proxy configurations: a link, a pasted JSON config, or a subscription. The panel starts a real core (Xray or sing-box), passes actual traffic through it and shows the outcome: whether the server is alive, whether traffic gets through, the latency, and which IP the connection exits from.

## What the verdict means

| Verdict | Meaning |
|---|---|
| **Works** | The request through the proxy succeeded |
| **Works with caveats** | Traffic passes, but latency is above 1.5 s or the exit IP could not be determined. Which of the two is stated in the expanded row |
| **Fails** | The reason is in the verdict column — hover over it or expand the row |

The TCP probe runs before the core starts and rules out dead servers immediately. Hysteria2 and TUIC skip it: they run over UDP, where a silent TCP port means nothing.

## Subscription client and HWID

Key-issuing panels look at what made the request. If device binding is enabled and the client sends no device identifier, you get a text instruction instead of keys — a formally valid subscription with nothing to check.

The "Client" field picks what the panel presents itself as. Happ profiles (iPhone, Android, Windows, macOS) send the device identifier and its other headers like the real app does; v2rayNG, Clash, sing-box and the browser send only a User-Agent.

The device identifier is derived from the subscription URL and never changes: the same address always yields the same HWID, regardless of the chosen device or panel restarts. That way checks do not register new devices in your panel or eat the key owner's limit.

A subscription may return a link list or ready-made configs — including an array of several profiles, each with its own name and set of servers. All of them are parsed, and the profile name is prefixed to the server name so you can tell which server came from where in a long list.

## Test options

**Full check.** On — the panel starts a proxy core and actually reaches the internet through it; that is the only way to know a key works. Off — only the fast probes remain, with no core started: domain resolution and a TCP connection to the port. Fast mode is handy for weeding dead servers out of a long list in seconds, but "the port answers" is not "traffic passes".

**Inspect the SNI domain certificate.** Separately from the proxy, the panel opens a TLS connection to the domain the key lists as SNI and looks at its certificate: who issued it, who it is for, how long it is valid, which TLS version is used.

Why it matters:

- For **REALITY**, the SNI is the masking domain your server hides behind. If it stopped answering or its certificate changed, the disguise no longer looks convincing.
- An unexpected issuer is a sign of **interception**: when a provider substitutes the connection, you get its certificate instead of the real one. Self-signed certificates get their own warning.
- Expiry is visible too — worth noticing early on your own servers.

The probe sends nothing through the proxy and costs almost no time, so it can stay on permanently.

**Measure link speed.** Downloads a ~10 MB test file through the proxy and computes megabits per second. Off by default: it takes noticeably longer and spends traffic on every check. Turn it on when suitability of the channel matters, not just liveness.

## How results are laid out

Results are stacked in three levels:

1. **Configuration** — the key's name and address, best ping, best latency and a counter like "2/6": how many checks passed out of how many. The verdict is the best of what it contains: if at least one SNI works, the configuration is usable. The same address coming from different subscription profiles stays as separate rows, because those are different keys.
2. **Where it ran** — the panel or a specific node. Always shown; a single location is expanded right away.
3. **SNI** — the check itself with its metrics. Expand it for the failure reason, the hint and the core's verbatim answer.

## Reading the results table

| Column | Meaning |
|---|---|
| **TCP** | Time to establish a plain TCP connection to the server. A measure of network closeness; the proxy is not involved yet |
| **Connect** | The whole first request through the proxy: handshake, encryption, session setup. A high value with low TCP means a heavy handshake |
| **Latency** | A repeat request over the established connection. This is what you actually feel in use |
| **Mbps** | Speed, if measurement is enabled |
| **Exit IP** | The address and country the connection leaves from. If the country differs from what the key's seller promised, it shows immediately |

The counters above the table — Works, Works with caveats, Fails — double as a filter. Click one and you get the servers that have at least one such check. They are shown whole: every SNI and every run location, not just the matching rows — so you see what broke and what it broke against. How many servers are currently listed is printed next to the counters. Several filters can be active at once; Reset brings everything back.

Next to them sits a separate **Possible DPI block** filter. It collects keys whose server port answers while traffic does not pass: the connection opens and silently stalls, resets or times out. That is what DPI filtering looks like — the TCP handshake is allowed through and the connection itself is throttled. Explicit failures stay out of it: a closed port, a wrong certificate or a bad key mean a configuration problem, not a block. Compare such a run with a check from another location: if the same keys are alive from there, the filtering is on your path.

Rows expand: inside are the server IP, DNS resolution time, average TCP and jitter, HTTP status, certificate details and the tail of the core's output explaining a failure.

## Block or a wrong key

These two look identical — "traffic does not pass" — but need different fixes, and the panel tells them apart.

REALITY has a fallback: a client that does not match gets the real masking site. So a live server must answer a plain TLS handshake with its own SNI. On failure the panel runs exactly that probe:

- **The masking site answers** — the server is alive and reachable, there is no block. The key parameters do not match: public key, short id, SNI or flow.
- **The masking site is silent while the port answers** — the connection is throttled on the way. The key is not at fault, and such rows land in the "Possible DPI block" filter.

A run from a node in another country settles it: if the same key is alive from there, the block is on your segment.

## Why a key fails

Expand a failed row — it holds the reason, a hint and the core's verbatim answer.

The most common cases:

- **The server certificate is issued for a different domain.** For REALITY this is the main sign that the server rejected the masking parameters: the public key, short id or SNI does not match. Check them in the key.
- **The server refused the connection.** The port is closed, the service is down, or the address in the key is wrong. The TCP probe in the same row shows whether the port answers at all.
- **The server rejected the credentials.** The UUID or password does not match — the key was revoked or copied with an error.
- **A different protocol answers on the port.** Usually means a wrong port or another service sitting on it.
- **The server accepted the connection but never answered the handshake.** For REALITY this is the normal reaction to wrong parameters: the server stays silent instead of refusing. Check the public key, short id and SNI. The other option is filtering on the way.
- **The server did not answer in time.** Overloaded, unreachable from this point, or cut on the way — try running the check from another location.

The "What the core reported" block is the verbatim answer from Xray or sing-box, untranslated. It helps with unusual failures: you can take it straight to the server owner.

## Saved sources and SNI sets

A subscription or link list you check regularly can be stored with the "Save" button next to the input — it then appears as a chip above the field, one click away. Subscription URLs are stored encrypted. SNI sets work the same way: save a list of domains under a name and apply it with one button. Renaming, editing and deleting live on the "Saved" tab.

## Multi-SNI

Enter several domains and every configuration is checked against each of them; the fastest one gets a badge. This shows which masking domains your provider has not blocked yet.

The "Change transport Host along with SNI" option is off by default — the Host is often not equal to the SNI, and changing it blindly does more harm than good. Turn it on when they do match on your key: with WebSocket, gRPC, XHTTP and HTTPUpgrade the server routes requests by the Host header, so replacing only the SNI would return 404 and the check would wrongly report a block.

The number of checks is not capped: they queue up and run in batches, a few at a time per selected location. A large run takes a while but finishes — the log shows how many are already done.

## Testing from another location

In "Run from" you can tick several places at once — the panel itself and any of your servers. The list has search and folders, just like bulk actions, so a whole folder can be ticked at once. Every key is checked from every ticked place, and a "From" column appears in the table — you immediately see that a key is alive from Germany but already dead from Russia.

The panel delivers the core to the node itself — the node never reaches out to GitHub — so this works on heavily filtered networks too. The server needs command execution permission; servers without it are not listed, and their count is shown in a line below.

Local ports 7501–7504 on the node are reserved for these checks — re-applying system optimizations enables the reservation.

## Core version

The "Cores" tab lists released Xray and sing-box versions and shows which one is in use. The default is "Always the newest", pre-releases included — new transports land there first. You can pin a specific version instead: downloaded ones sit side by side, so switching is instant.

The panel runs the core binary itself, so downloads are treated strictly. Xray publishes a checksum next to each release, so those versions are verified and may come through the mirror when GitHub is blocked. sing-box publishes none, so its unpinned versions are downloaded only straight from GitHub, where TLS provides the guarantee. Without direct access, pick a version marked "verified".

## Not supported

Clash YAML subscriptions are not parsed. mKCP obfuscation (`seed`, `headerType`) was removed in Xray 26, so such links are marked unsupported. Keys with certificate verification disabled are routed to sing-box automatically: Xray dropped that option.
