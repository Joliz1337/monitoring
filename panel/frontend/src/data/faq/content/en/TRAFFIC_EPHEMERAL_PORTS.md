# Ephemeral ports

When the server connects somewhere itself, the kernel assigns the connection a free port number from a configured range. That supply is not shared server-wide: it is counted separately for each pair of "our address → destination address and port". A single such route from one address cannot hold more connections than there are numbers in the range.

That is why two levels are shown here. The bar on an address is its busiest route, meaning how close that address is to the ceiling. Expanding it lists the routes themselves with the numbers they hold.

## What to do as it fills up

- **Add an address.** The supply multiplies by the number of outbound IPs. The Source Pool section spreads connections across every address of the server automatically.
- **Split by destination port.** Connections to ports 443 and 8443 of the same server are counted separately, each with its own supply.
- **Check Time Wait.** Closed connections hold their number for another couple of minutes. With `tcp_tw_reuse = 1` in system optimizations the kernel hands those numbers to new outbound connections right away, and they are not counted as taken.

Filling past 85% is dangerous beyond outright failures: to find a free number the kernel scans nearly the whole range, and the CPU drifts into system time at the same traffic level.
