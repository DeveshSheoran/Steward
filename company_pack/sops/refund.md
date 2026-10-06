---
id: customer_refund
title: Customer refund from a support ticket
triggers: [refund, charged twice, duplicate charge, money back, damaged, cancel, cancelled, helpdesk ticket]
systems: [helpdesk, billing]
done_when:
  - A refund for the correct order and amount is recorded in Billing
  - The helpdesk ticket has a reply to the customer that states the refund amount
  - The helpdesk ticket status is resolved
---
Goal: resolve a customer's refund request correctly and tell the customer.

1. Open the ticket in Helpdesk (find it by ticket id, or by customer name in the ticket list). Read the customer's message and note the linked order id.
2. Open that order in Billing. Confirm the customer matches, and note the amount paid and the amount already refunded.
3. If the reason in the request (e.g. 'never arrived') does not match the ticket or the Billing order note (e.g. 'damaged'), call ask_human to confirm the real reason BEFORE issuing the refund. Do not write a blended reason.
4. Check the claim against the order: for a duplicate charge, refund one charge worth; for a damaged item or cancellation, refund the refundable balance. Never refund more than the refundable balance.
5. Issue the refund in Billing with a short reason that mentions the ticket id. Refunds above the auto-approval limit pause for a finance manager; that is expected, wait for it.
6. Reply to the customer on the ticket: state the refund amount and the refund id (shown on the order page after the refund) and set the ticket status to resolved.
7. If you cannot refund (policy denial, rejected approval), do NOT reply that it was refunded: reply honestly if appropriate, then escalate with the reason.
