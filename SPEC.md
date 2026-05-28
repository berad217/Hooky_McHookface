# Project Spec: Hooky_McHookface

## 1. Overview

**Name**: Hooky_McHookface (Python import: `hooky`)

**Purpose**: An async-native, framework-grade webhook receiver — local server + tunnel + persistent storage + plugin handler system + signature validation + replay engine — that's pleasant to subclass and reusable across personal projects.

**Context**: an earlier Flask-based version (`webhook-reciever/` — sic, misspelled folder) was never put into production. With the agent-era rewrite-vs-migrate calculus shifted, this is a clean rewrite: FastAPI, async-native, Cloudflare Tunnel as a first-class provider, fresh tests, fresh folder, fresh git. The old folder stays on disk as a reference implementation + behavioral spec source.

**User**: the handler author, plus any future agent extending this codebase. The first real consumer is a separate private project. After that, anything else wired to a webhook — GitHub, Stripe, Linear, n8n, custom dashboards.

**Strategic framing**: Hooky_McHookface is **infrastructure**. It must be:
- A pleasure to subclass (the handler interface is the API)
- Honest about failure (HMAC verify, isolation between handlers, replay-on-demand)
- Easy to point a new tunnel + provider at without reading source
- Independent of any specific webhook source (no Quo, GitHub, Stripe coupling baked into core)

---

## 2. Visual Identity & UX Vibes

This is a library, not a UI. "Visual identity" = **API ergonomics, error message quality, and log readability**.

- **The Vibe**: Quietly competent infrastructure. Boring on purpose. Surprising only in how few surprises it has.
- **API tone**:
  - Handler authors write `async def process(self, webhook)` and that's it. No required boilerplate beyond the ABC.
  - Validators and tunnel providers follow the same pattern: small ABC, clear lifecycle.
  - Configuration is YAML or env, never both required.
  - Errors say what failed, what was expected, and what to try next.
- **Log readability**: Structured JSON to stdout by default. Each line includes: timestamp, level, request_id, handler_name, latency_ms, outcome. A human reading raw logs should be able to reconstruct what happened.
- **README aesthetic**: Quickstart in <60 seconds (install, configure tunnel, post a test webhook, see it stored). Then handler-authoring guide. Then advanced.

---

## 3. Success Criteria

v1 is complete when:

- [x] `pip install -e .` followed by `hooky start` brings up a FastAPI server on a configured port with a stored ngrok or Cloudflare tunnel pointing at it.
- [x] A `curl` to `https://<tunnel>/webhook/test` is stored in SQLite before any handler runs (store-first invariant verifiable).
- [x] An async `WebhookHandler` subclass can be registered programmatically OR via config and dispatches to receive each stored webhook.
- [x] Handler isolation: one handler raising an exception does not block other handlers; failure is logged with traceback.
- [x] HMAC signature validation works for: generic HMAC (configurable header + algorithm), GitHub, Stripe — verified against fixture payloads with known-good and known-bad signatures.
- [~] Cloudflare Tunnel provider can spawn `cloudflared`, expose the local server, return the public URL, and shut down cleanly. *Implemented + unit-tested (mocked subprocess, incl. failure paths); real-infra smoke pending a domain — see [`docs/cloudflare-smoke.md`](docs/cloudflare-smoke.md).*
- [x] `hooky list` shows recent stored webhooks; `hooky show <id>` displays one in detail; `hooky replay <id>` re-dispatches it to handlers.
- [x] `pytest` is green across all modules with at least 80% line coverage on `src/hooky/`. *(200 tests, 94%.)*
- [x] README has a quickstart a future-you (or another agent) can follow in under 5 minutes start-to-tunnel.
- [x] A downstream consumer can use Hooky_McHookface as a dependency without any internals-knowledge — only the documented public API. *(Handler contract — incl. `secret_env` for secrets — locked; see DEVLOG "Handler config contract".)*

> **As of 2026-05-28:** all criteria met except the Cloudflare real-infra smoke
> (`[~]` above), which is implemented and unit-tested but gated on a domain.

