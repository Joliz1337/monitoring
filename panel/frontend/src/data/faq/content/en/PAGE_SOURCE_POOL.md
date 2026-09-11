# Source IPs

Each pair "node address → destination address:port" has only 64,512 outbound ports. When tens of thousands of clients hit one popular site, the ports run out and the node's CPU burns on searching for a free one. This section spreads outbound TCP across all IPv4 addresses of the node: Xray marks connections with 30 marks (101–130), and the node itself maps the marks to its addresses and installs routing rules in the kernel — the port ceiling grows with the number of addresses.

## How to set it up

1. Give the node more addresses — the "Network / IP addresses" card on the server page. With a single address there is nothing to spread.
2. Turn on the toggle on this page. The node installs the rules and keeps them alive on its own; unchecking an address excludes it from the spread.
3. Paste 30 marked outbounds and a balancer into the Xray config (Remnawave → Config Profiles). The ready-made snippet with a copy button lives in the Exit proxy section, "Nodes" tab, block "Config for Remnawave: source IP pool".

The config is shared by the whole fleet: marks are the same everywhere, and which mark goes to which address is decided by each node itself, round-robin (30 marks over 3 addresses — 10 each). On a node with the pool off or a single address the marks change nothing — traffic flows as before.

## Config example

```json
{
  "outbounds": [
    { "tag": "DIRECT", "protocol": "freedom" },
    { "tag": "BLOCK", "protocol": "blackhole" },
    {
      "tag": "pool-01",
      "protocol": "freedom",
      "settings": {},
      "streamSettings": { "sockopt": { "mark": 101 } }
    },
    {
      "tag": "pool-02",
      "protocol": "freedom",
      "settings": {},
      "streamSettings": { "sockopt": { "mark": 102 } }
    },
    … pool-03 … pool-29 the same way, marks 103–129 …
    {
      "tag": "pool-30",
      "protocol": "freedom",
      "settings": {},
      "streamSettings": { "sockopt": { "mark": 130 } }
    }
  ],
  "routing": {
    "balancers": [
      {
        "tag": "source-pool",
        "selector": ["pool-"],
        "strategy": { "type": "roundRobin" }
      }
    ],
    "rules": [
      { "type": "field", "protocol": ["bittorrent"], "outboundTag": "BLOCK" },
      { "type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK" },
      … exit proxy rules and the rest of your rules …
      { "type": "field", "network": "tcp", "balancerTag": "source-pool" }
    ]
  }
}
```

What matters in the example:

- The first outbound stays your default one; pool outbounds go to the end of the list. If your direct outbound has a `domainStrategy`, repeat it in `settings` of every pool outbound.
- `selector: ["pool-"]` picks up every tag with this prefix, `roundRobin` hands out connections in turn.
- The rule with `balancerTag` must be the very last one in `rules`: it catches all remaining TCP, so blocks and exit proxy rules have to sit above it. UDP is not matched and flows as before — marks do not apply to UDP, and the port ceiling concerns TCP only.

## Good to know

- Xray can set the mark only with NET_ADMIN, and the node installs its routing rules in the host network — so remnanode has to run with `network_mode: host` and `cap_add: NET_ADMIN`. The panel installer deploys it exactly that way.
- The pool and Exit proxy cannot be enabled together on one node: both decide which address the node leaves from. In the shared Xray config they coexist — on a node with exit proxy the marks simply do nothing.
- Statuses: "Active" — the rules are in place; "Drift" — some marks vanished from the kernel, the node restores them within a minute; "Error" — the reason is in the hint next to it, most often exit proxy is enabled on the node. Requires node 10.29.0.
- To verify on the node: `ip rule` shows fwmark rules for marks 101–130 (hex 0x65–0x82), `ss -tn` shows connections with different source addresses.
