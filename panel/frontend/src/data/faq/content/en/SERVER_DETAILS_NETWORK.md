# Network / IP addresses

Your hoster gave the node another address or a block of addresses — attach them to the interface here, without SSH. The node writes them into its own network config (netplan, systemd-networkd, NetworkManager or `/etc/network/interfaces`), so they survive a reboot. Only the primary address, the address the panel uses to reach the node and DHCP-assigned addresses cannot be removed. To remove several at once, tick them (the checkbox next to the interface name selects them all) and click “Remove selected” — up to 256 addresses of one interface at a time.

## Hoster addresses

An address configured by the hoster is removed the same way, but the panel does not edit the hoster config: a mistake in someone else's network config can leave the server offline after a reboot. The node takes the address off the interface, and whenever the hoster config brings it back — after a reboot or a network restart — takes it off again. The address stays in the list struck through and marked “removed by panel”; the “Restore” button puts it back. Requires node 10.31.0 or newer.

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

## A new network card

If the cloud attached another network to the server, for example the external network as a second interface in VK Cloud, the new card shows up in the list as down. Pick it when adding addresses: the node brings the card up together with them, keeps it up after every reboot and takes it down again once you remove its last address. If the addresses come from another network, fill in the gateway too, so replies from them leave through the new card. Requires node 10.32.0 or newer.

## How the change is applied

1. The node backs up its config, writes the addresses, applies them and starts a 120-second rollback timer.
2. The panel reconnects to the node and confirms the change — that is the proof that connectivity survived.
3. No confirmation before the deadline — the node restores the backup on its own. If the server reboots mid-operation, the unfinished transaction is rolled back before the network comes up.

While waiting, the “Cancel” button rolls the change back manually. After confirmation the panel tries to connect to each new address on the node port — informational only: some hosters take a while to route a new address.
