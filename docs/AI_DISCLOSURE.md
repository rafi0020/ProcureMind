# AI assistance disclosure

ProcureMind AI was built with AI coding assistance. This document says which tools were used, for what, and how the
results were checked. It does not describe any approval process.

## Tools

| Tool | Use |
|---|---|
| Claude (Anthropic), via Claude Code | Code, tests, evaluation scripts, documentation, debugging, running the Docker stack and the evaluations |
| Playwright | Browser journeys in `frontend/e2e/` and the README screenshots in `docs/screenshots/` (browsing only) |
| Archify | The architecture diagram in `docs/architecture.html`, with its source in `docs/architecture.diagram.json` |
| Hallmark | Design rules for the user interface |
| An LLM (optional, at runtime) | Invoice field extraction and the Exception Agent, when an organisation configures a model; see [PROMPTS.md](PROMPTS.md) |

## What the assistant produced and what was measured

- The application, the migrations, the tests and the evaluation scripts were written with the assistant and are in this
  repository. The design and the trade-offs are recorded in [ARCHITECTURE.md](ARCHITECTURE.md), including what each
  choice costs if it is wrong.
- The evaluation numbers are the output of the scripts in `backend/eval/`, stored in `backend/eval/results/`. They are
  reproduced in [EVALUATION.md](EVALUATION.md), with the failures and the limits recorded there. The synthetic documents
  come from a generator in this repository (`backend/seed/generate.py`); nothing in them is presented as a real supplier's
  document.
- The load measurements in [SCALE.md](SCALE.md) were taken on one machine that was shared with other workloads.

## Runtime use of AI

A model is off until an organisation configures one. With `LLM_PROVIDER=stub` (the default) no document text leaves the
application, extraction uses the rules alone, and the Exception Agent runs a fixed script whose report says so.

When a model is configured:

- Document text is sent to that provider for extraction, and the Exception Agent's reads of invoice, order and vendor
  records are sent with its prompts. For sensitive documents use a local model (Ollama) or no model.
- The model cannot approve, pay, edit an invoice, change a vendor's bank details or send a message. It stores a
  recommendation and a draft; a person decides. See [SECURITY.md](SECURITY.md).
- Every value the extraction model proposes must be found on the page before it is stored
  (`backend/app/extraction/provenance.py`). Every figure in an agent report must match stored evidence
  (`backend/app/exceptions/grounding.py`).
- Prompt text is the only instruction the model receives. It is listed in full in [PROMPTS.md](PROMPTS.md).

## Verification

- **Backend tests.** `backend/tests`, run as `make test` does (`RUN_LIVE=0`). The latest run has 298 passed and 8 skipped.
  The skipped tests need a live model gateway or the Tesseract binary, which is not installed on the host; the OCR tests
  run in the test image (`make test-ocr`). Results are in [`docs/test-results`](test-results).
- **Browser journeys.** `frontend/e2e/` (Playwright), run against the full stack: upload to approval, an exception to a
  decision, a duplicate caught, separation of duties, sign-in and navigation by role, and organisation isolation.
- **Evaluation.** `backend/eval/` for extraction, matching, the Exception Agent (held-out cases per model) and attacks
  against the agent. Results and their limits are in [EVALUATION.md](EVALUATION.md) and `backend/eval/results/`.
- **Security review.** The controls, their tests and the OWASP LLM Top 10 mapping are in [SECURITY.md](SECURITY.md).

## Limitations

- The data is synthetic. The evaluation sets are small (48 documents, 12 with a model, 43 in the matching run, 9 held-out
  agent cases per model), and differences of one case are within run-to-run variation.
- Prompt injection is treated as unsolved. The attack suite was run against one model; a weaker model may follow injected
  text more often, and the privilege separation is what limits the damage.
- No independent security audit has been carried out.
