# Port tracking

Lets you see traffic per interesting port instead of just the interface total.

## Using it

Add a port and data starts accumulating from the next collection; the first numbers appear within a minute or two. Removing a port stops the counting; already collected history stays.

To add one port across dozens of nodes, use bulk actions.

## Good to know

- Counting happens on the node via packet accounting rules, so extra ports mean extra rules: keep the list short and meaningful.
- Port added but no traffic? Check that a service actually listens there and that traffic isn't going through a different port (clients connecting to 443 rather than the one you added, for example).
- Per-port data doesn't replace the interface counter: the sum of ports is always lower, because some traffic bypasses the tracked ones.