---

## 4. Technical Foundation

- **Stack**: Python 3.11+, FastAPI, uvicorn, async-native throughout.
- **Project layout**: src-layout, `pyproject.toml` (no setup.py, no requirements.txt — modern Python packaging).
- **Key dependencies**:
  - `fastapi` — web framework
  - `uvicorn[standard]` — ASGI server
  - `pydantic` (2.x) — models + validation
  - `pydantic-settings` — env + YAML config
  - `aiosqlite` — async SQLite for storage
  - `click` — CLI
  - `pyyaml` — config files
  - `pyngrok` — ngrok provider
  - `structlog` — structured logging
  - `httpx` (test dep) — `respx` mocking
  - `pytest`, `pytest-asyncio`, `pytest-cov`, `respx` — testing
  - `ruff`, `mypy` — lint + types
- **Cloudflare Tunnel**: requires `cloudflared` binary installed on the host. Provider spawns it as a subprocess. Documented in README setup.
- **Build/dev commands**:
  - `pip install -e ".[dev]"`
  - `hooky start --config config.yaml`
  - `pytest --cov=hooky`
  - `ruff check src/ tests/ && mypy src/`
- **License**: MIT. (Old project had unspecified license — this is a fresh choice.)

### ⚠ Open decisions for Brad to confirm

1. **Import name `hooky` vs `hooky_mchookface`** — defaulting to short ergonomic `hooky`. PyPI distribution name (if ever published) would be `hooky-mchookface`.
2. **Storage backend pluggability** — v1 ships with SQLite only. Should the storage layer be designed as an interface from day 1 (for future Postgres / Redis), or keep it concrete and refactor later? **Default: interface from day 1**, since this is durable infrastructure and the abstraction is cheap when designed in.
3. **Auth on the local HTTP API** — does the receiver itself need an auth layer beyond HMAC signature verification on webhooks? **Default: no.** Tunnel is the public-internet surface; local HTTP is behind it and behind allowlisted-handlers-only. If a future use case wants admin endpoints, that's a §10 Parking Lot item.

---

## 5. Architecture & Modules

```
┌─────────────────────────┐    HTTPS    ┌─────────────────────────────────┐
│  External webhook       │────────────▶│  Tunnel Provider                │
│  source (GitHub, Stripe,│             │  (ngrok / Cloudflare / none)    │
│   Quo, etc.)            │             └─────────────────┬───────────────┘
└─────────────────────────┘                               │
                                                          ▼
                                       ┌─────────────────────────────────┐
                                       │  FastAPI routes                 │
                                       │  POST /webhook/{path}           │
                                       │   1. Read body + headers        │
                                       │   2. Validate signature         │
                                       │      (per-path validator)       │
                                       │   3. Store immediately          │
                                       │   4. Return 200                 │
                                       │   5. Dispatch handlers (async)  │
                                       └─────────────────┬───────────────┘
                                                          │
                                ┌─────────────────────────┼─────────────────────────┐
                                ▼                         ▼                         ▼
                       ┌────────────────┐        ┌────────────────┐        ┌────────────────┐
                       │  Handler A     │        │  Handler B     │        │  Handler C     │
                       │  (async,       │        │                │        │                │
                       │   isolated)    │        │                │        │                │
                       └────────────────┘        └────────────────┘        └────────────────┘
                                                          │
                                                          ▼
                                              ┌────────────────────────┐
                                              │  Storage (aiosqlite)    │
                                              │  - webhooks table       │
                                              │  - handler_runs table   │
                                              └────────────────────────┘
```

### File tree

