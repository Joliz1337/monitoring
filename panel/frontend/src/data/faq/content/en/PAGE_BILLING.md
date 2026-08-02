# Server payments

Tracking when each server needs paying so nothing shuts down unexpectedly.

## Three accounting types

| Type | How the deadline is derived |
|---|---|
| Monthly | You set the paid-until date or the number of paid days |
| Resource | There's a balance and a daily cost — the panel works out how long it lasts |
| Yandex Cloud | The balance is pulled from the cloud automatically, spending is averaged over recent days |

## What you can do

- Add any server or hosting account, even one not connected to monitoring.
- Extend by a number of days or top up the balance — the panel recalculates the end date.
- Get Telegram reminders in advance, through the same bot as alerts.
- Keep notes: credentials, plan number, who pays for it.

## Good to know

- For the resource type an honest daily cost matters: the panel simply divides the remaining balance by it.
- With Yandex Cloud spending is smoothed, so after a sharp increase the forecast adjusts over a few days rather than instantly.
- Overdue and expiring servers are highlighted — you see them the moment you open the page.
- This is a ledger: it never pays or renews anything on its own.
