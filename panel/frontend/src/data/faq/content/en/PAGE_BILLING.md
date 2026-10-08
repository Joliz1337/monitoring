# Server payments

Tracking when each server needs paying so nothing shuts down unexpectedly.

## Three accounting types

| Type | How the deadline is derived |
|---|---|
| Monthly | You set the paid-until date or the number of paid days. Optionally add the monthly price, and the server counts toward monthly spend |
| Resource | There's a balance and a daily cost — the panel works out how long it lasts |
| Cloud | Balance and remaining time come from the provider's API: Yandex Cloud, Selectel, Timeweb Cloud, VK Cloud or Cloud.ru |

## What you can do

- Add any server or hosting account, even one not connected to monitoring.
- Extend by a number of days or top up the balance — before you confirm, you see the total paid period and the resulting end date.
- Refresh a cloud account from its card, or all of them at once with “Sync clouds” in the header. A newly added cloud project is refreshed automatically.
- Plan a cloud top-up: “Calculate” shows how much to add so the balance lasts a given number of days, or how long a given amount will last.
- Get Telegram reminders in advance, through the same bot as alerts.
- Keep notes: credentials, plan number, who pays for it.

## Cloud credentials

- **Yandex Cloud** — the billing account ID and a service account authorized key, see the steps below. Yandex Cloud stopped accepting new OAuth tokens on June 1, 2026. A token issued earlier keeps working in an existing project until it expires, but it's better to replace it with a key in advance.
- **Selectel** — a single static API key: in the Selectel panel go to “Profile → Access → API keys”. The key is shown once, so copy it right away. The user owning the key must have access to the Billing section.
- **Timeweb Cloud** — an API token from the “API & Terraform” section of the Timeweb Cloud panel.
- **VK Cloud** — login, password and Project ID (console → “Project settings” → “API access”). VK Cloud has no API keys, so the panel gets a short-lived token on every check. Use the account email or a service account created just for the panel. Two-factor authentication and API access must be enabled in the account.
- **Cloud.ru** — the Key ID and Key Secret of a service account access key, and the agreement ID. Create an organization-level service account with the Cost administrator role in “Users → Service accounts”, then “Access keys” → “Create key” on its page. The Key Secret is shown once. The agreement ID is in “Cost control” → “Agreement”, the copy icon under the agreement number.

How to connect Yandex Cloud:

1. Create a service account in any folder of the cloud.
2. Open [center.yandex.cloud/billing/accounts](https://center.yandex.cloud/billing/accounts), pick the billing account, then “Access management” → “Assign roles” on the left. Select the service account and add the `billing.accounts.viewer` role. The role has to be granted right here: if it's granted in the folder or cloud permissions, Yandex answers the panel with “Forbidden: need billing.accounts.viewer role”. If the service account isn't in the list, grant it the role on the whole organization in Cloud Center — it applies to all of the organization's billing accounts.
3. On the service account page click “Create new key” → “Create authorized key” and download the JSON file.
4. Paste the whole file into the key field and enter the billing account ID. Once saved, the panel fetches the balance and spending right away.

Keys are stored encrypted and never returned to the interface: the field in the edit form stays empty — leave it empty to keep the current key.

If the provider API is unreachable from the panel's address, or accounts shouldn't reach the provider from the same IP, a cloud project can use a SOCKS5 proxy in the `ip:port` or `ip:port@login:pass` format. All panel requests for that project go through it. If the proxy is down, the error on the card says so: “via proxy ip:port”.

## Good to know

- For the resource type an honest daily cost matters: the panel simply divides the remaining balance by it.
- The balance threshold is the amount you don't want to spend below: the term is counted down to it, not to zero.
- For Yandex Cloud spending is measured from actual consumption over the last three days, so after a sharp increase the number catches up within a couple of days.
- For Selectel the term comes from Selectel itself: its own “balance lasts for N days” forecast is shown as is, and the daily cost is the balance divided by those days.
- Timeweb Cloud has no charge history in its API, so the panel remembers the balance on every check and derives spending from its decline; top-ups don't affect the math. For the first hours after adding, while there's little history, the current plan price is shown instead.
- VK Cloud balance includes bonuses, because bonuses are spent first. Spending is the average of the last three full days from the usage report.
- Cloud.ru spending is also derived from the balance decline between checks. For the first hours after adding, yesterday's spending from the usage report is shown. Bonus grants are not included in the balance.
- The summary on top shows monthly spend, total balance and how many projects expire within a week. Different currencies are listed separately instead of being added up.
- Overdue and expiring servers are highlighted — you see them the moment you open the page.
- This is a ledger: it never pays or renews anything on its own.
