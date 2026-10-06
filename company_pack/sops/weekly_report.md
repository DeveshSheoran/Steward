---
id: weekly_ops_report
title: Weekly orders reconciliation report
triggers: [weekly report, ops report, reconcile, reconciliation, orders export, weekly orders, report]
systems: [billing]
done_when:
  - A report file exists with totals (gross, refunded, net) computed from the export and a list of every discrepancy versus Billing
  - The report summary has been emailed to the Operations Lead
---
Goal: tell the Operations Lead whether last week's orders export agrees with Billing.

1. Read the export at exports/orders_export.csv with files.read, and get its totals with files.summarize_table (sum of amount and refunded). Do not add numbers up in your head.
2. Fetch the live orders from the Billing API: http.get on the billing system's /billing/api/orders endpoint.
3. Reconcile order by order: flag orders missing from either side and any order whose amount or refunded value differs. Billing is the system of record.
4. Write the report with files.write as weekly_ops_report.md: totals table (gross, refunded, net), then a discrepancy list (order id, what differs, export value, billing value).
5. Email a short summary (totals + number of discrepancies, and where the report is) to the Operations Lead, found via directory.lookup.