```
Hooky_McHookface/
├── SPEC.md
├── onboarding.md
├── DEVLOG.md
├── README.md
├── LICENSE
├── pyproject.toml
├── .env.example
├── config/
│   └── config.example.yaml
├── src/
│   └── hooky/
│       ├── __init__.py            # public re-exports: WebhookHandler, etc.
│       ├── __main__.py            # python -m hooky → CLI
│       ├── app.py                 # FastAPI app factory
│       ├── routes.py              # /webhook/{path}, /healthz
│       ├── lifespan.py            # startup/shutdown: tunnel, storage, handlers
│       ├── config/
│       │   ├── __init__.py
│       │   ├── schema.py          # pydantic-settings + YAML
│       │   └── loader.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py            # StorageBackend ABC
│       │   ├── models.py          # pydantic WebhookRequest, StoredWebhook
│       │   └── sqlite.py          # AsyncSQLiteStorage
│       ├── handlers/
│       │   ├── __init__.py
│       │   ├── base.py            # WebhookHandler ABC
│       │   ├── manager.py         # HandlerManager (registration, dispatch, isolation)
│       │   ├── echo.py            # ported behavior
│       │   └── logger.py          # ported behavior
│       ├── validators/
│       │   ├── __init__.py
│       │   ├── base.py            # SignatureValidator ABC
│       │   ├── generic.py         # configurable HMAC
│       │   ├── github.py
│       │   └── stripe.py
│       ├── tunnel/
│       │   ├── __init__.py
│       │   ├── base.py            # TunnelProvider ABC
│       │   ├── ngrok.py           # NgrokProvider (pyngrok)
│       │   ├── cloudflare.py      # CloudflareProvider (subprocess cloudflared)
│       │   └── none.py            # NoTunnelProvider (local-only)
│       ├── replay/
│       │   ├── __init__.py
│       │   └── engine.py
│       ├── log.py                 # structlog setup
│       └── cli/
│           ├── __init__.py
│           └── main.py            # click commands: start, list, show, replay, stop
└── tests/
    ├── conftest.py
    ├── test_app.py
    ├── test_routes.py
    ├── test_config/
    ├── test_storage/
    ├── test_handlers/
    ├── test_validators/
    ├── test_tunnel/
    └── test_replay/
```

### Single responsibilities (the durable design decisions)

- **`storage/base.py`** — `StorageBackend` ABC with `store(webhook) -> StoredWebhook`, `get(id) -> StoredWebhook`, `list(limit, offset) -> list[StoredWebhook]`, `record_handler_run(...)`. Concrete: `AsyncSQLiteStorage`. Designed so a future `PostgresStorage` slots in without route changes.
- **`handlers/base.py`** — `WebhookHandler` ABC. `name: str` property + `async process(self, webhook: StoredWebhook) -> HandlerResult`. That's the entire surface. No required `__init__` shape, no lifecycle hooks v1.
- **`handlers/manager.py`** — `HandlerManager.register(handler)` / `dispatch(webhook)`. Dispatch runs all handlers concurrently with `asyncio.gather(..., return_exceptions=True)`. Exceptions are caught per handler, logged, and recorded to the `handler_runs` table. Failure of one handler never affects another.
- **`validators/base.py`** — `SignatureValidator` ABC with `validate(body: bytes, headers: dict) -> bool`. Concrete classes (`GenericHMACValidator`, `GitHubValidator`, `StripeValidator`) implement the actual algorithm.
- **`tunnel/base.py`** — `TunnelProvider` ABC with `async start() -> str` (returns public URL), `async stop() -> None`, `async health() -> TunnelHealth`. Lifecycle managed via FastAPI's `lifespan` context.
- **`replay/engine.py`** — Pure function `replay(stored_webhook, handlers) -> list[HandlerResult]`. Reuses `HandlerManager.dispatch` internals so behavior is identical to live receive.

The intent: a handler author touches `handlers/base.py` only. A validator author touches `validators/base.py` only. A tunnel author touches `tunnel/base.py` only. No cross-coupling.

---

## 6. Data Models

### `WebhookRequest` (in-flight, before storage)

```python
class WebhookRequest(BaseModel):
    path: str                      # e.g. "/webhook/github"
    method: str                    # always "POST" v1
    headers: dict[str, str]        # lowercased keys
    body: bytes
    body_json: dict | None         # parsed if Content-Type is JSON, else None
    received_at: datetime          # UTC
    source_ip: str | None
```

