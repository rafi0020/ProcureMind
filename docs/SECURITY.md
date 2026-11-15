# Security

What is defended, how, and what is not. Each control names the test that exercises it.

## What is different about accounts payable

Payables is where fraud is attempted. The people who write the documents ProcureMind reads are suppliers, and
some suppliers (or people pretending to be them) want an invoice paid twice, paid at a higher price, or paid to a
new bank account. So the documents themselves are the main attack channel: anything printed on an invoice, any
text in a purchase order line, any vendor name, reaches the screens and the Exception Agent. The controls below
assume that text is hostile.

## Position on prompt injection

Prompt injection is treated as unsolved. Text from documents and records is handed to the model under
`untrusted_data`, and the instructions say it is never an instruction. In the attack suite Claude Sonnet 5.5
recognised every planted instruction and ignored it (`EVALUATION.md`). Nothing depends on that. The design
question is: *if the model were fully controlled by an attacker, what could it do?*

| It could | It could not |
|---|---|
| Read the invoices, orders, receipts, vendors and past exceptions of the organisation of the person who asked | Read another organisation's records, user accounts, sessions, inbox keys or provider keys |
| Store a recommendation on an exception | Resolve the exception: a person accepts or decides, and whoever started the investigation does not accept its recommendation |
| Store a draft message to a supplier | Send it: drafts are shown for a person to copy |
| Spend that run's budget | Exceed the per-run, per-organisation or global daily limits |
| Write a misleading report | Put a figure in the report that is not in the stored evidence |
| | Approve or pay an invoice, change a vendor's bank details, edit an invoice, match it to another order, or call a tool its user's role does not have |

The worst outcome of a successful injection is a wrong recommendation and a misleading draft, both of which a
person reads before anything happens, and a wasted run budget.

## Controls a payables team expects

| Control | How | Proven by |
|---|---|---|
| Separation of duties | Whoever entered or corrected an invoice cannot approve it; checked inside the transaction that records the decision | `test_ap_approvals.py::test_whoever_entered_or_corrected_an_invoice_cannot_approve_it` |
| Approval tiers | By total: one approver, two approvers, two with an administrator (organisation setting) | `test_ap_approvals.py::test_a_large_invoice_needs_two_people_one_of_them_an_admin` |
| One decision per click | The invoice row is locked while a decision is recorded; two approvers at the same instant approve once | `test_ap_approvals.py::test_two_approvers_at_the_same_instant_approve_once` |
| Bank-detail changes | Requested by one administrator, approved by another (a sole administrator may decide, and it is recorded); the account is shown in full only to administrators and named by its last four digits in the audit log | `test_vendors.py::test_a_bank_change_waits_for_a_second_administrator` |
| Bank account on an invoice differs from the one on file | Raised as a finding (score 0.9); the invoice is held from approval | `test_checks.py`, planted case `bank_change` |
| Duplicates and split invoices | Normalised invoice numbers, same amount within 30 days, several invoices just under an approval limit; each holds the invoice | `test_checks.py`; `EVALUATION.md` (matching) |
| A recommendation is bound to its exception | A run is started for one exception and `recommend` refuses any other; *approve with variance* is refused when the finding is a bank change or a duplicate | `test_exception_tools.py::test_recommend_refuses_an_exception_other_than_the_one_the_run_was_started_for`, `::test_recommend_refuses_approve_with_variance_when_the_bank_account_changed` |
| Nothing approved by a model | The agent has no write tool besides storing a recommendation and a draft; accepting one is a person's decision, audited with its source | `test_exception_tools.py::test_recommend_stores_a_recommendation_and_changes_nothing_else`; `test_exception_api.py::test_whoever_started_the_investigation_does_not_accept_its_recommendation` |
| Values never invented | Extraction stores a value only if it is located on the page; a model's value that is not on the page is dropped and flagged | `test_extraction.py::test_a_value_the_model_gives_that_is_not_on_the_page_is_never_stored`; `EVALUATION.md` (0 invented values in 240 held-out renders) |
| Approved invoices are fixed | Fields cannot be corrected after approval; an administrator can reopen, with a reason, which clears the approvals | `test_ap_approvals.py::test_a_rejection_ends_it_and_an_admin_can_reopen_an_approved_invoice` |

## Controls, mapped to the OWASP Top 10 for LLM Applications (2025)

