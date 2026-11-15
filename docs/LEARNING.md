# Learning the code

A guided read for an engineer joining the project. It follows a document from upload to an approved invoice or an
exception that a person decides, then the evaluation harness. Each stop names the files and functions to read, what to
notice, and the tests that show the behaviour. Test names are real; run one with
`cd backend && uv run pytest tests/test_x.py::test_name -q`.

## Before you start

Read `docs/ARCHITECTURE.md` first; this file assumes its vocabulary (document, extraction, match, exception, run).

```bash
make dev-up                      # PostgreSQL on 55452, PgBouncer on 56452, Redis on 56399
cd backend && uv sync            # Python 3.13
make test                        # backend tests, RUN_LIVE=0; needs the three services above
```

Two facts make the rest easier to read:

- **Tenancy is in the database, not in the queries.** `app/db.py::tenant_session(org_id)` sets the organisation for
  one transaction, and row-level security decides what every query sees (`app/rls.py::enable_rls`). Read
  `tests/test_tenant_isolation.py::test_rows_of_one_org_are_invisible_to_another` and
  `test_org_does_not_leak_between_transactions_on_a_pooled_connection` before you write a query.
- **Every stage is re-runnable.** A document goes back to the queue after a failure and runs its steps again from the
  start, so each step deletes or upserts what it writes, or checks that it has already written it. Watch for this in
  every step below.

## 1. Intake: upload, inbox, storage by hash

**Read**

- `app/api/documents.py::upload` and `store_documents`: check every file first, then store; one bad file rejects the
  request.
- `app/api/inbox.py::inbox_org` and `receive`: the `POST /v1/inbox` endpoint, its inbox key and its 25 MB limit.
- `app/intake/storage.py`: `sniff` (type from the bytes), `check` (size, empty, type, PDF page count and password),
  `save` (by SHA-256 within the organisation, written to a temporary name and renamed), `read`.

**Notice**

- The type is decided by magic bytes. The only type decided by extension is CSV, which must also be valid UTF-8.
- The unique key is `(org_id, sha256)`. Uploading the same bytes twice returns the existing document with
  `duplicate_of` set; no second document or invoice is made.
- Inbox keys are looked up in the database on every request. Only the key's hash is stored
  (`app/api/inbox.py`, `inbox_org`).

**Tests**

- `test_intake.py::test_each_accepted_type_is_stored_once_and_queued`
- `test_intake.py::test_wrong_type_too_big_and_too_many_pages_are_refused`
- `test_intake.py::test_the_same_file_twice_is_one_document`
- `test_intake.py::test_viewers_cannot_upload_and_other_organisations_cannot_read`
- `test_intake.py::test_the_inbox_takes_attachments_with_a_key_and_refuses_revoked_keys`

## 2. The document queue

**Read**

- `app/intake/queue.py`: `CLAIM` (the SQL), `claim_document`, `process_one`, `_finish`, `process_pending`, `loop`,
  and `steps()`.
- `app/pipeline.py`: the list of steps, `STEPS`, in order.

**Notice**

- A document row is its own queue entry. `CLAIM` takes the oldest `queued` document, or a `processing` one whose lease
  has expired, with `FOR UPDATE SKIP LOCKED`, and sets a lease of `document_lease_seconds` (120 by default).
- `steps()` imports `app.pipeline` when a document is processed, not when the module loads. Before this was
  changed, a process that had not imported the pipeline ran no steps at all (see the evaluation section below).
- A step that raises sends the document back to `queued` with a back-off of five seconds times the attempt number.
  After `MAX_ATTEMPTS` (5) it is `dead` with the error kept, and the queue moves on.
- A step that completes without deciding the outcome leaves the document in `needs_review`; `_finish` does this only
  when the status is still `processing`.

**Tests**

- `test_intake.py::test_each_document_is_claimed_by_one_worker_and_reclaimed_after_its_lease`
- `test_intake.py::test_a_document_that_keeps_failing_ends_dead_with_the_reason`
- `test_intake.py::test_a_process_that_never_imported_the_pipeline_still_runs_every_step`