### `StoredWebhook`

```python
class StoredWebhook(WebhookRequest):
    id: str                        # ULID for sortability
    signature_valid: bool | None   # None if no validator configured for path
    signature_validator: str | None  # validator class name
```

### `HandlerResult`

```python
class HandlerResult(BaseModel):
    handler_name: str
    webhook_id: str
    started_at: datetime
    completed_at: datetime
    success: bool
    return_value: Any | None       # serialized JSON-compatibly
    error: str | None              # repr of exception if failed
    traceback: str | None
```

### SQLite schema

```sql
CREATE TABLE webhooks (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  method TEXT NOT NULL,
  headers TEXT NOT NULL,         -- JSON-encoded dict
  body BLOB NOT NULL,
  body_json TEXT,                -- nullable, JSON-encoded if parseable
  received_at TEXT NOT NULL,     -- ISO 8601 UTC
  source_ip TEXT,
  signature_valid INTEGER,       -- NULL / 0 / 1
  signature_validator TEXT
);

CREATE INDEX idx_webhooks_path_received ON webhooks(path, received_at DESC);

CREATE TABLE handler_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  webhook_id TEXT NOT NULL REFERENCES webhooks(id),
  handler_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  success INTEGER NOT NULL,
  return_value TEXT,             -- JSON-encoded
  error TEXT,
  traceback TEXT
);

CREATE INDEX idx_handler_runs_webhook ON handler_runs(webhook_id);
```

### `config.yaml` example

> **Note (v1, as-built):** this block reflects what the shipped code actually
> accepts. It differs from the original Sprint 0 sketch in a few places — see
> "v1 implementation amendments" below for the deltas + rationale.

```yaml
server:
  host: 0.0.0.0
  port: 8000
  log_level: INFO
  log_format: json               # json (default) | console

storage:
  backend: sqlite
  path: data/hooky.db
  retention_days: 30             # 0 = keep forever (enforcement: parking lot)

# Handlers loaded by dotted import path + registered at startup (Sprint 5).
# This is how a consuming app registers handlers via config alone — no
# launcher code.
handlers:
  - path: "myapp.handlers:SmsHandler"
    config:                      # optional non-secret kwargs -> SmsHandler(**config)
      from_number: "+15551234567"
    secret_env:                  # kwarg -> ENV_VAR; resolved from env at startup
      api_token: MYAPP_API_TOKEN   # -> SmsHandler(..., api_token=<from env>)
  - path: "hooky.handlers.logger:LoggerHandler"

tunnel:
  provider: none                 # none (default) | ngrok | cloudflare
  cloudflare:
    hostname: hooky-main.example.com   # public hostname (was 'tunnel_name';
                                       # 'tunnel_name' still accepted as alias)
    # token sourced from env CLOUDFLARED_TOKEN
  ngrok:
    region: us
    # auth_token sourced from env NGROK_AUTH_TOKEN

validators:
  - path: /webhook/github
    type: github
    secret_env: GITHUB_WEBHOOK_SECRET
  - path: /webhook/custom
    type: generic_hmac
    secret_env: CUSTOM_WEBHOOK_SECRET
    header: x-custom-signature   # exact header depends on the provider
    algorithm: sha256
    prefix: ""                   # e.g. "sha256=" for some providers
```

### `.env.example`

```bash
# Tunnel auth (one or the other depending on provider)
NGROK_AUTH_TOKEN=
CLOUDFLARED_TOKEN=

# Per-validator secrets (referenced by config.yaml)
GITHUB_WEBHOOK_SECRET=
STRIPE_WEBHOOK_SECRET=
CUSTOM_WEBHOOK_SECRET=
```

### v1 implementation amendments

The sprint sections below are the original plan. The shipped code (Sprints
1–5, all complete) intentionally diverged in the following ways. Each is
recorded in detail in the corresponding DEVLOG entry; this is the index.

