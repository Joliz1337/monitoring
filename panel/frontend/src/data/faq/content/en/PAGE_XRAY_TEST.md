# Xray test

Checks proxy configurations: a link, a pasted JSON config, or a subscription. The panel starts a real core (Xray or sing-box), passes actual traffic through it and shows the outcome: whether the server is alive, whether traffic gets through, the latency, and which IP the connection exits from.

## What the verdict means

| Verdict | Meaning |
|---|---|
| **Works** | The request through the proxy succeeded |
| **Works with caveats** | Traffic passes, but latency is above 1.5 s or the exit IP could not be determined |
| **Fails** | The reason is in the verdict column — hover over it or expand the row |

The TCP probe runs before the core starts and rules out dead servers immediately. Hysteria2 and TUIC skip it: they run over UDP, where a silent TCP port means nothing.

## Multi-SNI

Enter several domains and every configuration is checked against each of them; the fastest one gets a badge. This shows which masking domains your provider has not blocked yet.

The "Change transport Host along with SNI" option is on for a reason: with WebSocket, gRPC, XHTTP and HTTPUpgrade the server routes requests by the Host header, so replacing only the SNI returns 404 and the check would wrongly report a block. Turn it off only if you know the server ignores Host.

## Testing from another location

In "Run from" you can pick any of your servers, and the run happens there instead of the panel's datacenter. The panel delivers the core to the node itself — the node never reaches out to GitHub — so this works on heavily filtered networks too. The server needs command execution permission; if it is restricted, the server is listed but cannot be selected.

Local ports 7501–7504 on the node are reserved for these checks — re-applying system optimizations enables the reservation.

## Not supported

Clash YAML subscriptions are not parsed. mKCP obfuscation (`seed`, `headerType`) was removed in Xray 26, so such links are marked unsupported. Keys with certificate verification disabled are routed to sing-box automatically: Xray dropped that option.
