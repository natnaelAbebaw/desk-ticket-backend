Acme Billing is the payments product used by all paid workspaces. This guide answers the questions support gets most often.

# Plans and pricing

Acme has three plans: Free, Team, and Business. Prices are per seat, billed monthly or yearly.

Yearly billing gives two months free. Switching from monthly to yearly takes effect at the next renewal date.

## Changing plans

Upgrades apply immediately and the customer is charged a prorated amount for the rest of the billing period.

Downgrades apply at the end of the current period. Features from the higher plan stay active until then.

# Refunds

## Full refunds

Customers can get a full refund within 14 days of their first payment. After 14 days, only partial refunds are possible.

To issue a full refund, open the invoice in the admin panel and click Refund. The money returns to the original card in 5 to 10 business days.

## Partial refunds

Partial refunds cover unused seats only. They are calculated per day from the date the seat was removed until the end of the billing period. Agents must not promise a partial refund for a downgrade, because downgrades take effect at the end of the period and nothing is unused. If a customer was charged twice for the same invoice, that is not a partial refund case: escalate it to the payments team with the two charge ids, since duplicate charges usually mean a webhook retried and the fix has to happen on the payment provider side as well. Refunds over 500 USD need approval from a team lead before they are issued, and the approval must be noted on the ticket. Refunds can never be sent to a different card than the one that was charged, even if the original card has expired; the bank forwards the money to the replacement card automatically.

# Failed payments

When a card payment fails, Acme retries it after 1, 3, and 7 days. After the third failure the workspace is moved to read-only mode.

To check a failed payment from the command line:

```bash
# list the last five charges for a workspace
acme billing charges --workspace ws_123 --limit 5

# retry the most recent failed charge
acme billing retry --workspace ws_123
```

## Read-only mode

In read-only mode users can view tickets but cannot create or reply to them. Paying the open invoice restores full access within a minute.