## 3. Text and OCR

**Read**

- `app/intake/steps.py::read_pages`: calls the reader in a thread (`asyncio.to_thread`), classifies, and replaces the
  document's `document_pages` rows.
- `app/intake/text.py`: `page_texts` dispatches on type; `_pdf` uses the text layer when a page has at least
  `MIN_TEXT_CHARS` (20) characters and OCR otherwise; `_images` for PNG, JPEG and TIFF frames; `_xlsx` and `_cells`
  for spreadsheets (one page per sheet, one word per non-empty cell, placed on a grid); CSV as one sheet.
- `app/intake/ocr.py`: `ocr_image` runs Tesseract at 300 DPI; `_upright` turns a page scanned at 90 or 180 degrees
  using Tesseract's orientation detection; words with confidence 0 are dropped.
- `app/intake/words.py`: `Word` and `PageText`, the only shape the rest of the pipeline sees.

**Notice**

- Every later decision uses word positions. A value is "on the page" because a run of words at a known place reads
  as that value (`app/extraction/provenance.py::locate`), so the reader's output is the evidence.
- OCR is the cost: a scan takes several seconds of one core, a digital PDF a fraction of a second
  (`SCALE.md`, section 1).
- When Tesseract is missing, `OcrUnavailable` is raised with a plain message. `tests/test_text.py` skips the OCR tests
  on a host without it and runs them in the test image (`make test-ocr`).

**Tests**

- `test_text.py::test_a_digital_pdf_gives_words_with_positions_from_its_text_layer`
- `test_text.py::test_a_spreadsheet_gives_one_page_per_sheet_with_its_cells`
- `test_text.py::test_a_scanned_pdf_is_read_by_ocr` (marker `ocr`)
- `test_text.py::test_a_photo_and_a_blank_image_go_through_ocr` (marker `ocr`)
- `test_text.py::test_the_pipeline_stores_each_page_and_the_kind`

## 4. Classification

**Read**

- `app/intake/classify.py`: `RULES` (pattern, kind, confidence), `classify`, `_match`, and `REFERENCE`.

**Notice**

- The printed title decides. `classify` takes the largest words in the top quarter of page one. Only when that title
  matches no rule does it try the wording of the whole band, at 90 % of the confidence.
- A "Purchase Order:" label inside an invoice is a reference, not a heading (`REFERENCE`). Without this, every invoice
  quoting its order would be read as an order.
- Classification is rules only. The model is offered the rules' kind as a hint (`app/extraction/llm.py::extract`),
  and its kind is used only when the rules have none (`extract_pages`).

**Tests**

- `test_text.py::test_each_kind_of_document_is_recognised`
- `test_text.py::test_a_page_with_nothing_on_it_is_unclassified_not_an_error`
- `test_text.py::test_the_title_decides_even_when_a_label_below_it_says_purchase_order`
- `test_text.py::test_a_garbled_title_does_not_turn_an_invoice_into_a_purchase_order`

## 5. Extraction with provenance

This is the stop where the design's central rule lives: a stored value must be found on the page.

**Read**

- `app/extraction/rules.py`: `LABELS` (the label words for each field), `lines_of` (words grouped into printed lines),
  `label_at_start`, `table` (the line-item table under a header that names quantity and amount), `first_line_vendor`,
  `extract`.
- `app/extraction/llm.py`: `SYSTEM`, `TOOL` (the `record_document` tool), `document_text`, `extract`, `model_client`.
  The document is passed between `<document>` tags and described as untrusted.
- `app/extraction/provenance.py`: `typed` (the value as its type, or `None`), `locate` (where the value is printed).
- `app/extraction/extract.py`: `_decide` (the decision table), `decide` (the same, without the flag), `_lines`,
  `extract_pages`, `extract_step`.
