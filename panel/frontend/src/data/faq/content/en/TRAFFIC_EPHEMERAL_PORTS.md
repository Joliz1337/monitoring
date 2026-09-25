# Ephemeral ports

When the server connects somewhere itself, the kernel assigns the connection a free port number from a configured range. That supply is not shared server-wide: it is counted separately for each pair of "our address → destination address and port".

The kernel hands out only half of the range quickly — ports of one parity. Once that half runs out on a route, every new connection scans it end to end, and the CPU drifts into system time at the same traffic level even though the other half is still free. That is why the bar and the remainder are counted up to the end of this fast half, not the whole range.

The bar on an address is its busiest route. Expanding it lists the routes themselves with the numbers they hold.

## What to do as it fills up

- **Add an address.** The supply multiplies by the number of outbound IPs. The Source Pool section spreads connections across every address of the server automatically.
- **Split by destination port.** Connections to ports 443 and 8443 of the same server are counted separately, each with its own supply.
- **A `127.0.0.1 → 127.0.0.1:port` route** is usually nginx in front of Xray. The Remnawave nginx profile template spreads these connections across sixteen addresses `127.0.0.1–127.0.0.16`, each with its own supply; if the profile holds an older config, press "Apply template".
- **Check Time Wait.** Closed connections hold their number for another couple of minutes. With `tcp_tw_reuse = 1` in system optimizations the kernel hands those numbers to new outbound connections right away, and they are not counted as taken.
