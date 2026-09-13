# AI assist (optional)

SpaceSage is a hybrid: the deterministic rule engine decides what it can prove,
and an optional AI layer helps with the long tail it cannot — suggested solutions
for undecided entries, labels for ambiguous paths, an explanation, a risk review,
a plan summary (design.md §10). The AI only ever suggests: it cannot execute,
approve or destroy anything and never reads file contents, while the plan
generator and your approval stay in charge (design.md §2, principle 9). With no
`ai.toml` (or `enabled = false`) the app behaves as if the layer did not exist —
the status says *AI is off*.

## What the AI produces

| use case | output |
|---|---|
| `suggest` | a course of action per item — action, one-line why, confidence, side effects, alternatives, or *No action*; large lists fill in background batches |
| `classify` | category, tier, action, confidence and rationale for ambiguous entries; accepted answers can be promoted into rules |
| `explain` | a deep, streamed explanation of a selection |
| `review` | severity-tagged (`info`/`warning`/`danger`) risk annotations on a plan, keyed by action id |
| `summarize` | a plain-language summary of a plan |

No chat surface: output renders next to the items it concerns (design.md §9).

## Provider setup

The client speaks one protocol — the OpenAI-compatible API over stdlib `urllib`,
no SDK — so anything that speaks it works. Five presets fill in the defaults:

| preset | base URL | default model | key | local |
|---|---|---|---|---|
| `ollama` | `http://localhost:11434/v1` | `llama3.2` | — | yes |
| `lmstudio` | `http://localhost:1234/v1` | `local-model` | — | yes |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | `OPENAI_API_KEY` | no |
| `openrouter` | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` | `OPENROUTER_API_KEY` | no |
| `custom` | (you set it) | (you set it) | `SPACESAGE_AI_API_KEY` | no |

Local presets need no key; `openai` carries prices; `custom` covers vLLM,
llama.cpp and gateways.

### The config file

The file lives at `~/.config/spacesage/ai.toml` on Linux/macOS
(`$XDG_CONFIG_HOME` respected) and `%APPDATA%\spacesage\ai.toml` on Windows;
`$SPACESAGE_AI_CONFIG` overrides the path. This is exactly the file the app
writes, providers side by side:

```toml
# SpaceSage AI providers (docs/ai.md).
# API keys are read from the environment variables named here -
# never stored in this file.

[ai]
enabled = true
default_provider = "ollama"
streaming = true
redact_paths = false
local_only = false
cache = true
retries = 2
batch_size = 8
max_items = 200
max_prompt_chars = 24000
max_tokens = 1200
temperature = 0.2

[[ai.providers]]
name = "ollama"
preset = "ollama"
base_url = "http://localhost:11434/v1"
model = "llama3.2"

