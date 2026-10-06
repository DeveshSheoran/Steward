# Demo video script (target 6-7 minutes)

Setup before recording: `python -m mock_apps.server` in terminal 1 (restart it so state is fresh). Terminal 2 with a large font,
`source .venv/bin/activate.fish`, `STEWARD_HEADLESS=0` so the browser is visible. Keep the browser window and terminal side by side.
Have `company_pack/policies.yaml` and `company_pack/sops/refund.md` open in an editor tab.

1. **(0:00) The idea, 30 s.** One sentence: an AI employee that completes company work, asks when it must, and proves the result.
   Show the loop line from the README. Say the three tasks run on the same core; only `company_pack/` differs.
2. **(0:30) Task B small refund, 90 s.** `python -m steward run "Rohan Mehta wrote in (ticket T-501) ..."`.
   Point at: company context picks the SOP; it reads the ticket then the order (two apps); `policy: ALLOW ... INR 2000`;
   `WRITE applied`; the separate **verifier** re-reading the systems; the report with evidence hashes.
3. **(2:00) Approval + a question, 100 s.** Restart mock (reset). Run the A-1002 refund with the deliberately wrong reason:
   `python -m steward run "Customer on order A-1002 wants a full refund of INR 4200 because the product never arrived."`
   The agent notices the ticket says "damaged" and asks. Answer. Then the policy pause: show `APPROVAL NEEDED (finance-manager)`,
   say what is bound to the grant (task, tool, exact args), approve. Show it resumes and completes.
4. **(3:30) Rejection, 60 s.** Run the Arjun onboarding with `finance-admins`; reject with a comment. The agent does not retry,
   finishes everything else, tells the manager, escalates the rest.
5. **(4:30) Reliability, 60 s.** Show the `refund_applied_but_503` eval line: refund applied but the response failed; exactly one
   refund exists. Open `eval_results/<latest>/report.md`. State honestly: small sample, ground truth not self-report.
6. **(5:30) Task C + audit, 45 s.** Run the weekly report (no browser). Then `python -m steward audit <task_id>` and
   `python -m steward verify-audit` (hash chain OK).
7. **(6:15) Limits and next, 45 s.** Mock apps only, non-deterministic agent, CLI-only approvals, what you would build next.
   Say plainly that the demo ran on your own Claude plan via the `claude` CLI and that production uses the API backend.

Do a dry run first; keep every command in a text file so you paste, not type.
