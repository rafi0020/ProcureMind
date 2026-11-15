# Model providers

ProcureMind uses one language model per organisation, in two places. Each can be used or left out separately, and
the accounts-payable decisions never depend on it.

| Use | Where it runs | Model | Notes |
|---|---|---|---|
| The Exception Agent | `backend/app/agent/runtime.py`, through `factory.get_client` | The organisation's model | Investigates an exception and stores a recommendation and drafts. Its model calls count against run and daily budgets. With provider `stub` the built-in script answers instead, and its reports say so |
| Extraction (optional) | `backend/app/extraction/llm.py`, called by `extract.py` | The organisation's model | Proposes the values of a document as a second extractor, beside the rules. Stored only where the value is found on the page (see below). With provider `stub`, extraction is rules only |

There is no separate judge model and no separate price table: the same client, with the same prices, serves both.
The matcher, the checks and the approval chain use no model at all.

## How the model is chosen

Two places configure it. The environment is the default for every organisation. The Model screen of the
administration area (`frontend/src/app/(app)/admin/page.tsx`) saves settings for one organisation.

**Environment** (`.env` in the `procuremind/` folder, read by Docker Compose; `.env.example` lists the common ones).
Settings are read in `backend/app/config.py`.

| Variable | Setting | Default | Notes |
|---|---|---|---|
| `LLM_PROVIDER` | `llm_provider` | `stub` | `openai_compat`, `anthropic`, `ollama` or `stub`. Any other value silently becomes `stub` (`backend/app/llm/factory.py`, `environment_config`) |
| `LLM_MODEL` | `llm_model` | empty, which means `stub` | The model id the provider knows |
| `LLM_API_KEY` | `llm_api_key` | empty | Sent as a Bearer token or `x-api-key`, depending on the provider |
| `LLM_BASE_URL` | `llm_base_url` | empty | Required for `openai_compat`; for `anthropic` it overrides `https://api.anthropic.com`; for `ollama` it overrides `http://localhost:11434/v1` |
| `LLM_FALLBACK_MODEL` | `llm_fallback_model` | empty | Used once when the model fails with a retryable error (see below) |
| `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK` | `llm_price_input_per_mtok`, `llm_price_output_per_mtok` | 3.0 and 15.0 | USD per million tokens. The comment in `config.py` says to set your provider's real prices; 3 and 15 are placeholders |
| `ALLOW_PRIVATE_LLM_URLS` | `allow_private_llm_urls` | `true` | `false` on a shared deployment: refuses base URLs on private and loopback addresses |
| `LLM_MAX_CONCURRENCY` | `llm_max_concurrency` | 16 | Model calls in flight per API or worker process |
| `LLM_MAX_OUTPUT_TOKENS` | `llm_max_output_tokens` | 4096 | Output limit of each model call that does not set its own (the agent uses this; extraction asks for 1500) |
| `STUB_RESPONDER` | `stub_responder` | `app.exceptions.stub_agent:demo_responder` | The script the stub provider follows |
| `AUTO_INVESTIGATE` | `auto_investigate` | `false` | `true` starts the agent on every new exception. Set a real model first |
| `RUN_MAX_STEPS`, `RUN_MAX_TOKENS`, `RUN_MAX_SECONDS`, `RUN_MAX_COST_USD` | `run_max_*` | 25, 200000, 300, 1.00 | Limits for one run, checked before each model call |
| `ORG_DAILY_COST_USD`, `GLOBAL_DAILY_COST_USD` | `org_daily_cost_usd`, `global_daily_cost_usd` | 20.00, 200.00 | Daily limits for one organisation and for the installation |
| `SECRET_KEY` | `secret_key` | empty | Encrypts keys saved from the Model screen. Compose falls back to a public default when it is empty (`docker-compose.yml`); set your own for any real use |

Compose forwards only the variables listed under `x-backend-env` in `docker-compose.yml`. `LLM_MAX_CONCURRENCY` and
`LLM_MAX_OUTPUT_TOKENS` are read by `config.py` but are neither forwarded nor listed in `.env.example`, so setting
them in `.env` has no effect under Compose. The same holds for every other setting that is not in that list.

**Organisation settings** (`PUT /api/settings/llm`, administrators only). When an organisation has a saved row in
`llm_settings`, it replaces the environment for that organisation as a whole: provider, model, base URL, key, fallback
and prices all come from the row, and the environment values are ignored (`backend/app/llm/factory.py`,
`load_config`). A saved key is therefore not overridden by changing `.env`. `GET /api/settings/llm` reports
`source: "organisation"` or `"environment"`, and `api_key_set` without the key itself.

The key is write-only. In `PUT`, omitting `api_key` keeps the stored key and an empty string clears it. The key is
encrypted with Fernet, using a key derived from the SHA-256 of `SECRET_KEY` (`backend/app/security/crypto.py`). The
audit log records `settings.llm_updated` with every field except the key, and whether the key changed.