[[ai.providers]]
name = "openai"
preset = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key_env = "OPENAI_API_KEY"
pricing_in = 0.15
pricing_out = 0.6
```

`[ai]` settings: `enabled` (a file with providers defaults to on),
`default_provider` (first by default), `streaming`, `redact_paths`, `local_only`,
`cache` / `cache_dir`, `retries` (0–10, default 2), `batch_size` (1–64, default
8), `max_items` (default 200), `max_prompt_chars` (≥ 1000, default 24 000), and
`max_tokens` / `temperature` (reserved — the shipped use cases send their own
per-case values).

Per-provider keys: `name` (unique), `preset` (defaults to the name, else
`custom`), `base_url`, `model`, `api_key_env` or `api_key_file`, `pricing_in` /
`pricing_out`, `timeout_s` (default 60), `stream`, `json_mode`, `extra_headers`.
Unknown keys are an error. **Keys are never stored in `ai.toml`** — a provider
names the environment variable that holds its key (`api_key_env`) or a file
(`api_key_file`, warned about if world-readable); the key is read at request
time, never logged, and the file is written atomically, `0600` on POSIX.

Switch provider or model with `default_provider` and the provider's `model`, or
override from the environment: `SPACESAGE_AI_CONFIG`, `SPACESAGE_AI_OFF`,
`SPACESAGE_AI_PROVIDER`, `SPACESAGE_AI_MODEL`, `SPACESAGE_AI_BASE_URL`,
`SPACESAGE_AI_KEY_ENV`, `SPACESAGE_AI_LOCAL_ONLY`, `SPACESAGE_AI_REDACT_PATHS`
and `SPACESAGE_AI_CACHE_DIR` (`OFF`, `LOCAL_ONLY` and `REDACT_PATHS` take `1`).

## Privacy

For each item a request carries only **facts about it**: the path (or a token),
folder flag, size, extension, age, the rule verdict it already has (category,
tier, action, rationale), and — for grouped rows — the member paths; a plan
review sends a reduced document (action id, type, path, destination, size, tier,
why), wrapped in an `<item-data>` block with `<`/`>` escaped: file names are
data, so a path cannot close the block or issue instructions.

Never sent: your files' contents, the index database, the export. The AI layer
never reads the filesystem — it works from the index's records — and the cache
stays local, keyed by content, not by conversation.

With `redact_paths = true` (or `SPACESAGE_AI_REDACT_PATHS=1`) every path becomes
a stable token (`path-1`, `path-2`, …) before the payload is built; the mapping
stays local, stored beside the cached answer so results remain attributable. The
cost is real: **advice quality drops with the names** — `node_modules` or a
`SteamLibrary` path carries signal a token does not; local-only mode keeps both
quality and privacy.

### Local-only mode

`local_only = true` (or `SPACESAGE_AI_LOCAL_ONLY=1`) refuses every non-loopback
endpoint **before any socket is opened** — no request, not even a DNS lookup;
`localhost`, `*.localhost`, `127.0.0.0/8` and `::1` are loopback, and anything
else fails with the coded error `local_only` and a hint. Ollama and LM Studio run
on `http://localhost:*`, so the `ollama` and `lmstudio` presets satisfy the mode
unchanged; a cloud provider configured while it is on fails loudly, never by
leaking.

## Cost control

**Pre-flight estimate.** A batch fill is planned first: items split into bounded
calls, cached calls counted, cost estimated as `N call(s), ~X tokens, ~$Y`
(without prices: `cost unknown (no prices configured)`; with everything cached:
`nothing to ask: every item is already cached`). Tokens come from payload size
(~4 characters per token) plus a completion allowance.

**The answer cache.** A validated answer is stored under a `sha256` of guard
version, use case, prompt version, provider, model, payload and dataset
fingerprint — exactly the inputs that determine it: repeated fills are free and
instant (hits are counted), and a changed model, prompt or payload is a miss by
construction. It lives under `~/.cache/spacesage/ai` (Linux),
`%LOCALAPPDATA%\spacesage\ai-cache` (Windows) or
`~/Library/Caches/spacesage/ai` (macOS); disable, inspect or empty it any time
(`cache = false`).

**The meter.** Every call's tokens — and its cost, when the provider names
prices — go to a session meter (`meter_snapshot()`: calls, failures, cache hits,
tokens, cost); usage the provider did not report is estimated and flagged. If any
call's cost cannot be computed, the meter reports *cost unknown* instead of a
total.

