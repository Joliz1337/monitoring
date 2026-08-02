# Deadlines, balance and extension

How the date after which a server stops being paid for is calculated.

## Fields

| Field | Meaning |
|---|---|
| Paid until | The end date. Enter it as a date or as a number of paid days |
| Daily cost | For the resource type: how much is spent per day |
| Balance | Current funds in the provider account |
| Currency | For displaying amounts |
| Notes | Credentials, plan, who pays |

For the resource type the deadline is the balance divided by the daily cost. Topping up the balance or extending by days moves the end date immediately.

## Good to know

- Reminders arrive early enough to pay in time rather than after the fact.
- If your provider bills unevenly (hourly rates, several machines), the resource type with an averaged daily cost is more accurate.
- For Yandex Cloud the balance is pulled automatically and shouldn't be edited by hand; a manual top-up is only useful when money is already in but the sync hasn't run yet.
- Removing a server from monitoring keeps its payment record: this ledger is independent.
