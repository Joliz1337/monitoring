# Charts

How the panel draws metric history on the server page and on the speed and TCP charts of the traffic page: a soft curve or the points as recorded, and whether to show the peak band.

## How it works

The panel polls nodes once per collection interval (see "Data Collection Intervals"). A node returns not a momentary sample but the average and the maximum over the window between two polls — everything that happened makes it into history, not one second out of ten. On longer ranges the points are folded into buckets: 5 minutes on 24 hours, an hour on 7 and 30 days, a day on a year.

- **Smoothed** — a soft curve runs through the averages; the trend is easier to see, short spikes are toned down.
- **As is** — every point is drawn exactly as recorded, as a straight polyline; short peaks and dips show more honestly.
- **Peak band** — a translucent band from the average up to the interval maximum. The line shows the typical load, the top of the band shows how high it went within the interval.

The mode can be overridden per metric: for example, network "as is" and everything else smoothed.

## Good to know

- The setting is panel-wide: everyone who opens the panel sees it, not only your browser.
- The CPU peak is the maximum of the average load across all cores over the interval, not the hottest core. Look at the per-core heatmap under the CPU chart for that.
- Nodes running an older version show no band on the last hour: such a node returns a single sample per poll, so the panel has no in-interval maximum. On 24 hours and longer the band is built from several samples and exists for everyone; after a node update the peaks appear on the hour range too.
- The traffic total in bytes on the traffic page is always drawn as is: averaging counted bytes would be dishonest.
