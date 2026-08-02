# Traffic

Network load for one node: how much data passed, through which ports and in what connection states.

## What's on the page

- Total volume for the selected period, split into received and sent.
- Hourly and daily charts — the daily rhythm and abnormal days are easy to spot.
- Tracked ports with per-port volume.
- A snapshot of TCP states.

## Good to know

- Only physical interfaces are counted: docker bridges and veth are excluded, otherwise the same traffic would be counted twice.
- Counters come from the kernel and reset on reboot — the panel keeps history of its own, so a reboot doesn't erase statistics, though a gap may appear at the moment of restart.
- Disagreement with your provider's billing is normal: providers usually count outgoing traffic only, at their own metering points.
- A sharp rise in outgoing traffic with calm incoming is the classic picture of a leak — or of the node being used as an attack source.