- `app/extraction/schema.py`: `FIELDS`, `Located`, `Missing`, `Extraction.status`.

**Notice**

- `_decide` is the whole rule for header fields. Both located and equal: 0.95. One located: 0.7. Both located and
  different: the rules' value at 0.5, flagged `disagreement`. Neither located: `Missing`, with the rejected candidate
  kept for a person to see. A model value that is not on the page is flagged `model_not_on_page`.
- `locate` compares amounts by number, dates by parsed date, currency by code, and other fields by normalised text,
  within one printed line, up to eight words.
- Line amounts are checked by `_lines` in the same way. Line quantities, unit prices, descriptions and item codes
  are not: `_lines` takes them from the rules or the model as they are. Read `_lines` and decide for yourself whether
  the design's rule ("every value located") covers them; the arithmetic check below is the only control on them.
- `extract_step` upserts one `extractions` row per document, so a retry replaces it.
- The model call's cost is returned (`response, _cost = await client.complete(...)` in `llm.py`) and not recorded.

**Tests**

- `test_extraction.py::test_every_field_of_a_digital_invoice_is_found_and_located`
- `test_extraction.py::test_a_value_the_model_gives_that_is_not_on_the_page_is_never_stored`
- `test_extraction.py::test_only_the_model_found_it_but_it_is_not_on_the_page_so_it_is_missing`
- `test_extraction.py::test_rules_and_model_disagreeing_on_located_values_is_flagged`
- `test_extraction.py::test_document_text_reaches_the_model_only_as_untrusted_data`
- `test_extraction.py::test_an_amount_too_large_to_be_real_is_never_accepted`
- `test_extraction.py::test_the_pipeline_step_stores_the_extraction_and_sets_the_status`
- `test_extraction_live.py::test_a_medium_quality_scan_is_extracted_correctly_with_a_model` (live and OCR)

## 6. Arithmetic checks

**Read**

- `app/extraction/arithmetic.py::check`: runs on the extraction.
- `app/ap/review.py::issues_of` and `review_status`: runs when an invoice is created from an extraction, and again on
  the stored invoice and its lines after a human correction.

**Notice**

- The same three rules are written twice, once for the extraction and once for the invoice. A change to one must be
  made to the other; there is no shared function.
- Lines are checked as quantity times unit price against amount, to the cent; the lines against the subtotal with a
  cent per line; subtotal plus tax against the total.
- Any issue sends the invoice to `needs_review`. A person corrects it and the check runs again.

**Tests**

- `test_extraction.py::test_a_wrong_line_amount_is_an_arithmetic_issue_and_needs_review`
- `test_extraction.py::test_a_missing_required_field_needs_review`
- `test_ap_records.py::test_a_correction_that_breaks_the_arithmetic_stays_in_review`

## 7. Accounts-payable records

**Read**