"Test the saved settings" calls `POST /api/settings/llm/test`. It sends one request, `Reply with the single word: ok`,
with a 16-token limit, and returns `{"ok": true, "model", "latency_ms", "reply"}` or `{"ok": false, "error"}`. The
endpoint is limited to six requests a minute per administrator.

A base URL is checked when it is saved, and again each time a client is built for an organisation (see
"Validation of base URLs" below).

## Providers

The runtime knows only `Message`, `ToolSpec`, `ToolCall` and `LLMResponse` (`backend/app/llm/types.py`). Each adapter
translates to and from its provider's wire format.

| `provider` | Adapter | Endpoint | Key | Notes |
|---|---|---|---|---|
| `openai_compat` | `backend/app/llm/openai_compat.py` | `{base}/chat/completions`. `base` is required | Bearer token, if set | Any server that speaks the OpenAI chat API with tools. Always sends `"stream": false`. If a gateway streams anyway, `merge_stream` folds the chunks into the same result |
| `anthropic` | `backend/app/llm/anthropic.py` | `https://api.anthropic.com/v1/messages`, or `{LLM_BASE_URL}/v1/messages` | `x-api-key`, with `anthropic-version: 2023-06-01` | The system prompt and the last tool definition are marked `cache_control: ephemeral`, so the stable prefix is cached |
| `ollama` | `backend/app/llm/openai_compat.py` (the same class) | `http://localhost:11434/v1` unless `LLM_BASE_URL` is set | Optional | Ollama's OpenAI-compatible endpoint |
| `stub` | `backend/app/llm/stub.py` | none | none | Scripted answers. Extraction uses rules only; the agent follows `STUB_RESPONDER`. Works without any key |

Timeouts (`backend/app/llm/http.py`): 10 seconds to connect, 120 to read, 30 to write, 30 to get a pooled connection.
A timeout, a connection failure and a response body that cannot be read are retryable. Tests:
`backend/tests/test_llm_adapters.py`, including `test_request_asks_for_no_stream_and_sends_tools_and_key` and
`test_anthropic_request_and_response_mapping`.

### Retry, fallback and concurrency

`call_with_retry` (`backend/app/llm/base.py`) is the policy every provider shares.

1. A timeout, a connection failure, an unreadable body, or a status of 408, 409, 425, 429, 500, 502, 503, 504 or 529
   is retried, up to four attempts in all. The wait starts at 0.5 seconds and doubles, is capped at 30 seconds, has
   ±20% jitter, and is replaced by the provider's `Retry-After` when it sends one.
2. Any other error, such as a bad request, a wrong key or an unknown model, is raised at once
   (`test_errors_that_cannot_succeed_are_not_retried`). It does not reach the fallback.
3. When the primary model has used its attempts, and `LLM_FALLBACK_MODEL` is set and different, the fallback gets one
   attempt, on the same provider.
4. If that fails, the call raises `LLMUnavailable` ("The model is unavailable: ..."). In the agent the run ends
   `failed` with that message; in extraction the document step fails and is retried.

A semaphore caps the calls in flight per process at `LLM_MAX_CONCURRENCY`, so a burst of runs produces waiting
rather than a burst of 429s. It is per process, not per organisation.

## Extraction with a model

`backend/app/extraction/extract.py` runs the rules first. `backend/app/extraction/llm.py` then makes one more call
when the organisation's provider is not `stub` (or a test or evaluation has replaced the client):

- One forced tool, `record_document`, with the kind and the eleven header fields as strings, and the lines. The
  output limit is 1,500 tokens.
- The document text is the stored page text joined with `[page n]` markers, cut to the first 12,000 characters
  (`document_text`). Anything after that is not seen by the model.
- The text is placed between `<document>` tags, and the system prompt says it was written by an outside party and is
  never to be followed as an instruction.
- Each header field: the value is kept only if it is located on the page (`provenance.locate`). Rules and model agree
  and both are on the page: confidence 0.95. Only one is on the page: 0.7. Both are on the page and differ: the rules'
  value at 0.5, flagged as a disagreement. Neither: missing, with the rejected candidate kept and the reason.
- Lines: the model's lines are used only when the rules found none. The line amount is checked against the page. The
  item code, description, quantity and unit price are not (see "Known limits").
- The document kind comes from the model only when the classifier could not decide it.

The model's call is not written to `steps` and its cost is not recorded (see "Known limits").

## Prices and budgets

Each organisation's price row (or the environment's prices) gives the cost of each model call:
`(fresh input × input price + cached input × input price × 0.1 + output × output price) / 1,000,000`
(`backend/app/llm/pricing.py`). The cost is stored on the step and added to the run's `cost_usd`.