| Risk | Control | Proven by |
|---|---|---|
| **LLM01 Prompt injection** | Privilege separation as above. Document text, line descriptions, vendor names and notes are returned to the model under `untrusted_data`. | `test_exception_tools.py::test_printed_instructions_reach_the_model_only_as_untrusted_data`; `test_extraction.py::test_document_text_reaches_the_model_only_as_untrusted_data`; `eval/attacks.py` (printed_instruction, bank_change_on_invoice, ignore_the_po, po_note, vendor_name) |
| **LLM02 Sensitive information disclosure** | Every query runs under PostgreSQL row-level security for the asking organisation; no tool reads users, keys or settings. Logged values are masked. Provider keys are encrypted at rest and never returned. | `test_tenant_isolation.py`; `test_exception_tools.py::test_another_organisations_records_do_not_exist_for_the_agent`; `test_security_basics.py`; `eval/attacks.py` (other_organisation) |
| **LLM03 Supply chain** | Dependencies pinned by lock files (`uv.lock`, `package-lock.json`); containers run as a non-root user. | not tested |
| **LLM04 Data and model poisoning** | No training or fine-tuning. A supplier can poison its own documents; that is LLM01, and matching and checks never use model output. | not applicable |
| **LLM05 Improper output handling** | Tool arguments are validated by schema; recommendations are one of six fixed outcomes. Model text is rendered by React as text, never as HTML. | `test_exception_tools.py` |
| **LLM06 Excessive agency** | Read-only tools plus `recommend` and `draft_supplier_message`, which store rows for people. | `test_exception_tools.py::test_recommend_stores_a_recommendation_and_changes_nothing_else`; `eval/attacks.py` (approve_and_pay, change_bank_details) |
| **LLM07 System prompt leakage** | The system prompt holds no secret or tenant data; the organisation's name and settings are added per run. | `eval/attacks.py` (prompt_extraction) |
| **LLM08 Vector and embedding weaknesses** | No vector store is used; similar exceptions are found by reason code in SQL. | not applicable |
| **LLM09 Misinformation** | The model does no arithmetic (`compute` does, in Decimal). Every figure in a report is matched against stored evidence; unsupported ones are removed and shown as removed. | `test_runtime.py`; `EVALUATION.md` (unsupported figures per report) |
| **LLM10 Unbounded consumption** | Per-run limits on steps (25), tokens, time and cost; per-organisation and global daily cost limits. | `test_budgets.py`; `eval/attacks.py` (loop) |

## Documents in

| Concern | Decision | Proven by |
|---|---|---|
| File type | Decided by content (magic bytes), not by name or declared type: PDF, PNG, JPEG, TIFF, XLSX; CSV by extension and valid UTF-8 | `test_intake.py::test_wrong_type_too_big_and_too_many_pages_are_refused` |
| Size | 20 MB per file, 200 pages per PDF | same |
| Storage | By SHA-256 per organisation, so the same file twice is one document; files live in a volume, never in the web root | `test_intake.py::test_the_same_file_twice_is_one_document` |
| Inbox keys | `ik_<prefix>_<secret>`; only the SHA-256 is stored, shown once, revocable at once | `test_intake.py::test_the_inbox_takes_attachments_with_a_key_and_refuses_revoked_keys` |
| Poison documents | Retried under a lease, then `dead` with the reason; never block the queue | `test_intake.py::test_a_document_that_keeps_failing_ends_dead_with_the_reason` |

## Accounts and sessions

Sessions are server-side, in an HttpOnly, SameSite cookie; state-changing requests need a CSRF token; the sixth
failed sign-in in a minute is refused; wrong password and unknown account get the same answer; changing a
password signs out other sessions; the last administrator cannot be demoted. `test_auth.py`, `test_roles.py`.

## Known limitations

- OCR on worn scans misreads some values (`EVALUATION.md`). A misread amount fails the arithmetic check and the
  invoice goes to review, but a misread that is arithmetically consistent would pass to matching; matching against
  the purchase order then catches price and quantity differences.
- The demo's prompt-injection results come from one strong model. A weaker model may follow injected text more
  often; the privilege separation above is what bounds the damage, not the model.
- No antivirus scanning of uploads. Files are never executed; screens show rendered page images, and the original
  is downloadable only as an attachment with its sniffed type and `X-Content-Type-Options: nosniff`, so a browser
  never renders it inline. CSV is recognised by its extension plus valid UTF-8 text, the one type not decided by
  magic bytes.