**Prices** are per provider, in USD per 1M tokens: `pricing_in` (prompt),
`pricing_out` (completion). Without them cost shows as `unknown`, never a wrong
number (`openai` carries its own; `openrouter`'s depends on the routed model).

**Bounds.** `batch_size` (items per request, default 8, range 1–64),
`max_prompt_chars` (a request's data block, default 24 000 — a batch splits
further when items are chatty), `max_items` (items per run, default 200 — the
rest are reported as not filled, never silently dropped).

## How a suggestion is produced

Every call goes through the same pipeline:

1. **Scope** — items (or plan action ids) become the call's dataset lock.
2. **Payload** — records rendered into `<item-data>`, path tokens first when
   `redact_paths` is on; `<`/`>` escaped so a file name cannot break out.
3. **Cache** — the content key is looked up; a hit costs nothing and returns the
   stored answer.
4. **Call** — `POST /chat/completions` (SSE streaming when the caller wants
   deltas — `explain` streams), with the provider's `timeout_s` and up to
   `retries` retries on rate limits, timeouts, unreachable endpoints and 5xx.
5. **Validation** — JSON extracted (code fences tolerated), checked against the
   use case's schema; one repair retry carrying the validator's complaints, then
   `repair_failed`.
6. **Locking** — every referenced path must have been sent: hallucinated paths
   are rejected and reported, never used; skipped items are reported unanswered.
7. **Accounting** — tokens, cost and latency to the meter; the validated answer
   to the cache.

Failures are outcomes, not exceptions: `ok=false`, a coded error and a hint. A
batch fill keeps going after a failed batch — one rate-limited request must not
cost the user the other items — and partial results are kept; it stops early only
when continuing cannot work (`disabled`, `invalid_config`, `local_only`, `auth`,
`model_missing`, or a cancel).

| code | meaning |
|---|---|
| `disabled`, `invalid_config` | AI off; provider misconfigured |
| `local_only` | non-loopback endpoint blocked |
| `auth`, `model_missing` | key rejected (401/403); model not served |
| `unreachable`, `timeout` | refused/DNS failure; deadline passed |
| `rate_limited`, `server_error` | HTTP 429; HTTP 5xx (both retried) |
| `bad_response`, `schema` | not a chat completion; not the expected JSON |
| `repair_failed` | invalid after the one repair retry |
| `blocked_path`, `cancelled` | path outside the dataset; caller cancelled |

`rate_limited`, `unreachable`, `timeout` and `server_error` retry automatically
(default 2, exponential backoff, `Retry-After` up to 30 s); the rest fail at once
with their hint.

## Promoting an accepted verdict to a rule

An accepted suggestion or classification can become an ordinary rule-pack entry,
handled by the deterministic engine from then on. Promotions land in the
**`ai-promoted`** pack: `~/.config/spacesage/rules/ai-promoted.toml` on
Linux/macOS, `%APPDATA%\spacesage\rules\ai-promoted.toml` on Windows
(`$SPACESAGE_RULES_DIR` overrides).

- **Destructive actions** (`DELETE_QUARANTINE`, `COMPRESS_NTFS`) are promotable
  only at **T1/T2** and confidence **≥ 0.8** — "probably a cache" is not good
  enough to delete on sight; classify the entry first, or promote a `REVIEW` rule.
- The pattern is the **narrowest that covers the item**: the file's own path, or a
  folder and its subtree — never a broad wildcard.
- The rule is rendered as TOML, **validated with the engine's own loader and only
  then moved into place** (atomically); a broken pack is never written, another
  id's pack is never touched, and a dry run writes nothing.
- Promoted rules are user-pack rules, tried before the built-in packs
  ([`rules.md`](rules.md)).

## Internal CLI

The AI layer is drivable headlessly (development, CI and automation; the product
surface is the app — design.md §11):

```sh
spacesage ai status                    # configuration, cache, mode flags (no network)
spacesage ai check                     # test the configured provider
spacesage ai models                    # the model ids the endpoint offers
spacesage ai suggest --db <index>      # suggestions for the undecided rows, batched
spacesage ai explain --path <path>...  # a deep explanation of a selection
spacesage ai review --plan <plan.json> # risk annotations for a plan
spacesage ai cache [--clear]           # what the answer cache holds
```

- `status` prints the provider, model, streaming, `redact_paths`, local-only and
  the cache location; it never touches the network.
- `check` prints the endpoint, whether it answered and how fast, the model, how
  many models are offered, and warnings; failures print the coded error and hint.
- `models` prints the models the endpoint reports, sorted — pick one for `model`.
- `suggest --db <index>` reads the ranked Opportunities list from an index and
  fills the rows the rules could not decide: bounded batches, cache hits first, a
  cost estimate, per-batch progress on stderr, then the suggestions with the
  rejected and unanswered counts (`--top`, `--all`, `--kind`, `--min-size`,
  `--no-cache`, `--no-progress`).
- `explain --path <path>` (repeatable) prints the explanation of the selection,
  streamed as it is written.
- `review --plan <plan.json>` annotates a plan's actions with severity-tagged
  risks and prints the summary; annotations that name an action the plan does not
  have are dropped and reported.
- `cache` lists entries, hits, misses and size, or `--clear`s them.

Every command takes `--json` (the same objects the app's screens use) and every
one of them degrades the same way: a broken config file, an unreachable provider
or a refused call prints a coded `error: <code>: ...` with its fix and exits 1 -
never a traceback, and never a partial write. `--json` keeps stdout parseable and
leaves the human-readable diagnostics on stderr.
