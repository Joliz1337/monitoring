# DDoS-Guard GRE tunnels

DDoS-Guard ("Network protection") filters the traffic of a protected address and delivers it to the server through a GRE tunnel. Clients connect to the DDoS-Guard address (CE), and the real IP of the server stays hidden from them. It works like a relay, only without a separate server and HAProxy.

## Setup

1. In the DDoS-Guard dashboard open the service → "About the service" → "Connection" and copy the whole "Connection parameters" block.
2. Here, click "Add tunnel", paste the text into "Paste from the DDoS-Guard page" and click "Fill in". PE is filled in automatically — it is the other address of the /30, usually CE − 1.
3. Save. In a minute or two the tunnel status in the DDoS-Guard dashboard turns "Active", and this page shows "Packets are flowing".
4. Give clients the CE address: set it as the host address in Remnawave. Xray listening on `0.0.0.0` accepts connections on CE by itself.

There is no need to add the static routes from the DDoS-Guard instructions by hand. The node sends only traffic from the CE address into the tunnel and leaves the server's main route alone.

## Firewall

| What to allow | Why |
|---|---|
| Everything from the GRE DST address (`185.129.101.255`) | GRE packets come from it. Without this rule UFW lets the tunnel through only while the server itself is sending something, and the tunnel drops after a pause or a reboot |
| ICMP from `198.18.100.0/24` | The DDoS-Guard health-check network. UFW lets ping through by default; the rule is needed only if ICMP is closed |

Add the GRE DST rule in the server terminal: `ufw allow from 185.129.101.255`. A panel firewall profile cannot express such a rule (a rule needs a port) and resets manual rules when applied. The panel adds both addresses to the anti-DDoS whitelist itself.

## Good to know

- "Packets are flowing" is based on incoming packets of the tunnel. DDoS-Guard pings CE regularly, so packets keep coming even without clients. If none arrived for 5 minutes, the tunnel is inactive on the DDoS-Guard side or the hoster blocks GRE. With notifications on, a Telegram message is sent.
- "Check" pings PE from the CE address and sends a request out from CE. Seen from outside, the server must be CE.
- GRE SRC must be on a network card. If the server is behind a cloud NAT and the public address is not on its interfaces, GRE usually does not pass such NAT.
- Moving from the `ddg-tunnel.sh` script — disable the tunnel there first (menu item 4). The kernel will not bring up a second tunnel with the same GRE SRC and DST, and the panel shows "Conflict".
- DNAT rules and Docker ports published in bridge mode do not work on the CE address: the kernel picks the reply route before the address is translated back. HAProxy and Xray on the host network work.
- Requires node 10.35.0. To check on the server: `ip -d link show gre-ddg1`, `ip rule` (rules 1150 and 1151), `systemctl status mon-gre`.