- `app/ap/records.py`: `records_step` (dispatch on kind, idempotent by document id), `create_invoice`, `create_po`,
  `create_receipt`, `_vendor`, `value` (returns a field's value only when it is located).
- `app/ap/numbers.py::normalise_number`: strips leading words and zeros, so `INV-0012`, `inv 12` and `Invoice No. 12`
  are the same number.
- `app/vendors.py::find_vendor`: by name, alias and close spelling.

**Notice**

- `value()` is the single place where extraction becomes records. It returns `None` for a missing or unlocated field,
  so an unlocated value cannot reach an invoice row.
- `create_invoice` stores every field's provenance in `field_provenance`, with the page, box, confidence and rejected
  candidate. The review screen reads these rows to draw boxes on the page.
- A purchase order whose vendor, number or lines cannot be read is not created; the document goes to review with the
  reason. A goods receipt for an unknown order is the same.
- A credit note is an invoice of kind `credit_note`, with positive amounts. The sign is applied in the match.

**Tests**

- `test_ap_records.py::test_an_extracted_invoice_becomes_an_invoice_with_where_each_field_was_found`
- `test_ap_records.py::test_an_unknown_vendor_or_a_missing_field_needs_review_and_a_correction_fixes_it`
- `test_ap_records.py::test_a_credit_note_is_stored_as_one_with_positive_amounts`
- `test_ap_records.py::test_purchase_orders_and_receipts_by_api_with_changes_recorded`
- `test_ap_records.py::test_a_purchase_order_whose_lines_cannot_be_read_needs_review_and_creates_no_order`
- `test_checks.py::test_the_same_file_twice_is_one_invoice_and_a_renumbered_copy_is_caught`

## 8. Three-way matching

**Read**

- `app/matching/match.py::match_invoice`: the whole decision, in order. Read it with `MatchResult` and `Reason` open.
- `_pair`: by item code, else by `similarity()` from `pg_trgm` at 0.5 or more.
- `_billed`: what other accepted invoices billed on a line, with credit notes subtracted.
- `store_match`: replaces the match and sets the invoice status.
- `match_step`: runs only for an invoice that is `ready`.
- `app/matching/tolerances.py`: `Settings`, `load`, `DEFAULT_TIERS`.
- `app/api/invoices.py::match`: match now, or by hand to a named order (audited as `invoice.matched_by_hand`).

**Notice**

- Four guards come before any arithmetic: the order must exist, be open, have the same vendor, and be in the same
  currency. A currency mismatch is reported without comparing amounts.
- The quantity check is `billed + invoiced now > received * (1 + tolerance)`, with `billed` excluding the invoice
  being matched, so a retry does not count itself.
- A price above tolerance is an exception. A price that is positive but within tolerance is `matched_with_variance`
  and goes to approval like any other matched invoice.
- Matching is deterministic. Nothing in this file calls a model; `EVALUATION.md` measures it against an answer key.
- An invoice with no lines is held to what was received less what was billed, not to the order. A header-only
  invoice is checked only when it prints a subtotal (`if not lines and inv.subtotal is not None`).
- A matched credit note goes to `matched`, never `pending_approval`.

**Tests**

- `test_matching.py::test_an_exact_invoice_matches_and_goes_to_approval`
- `test_matching.py::test_price_within_tolerance_is_a_variance_and_beyond_it_an_exception`
- `test_matching.py::test_two_invoices_cannot_bill_more_than_was_received`
- `test_matching.py::test_invoicing_more_than_was_received_is_an_exception`
- `test_matching.py::test_a_currency_mismatch_is_reported_without_comparing_amounts`
- `test_matching.py::test_a_credit_note_reduces_what_was_invoiced_and_is_never_approved_for_payment`
- `test_matching.py::test_no_po_wrong_vendor_and_unmatched_lines_are_exceptions`
- `test_matching.py::test_a_clerk_can_match_to_another_po_by_hand_and_it_is_audited`
- `test_matching.py::test_tolerances_are_an_admin_setting`
- `test_matching.py::test_an_invoice_from_a_document_is_matched_by_the_pipeline`
- `test_matching.py::test_an_invoice_without_lines_is_held_to_what_was_received_and_not_yet_billed`

## 9. Checks, findings and exceptions

**Read**

- `app/checks/run.py`: `run_checks` (the four checks), `open_exception` (the threshold, the one-open-per-invoice rule,
  the hold), `checks_step`.
- `app/checks/duplicates.py::duplicates`: same number (normalised), same total within 30 days.
- `app/checks/anomalies.py`: `bank_changed` (against the vendor's active account), `price_drift` (1.20 times the
  highest price in 180 days), `split_under_threshold` (seven days, 90 to 100 % of a limit).
- `app/checks/findings.py`: `Finding`, `THRESHOLD` (0.6).

**Notice**

- A finding is not a decision. It holds the invoice only when it scores 0.6 or more, and it is shown with its reason.
- `open_exception` returns early when there are no findings and no match reasons, and does not touch an exception
  that is already open (`if not findings and not reasons: return None`). Read that together with the correction path
  in `app/api/invoices.py::correct` and decide whether a corrected invoice can reach approval while an earlier
  exception on it is still open.
- The partial unique index `exceptions_one_open` (migration `0016`) is the database's guarantee of one open
  exception per invoice; the code's check is the same rule written in Python.
- A duplicate found by number scores 0.95, by amount 0.7. Both are shown; the amount check skips invoices already
  found by number, so one copy raises one finding.

**Tests**

- `test_checks.py::test_the_same_file_twice_is_one_invoice_and_a_renumbered_copy_is_caught`
- `test_checks.py::test_a_bank_account_that_differs_from_the_vendors_is_flagged`
- `test_checks.py::test_invoices_split_to_stay_under_an_approval_limit_are_flagged`
- `test_checks.py::test_a_price_far_above_the_vendors_history_is_flagged`
- `test_checks.py::test_a_same_amount_from_the_same_vendor_within_30_days_is_flagged`
- `test_checks.py::test_a_clean_invoice_opens_nothing_and_resolving_is_for_approvers`
- `test_checks.py::test_approving_with_variance_sends_the_invoice_to_approval`
- `test_checks.py::test_other_organisations_see_no_exceptions`

## 10. Resolving an exception

**Read**

- `app/api/exceptions.py`: `Outcome` (six values), `INVOICE_STATE` (what each outcome does to the invoice),
  `resolve_exception` (the one place that applies a decision), `resolve`, `accept`, `investigate`, `drafts`,
  `question_for`.
- `app/api/invoices.py`: `correct` (only while editable; deletes the match), `send_back`, `reopen`, `EDITABLE`,
  `MATCHABLE`.

**Notice**

- `resolve_exception` locks the exception row and the invoice row before writing. An escalated exception can be
  decided only by an administrator.
- `accept` is the agent's recommendation applied by a person. It refuses when the person started the investigation
  and another active approver or administrator exists.
- `approve_with_variance` on a credit note makes it `matched` rather than `pending_approval`, the same rule as the
  match.
- The agent cannot reach any of this. It can store a recommendation; the table above is what a person's decision does.

**Tests**

- `test_exception_api.py::test_an_investigation_ends_in_a_recommendation_that_an_approver_accepts_once`
- `test_exception_api.py::test_whoever_started_the_investigation_does_not_accept_its_recommendation`
- `test_exception_api.py::test_drafts_are_listed_and_nothing_is_recommended_until_the_agent_ran`
- `test_exception_api.py::test_new_exceptions_are_investigated_automatically_when_switched_on`
- `test_exception_api.py::test_other_organisations_cannot_investigate_or_accept`
- `test_ap_approvals.py::test_a_waiting_invoice_can_be_sent_back_for_correction_and_its_approvals_are_cleared`

## 11. Approvals

**Read**

- `app/approvals/policy.py`: `tier_for` (which tier a total falls in), `conflicts` (separation of duties, for many
  invoices in two queries), `conflict` (one invoice).
- `app/approvals/chain.py`: `describe` (what an invoice still waits for), `states` (the same for a queue),
  `decide` (the locked decision), `escalate_overdue` (scheduled every 900 seconds).
- `app/api/ap_approvals.py`: `queue` (my queue, filtered by conflicts when `mine=true`), `decide_route`.
- `alembic/versions/0017_invoice_approvals.py`: the `pending_since` trigger and `UNIQUE (invoice_id, approver_id)`.
- `app/api/ap_settings.py`: the tiers and escalation delay, read by everyone and changed by administrators.

**Notice**

- `decide` locks the invoice row first (`with_for_update()`), then checks status, conflicts and a repeat decision.
  Two approvers at the same moment are serialised by that lock; the second one sees the first one's approval.
- Separation of duties has four sources: `entered_by` (who uploaded the document), `corrected_by` (who changed a
  field), `matched_by` for a manual match, and `resolved_by` for an accepted variance. Each is checked in `conflicts`.
- The tier for an invoice counts approvals, not approver identities; for the top tier one of them must be an
  administrator (`describe`, `needs_admin`).
- Approving a credit note is not a payment, and nothing here pays. `approved` is the end state.
- Escalation sets `escalated_at` and nothing else; the queue lists escalated invoices first.
- `app/api/approvals.py` is a different system: approvals of actions an agent run proposed, which is the runtime's
  own approval gate (`needs_approval` tools). No Exception Agent tool uses it today.

**Tests**

- `test_ap_approvals.py::test_a_small_invoice_needs_one_approver`
- `test_ap_approvals.py::test_whoever_entered_or_corrected_an_invoice_cannot_approve_it`
- `test_ap_approvals.py::test_a_large_invoice_needs_two_people_one_of_them_an_admin`
- `test_ap_approvals.py::test_two_approvers_at_the_same_instant_approve_once`
- `test_ap_approvals.py::test_a_rejection_ends_it_and_an_admin_can_reopen_an_approved_invoice`
- `test_ap_approvals.py::test_invoices_waiting_too_long_are_escalated_and_listed_first`
- `test_ap_approvals.py::test_my_queue_leaves_out_what_i_may_not_approve`
- `test_ap_approvals.py::test_the_queue_costs_the_same_number_of_queries_however_many_invoices_wait`
- `test_approvals_api.py::test_approval_runs_the_action_once_and_is_audited` (the runtime's own gate)
- `test_vendors.py::test_a_bank_change_waits_for_a_second_administrator`

## 12. The Exception Agent

**Read**

- `app/agent/runtime.py`: `advance` (one action per call), `next_action` (decides from stored steps alone),
  `_model_step` (the model call, the one-time reminder when it answers in prose), `_start_tool` (validation, the
  two-invalid-calls rule, the approval gate), `_execute` (runs the tool and its database effect), `_record_outcome`
  (commits the outcome and the run's status), `_resolve` (what to do with a step a crash left unfinished).
- `app/agent/tools.py`: `ToolRegistry.tool` (name, description, level, roles, timeout, idempotency), `ToolResult.commit`
  (writes that happen in the step's transaction), `AgentSpec`.
- `app/agent/context.py::build_messages`: rebuilds the conversation from the steps every time.
- `app/exceptions/agent.py::AGENT`: the one place the agent is assembled.
- `app/exceptions/prompts.py::SYSTEM_PROMPT`: the method and the six outcomes.
- `app/exceptions/tools.py`: the read tools, `recommend` (line 255 onward) and `draft_supplier_message`.
- `app/exceptions/common.py`: `compute`, `chart`, `record_finding`, `ask_user` (at most two per run), `finish`.
- `app/exceptions/grounding.py`: `extract_numbers`, `EvidencePool`, `check_text`. `finish` calls `check_text`.
- `app/exceptions/compute.py`: `run_compute` and the operations; `total` is the one the prompt names.
- `app/agent/budgets.py::check_budget`: called before every model call.
- `app/worker/run.py`: `claim_next` (per-organisation limit, at most five running), `heartbeat`, `run_one` (the lease
  is renewed every third of its length, so every 10 seconds at the default 30), `serve`.

**Notice**

- The loop has no memory. `next_action` decides from the steps: a waiting tool call is resolved first, then a tool the
  model asked for and not yet started, then the next model call. Any worker can continue any run.
- A tool's `level` says what a crash means. `read` tools run again after a crash. `recommend` and
  `draft_supplier_message` are declared without a level, so they are `read` and idempotent; read
  `_resolve` and decide whether a second run of `draft_supplier_message` can add a second draft.
- The prompt's instruction to ignore `untrusted_data` is not what limits the damage. The tool list is: no tool in
  `AGENT`'s registry changes an invoice, an approval or a vendor, so a model that followed injected text could only
  store a recommendation or a draft. `tests/test_exception_tools.py::test_recommend_stores_a_recommendation_and_changes_nothing_else`
  checks that recommending changes nothing else.
- `finish` is where the report is checked. The first attempt with an unsupported figure is refused and the model
  repairs it; the second removes it and says so.
- `compute` offers operations this application does not use (`growth`, `margin`, `zscore_anomalies` and others
  carried over from the FinOps agent). Find them in `OPS` and decide whether to keep them.
- `_resolve` never repeats a `started` tool that is not idempotent; the model is told it may or may not have taken
  effect.

**Tests**

- `test_runtime.py::test_happy_path_runs_tools_in_order_and_completes`
- `test_runtime.py::test_the_model_sees_every_earlier_tool_result`
- `test_runtime.py::test_two_invalid_calls_in_a_row_fail_the_run`
- `test_runtime.py::test_a_tool_the_role_may_not_use_does_not_exist_for_it`
- `test_runtime.py::test_prose_instead_of_a_tool_call_gets_one_reminder_then_fails`
- `test_runtime.py::test_database_effects_commit_together_with_the_step`
- `test_runtime.py::test_approval_parks_the_run_without_executing`
- `test_runtime.py::test_conversation_is_rebuilt_identically_from_the_database`
- `test_budgets.py::test_step_limit`
- `test_budgets.py::test_organisation_daily_limit_applies_across_runs`
- `test_worker_claim.py::test_each_run_is_claimed_by_exactly_one_worker`
- `test_worker_claim.py::test_organisation_concurrency_limit_holds_under_concurrent_claims`
- `test_worker_claim.py::test_a_worker_that_lost_its_lease_stops_without_recording_anything`
- `test_worker_crash.py::test_killed_worker_is_replaced_and_the_run_completes`
- `test_exception_tools.py::test_compare_lines_shows_billing_beyond_what_was_received`
- `test_exception_tools.py::test_vendor_history_shows_the_account_on_file_and_the_one_on_the_invoice`
- `test_exception_tools.py::test_printed_instructions_reach_the_model_only_as_untrusted_data`
- `test_exception_tools.py::test_another_organisations_records_do_not_exist_for_the_agent`
- `test_exception_tools.py::test_the_demo_script_runs_end_to_end`
- `test_exception_live.py::test_price_over_tolerance_gets_a_credit_note_or_hold_with_the_variance_cited` (live)

## 13. Tracing

**Read**

- `app/obs/tracing.py`: `traced` (one span per model or tool call), `_run_context` (the trace id is the run id),
  `configure_tracing` (exports only when an endpoint is set).

**Notice**

- The attribute names are defined once, at the top of the file, because the GenAI conventions have been renamed.
- No prompt, completion or tool argument goes on a span. The test checks it.

**Tests**

- `test_tracing.py::test_one_span_per_step_with_genai_attributes_and_no_content`
- `test_tracing.py::test_metrics_endpoint_reports_requests_and_runs`

## 14. The evaluation harness

Each script measures one question on data the system was not tuned on, and writes its result under
`backend/eval/results/`. `api/evaluation.py` shows those files in the application, and
`tests/test_eval_gate.py` fails when a recorded result drops below `eval/baseline.json`.

**Read**

- `eval/run_extraction.py`: `grade` (each of the 11 header fields: correct, missing, misread, invented), `on_page`,
  `summarise`. The rule to check is that "invented" is measured from the outside and must be zero.
- `eval/run_matching.py`: `fresh_demo` (rebuilds the demo in the test database with its answer key), `outcomes`,
  `score` (documents in the right state, findings raised, missed and wrong).
- `eval/build_questions.py`: `build` (thirty agent cases in their own organisation, each run through the real matcher
  and checks, and asserted to raise the exception it was built for).
- `eval/run_eval.py`: `grade` (outcome and deciding figure, both read from the database), `run_case`, `aggregate`.
- `eval/attacks.py`: each `case_*` function runs one attack through the real agent and then checks the database.
  The model's own account is not the judge.
- `eval/common.py::configure_model`: points every model call at the gateway for the run, and ignores what an
  organisation saved.
- `seed/generate.py`: `PLANTED` (the eight planted cases and their acceptable outcomes), `_planted`, `plan`.

**Notice**

- The grader is tested before the held-out run (`test_eval_grader.py`), and the development and held-out splits are
  disjoint by construction (`test_question_sets_do_not_overlap_and_every_case_has_acceptable_outcomes`).
- The matching evaluation found a real defect: the queue ran only the steps the process had imported. The queue now
  loads its own steps.
- Results are recorded once. A run that could not be scored (the second model on the gateway refused every call) is
  kept as `unavailable-*` and is not counted.
- `EVALUATION.md` states each result with its method, its failures and its limits. Read it alongside the code.

**Tests**

- `test_eval_grader.py::test_an_acceptable_outcome_that_states_the_figure_passes`
- `test_eval_grader.py::test_a_right_outcome_without_the_deciding_figure_fails`
- `test_eval_grader.py::test_a_small_whole_quantity_counts_but_not_inside_a_code`
- `test_eval_gate.py::test_extraction_never_stores_a_value_that_is_not_on_the_page`
- `test_eval_gate.py::test_matching_holds_its_status_accuracy_and_recall`
- `test_eval_gate.py::test_held_out_agent_result_is_within_tolerance_of_its_baseline`
- `test_eval_gate.py::test_every_recorded_attack_held_or_is_a_listed_limitation`
- `test_eval_gate.py::test_question_sets_do_not_overlap_and_every_case_has_acceptable_outcomes`

## 15. The browser journeys

The journeys run against the full stack and check the flows a person uses. They are in `frontend/e2e/` and run with
`npx playwright test` from `frontend/` once `docker compose up` is running.

| File | Test | What it shows |
|---|---|---|
| `invoice-flow.spec.ts` | an invoice is uploaded, read, matched and approved; above 1,000 it takes two people | Stops 1 to 11 together |
| `exception.spec.ts` | a price over the order becomes an exception; the agent recommends; an approver accepts | Stops 9, 10 and 12 |
| `exception.spec.ts` | a duplicate sent again under another spelling is caught and rejected | Stops 7 and 9 |
| `scanned.spec.ts` | a scanned invoice under a trading name is put right on the review screen and matched | Stops 3, 5, 7 and 8 |
| `duties.spec.ts` | whoever corrected an invoice cannot approve it, in the screen or through the API | Stop 11 |
| `duties.spec.ts` | one organisation cannot open another's invoice, by URL or through the API | Stop 1 (tenancy) |
| `roles.spec.ts` | signing in through the form lands on the Inbox; each role sees what that role may use | Navigation by role |

The demo accounts are created by `seed/demo_users.py`; `frontend/e2e/global-setup.ts` signs them in and keeps the
sessions under `frontend/e2e/.auth/`, which is ignored by git.

## Where to look when something is wrong

| Symptom | Start at |
|---|---|
| A document is `dead` or keeps returning to `queued` | `app/intake/queue.py::_finish`, then the `error` on the document |
| A value is missing on the review screen | `app/extraction/extract.py::_decide` and the `flags` on the extraction |
| An invoice is in `needs_review` with no visible problem | `app/ap/review.py::issues_of` |
| An invoice is `exception` and the reason is unclear | `matches.reasons` and `exceptions.findings`; `app/matching/match.py::match_invoice` |
| An approver cannot decide | `app/approvals/policy.py::conflict`, then `app/approvals/chain.py::describe` |
| A run is stuck in `running` | `runs.lease_expires_at`, `app/worker/run.py::claim_next` |
| A run is `budget_exceeded` | `app/agent/budgets.py::check_budget` and the `runs` cost and step counters |
| The report lists a figure as removed | `app/exceptions/grounding.py::check_text` and the run's `finish` step |
