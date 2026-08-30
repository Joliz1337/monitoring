# Server payments

Tracking when each server needs paying so nothing shuts down unexpectedly.

## Three accounting types

| Type | How the deadline is derived |
|---|---|
| Monthly | You set the paid-until date or the number of paid days |
| Resource | There's a balance and a daily cost — the panel works out how long it lasts |
| Cloud | Balance and remaining time come from the provider's API: Yandex Cloud or Selectel |

## What you can do

- Add any server or hosting account, even one not connected to monitoring.
- Extend by a number of days or top up the balance — before you confirm, you see the total paid period and the resulting end date.
- Refresh a cloud account from its card, or all of them at once with “Sync clouds” in the header.
- Plan a cloud top-up: “Calculate” shows how much to add so the balance lasts a given number of days, or how long a given amount will last.
- Get Telegram reminders in advance, through the same bot as alerts.
- Keep notes: credentials, plan number, who pays for it.

## Cloud credentials

- **Yandex Cloud** — an OAuth token (link is in the form) and the billing account ID from the cloud console.
- **Selectel** — a single static API key: in the Selectel panel go to “Profile → Access → API keys”. The key is shown once, so copy it right away. The user owning the key must have access to the Billing section.

Keys are stored encrypted and never returned to the interface: the field in the edit form stays empty — leave it empty to keep the current key.

## Good to know

- For the resource type an honest daily cost matters: the panel simply divides the remaining balance by it.
- The balance threshold is the amount you don't want to spend below: the term is counted down to it, not to zero.
- Spending is measured from actual charges: recent consumption for Yandex Cloud, a month of transactions for Selectel (so one-off monthly payments don't inflate the daily average). After a sharp increase the number adjusts gradually rather than instantly.
- The summary on top shows monthly spend, total balance and how many projects expire within a week. Different currencies are listed separately instead of being added up.
- Overdue and expiring servers are highlighted — you see them the moment you open the page.
- This is a ledger: it never pays or renews anything on its own.
