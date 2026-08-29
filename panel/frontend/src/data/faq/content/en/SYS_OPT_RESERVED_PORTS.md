# Reserved ports

When a server opens an outbound connection, the kernel assigns it a random free port. With optimizations applied, that range on VPN nodes is widened (1024–65535) and includes ports your services listen on. While a service is running its port is busy, but during a restart the port is free for a few seconds — an outbound connection can grab it, and the service then fails to start. Reserving a port removes it from automatic assignment for good: the service binds it as usual, and nothing can take it by accident.

The node's own ports — the panel API port, internal 7500 and Remnawave's 2222 — are always reserved automatically. This screen adds extras: the global field applies to every node, "Per-node ports" only to that node (the lists are combined).

## Good to know

- Format: ports and ranges separated by commas — `5201, 8443-8450`.
- Add ports of your own services running on nodes (databases, panels, bots), especially anything that restarts automatically.
- Reserving costs nothing and breaks nothing: it only blocks automatic assignment to outbound connections; listening on the port works as before.
- Changes reach nodes immediately; an offline node receives the list once it is back. Takes effect on nodes with system optimizations installed (without them the list is stored and applied together with the optimizations).