| # | Area | Plan | As-built | Why |
|---|---|---|---|---|
| 1 | **`handlers:` config** | Not in the YAML schema; handlers "registered programmatically OR via config" (§goals) left undefined | New `handlers:` list of dotted import paths (`"pkg.mod:Class"`) + optional `config:` kwargs + optional `secret_env:` (kwarg→ENV_VAR indirection, mirroring validators). `create_app` + CLI build the `HandlerManager` from it. **Contract locked post-v1** — see DEVLOG "Handler config contract". | Makes a consumer config-only — no launcher code. The old CLI couldn't replay to handlers at all. `secret_env` keeps secrets out of YAML *and* handler code, and out of error messages. (Sprint 5 + post-v1) |
| 2 | **Cloudflare `tunnel_name`** | `cloudflare.tunnel_name: hooky-main` | Field renamed to `hostname`; `tunnel_name` kept as a deprecated alias | "tunnel name" was misleading — it's the public hostname. (Sprint 4) |
| 3 | **Default tunnel provider** | Example showed `provider: cloudflare` | Defaults to `provider: none` | Default config must need no domain/token/env; opt in to real tunnels. (Sprint 4) |
| 4 | **`/healthz` shape** | `{status, last_webhook_at}` | Adds a `tunnel` block `{status, public_url, last_checked, error}` when a tunnel is wired in | Tunnel health is an operational must-have. (Sprint 4) |
| 5 | **`StorageBackend` ABC** | `store/get/list/last_received_at/record_handler_run` | + `update_signature_status(id, valid, name)` (Sprint 3), + `get_handler_runs(id)` and `list(..., path=...)` (Sprint 5) | Needed for store-then-validate, `hooky show`, and `hooky list --path` |
| 6 | **Replay engine** | `replay(stored_webhook, handlers) -> list[HandlerResult]` pure function | `replay_webhook(storage, manager, id, handler_names=None) -> ReplayResult`; reuses `HandlerManager.dispatch`; records new `handler_runs` rows (does not mutate the original) | Reuse-live-dispatch rule; richer return for the CLI. (Sprint 5) |

**Still outstanding against the plan:** the Sprint 4 Cloudflare manual smoke
(survives `docker compose restart`) has not run against real infrastructure —
blocked on a Cloudflare account + domain. All other success criteria are met.

---

## 7. Sprint Plan

Five sprints. Each ~2-3 hours of focused work.

### Sprint 1: FastAPI scaffold + storage layer

**Goal**: A FastAPI app that receives any POST to `/webhook/{path}`, stores it in SQLite, returns 200. No handlers yet, no validators yet, no tunnel.

**Success Criteria**:
- ✓ `pip install -e ".[dev]"` succeeds.
- ✓ `uvicorn hooky.app:create_app --factory` starts the server.
- ✓ POST to `http://localhost:8000/webhook/test` with arbitrary JSON body returns 200 and creates a row in `webhooks` table.
- ✓ Headers stored as JSON dict; body stored as BLOB; ULID id assigned.
- ✓ Store-first invariant: storage happens before the response is returned (verified by test that mocks storage to raise + asserts 5xx).
- ✓ `/healthz` returns 200 with last-webhook timestamp.
- ✓ Tests cover: storage round-trip, route happy path, route with malformed JSON body (stores as bytes, body_json=None), store-first invariant.

**Deliverables**:
- `pyproject.toml`, `src/hooky/{app.py, routes.py, log.py, storage/}`, `config/` minimum schema.
- `tests/test_routes.py`, `tests/test_storage/`.
- DEVLOG Sprint 1 entry.

**Test count target**: 12-16 tests.

---

### Sprint 2: Async handler interface + manager + Echo/Logger

**Goal**: Webhook receive dispatches to registered handlers concurrently and in isolation. Ship two reference handlers.