| Limit | Setting | Checked |
|---|---|---|
| One run | `RUN_MAX_STEPS`, `RUN_MAX_TOKENS`, `RUN_MAX_SECONDS`, `RUN_MAX_COST_USD` | Before each model call; the run ends `budget_exceeded` and keeps its evidence |
| One organisation, one day | `ORG_DAILY_COST_USD` | Sum of `runs.cost_usd` for the current day, before each model call |
| The installation, one day | `GLOBAL_DAILY_COST_USD` | Sum over every organisation, the same way |

The defaults of 3 and 15 USD per million tokens are placeholders. If they are not your provider's prices, the limits
are enforced against the wrong figures.

## Concrete setups

Put these in `procuremind/.env` for the Docker stack, or in the Model screen for one organisation. Replace each
placeholder with your value and keep keys out of the repository. The placeholders below (`sk-...`, `ak_...`,
`<...>`) are not keys.

From inside a container, `localhost` is the container itself. To reach something on the host (a gateway, Ollama),
use `http://host.docker.internal:<port>`. The `api` and `worker` services have that name
(`extra_hosts: host.docker.internal:host-gateway`). Scripts run on the host use `localhost`.

### 9Router (a local OpenAI-compatible gateway, used for development and the evaluation)

9Router runs on the development machine at `http://localhost:20128/v1`. Its base URL and key are kept in a file
named `.env.local` in the `New_Project` folder, under the names `NINEROUTER_BASE_URL` and `NINEROUTER_API_KEY`. The
live tests (`backend/tests/conftest.py` reads that file), the `make` evaluation targets and `make test-ocr-live` take
them from there. Do not paste the key into documents or commits.

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=http://host.docker.internal:20128/v1   # from the containers; http://localhost:20128/v1 from the host
LLM_API_KEY=ak_...                                  # the gateway key
LLM_MODEL=cc/claude-sonnet-5-5
```

Some routes of this gateway return server-sent events even when the request says `stream: false`. The adapter sends
`stream: false` and folds the chunks back into one response when the body is a stream (`merge_stream`), so both
kinds of reply give the same result (`test_plain_and_streamed_responses_give_the_same_result`). If a route streams
without reporting usage, the token counts are estimated from the text length (about four characters per token) and
the cost is estimated from those counts.

Models measured through this gateway are listed under "Measured models".

### Anthropic API

```bash
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
LLM_MODEL=<an Anthropic model id>
LLM_PRICE_INPUT_PER_MTOK=<that model's input price>
LLM_PRICE_OUTPUT_PER_MTOK=<that model's output price>
```

The request and response shapes are covered by `test_anthropic_request_and_response_mapping`. No test or evaluation
in this repository calls the Anthropic API directly; the Claude results below came through 9Router.

### OpenAI

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=<an OpenAI model id that supports tool calling>
```

The protocol is the one exercised through 9Router. The OpenAI endpoint itself is not called by any test here.

### Other OpenAI-compatible endpoints (vLLM, LM Studio, LiteLLM, OpenRouter, Groq, Gemini's OpenAI endpoint)

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=<its /v1 URL>                       # for example http://host.docker.internal:8000/v1 for vLLM
LLM_API_KEY=<key, or empty if the server takes none>
LLM_MODEL=<the model id that server reports>
```

The base URL must include `/v1`; the adapter appends `/chat/completions`. Two URLs that are often used are
`https://api.groq.com/openai/v1` (Groq) and `https://generativelanguage.googleapis.com/v1beta/openai` (Gemini's
OpenAI-compatible endpoint). Check each provider's current documentation for its URL. None of these is called by a
test or by the evaluation in this repository.

### Ollama (a model on your machine)

```bash
LLM_PROVIDER=ollama
LLM_BASE_URL=http://host.docker.internal:11434/v1   # from the containers; http://localhost:11434/v1 from the host
LLM_MODEL=<a model whose Ollama page lists tool support>
ALLOW_PRIVATE_LLM_URLS=true                         # the default; see below
```

- Pick a model whose Ollama page lists tool support. An investigation makes several calls, each with the
  instructions and the tool definitions, so raise the model's context window in Ollama; the server default is small
  for a long run.
- `ALLOW_PRIVATE_LLM_URLS` defaults to `true`, which a local address needs. On a shared installation set it to
  `false`; the address is then refused and Ollama on the host cannot be used.
- The key is optional. Trace content and documents stay on the machine with Ollama.
- No local model has been measured for ProcureMind. The figures in `docs/EVALUATION.md` are for hosted models
  through 9Router.

## Evaluation

`docs/EVALUATION.md` describes the four measurements. The commands are in the `Makefile`. Each runs in the test image,
reads the gateway variables from `../.env.local`, and writes its result to `backend/eval/results/`:

