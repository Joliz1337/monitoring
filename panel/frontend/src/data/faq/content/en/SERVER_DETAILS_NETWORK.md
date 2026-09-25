# Network / IP addresses

Your hoster gave the node another address or a block of addresses — attach them to the interface here, without SSH. The node writes them into its own network config (netplan, systemd-networkd, NetworkManager or `/etc/network/interfaces`), so they survive a reboot. The primary address and anything configured by the hoster are never touched: only addresses added through the panel can be removed.

## Input formats

| Entry | Result |
|-------|--------|
| `203.0.113.10` | one address with a /32 mask (/128 for IPv6) |
| `203.0.113.10/24` | one address with the given mask |
| `203.0.113.10-203.0.113.15` | one address per number in the range (`…10-15` works too) |
| `203.0.113.32/29` | every address of the subnet (network and broadcast are skipped) |
| `2001:db8::2/64` | one IPv6 address; IPv6 subnets are not expanded |

At most 256 addresses per apply. Addresses already present on the interface are skipped.

## Gateway

Usually the field stays empty: replies from the new address leave through the primary address gateway. A gateway is needed when the hoster gave addresses from another network and told you which gateway to send them through. Traffic from those addresses then goes through that gateway, while the primary address keeps working as before. One gateway covers the whole list; addresses with other gateways are added in separate runs and all work at the same time. To change the gateway of an address, remove it and add it again. Requires node 10.31.0 or newer.

## How the change is applied

1. The node backs up its config, writes the addresses, applies them and starts a 120-second rollback timer.
2. The panel reconnects to the node and confirms the change — that is the proof that connectivity survived.
3. No confirmation before the deadline — the node restores the backup on its own. If the server reboots mid-operation, the unfinished transaction is rolled back before the network comes up.

While waiting, the “Cancel” button rolls the change back manually. After confirmation the panel tries to connect to each new address on the node port — informational only: some hosters take a while to route a new address.