**Success Criteria**:
- ✓ `WebhookHandler` ABC with async `process(webhook)`.
- ✓ `HandlerManager.register(handler)`, `dispatch(webhook)` runs all concurrently.
- ✓ Handler exceptions are caught per-handler, logged with traceback, recorded to `handler_runs`. Other handlers complete.
- ✓ `EchoHandler` returns the webhook body. `LoggerHandler` logs the webhook at INFO.
- ✓ Route dispatches handlers async-after-200 (returns 200 first, dispatches second; no head-of-line blocking).
- ✓ Tests: handler isolation (one raises, others run), handler ordering doesn't matter, results recorded correctly, replay-compatible (same `dispatch` used in Sprint 5 replay).

**Deliverables**:
- `src/hooky/handlers/{base, manager, echo, logger}.py`.
- `tests/test_handlers/`.
- DEVLOG Sprint 2 entry.

**Test count target**: 10-14 tests.

---

### Sprint 3: Signature validators (Generic HMAC, GitHub, Stripe)

**Goal**: Path-scoped signature validation; invalid signatures return 401 but still store (for debugging).

**Success Criteria**:
- ✓ `SignatureValidator` ABC with `validate(body, headers) -> bool`.
- ✓ `GenericHMACValidator(secret, header, algorithm, prefix)` — configurable, the workhorse.
- ✓ `GitHubValidator(secret)` — verifies `X-Hub-Signature-256: sha256=<hex>`.
- ✓ `StripeValidator(secret, tolerance_seconds)` — verifies `Stripe-Signature` header including timestamp + replay-window check.
- ✓ Route loads per-path validator from config; on invalid signature, stores webhook with `signature_valid=0` and returns 401 (still stored = debuggable).
- ✓ On valid signature, stores with `signature_valid=1` and dispatches handlers.
- ✓ Tests: known-good and known-bad fixtures per validator; missing-header behavior; case-insensitive header lookup; Stripe replay-window expiry.

**Deliverables**:
- `src/hooky/validators/{base, generic, github, stripe}.py`.
- `tests/test_validators/` with fixture payloads.
- DEVLOG Sprint 3 entry.

**Test count target**: 14-18 tests.

**Notes on lifting**: HMAC math from old `webhook-reciever/src/webhook_receiver/validators/` can be lifted near-verbatim. Re-read old tests as behavioral spec; don't port the test files themselves.

---

### Sprint 4: Tunnel providers + Cloudflare integration

**Goal**: Pluggable tunnel system. Ship `NoTunnelProvider`, `NgrokProvider`, and the new `CloudflareProvider`.