```bash
make eval-extraction                                  # rules alone
make eval-extraction MODEL=cc/claude-sonnet-5-5       # rules and the model, first 12 documents
make eval-matching                                    # rebuilds the demo in the TEST database
make eval-build                                       # the agent's cases, in their own organisation (dev database)
make eval-agent SPLIT=dev MODEL=cc/claude-sonnet-5-5
make eval-agent SPLIT=test MODEL=cc/claude-sonnet-5-5
make eval-attacks MODEL=cc/claude-sonnet-5-5
```

`MODEL` is a route on the gateway. The evaluation's `configure_model` (`backend/eval/common.py`) replaces the
organisation's saved settings for the run: every call goes to the model named, through `EVAL_BASE_URL` and
`EVAL_API_KEY` if they are set, otherwise `NINEROUTER_BASE_URL` and `NINEROUTER_API_KEY`. Through `make`, only the
`NINEROUTER_*` variables reach the container; to use `EVAL_*` (for example with Ollama) run the script on the host:

```bash
cd backend
EVAL_BASE_URL=http://localhost:11434/v1 EVAL_API_KEY=ollama \
  uv run python -m eval.run_eval --split dev --model <model name>
```

The host run needs `DATABASE_URL` and `DATABASE_URL_OWNER` to point at a database that has been migrated (the
defaults in `config.py` are the development database on port 55452).

## Measured models

Results are in `docs/EVALUATION.md`. Only the figures recorded there are repeated here.

| Model (through 9Router) | Extraction | Agent, held out | Attack suite |
|---|---|---|---|
| `cc/claude-sonnet-5-5` | 99.6 % of fields right with the model, 0 invented values in 240 renders | 9 of 9 cases | 10 of 10 held |
| `cx/gpt-6-sol` | not measured | 8 of 9 cases | not measured |
| `cx/gpt-5.5` | not measured | refused every call on this gateway account; recorded as unavailable, not as a score | not measured |

The held-out agent set is run once per model, after the prompt and tools were fixed on the development set. The
regression gate (`backend/eval/baseline.json`, `backend/tests/test_eval_gate.py`) fails when a recorded held-out result
falls below its baseline.

## Validation of base URLs

Every base URL is checked when it is saved (`PUT /api/settings/llm`) and before a client is built for an organisation
(`backend/app/llm/factory.py`, `validate_base_url`). Only `http` and `https` are accepted, the host must resolve, and a
link-local, multicast or unspecified address is refused, including the cloud metadata address `169.254.169.254`.
Private and loopback addresses are refused only when `ALLOW_PRIVATE_LLM_URLS=false`. The test
`test_base_urls_the_server_must_not_call_are_refused` covers the refusals.

## Known limits

These follow from the code as it is. Each has a location, and none is changed by this document.

- **Temperature is never sent.** The provider interface has `temperature: float = 0`, but both adapters send it only
  when it is non-zero (`backend/app/llm/openai_compat.py`, `complete`; `backend/app/llm/anthropic.py`, `complete`), so
  requests carry the provider's own default sampling setting.
- **The fallback model does not cover errors that cannot succeed.** A wrong model name or a rejected key raises at
  once and never reaches `LLM_FALLBACK_MODEL`. The fallback helps only with transient failures.
- **Extraction's model calls are not metered.** `backend/app/extraction/llm.py` discards the cost of its call. The
  call writes no `steps` row, so it does not appear in the usage screen and does not count against any budget.
- **Anthropic cache writes are billed at the plain input price in the estimate.** The adapter reports the cache
  creation tokens as input (`backend/app/llm/anthropic.py`, `complete`), and `pricing.py` charges them at the input
  price. Anthropic charges cache writes above the input price, so the estimate is low for those tokens. Cache reads
  are charged at one tenth, as in the estimate.
- **Line items from the model are not checked against the page.** Only a line's amount is located
  (`backend/app/extraction/extract.py`, `_lines`). The "0 invented values" figure in `docs/EVALUATION.md` concerns the
  eleven header fields, and that is the guarantee the code gives.
- **The base-URL check is not pinned to an address.** The address is checked when the client is built, and the HTTP
  client resolves the name again when it connects.
- **A key that cannot be decrypted breaks the organisation's model.** Changing `SECRET_KEY` makes stored keys unreadable.
  `load_config` then raises a `ValueError` (`backend/app/security/crypto.py`), and nothing in the Model screen or in
  extraction handles it on that path (`backend/app/api/settings.py`, `_view`; `backend/app/extraction/llm.py`,
  `model_client`).
- **An unknown `LLM_PROVIDER` becomes `stub` without a message.** Check the Model screen or the reply of the test
  endpoint after changing it.
- **Compose forwards a fixed list of variables** (see the table at the top).
