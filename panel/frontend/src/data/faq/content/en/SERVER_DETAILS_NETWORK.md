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

## How the change is applied

1. The node backs up its config, writes the addresses, applies them and starts a 120-second rollback timer.
2. The panel reconnects to the node and confirms the change — that is the proof that connectivity survived.
3. No confirmation before the deadline — the node restores the backup on its own. If the server reboots mid-operation, the unfinished transaction is rolled back before the network comes up.

While waiting, the “Cancel” button rolls the change back manually. After confirmation the panel tries to connect to each new address on the node port — informational only: some hosters take a while to route a new address.