**Success Criteria**:
- ✓ `TunnelProvider` ABC: `async start() -> public_url`, `async stop()`, `async health() -> TunnelHealth`.
- ✓ Tunnel lifecycle managed by FastAPI's `lifespan` — starts on app startup, stops on shutdown.
- ✓ `NoTunnelProvider.start()` returns `http://localhost:{port}`.
- ✓ `NgrokProvider` uses `pyngrok` to open a tunnel and returns the public HTTPS URL.
- ✓ `CloudflareProvider` spawns `cloudflared tunnel run --token $CLOUDFLARED_TOKEN` as a subprocess, reads stdout to confirm "Connection registered," returns the configured tunnel hostname (from config). Kills subprocess on stop.
- ✓ `/healthz` includes tunnel health (connected / disconnected / error + last-checked timestamp).
- ✓ Tests: `NoTunnelProvider` unit tests, `NgrokProvider` against mocked pyngrok, `CloudflareProvider` against mocked subprocess.
- ✓ Manual smoke: with a real Cloudflare tunnel token (the author's), Sprint 4 ships a working tunnel that survives `docker compose restart`.

**Deliverables**:
- `src/hooky/tunnel/{base, none, ngrok, cloudflare}.py`.
- `tests/test_tunnel/` with mocked subprocess + pyngrok.
- DEVLOG Sprint 4 entry + manual smoke notes.

**Test count target**: 10-12 tests + manual smoke.

---

### Sprint 5: CLI + replay engine + README

**Goal**: Operational ergonomics. Ship the CLI commands a human (or a consuming app) actually uses, plus the replay engine.

**Success Criteria**:
- ✓ `hooky start [--config PATH]` starts the server with tunnel.
- ✓ `hooky list [--limit N] [--path /webhook/x]` shows recent webhooks as a table.
- ✓ `hooky show <id>` displays full webhook detail (headers, body, signature status, handler runs).
- ✓ `hooky replay <id> [--handler NAME]...` re-dispatches stored webhook to all (or selected) handlers; results displayed.
- ✓ `hooky stop` gracefully shuts down a running server.
- ✓ Replay uses the *same* `HandlerManager.dispatch` as live receive — no divergent code paths.
- ✓ README quickstart works in <5 minutes for a fresh clone.
- ✓ Test coverage on `src/hooky/` ≥ 80%.
- ✓ Manual integration smoke: configure GitHub webhook on a real test repo (or just curl a fixture) → see it stored → replay it → see handler results.

**Deliverables**:
- `src/hooky/cli/main.py`, `src/hooky/replay/engine.py`.
- `tests/test_replay/`, `tests/test_cli/` (CLI tests via click's `CliRunner`).
- `README.md` with quickstart + handler-authoring guide + advanced config.
- DEVLOG Sprint 5 entry.

**Test count target**: 10-14 tests + coverage gate.

---

## 8. Testing Strategy

**Framework**: `pytest` + `pytest-asyncio` (async tests), `respx` (HTTP mocking for any handlers that call APIs), `fastapi.testclient.TestClient` (async-compatible).

**Per-sprint requirements**:
- Tests written in the same sprint as the code.
- Each success criterion maps to ≥1 test.
- No tests that just exist — every test asserts behavior.
- ≥80% line coverage on `src/hooky/` by end of Sprint 5.

**What we mock**:
- pyngrok in NgrokProvider tests.
- subprocess in CloudflareProvider tests.
- File system in storage tests (use a temp DB per test).

**What we DON'T mock**:
- Internal interfaces — exercise `HandlerManager`, `SignatureValidator`, etc. as built.
- FastAPI routing — use `TestClient` end-to-end.

**Fixture payloads**: Sprint 3 includes real GitHub and Stripe webhook fixtures (publicly available test vectors) for signature validator tests.

**Manual smoke**: Sprints 4 and 5 include real-services smoke tests, logged in DEVLOG.

---

## 9. Constraints & Boundaries

### Do

- ✅ Stay strictly within sprint scope. New ideas → §10 Parking Lot.
- ✅ Treat the handler/validator/tunnel ABCs as the public API. Backwards-compatibility matters here in a way it doesn't for a downstream consumer.
- ✅ Default-deny: invalid signature → 401, but stored (debugging unlock).
- ✅ Async everywhere. No sync-with-asyncio.run() hacks inside the framework.
- ✅ Structured JSON logs to stdout. Never log raw secrets.
- ✅ Update DEVLOG every sprint with decisions + *why*.
- ✅ When lifting code from old `webhook-reciever/`, lift the *algorithm*, not the surrounding scaffolding.

### Do NOT

- ❌ Add features specific to any single consumer (no provider-specific business logic, no GitHub-specific paths beyond signature validation, no consumer assumptions in core).
- ❌ Add admin endpoints, dashboards, or web UIs in v1.
- ❌ Add Postgres or Redis storage in v1 — design the interface, ship SQLite only.
- ❌ Auto-retry failed handlers in v1. (Replay is manual via CLI.)
- ❌ Block storage on validator success — store first, validate after, return 401 with stored=true.
- ❌ Couple the tunnel system to FastAPI lifespan internals beyond the documented hook.
- ❌ "Helpfully" add features not in the spec.

### Known accepted tradeoffs

- Local SQLite only in v1. Scales fine for personal use; refactor when a real reason emerges.
- No admin auth on `/healthz` or CLI — local-only assumption.
- Handler failure isolation is per-dispatch, not per-handler-thread. A handler that hangs forever blocks its own slot but not others.
- Cloudflare Tunnel requires `cloudflared` binary present on host — documented in README, not bundled.

---

## 10. Out of Scope (v1)

- Postgres/Redis storage backends (interface designed, implementations deferred)
- Admin web UI / dashboard
- Webhook source-side configuration helpers (we don't help you configure GitHub's webhook UI)
- Auto-retry / dead-letter queues for failed handlers
- Streaming / Server-Sent-Events from handlers back to caller
- Multi-tenant support (single-tenant assumed)
- Built-in handlers for specific services beyond Echo + Logger
- Distribution to PyPI (local install only v1)
- Web UI for the replay engine

### 🅿️ Parking Lot

#### 🅿️ Pluggable storage backends (Postgres, Redis)
- **Core idea**: `StorageBackend` ABC is designed in v1; v2 ships concrete `PostgresStorage` and/or `RedisStorage`.
- **Value**: Multi-host deploys, durability beyond local SQLite, cross-process replay.
- **Blocker**: No actual need yet. Add when a consumer hits SQLite's limits.
- **Required context**: Real production workload to size against.

#### 🅿️ Built-in handlers for common services
- **Core idea**: `SlackNotifier`, `DiscordWebhookRelay`, `AirtableLogger`, `LinearTicketCreator`, etc.
- **Value**: Faster wiring for common webhook → action patterns.
- **Blocker**: Not v1 scope. Risk of accumulating shallow handlers that bit-rot.
- **Required context**: Demand signal — second or third consumer wants the same thing.

#### 🅿️ Admin dashboard
- **Core idea**: Simple web UI for browsing stored webhooks, viewing handler runs, triggering replays.
- **Value**: Better UX than CLI for debugging at scale.
- **Blocker**: v1 scope. CLI is sufficient for personal use.
- **Required context**: Replay frequency high enough that CLI ergonomics hurt.

#### 🅿️ Auto-retry with backoff for failed handlers
- **Core idea**: Failed handler runs get auto-retried with exponential backoff up to N attempts; permanent failures land in a dead-letter view.
- **Value**: Reliability for handlers that depend on flaky downstream APIs.
- **Blocker**: v1 scope. Manual replay covers it for now.
- **Required context**: Specific handler patterns that warrant the complexity.

#### 🅿️ PyPI distribution
- **Core idea**: Publish `hooky-mchookface` to PyPI for clean `pip install`.
- **Value**: Other people (or other machines) can install it without local path tricks.
- **Blocker**: v1 is single-user, local-install. Cross the bridge when there's a reason.
- **Required context**: Multi-machine deployment scenario, or someone other than the author wants to use it.

---

## 11. Spec Maintenance

Living document. When implementation reveals a better approach:

1. Propose the change with rationale.
2. Get approval.
3. Update the spec inline.
4. Note the change in DEVLOG.

The spec reflects as-built, not just as-planned.

---

## 12. Process Notes (Meta-Feedback)

Written immediately after drafting.

**What worked well**:
- The existing old `webhook-reciever/` codebase served as a behavioral spec without us having to port it. Reading its module layout, README features list, and dependency list told us exactly what the v1 of *this* project should cover and what to lift.
- Treating ABCs as the public API is a useful framing for library specs vs service specs. The "what does a future handler author see" question forced clean module boundaries.
- Naming the first consumer up front made it easy to decide what's in-scope (signature validation infra) vs out-of-scope (any provider-specific logic).

**What was tricky**:
- Storage interface vs concrete implementation. Designed an ABC for forward-compatibility despite shipping only SQLite. Could be over-engineering, but it's a low-cost abstraction designed in vs retrofitted.
- Tunnel lifecycle. Coupling `TunnelProvider` to FastAPI's `lifespan` is mildly framework-specific; if Hooky_McHookface ever ran outside FastAPI (unlikely), the abstraction would need adjustment.
- Replay semantics. The decision to make replay use the same `HandlerManager.dispatch` path as live receive was important and easy to forget — without it, replays subtly diverge from production behavior.

**Suggestions for the template**:
- A "Public API surface" subsection — for library specs, explicitly enumerating which classes/functions are part of the contract vs internal — would be valuable. Different shape than what services need.
