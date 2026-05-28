# Development Log — Hooky_McHookface

> What got built each sprint, why, and what surprised us. Add entries as work happens — not retroactively.

---

## Sprint 0 — Genesis (2026-05-27)

**What this project is**: An async-native FastAPI-based webhook receiver framework with pluggable handlers, validators, tunnel providers, and persistent storage. Designed as durable infrastructure for Brad's future projects.

**Where it came from**: an earlier Flask version (`webhook-reciever/`, folder name misspelled, never put into production) preceded this. During Sprint 0 of the first consumer project, it became clear that:

1. That consumer is exactly the kind of webhook handler that webhook-reciever was designed to host.
2. Flask vs FastAPI is a real upgrade (async-native, Pydantic-first, OpenAPI for free, better for I/O-bound webhook workloads).
3. The Cloudflare Tunnel provider was a planned-but-unbuilt slot.
4. Brad correctly identified that with agent-era labor costs near-zero, the rewrite-vs-migrate calculus favors clean rewrite when (a) zero production users, (b) existing tests serve as behavioral spec, (c) existing code is entangled with assumptions we're trying to escape, (d) existing code was never production-validated. All four were true.

**Clean break, fresh name**: New folder, new git repo, new name. Python import name: `hooky` (short, ergonomic; PyPI dist name would be `hooky-mchookface` if ever published).

**Relationship to the old codebase**: `webhook-reciever/` stays on disk as a **reference implementation + behavioral spec source**. Lift HMAC math, SQLite schema decisions, config schemas. Do NOT inherit Flask code, sync handler interfaces, or test scaffolding.

**Key design decisions (v1)**:
- **FastAPI + async-native** throughout. No `asyncio.run()` inside the framework.
- **ABCs are the public API.** Handler, Validator, Tunnel, Storage all have ABCs. Backwards-compat matters here.
- **Store-first invariant.** Webhook is persisted before validator runs and before any handler dispatches. Invalid signatures still store (with `signature_valid=0`) for debugging.
- **Handler isolation** via `asyncio.gather(..., return_exceptions=True)`. One handler failure never affects another.
- **Replay reuses live dispatch** — same `HandlerManager.dispatch` path. No divergent code paths between live and replay.
- **Storage interface designed in from day 1**, only SQLite implementation ships. Postgres/Redis are parking lot.
- **Cloudflare Tunnel** as a first-class provider (the upgrade over the old codebase's planned-only slot). Spawns `cloudflared` as a subprocess.
- **MIT License**. Old project was unspecified.

**v1 scope (5 sprints)**:
1. FastAPI scaffold + SQLite storage layer (store-first invariant)
2. Async handler interface + manager + EchoHandler + LoggerHandler
3. Signature validators: GenericHMAC, GitHub, Stripe (lift algorithms from old code)
4. Tunnel providers: None, Ngrok, Cloudflare (the new one)
5. CLI + replay engine + README + 80% coverage gate

**First consumer**: a separate private project. Its spec assumes Hooky_McHookface as a dependency; its early sprints collapse to "implement a `WebhookHandler` subclass" once this framework is shipped.

**Open decisions deferred to Sprint 1 kickoff** (defaulted in spec):
1. Import name = `hooky` (short).
2. Storage backend interface designed in v1, only SQLite ships.
3. No auth on local HTTP API beyond HMAC on webhooks.

**Deliverables**:
- [SPEC.md](SPEC.md) — full spec, 5 sprints, parking lot.
- [onboarding.md](onboarding.md) — agent entry point.
- This DEVLOG.

**Next**: Sprint 1 — FastAPI scaffold + storage layer.

---

## Sprint 1 — FastAPI scaffold + storage layer (2026-05-27)

### Summary
Shipped the FastAPI scaffold + async SQLite storage layer. `uvicorn hooky.app:create_app --factory` brings up a server with `POST /webhook/{path:path}` and `GET /healthz`. Every received webhook is stored — with a ULID id, lowercase headers as JSON, raw body as BLOB, and best-effort `body_json` parse — before the 200 is returned. No handlers, no validators, no tunnel yet (those are Sprints 2-4).

### Decisions
- **`python-ulid` for the id**: ULIDs are 26-char canonical strings, lexicographically sortable by time, no extra index needed for "most recent first" ordering — the PK is already the right shape. Cheaper than `uuid4 + separate timestamp index`.
- **Single aiosqlite connection per backend**: aiosqlite serialises access internally via its worker thread. For our workload (low write rate, append-mostly, no concurrent writers) this is correct. If we ever hit lock contention, we'll revisit with a small pool.
- **`body_json: dict | None` (not `Any | None`)**: SPEC §6 types it as `dict | None`. JSON arrays/scalars are stored only in `body` (raw bytes); `body_json` stays `None` for them. Test `test_post_webhook_with_json_array_keeps_body_json_none` pins this so the next maintainer doesn't widen it accidentally.
- **JSON parse gated on Content-Type**: only attempt `json.loads(body)` if `Content-Type` contains `"json"`. Avoids accidentally parsing a binary blob whose first bytes happen to look like `{`.
- **App factory takes injected `storage`**: when tests pass storage in, lifespan does NOT manage it (`owns_storage=False`). Lets tests keep an in-memory backend live across the request-then-assert cycle and inspect rows directly.
- **`create_app` runs `configure_logging` on construction**: side-effecty but predictable — uvicorn calls the factory once per process. Avoids requiring users to call a setup function manually before `uvicorn`.
- **Store-first invariant lives in the route, not a middleware**: cleaner stack trace on failure, no chance of a middleware ordering bug skipping the store. The single `await storage.store(webhook)` line is the invariant.
- **`HandlerResult` model + `handler_runs` table shipped in Sprint 1** even though no handlers run. The ABC method `record_handler_run` is part of the storage contract from day 1 so we don't have to bump the contract in Sprint 2.

### Tests
- **Count: 23** (target was 12-16; came in higher because storage edge cases were cheap to cover).
- **Pass: 23/23.** Run with `pytest --cov=hooky`.
- **Coverage: 91% on `src/hooky/`.** Untested code is the `__main__.py` placeholder (Sprint 5 ships the real CLI), the `load_config` YAML-path branch (no tests exercise file loading yet — defaults are used), and a couple of defensive branches.
- **Sprint 1 success criteria coverage:**
  - Storage round-trip — `test_store_roundtrip_preserves_all_fields` + 5 supporting tests.
  - Route happy path — `test_post_webhook_happy_path_returns_200_and_id` + persistence asserted in `test_post_webhook_persists_full_request`.
  - Malformed JSON body — `test_post_webhook_with_malformed_json_stores_bytes_and_null_json`.
  - Store-first invariant — `test_store_failure_yields_5xx_no_silent_200` (mocks storage to raise, asserts 5xx).
  - `/healthz` last-webhook timestamp — `test_healthz_when_empty_returns_null_last` + `test_healthz_reflects_latest_webhook`.

### Surprises / gotchas
- **httpx serialises JSON compactly** (`{"a":1}`, no spaces) where stdlib `json.dumps` defaults to spaces. Round-trip test originally compared against `json.dumps(payload).encode()` and failed. Fixed by using `separators=(",", ":")`. Trivial but easy to lose 10 minutes on.
- **`pytest-asyncio` v1.x with `asyncio_mode = "auto"`** runs *all* tests in an event loop, including sync ones. That's fine, but it means the TestClient (sync) and async fixture both live under the same loop — which actually works in our favour: the in-memory `aiosqlite` connection stays the same object across `client.post(...)` and the test's `await storage.get(...)`.
- **FastAPI lifespan + injected storage**: I almost made the lifespan close storage unconditionally — that would have closed the test fixture's connection mid-test. The `owns_storage` flag fixes it. Also surfaces an API question for later sprints: should `create_app` take a `lifespan_managed: bool` knob? Leaving as parking-lot until a real consumer hits it.
- **`extra="forbid"` on the config schema**: deliberately strict — typos in YAML keys should fail loudly, not silently default. We'll relax per-section if it ever bites.

### Lifted from old codebase (`webhook-reciever/`)
- **SQLite schema shape**: the old `webhooks` table had `headers TEXT NOT NULL` (JSON-encoded), `body TEXT`, `body_json TEXT` — same idea, but the old code used `TEXT` for body which would corrupt binary payloads. New schema uses `BLOB` per SPEC §6. Took the storage *idea*, redesigned the column type.
- **Header normalisation pattern**: old code took `dict(request.headers)` (Flask) which preserves canonicalised casing. The new code goes through Starlette's `request.headers.raw` and lowercases keys explicitly, since signature validators (Sprint 3) need case-insensitive lookups and "store what came in, normalised once at the boundary" beats "store as-is + lowercase on every read."
- **Store-before-respond ordering**: the old Flask route stored before returning. We kept the same ordering and added an explicit test for it. Old code did *not* test that the store-failure path returns non-200 — that's a Sprint 1 gain.
- **Did NOT lift**: the old `update_handler_results` JSON-blob-on-the-webhook-row pattern. SPEC §6's `handler_runs` table is the right model — one row per (webhook, handler) so we can filter on handler failures, see latencies, etc.
- **Did NOT lift**: `threading.Thread` for async handler dispatch. Sprint 2 will use `asyncio.create_task` + `gather(..., return_exceptions=True)` per SPEC §5 and onboarding.md's "async-native throughout" rule.

### Spec amendments
- None. SPEC §5/§6/§7 matched what the code wanted to do.

### Open questions for Brad
- **`HookyConfig` defaults**: I set `storage.path = "data/hooky.db"` as the default. The smoke test created a `data/` directory at the repo root. Fine for dev, but you may want `~/.hooky/hooky.db` or `$XDG_DATA_HOME/hooky/...` as the default. Deferred — current default is fine for Sprint 2's needs.
- **JSON body_json policy for arrays**: I treat JSON arrays as "store raw, body_json=None" because the type is `dict | None`. If you'd rather have arrays parsed into a wrapper (e.g. `{"_array": [...]}`) for handler convenience, say so and I'll revisit in Sprint 2.

### Confidence
**HIGH.** Sprint 1's scope was tightly defined and the success criteria mapped 1:1 to tests that pass. The ABC + injected-storage design gives Sprint 2 a clean handler-manager seam to plug into.

### Next (Sprint 2)
- `WebhookHandler` ABC + `HandlerManager` with `asyncio.gather(..., return_exceptions=True)`.
- `EchoHandler`, `LoggerHandler` as reference impls.
- Route dispatches handlers async-after-200 (use `BackgroundTasks` or `asyncio.create_task` + a tracked task set).
- Handler runs recorded via the `record_handler_run` method that's already on `StorageBackend`.

---

## Sprint 2 — Async handler interface + Echo/Logger (2026-05-27)

### Summary
Shipped the async handler subsystem: `WebhookHandler` ABC, `HandlerManager` with concurrent isolated dispatch, and the two reference handlers (`EchoHandler`, `LoggerHandler`). The route now dispatches handlers via FastAPI `BackgroundTasks` after the 200 is returned — the store-first invariant from Sprint 1 is preserved (storage runs synchronously; only handler dispatch is deferred). Handler exceptions are caught per-handler, traceback is logged, and a failed `HandlerResult` is recorded to `handler_runs`. Same `dispatch()` will be reused by Sprint 5 replay — no divergent code path planned.

### Decisions
- **Manager owns persistence; storage is optional.** `HandlerManager(storage=...)` records each result via `record_handler_run` after dispatch completes. Passing `storage=None` returns results without persisting — useful for unit tests *and* the future replay engine if Brad wants a "replay-without-record" mode. Default for the live app is "always record".
- **`asyncio.gather(..., return_exceptions=True)` as belt-and-braces.** `_run_one` already catches everything that `process()` can raise, so in practice gather never sees an exception. The `return_exceptions=True` is a guard against a bug in `_run_one` itself — a synthesised failed `HandlerResult` is produced rather than poisoning the whole dispatch.
- **`BackgroundTasks` for dispatch-after-200.** Considered `asyncio.create_task` + a tracked task set; rejected for v1 because FastAPI's `BackgroundTasks` is purpose-built for exactly this ("run after response is sent") and integrates with the lifespan cleanup story we'll need in Sprint 4. A wrapper (`_dispatch_safely`) catches any catastrophic error to keep the background task pool clean.
- **Non-JSON-serialisable return values are silently dropped from persistence.** If a handler returns e.g. a custom object, the in-memory `HandlerResult.return_value` is set to `None` rather than blowing up `json.dumps` during `record_handler_run`. The handler still records `success=True`. This favours "the run was successful" over "we recorded a perfect round-trip" — debugging is recoverable from logs.
- **Storage failures during `record_handler_run` are logged-and-swallowed.** By the time the manager calls record, the route has already returned 200. Re-raising would crash the background task; we log instead. The webhook is still in the `webhooks` table, so the run can be re-discovered by re-running the handler in replay.
- **`cache_logger_on_first_use=False` in structlog.** Discovered during testing: `PrintLoggerFactory(file=sys.stdout)` captures `sys.stdout` at configure time, but `cache_logger_on_first_use=True` then caches the resulting bound logger forever, so when pytest's `capsys` swaps stdout the cached logger writes to a closed file. Disabling the cache costs one dict lookup per `.info(...)` call — entirely fine for our throughput. Production benefit too: log output remains correct if anyone redirects stdout at runtime.
- **`name`-uniqueness is enforced at register time.** Two handlers with the same `name` would collide in `handler_runs` queries. Empty-string `name` is also rejected, since an empty handler name has no replay-CLI affordance.
- **`handlers` is a snapshot property, not the live list.** Returning `list(self._handlers)` prevents outside callers from mutating internal state. (Pinned by `test_handlers_snapshot_is_a_copy`.)

### Tests
- **Count: 25 new** (24 in `tests/test_handlers/`, 2 extending `tests/test_routes.py`). Cumulative: **48 tests** (Sprint 1 + Sprint 2). Target was 10-14 — came in higher because manager edge cases were cheap to cover.
- **Pass: 48/48.** Run with `pytest --cov=hooky`.
- **Coverage: 92% on `src/hooky/`** (up from 91% — handlers tree pulls the average up). Untested code is the `__main__.py` placeholder (Sprint 5), the `load_config` YAML-file path (no fixture exercises it), and the manager's defensive "_run_one itself raised" branch (only reachable by monkey-patching the manager).
- **Sprint 2 success criteria coverage:**
  - `WebhookHandler` ABC contract — `test_base.py` (4 tests: abstract enforcement on instantiation, missing `name`, missing `process`, full impl works).
  - `HandlerManager.register` + uniqueness — `test_register_rejects_duplicate_name`, `test_register_rejects_empty_name`, `test_handlers_snapshot_is_a_copy`.
  - Concurrent dispatch — `test_dispatch_runs_handlers_concurrently` (two 50ms sleeps complete in <90ms).
  - Handler isolation — `test_dispatch_isolates_handler_exception` (one raises, two succeed; all three recorded).
  - All-fail dispatch — `test_dispatch_handles_all_handlers_failing`.
  - Result ordering matches registration — `test_dispatch_results_preserve_registration_order` (Sprint 5 replay relies on this).
  - Empty manager — `test_dispatch_with_no_handlers_returns_empty`.
  - Persistence to `handler_runs` — `test_dispatch_persists_handler_runs` (round-trips via real `AsyncSQLiteStorage`).
  - Storage-failure isolation — `test_dispatch_swallows_persist_failure`.
  - `EchoHandler` returns body — `test_echo.py` (4 tests covering JSON, text, undecodable bytes, name).
  - `LoggerHandler` emits + returns ack — `test_logger.py` (3 tests).
  - Route dispatches after 200 — `test_post_webhook_dispatches_handlers_after_200`, `test_post_webhook_returns_200_even_if_handler_raises`.

### Surprises / gotchas
- **`structlog` cache + `capsys` interaction.** Initial test run had 13 failures all manifesting as `ValueError: I/O operation on closed file`. Root cause was the structlog logger cache holding a reference to the capsys-swapped stdout after the test exited. Fixed in production code (`cache_logger_on_first_use=False`) rather than test-side because production code is the durable surface — the test would have masked a real "redirect stdout at runtime" bug.
- **TestClient + BackgroundTasks ordering.** FastAPI's `TestClient` (Starlette-based) blocks until background tasks complete before returning from `client.post(...)`. That's actually helpful here — the route test can `assert resp.status_code == 200` and then immediately read from `handler_runs` without sleeping. In production (real uvicorn), the 200 ships first and tasks run async. The test doesn't catch that ordering bug — but the manager-level concurrency test does, and the `BackgroundTasks.add_task` call happens AFTER `JSONResponse` construction, which is the architectural guarantee.
- **`json.dumps(None) == "null"`, not None.** Initial draft of `_run_one` did `json.dumps(value) if value is not None else None` for the persisted return value. That subtly wrong because `False`, `0`, `""`, `[]` are all falsy-but-serialisable. Fixed to use `_is_json_serialisable(value)` which calls `json.dumps` once and catches `TypeError`. Caught by the persistence test asserting `return_value == '{"value": 1}'`.
- **Background task does not see the route's `Request` object.** Good — it shouldn't. The `StoredWebhook` is the contract boundary. If a handler ever needs raw `Request` data (it shouldn't), that's a Sprint 6+ design problem.

### Lifted from old codebase (`webhook-reciever/`)
- **`name` property as the handler identity surface.** Old `WebhookHandler` ABC also exposed `name` as the duplicate-rejection key. Kept that shape because it's clearly correct.
- **Per-handler isolation in dispatch.** Old code used `ThreadPoolExecutor` + per-future try/except. New code uses `asyncio.gather(return_exceptions=True)` per onboarding's "async-native throughout" rule. Lifted the *isolation pattern*, not the sync threading mechanism.
- **Did NOT lift:** the old `configure(config: dict)` lifecycle hook on `WebhookHandler`. Spec §5 says "no required `__init__` shape, no lifecycle hooks v1" — handler authors compose configuration into their own `__init__` instead. Cleaner ABC surface; revisit if multiple handlers want config-file-driven setup.
- **Did NOT lift:** the old `timeout_seconds` on the manager. Spec §9 accepted that "a handler that hangs forever blocks its own slot but not others." Timeouts are a Sprint 6+ parking-lot feature once we have a real handler that warrants it.
- **Did NOT lift:** the old `EchoHandler`'s text/JSON formatting modes + sensitive-header masking. The spec says "EchoHandler returns the webhook body" — that's the contract. Anything richer is a userland handler, not framework-shipped.

### Spec amendments
- None. SPEC §5/§6/§7 Sprint 2 matched the implementation 1:1. `app.state.handler_manager` is now exposed (alongside `app.state.storage` and `app.state.config`) — undocumented detail, not part of the public contract.

### Open questions for Brad
- **Handler timeouts.** Old code had a 30s default. New code has none. If a handler hangs forever, the background task pool slot is leaked indefinitely. Acceptable for v1 (manual `hooky stop` recycles the process); flag for Sprint 6 if it bites in real usage.
- **`BackgroundTasks` vs `asyncio.create_task`.** Chose `BackgroundTasks` because it integrates with FastAPI lifespan. If a future scenario wants "dispatch fires regardless of response status" (e.g. middleware short-circuits the route), we'd need to switch to `create_task` + a tracked task set. Not a Sprint 2 problem.

### Confidence
**HIGH.** Sprint 2's contract is tight — the ABC surface is the public API, the dispatch semantics are spec'd exactly, and every success criterion has at least one pinning test. The `BackgroundTasks` choice is reversible if Sprint 4 surfaces a lifespan constraint. The structlog cache discovery was a free win on production correctness too.

### Next (Sprint 3)
- `SignatureValidator` ABC.
- `GenericHMACValidator(secret, header, algorithm, prefix)` — the workhorse.
- `GitHubValidator`, `StripeValidator` (Stripe includes timestamp + replay-window).
- Route: load per-path validator from config, store with `signature_valid=0/1`, return 401 on invalid (still stored — debuggable).
- Lift HMAC math from `webhook-reciever/src/webhook_receiver/validators/` near-verbatim per spec note.

---

## Sprint 3 — Signature validators (2026-05-28)

### Summary
Shipped the signature validator subsystem: `SignatureValidator` ABC plus three concrete validators (`GenericHMACValidator`, `GitHubValidator`, `StripeValidator`), per-path config-driven instantiation via a small registry, and route-layer integration that preserves the store-first invariant. A POST to a guarded path now: (1) stores the webhook with `signature_valid=NULL`, (2) runs the validator against the raw body, (3) updates `signature_valid`/`signature_validator` on the row, (4) returns 401 + skips handler dispatch on mismatch, or 200 + dispatches on match. Unsigned paths behave exactly as Sprint 2.

### Decisions
- **`validate(body, headers) -> bool` is sync, not async.** Validators do pure HMAC arithmetic — sub-millisecond, CPU-bound, no I/O. Async would be ceremony with no payoff. The route still `await`s on `storage.update_signature_status`, so the async surface where it matters is preserved.
- **No `name` property on `SignatureValidator`.** Unlike `WebhookHandler` (where `name` keys `handler_runs` queries), validators don't need a registration-time identity — they're keyed by path in the registry. The `signature_validator` column gets `type(v).__name__`. Keeps the ABC surface to one method.
- **Store-first means update-after-validate.** Two writes per validated webhook (insert, then update). Considered "validate first, store with result" — rejected because a validator bug or storage hiccup mid-validation must still leave a durable row. The invariant is "every received webhook is stored," not "every webhook is stored exactly once."
- **Validator exceptions are NOT caught in the route.** A crashing validator is a developer bug, not adversarial input. Let it 5xx — the webhook is already durably stored (store-first), so it's debuggable. Different from handler isolation (where one user-written handler shouldn't break dispatch); validators are framework-shipped + few in number.
- **Validator config built at app construction, not per-request.** `build_validator_map` runs once in `create_app`, reads env vars eagerly, fails loud on missing secrets. Avoids per-request env reads + secret-missing-in-prod-only failures. Tests can bypass entirely by injecting `validators=` directly.
- **Empty `secret` is rejected at construction.** A validator with no secret would either fail every request (confusing 401s) or accept every request (security hole, depending on impl). Better to fail at construction. The registry raises `ValidatorConfigError` when `secret_env` is unset.
- **Stripe `time_source` injection.** `StripeValidator` takes a `time_source: Callable[[], float]` kwarg defaulting to `time.time`. Lets tests pin "now" against fixture timestamps deterministically without monkeypatching `time`. Old code didn't have this seam and would have needed `freezegun` or similar. Pattern reusable when Sprint 4's tunnel providers need polling intervals.
- **Generic HMAC is a single model with optional type-specific fields, not a discriminated union.** Pydantic discriminated unions would force separate models per type and bloat the YAML schema. Instead `ValidatorConfig` carries `header`/`algorithm`/`prefix`/`tolerance_seconds` as optional fields and `build_validator()` validates required-per-type at instantiation. Errors point to the offending YAML entry by path.
- **Exact path match, not prefix or regex.** `validators.get(full_path)` — no fuzzy matching. Simpler, faster, no surprise routes. If a consumer wants pattern-based matching they can compose validators in front of `create_app` themselves.

### Tests
- **Count: 59 new** (8 `test_base.py`, 11 `test_generic.py`, 9 `test_github.py`, 15 `test_stripe.py`, 10 `test_registry.py`, 4 new route tests, 2 new storage tests). Cumulative: **107 tests** (Sprints 1+2+3). Target was 14-18 — came in well over because (a) `GenericHMAC` has many config knobs, (b) Stripe has timestamp/replay-window edge cases worth pinning, (c) the registry has its own error surface.
- **Pass: 107/107.** Run with `.venv/Scripts/python.exe -m pytest --cov=hooky`.
- **Coverage: 94% on `src/hooky/`** (up from 92%). All new modules (`validators/*`) at 97-100%. Untested code is still the `__main__.py` placeholder (Sprint 5) and the `load_config` YAML-file branch.
- **Sprint 3 success criteria coverage:**
  - ABC contract — `test_signature_validator_is_abstract` + `test_subclass_missing_validate_cannot_instantiate`.
  - HMAC primitives match RFC vectors — `test_hmac_sha256_matches_known_vector`, `test_hmac_sha1_matches_known_vector` (RFC 4231 / RFC 2202 test case 1).
  - `GenericHMACValidator` — 11 tests including known-good/bad, missing-header, prefix-strip, prefix-required-when-configured, case-insensitive lookup, SHA1 mode, body-specificity.
  - `GitHubValidator` — verified against GitHub's documented webhook-signing example (`secret="It's a Secret to Everybody"`, body `"Hello, World!"`, expected `757107ea0eb2509f...`). Plus SHA256-preferred-over-SHA1, SHA1 legacy fallback, wrong-secret rejection, unknown-prefix rejection.
  - `StripeValidator` — 15 tests covering known-good, replay-window expiry (past + future skew), multiple-v1 (key rotation), legacy `v0=` ignored, malformed `t=`, missing-header, case-insensitive.
  - Route integration — `test_valid_signature_returns_200_and_dispatches`, `test_invalid_signature_returns_401_stored_no_dispatch` (proves store-first + no dispatch on reject), `test_path_without_validator_passes_through_unchanged`, `test_missing_signature_header_returns_401`.
  - Storage update — `test_update_signature_status_sets_valid_true` / `_false`.

### Surprises / gotchas
- **`from typing import Callable` is deprecated for new code** — using `collections.abc.Callable` instead. Same lifted-from-Sprint-1 pattern (`collections.abc.Mapping` for headers).
- **Lowercase header normalisation in the route saves work.** Sprint 1's route already lowercases header keys at the ASGI boundary, so validators' `_get_header` has a fast path (`if target in headers`) for the common case. Defensive case-folding fallback is still there for direct callers (tests pass mixed-case to verify both paths).
- **GitHub fixture vector is itself the implementation check.** I hardcoded the SHA256 hex from GitHub's docs (`757107ea0eb2509f...`). If my HMAC math diverged from real HMAC, the test would fail. Cheaper than maintaining a separate "verify against openssl" test. SHA1 fallback test computes inline rather than hardcoding — only the SHA256 vector is publicly documented.
- **`time_source` kwarg-only.** Made `time_source` keyword-only on `StripeValidator` to avoid accidentally passing it as the third positional (which would shadow `tolerance_seconds`). Small ergonomics win.

### Lifted from old codebase (`webhook-reciever/`)
- **HMAC math (`_hmac_sha256`, `_hmac_sha1`, `_constant_time_compare`)**: lifted as module-level functions (`hmac_sha256`, etc.) in `validators/base.py`. Old code had them as static methods on the ABC — moved out because validator subclasses access them without inheriting state; standalone functions are simpler.
- **Stripe parsing logic**: lifted `_parse_signature_header` near-verbatim. The old code's parser was already correct; no reason to redesign.
- **GitHub SHA256-preferred-over-SHA1 strategy**: kept exactly. Falling back to SHA1 only when SHA256 absent is the right call for legacy compat.
- **Did NOT lift**: the old base.py's `name` abstract property + `extract_signature` method. The new ABC is one method (`validate`). Cleaner contract.
- **Did NOT lift**: the old `get_signature_header()` abstract method. Each validator knows its own header(s) and looks them up internally; no need to expose that to the route.
- **Did NOT lift**: the old `validate(body, signature, secret)` 3-arg signature. New signature is `validate(body, headers)` — secret lives on the validator instance (configured at construction), and header extraction happens inside the validator. The route just hands over body + headers — much cleaner integration.

### Spec amendments
- None to SPEC §5 / §6 / §7. The config shape in §6 was followed exactly (`validators:` list with `path/type/secret_env/header/algorithm/prefix/tolerance_seconds`). The `StorageBackend` ABC grew one method (`update_signature_status`) — implied by §7 Sprint 3's "stores with signature_valid=0/1" success criterion but not previously explicit in the ABC contract. Documented in `storage/base.py`.

### Open questions for Brad
- **`signature_validator` column value**: I store `type(v).__name__` (e.g. `"GitHubValidator"`). SPEC §6 says "validator class name" so that matches. If you'd rather store the validator's "type" string (`"github"`, `"stripe"`, `"generic_hmac"`) for nicer replay-UX, the registry has that string at build time and could thread it through. Flag for Sprint 5 if the CLI wants type filtering.
- **Rejected webhooks + retention**: Sprint 4+ will need to decide whether `signature_valid=False` rows count toward `retention_days` or get a separate retention policy (e.g. shorter — debug data, not real signal). Parking lot.
- **Validator timeouts**: None today. A pathological body could in theory slow down HMAC — but HMAC is constant time in body length and we're not allowing 100MB bodies. If we ever do, revisit.

### Confidence
**HIGH.** Sprint 3 success criteria all map to passing tests. The known-vector tests against published RFC + GitHub docs vectors provide external verification that the math is right, not just self-consistent. Store-first invariant is preserved and now has stronger tests (the invalid-signature path explicitly asserts the row is stored). Sprint 4 has a clean seam (tunnel providers don't touch validators).

### Next (Sprint 4)
- `TunnelProvider` ABC: `async start() -> public_url`, `async stop()`, `async health() -> TunnelHealth`.
- `NoTunnelProvider` (no-op), `NgrokProvider` (subprocess + API), `CloudflareProvider` (the new one — subprocess `cloudflared`).
- Tunnel lifecycle managed by FastAPI's `lifespan`.
- Config: extend `HookyConfig` with `tunnel: TunnelConfig`.

---

## Sprint 4 — Tunnel providers + Cloudflare (2026-05-28)

### Summary
Shipped the tunnel subsystem: `TunnelProvider` ABC with `start()` / `stop()` / `health()`, plus three concrete providers (`NoTunnelProvider`, `NgrokProvider`, `CloudflareProvider`). The app factory now manages the tunnel via FastAPI's `lifespan` — starts on app startup, stops on shutdown. `/healthz` includes a `tunnel` block (status / public_url / last_checked / error). Default config is `provider: none`, so existing tests and dev workflows are unaffected unless tunnel config is added. Cloudflare is the new arrival — the old codebase had a planned-but-empty slot for it; v1 ships it spawning `cloudflared` as an asyncio subprocess.

### Decisions
- **`async start()` / `async stop()` / `async health()` — all three async.** Even `NoTunnelProvider` (which does no I/O) keeps the async contract for ABC uniformity. The cost is one `await` ceremony per call; the benefit is that mixing sync and async providers behind one interface is a known footgun (`asyncio.run()` inside the framework was already banned by onboarding). The ABC enforces async at the type-system level.
- **`health()` returns cached state — never blocks on a network call.** This is critical because `/healthz` is called on every liveness probe. Providers update their cached state via background tasks (the cloudflared watcher) or on-demand inside `start()`/`stop()`. A naive design that polled "is the tunnel really up?" on every `/healthz` would have multiplied probe traffic and added a latency dependency on Cloudflare's infrastructure into our own health endpoint.
- **`start()` and `stop()` are idempotent.** Both providers cache state and short-circuit redundant calls. Matches FastAPI lifespan semantics where shutdown may be called after a failed startup, and matches the storage backend's contract (Sprint 1).
- **`NgrokProvider` lazy-imports pyngrok inside `_load_pyngrok()`.** pyngrok is an optional dep (`extras = ngrok`). The module imports fine without it; only `start()` triggers the import. Tests inject a fake module tuple via `pyngrok_module=` kwarg — no need to install pyngrok in CI.
- **`NgrokProvider` wraps sync pyngrok in `asyncio.to_thread`.** pyngrok's API is synchronous and does I/O. Calling it directly from an async function would block the event loop during tunnel setup (~1-2s in practice). Threading it keeps the event loop responsive. The thread pool overhead is a one-off cost at startup/shutdown.
- **`CloudflareProvider` watches subprocess output via a background `asyncio.Task`.** When `start()` returns, the watcher continues reading stdout — if cloudflared crashes later, the watcher catches the EOF + non-zero exit code and updates cached state to `ERROR`. That means `health()` reports reality without polling.
- **`CloudflareProvider` ignores the `port` argument.** Cloudflare Tunnels are pre-routed on Cloudflare's side via the token's configuration; cloudflared doesn't take a `--port` argument in the `tunnel run --token` mode. Accepted in the signature for ABC compatibility and documented in the docstring. The hostname is taken from config, not discovered from cloudflared output — cloudflared never reports it.
- **Two readiness markers accepted: `"Connection registered"` and `"Registered tunnel connection"`.** The exact wording has drifted across cloudflared versions; accepting both gives us a wider compatibility window without locking us to a specific binary release.
- **`subprocess_factory` injection kwarg, not module-level monkey-patch.** Tests pass a custom async factory that returns a hand-rolled `_FakeProcess`. Cleaner than `unittest.mock.patch("asyncio.create_subprocess_exec")` and survives test parallelization. The factory signature is `async (argv) -> Process`, so the real factory and the fake have the same shape.
- **App factory owns lifecycle when it builds the tunnel; not when caller injects.** Mirrors `owns_storage` from Sprint 1. If you inject a tunnel, you manage its lifecycle (start before, stop after). Tests that pre-`start()` the injected tunnel pin this branch.
- **Tunnel start failure refuses the whole app boot.** A webhook receiver with no tunnel in production is almost always a misconfiguration. Better to fail loud at startup than serve quietly on `localhost:8000` with no way for GitHub to reach us. Storage is closed in the failure path so we don't leak the SQLite connection.
- **SIGTERM with 5s grace, then SIGKILL.** Standard Unix shutdown pattern. On Windows, `proc.terminate()` maps to `TerminateProcess` which is immediate — the grace period is a no-op there but harmless. `proc.kill()` is the same as `terminate()` on Windows, but we keep both for symmetry with POSIX.
- **`tunnel_name` vs `hostname` in CloudflareConfig.** SPEC §6 said `tunnel_name`. Implementing it, that name is misleading — it's actually the public hostname, not just an identifier on Cloudflare's side. Added a `hostname` field as the preferred name, kept `tunnel_name` as a deprecated alias for spec-compat. The registry checks both.

### Tests
- **Count: 47 new** (4 `test_base.py`, 7 `test_none.py`, 14 `test_ngrok.py`, 14 `test_cloudflare.py`, 6 `test_registry.py`, 2 `test_healthz_integration.py`). Cumulative: **154 tests** (Sprints 1+2+3+4). Spec target was 10-12 — came in well over because (a) Cloudflare's subprocess + watcher state machine has many edge cases, (b) NgrokProvider's mock-pyngrok seam expanded what's verifiable in unit tests.
- **Pass: 154/154.**
- **Coverage: 92% overall.** New modules: `tunnel/base.py` 100%, `none_provider.py` 100%, `ngrok.py` 90%, `cloudflare.py` 80%, `registry.py` 94%. The 20% gap on `cloudflare.py` is hard-to-test ops paths (`shutil.which` PATH miss, SIGKILL escalation, watcher exception handler). Acceptable given the subprocess complexity — those branches will be exercised by the manual smoke test instead.
- **Sprint 4 success criteria coverage:**
  - ABC contract — `test_tunnel_provider_is_abstract` + `test_subclass_missing_methods_cannot_instantiate`.
  - `NoTunnelProvider.start()` returns `http://localhost:{port}` — `test_start_returns_localhost_url`.
  - `NgrokProvider` with mocked pyngrok — 14 tests covering happy path, HTTP→HTTPS rewrite, idempotency, PyngrokError wrapping, unexpected-exception wrapping, health states, stop() idempotency, disconnect-error swallowing, subdomain + region plumbing.
  - `CloudflareProvider` with mocked subprocess — 14 tests covering both readiness markers, timeout-no-marker, exit-before-ready, start() idempotency, stop() terminates, stop() idempotent, health states.
  - `/healthz` integration — `test_healthz_includes_tunnel_block_when_tunnel_started` (lifespan-built tunnel) + `test_healthz_with_injected_started_tunnel` (caller-managed tunnel).
  - Config-driven instantiation — 7 registry tests including missing-env-var fail-loud paths.

### Surprises / gotchas
- **`module_name = "none"`**: I almost named the file `none.py`. Python would have happily imported it, but `from hooky.tunnel.none import NoTunnelProvider` would have been ugly to read (and the type-checker briefly thought `none` was the keyword in unrelated code). Renamed to `none_provider.py` early.
- **The original `/healthz` test (`test_healthz_when_empty_returns_null_last` from Sprint 1) still passes** — adding the `tunnel` block to the response is additive, not breaking. The Sprint 1 test only asserted on `status` and `last_webhook_at`. (I considered tightening it to assert no extra keys; decided against — extension-friendly health endpoints are healthier than strict-shape ones.)
- **`shared client fixture in conftest.py` got a tunnel for free in Sprint 4.** The fixture builds an app via `create_app(storage=storage)` — no `tunnel=` kwarg — so the factory builds a `NoTunnelProvider` by default. Every Sprint 1/2/3 test that uses `client` is now silently exercising the tunnel lifecycle too. No tests broke; the new behavior is a strict superset. Pinned via the new healthz test rather than reaching into the shared fixture.
- **The `simulate_exit` helper on `_FakeProcess`** initially didn't set `returncode` *and* close stdout, leading to a hung watcher task in one test. Fixed by closing both. Mocking long-running subprocess state machines requires care — leaving any "still alive" channel open will deadlock pytest.
- **Initial Cloudflare integration test injected a tunnel via `tunnel=` kwarg and expected lifespan to start it.** That's the wrong mental model — `owns_tunnel=False` means the caller is responsible, same as storage. Rewrote the test to let the factory build the tunnel; added a second test that pre-starts an injected tunnel to cover the other branch.

### Lifted from old codebase (`webhook-reciever/`)
- **`NgrokTunnel.disconnect()` pattern**: lifted the disconnect-then-kill ordering + the try/except around each. Old code used `PyngrokError`; new code does the same (via the lazy-imported tuple) and additionally logs each failure rather than silently `pass`-ing.
- **Did NOT lift**: the old `connect(port) -> str` / `disconnect()` / `get_url()` / `is_connected()` 4-method ABC. New ABC is 3 methods (`start` / `stop` / `health`) and the URL is exposed via `health().public_url` rather than a separate getter. Cleaner contract; the health object carries everything the route needs.
- **Did NOT lift**: the old `__enter__` / `__exit__` context manager protocol. The new providers are managed by FastAPI's `lifespan` async context manager directly; nested context managers add no value.
- **Did NOT lift**: Cloudflare implementation — there wasn't one to lift. The old codebase had a planned slot but no code. v1 ships it fresh.

### Spec amendments
- **`tunnel_name` → `hostname`**: SPEC §6 used `tunnel_name`; implementation renamed to `hostname` (with `tunnel_name` kept as a deprecated alias). The spec wording was misleading — "tunnel name" sounds like a Cloudflare-side identifier, but we actually need the public hostname. Spec hasn't been edited; the alias preserves backwards compat. Flag for SPEC.md update if Brad wants.
- **Default tunnel provider is `none`**: SPEC §6 showed `provider: cloudflare` as the example. Implementation defaults to `none` because (a) most local dev doesn't need a tunnel, (b) factory should never need env vars in the default-config path. Operators opt in to a real tunnel via YAML.
- **`/healthz` shape**: Sprint 1 SPEC §6 didn't specify a `tunnel` sub-block in the response. Added it as part of Sprint 4 success criterion "✓ `/healthz` includes tunnel health." Pinned by tests.

### Open questions for Brad
- **Manual smoke test deferred.** Spec says "Manual smoke: with a real Cloudflare tunnel token (Brad's), Sprint 4 ships a working tunnel that survives `docker compose restart`." I haven't run this — needs your token and a configured tunnel on the Cloudflare side. Suggest pairing on it before Sprint 5 starts. If smoke surfaces issues with the readiness-marker detection or subprocess teardown, I'll iterate.
- **pyngrok install in CI**: Currently optional, mocked in tests. If you want CI to verify against the *real* pyngrok library too (since the mock could drift from the real API surface), we'd add it to dev-deps. Parking-lot — current confidence is fine.
- **Cloudflare hostname discovery**: I take hostname from config because cloudflared doesn't print it. There's a `cloudflared tunnel info <name>` subcommand that *would* print it, at the cost of a second subprocess invocation. Not worth the complexity for v1 — config is the source of truth. Revisit if a consumer needs to dynamically discover the URL.

### Confidence
**MODERATE.** The unit tests are tight and the mocked-subprocess + mocked-pyngrok seams give good coverage of the state machine. But the cloudflared subprocess path has had zero real-world exercise — the readiness-marker strings could be wrong for your specific cloudflared version, and the SIGTERM/SIGKILL handling on Windows vs Linux differs in subtle ways I haven't observed. The manual smoke test (deferred above) is the right tiebreaker. Promote to HIGH after that runs clean.

### Next (Sprint 5)
- `hooky` CLI via Click: `start`, `list`, `show`, `replay`, `stop`.
- Replay engine — reuses `HandlerManager.dispatch` per the no-divergent-code-paths rule.
- README with quickstart.
- Coverage gate at 80% (we're at 92%, comfortable margin).

---

## Sprint 5 — CLI + replay + README (2026-05-28)

### Summary
Shipped v1's operator surface: the `hooky` CLI (`start` / `stop` / `list` / `show` / `replay`), the replay engine, config-driven handler loading, and the README. Replay reuses `HandlerManager.dispatch` verbatim — no divergent code path. The keystone decision: a `handlers:` config section with **dotted import paths**, so a consumer becomes a *config-only* consumer — `hooky start --config myapp.yaml` with zero launcher boilerplate. Full SPEC integration smoke (store → list → show → replay → subset-replay) passed through the real installed console script. 187 tests, 92% coverage (gate was 80%).

### Decisions
- **Config-driven handler loading via dotted paths (`"pkg.mod:Class"`).** This is the decision that makes a config-only consumer real. SPEC §line-42 blessed "registered programmatically OR via config" but never defined the config shape. The old codebase's CLI couldn't actually replay to handlers — it built an empty manager and printed "handlers must be registered in application code." I closed that gap with a `handlers:` list of dotted import paths + optional `config:` kwargs, resolved by `hooky/handlers/registry.py`. Both `create_app` and the CLI build the `HandlerManager` from it. Standard idiom (uvicorn/celery/pydantic all do dotted-path loading). **MODERATE confidence** — it adds a new public contract surface (the `handlers:` config schema), flagged here for Brad to veto.
- **Handler constructor kwargs via `config:` dict.** `HandlerClass(**cfg.config)`. Covers both zero-arg handlers (`EchoHandler`) and parametrised ones (e.g. an SMS handler that needs a from-number). Construction failures surface as `HandlerConfigError` with the offending path + kwargs in the message.
- **Replay reuses dispatch; subsetting builds a temporary manager.** Per the no-divergent-paths rule, `replay_webhook` calls `manager.dispatch(webhook)` — identical to live. For `--handler foo` subsetting, it constructs a *temporary* `HandlerManager` containing only the named handlers (sharing the same storage) and dispatches through that. The dispatch logic itself is never forked.
- **Replay records new `handler_runs` rows; never mutates the original.** The old engine merged replayed results back onto the webhook row and tagged them `replayed=True`. Rejected — that conflates original and replayed outcomes and fights the `handler_runs`-table model from Sprint 1. New replays are just new rows, naturally ordered by `started_at`. `show` displays all of them. (Verified in smoke: `show` went from `(none)` to two runs after a replay.)
- **CLI uses `asyncio.run` — and that's fine.** Onboarding bans `asyncio.run` *inside the framework*. The CLI is an application entry point, the boundary where we cross from async framework code into synchronous Click. Documented inline so a future maintainer doesn't "fix" it.
- **Two storage ABC additions: `list(..., path=...)` and `get_handler_runs(id)`.** `list` gained an optional `path` filter (additive, defaulted — backwards-compatible) for `hooky list --path`. `get_handler_runs` is a new abstractmethod (breaks any third-party `StorageBackend` impl, but the only impls are `AsyncSQLiteStorage` + the test stub, both updated). `show` needs it to display handler outcomes.
- **`--db` overrides config storage path.** Lets `list`/`show`/`replay` point at any DB without a config file. `--config` + `--db` compose (db wins). Makes the CLI usable ad-hoc against a prod DB copy.
- **`start`/`stop` via pidfile + `os.kill(pid, SIGTERM)`.** On Windows `os.kill` with SIGTERM maps to `TerminateProcess` — graceful-ish, cross-platform-enough. `stop` handles missing pidfile (exit 1), stale pidfile (clean up, exit 0), and unreadable pid (exit 1). This is the flakiest surface (signals + blocking server); tested with `os.kill` + `uvicorn.run` mocked rather than launching real servers.
- **`start` replaces the Sprint-4 `smoke_launcher.py` hack.** That throwaway factory closure is gone; `hooky start --config` is the real thing now.
- **Version bumped to 0.5.0** across `pyproject.toml` + `__init__.py` (they had drifted to 0.1.0 / 0.2.0). `--version` reads package metadata, now consistent.

### Tests
- **Count: 33 new** (7 `test_replay/test_engine.py`, 11 `test_handlers/test_registry.py`, 10 `test_cli/test_cli.py`, 5 `test_cli/test_lifecycle.py`). Cumulative: **187 tests** (Sprints 1–5). Spec target was 10-14 — over again, mostly because the handler-registry error surface + CLI command matrix are cheap to pin and worth pinning.
- **Pass: 187/187.**
- **Coverage: 92% on `src/hooky/`** — gate was 80%. New modules: `replay/engine.py` 100%, `handlers/registry.py` 100%, `cli/main.py` ~85% (the `start` foreground-serve loop body and a couple of error branches are the gap), `storage/sqlite.py` 100%.
- **Sprint 5 success criteria coverage:**
  - `hooky start --config` — `test_start_writes_pidfile_and_runs_uvicorn` (uvicorn + create_app mocked).
  - `hooky list [--limit] [--path]` — `test_list_shows_webhooks`, `test_list_path_filter`, `test_list_limit`, `test_list_empty_db`.
  - `hooky show <id>` — `test_show_detail` (headers/body/handler-runs), `test_show_unknown_id_exits_1`.
  - `hooky replay <id> [--handler]` — `test_replay_with_configured_handler`, `test_replay_unknown_handler_name`, `test_replay_without_handlers_exits_2`, plus 7 engine-level tests.
  - `hooky stop` — `test_stop_sends_signal`, `test_stop_stale_pidfile_cleaned`, `test_stop_no_pidfile_exits_1`, `test_stop_unreadable_pid_exits_1`.
  - Replay reuses live dispatch — `test_replay_isolates_failing_handler` (same isolation semantics), `test_replay_records_handler_runs`, `test_replay_twice_appends_runs`.
  - Coverage gate ≥80% — 92%.
  - Integration smoke — see below.

### Manual integration smoke (SPEC §7 Sprint 5 criterion)
Ran the full loop through the **real installed `hooky` console script** (not CliRunner):
1. Seeded a `/webhook/github` delivery into a file-backed DB.
2. `hooky list --db` — showed the row.
3. `hooky show <id>` — full detail, `Handler runs: (none)`.
4. `hooky replay <id> --config` (echo + logger handlers) — `[OK] echo`, `[OK] logger`.
5. `hooky show <id>` again — now lists both handler runs (replay-records-rows confirmed).
6. `hooky replay <id> --handler echo` — dispatched to echo only (subsetting confirmed).
All passed. Also confirmed `hooky --version` and `python -m hooky --version` both work via the entry point.

### Surprises / gotchas
- **The smoke test caught a *test-harness* path bug, not a code bug.** First integration-smoke attempt mixed MSYS bash paths (`/tmp/...`) with native Windows Python, which resolves `/tmp` relative to the current drive (`P:\tmp`). `list`/`show` (using `--db`) and `replay` (reading `storage.path` from a bash-written YAML) ended up pointing at different files → "Webhook not found." Re-ran with project-relative paths and it passed clean. Worth remembering for any future cross-tool smoke on Windows: **don't mix `/tmp` between bash and native Python.** Logged here so the next person doesn't burn 10 minutes on it.
- **`click.version_option(package_name=...)` reads installed metadata, not `__version__`.** That's why the version drift (pyproject 0.1.0 vs `__init__` 0.2.0) showed as `0.1.0` in `--version` — it was reading the *installed* package version. Bumped both to 0.5.0.
- **CliRunner blocks until background tasks finish, but the CLI doesn't use BackgroundTasks** — replay dispatch is awaited directly in the command, so CLI tests are fully deterministic with no sleeps. (Different from the route tests, which lean on TestClient's background-task draining.)

### Lifted from old codebase (`webhook-reciever/`)
- **CLI command shape** (`start`/`list`/`show`/`replay`/`stop` as a Click group, `--config`/`--db` options, `--handler` multiple). Kept the surface; rewrote the bodies for async + the new storage/replay APIs.
- **Pidfile-based `start`/`stop`.** Lifted the concept (write pid on start, signal on stop). Old code's `DEFAULT_PID_FILE = "data/webhook-receiver.pid"` → ours is `data/hooky.pid`.
- **Did NOT lift**: the old `ReplayEngine` class with `replay_multiple` / `replay_by_path` / result-merging-onto-webhook. New replay is a single pure async function reusing dispatch. The multi/by-path variants were unused complexity; if a real need appears they're a clean addition.
- **Did NOT lift**: the old engine's `execute_all` / `execute_single` manager methods (the divergent replay path). New engine calls `dispatch` — the whole point.

### Spec amendments
- **New `handlers:` config section** (dotted import paths). SPEC §6's YAML didn't include it; §line-42 implied it ("registered ... via config"). This is the most significant new public surface in Sprint 5 — flag for SPEC.md update.
- **`StorageBackend` ABC grew `get_handler_runs` + `list(path=...)`.** Implied by `hooky show` (handler runs) and `hooky list --path`. Documented in `storage/base.py`.
- **Carried over from Sprint 4, still pending SPEC.md edits**: `tunnel_name`→`hostname` rename, default tunnel provider `none`, `/healthz` tunnel block.

### Open questions for Brad
- **`handlers:` config contract** is the big one. Do you want handler kwargs as a flat `config:` dict (current), or something richer (e.g. env-var indirection like validators' `secret_env`)? For the consumer's SMS handler, will it read its API token from env in `__init__` (simplest), or do you want the framework to inject it? This shapes how the consumer's handler is written.
- **SPEC.md is now behind the code** in several places (above). Want me to do a consolidated SPEC.md update pass to reconcile it with what shipped, so it's accurate for the next agent?
- **Cloudflare smoke still outstanding** — the only success criterion not yet exercised against real infrastructure. Unblocked once you buy the domain (~$10.44).

### Confidence
**HIGH for everything except the parts that were already MODERATE.** The CLI, replay engine, and config-driven handler loading are all covered by tests + the end-to-end console-script smoke. The two standing MODERATE items are unchanged: (1) the Cloudflare subprocess path still hasn't run against real infra (Sprint 4), and (2) the `handlers:` config contract is a new public surface I chose under MODERATE confidence — it works and is tested, but it's the kind of API decision you might want to shape before the consumer hard-codes against it.

### v1 status
Sprints 1–5 complete. 187 tests, 92% coverage. The framework does what SPEC §1 promised: receive → store-first → validate → dispatch (isolated, async) → replay, with pluggable handlers/validators/tunnels and a CLI. Ready for the first consumer to consume — pending your call on the `handlers:` config contract + the Cloudflare domain for a real public URL.

---

## Post-v1 — Cloudflare smoke de-risking (2026-05-28)

### Summary
While waiting on a domain to run the one outstanding success criterion (Sprint 4 Cloudflare manual smoke against real infra), pre-tested the subprocess **failure** paths that the smoke run will exercise and wrote a push-button smoke runbook. `cloudflare.py` coverage went 80% → 93%; suite 187 → 193 tests, overall 92% → 94%. No behavioral change to the happy path — only new failure-path tests + two test seams + a runbook.

### Decisions
- **Added two keyword-only timing seams to `CloudflareProvider.__init__`: `terminate_grace` (default 5.0) and `watcher_join_timeout` (default 2.0).** Both were hardcoded; the SIGTERM→SIGKILL escalation path and the watcher-cancel path could not be tested without literally waiting 5s/2s. Exposing them as injectable kwargs (defaults unchanged) matches the existing seam philosophy (`subprocess_factory`, Stripe's `time_source`, ngrok's `pyngrok_module`). Purely additive + keyword-only → backwards-compatible with the public ABC contract.
- **Covered the real-infra failure modes, not just line count:** binary-not-on-PATH (actionable `TunnelError`), unexpected spawn error wrapped + recorded in `health()`, watcher survives a stream exception and still reaps the process, post-start crash flips `/healthz` to ERROR and clears `public_url`, SIGTERM-ignored → SIGKILL escalation, already-reaped process (`ProcessLookupError` on terminate) + stuck-watcher cancel. These are exactly the paths the original author deferred to "manual smoke" — pre-testing them means the smoke starts from a known-good baseline and a smoke failure points at infra/config, not our state machine.
- **Did NOT chase the last 10 lines** of `cloudflare.py`. Line 125 is the real `asyncio.create_subprocess_exec` — that *is* the smoke test (a live binary is the only thing that hits it). The rest are defensive `except` arms for a process that ignores even SIGKILL. Contorting fakes to hit them would test the mocks, not the code.
- **Smoke runbook lives in `docs/cloudflare-smoke.md`** — prerequisites, a copy-paste smoke config, exact commands (PowerShell + bash), pass-criteria checklist, and a symptom→code-path troubleshooting table tied to the hardened error messages. Makes the domain-arrival smoke push-button.

### Tests
- **6 new** in `tests/test_tunnel/test_cloudflare.py` (24 total there). Cumulative **193 tests, all green.** `cloudflare.py` 93%; overall `src/hooky/` 94%.

### Surprises / gotchas
- **The committed tree fails `ruff check` (33 errors) and `mypy --strict` (8 errors).** Only the coverage gate (≥80%) was actually enforced through v1; the lint/type gates in SPEC §4 were run ad hoc and drifted. My change introduced exactly one new ruff error (an E501 in a test) which I fixed — I did **not** fold the project-wide pre-existing debt into this hardening diff (that conflates two concerns). Flagged to Brad as a separate cleanup pass. The pre-existing mypy errors are real (e.g. `sqlite.py:198` shadows `list`, `routes.py:61` bare `dict`, several `no-any-return`).

### Confidence
**HIGH** on the hardening itself (every new test maps to a real failure mode, all green, lint-clean additions). The Sprint 4 Cloudflare item stays **MODERATE** until the real-infra smoke runs — that's unchanged and gated on the domain, not on code.

---

## Post-v1 — Handler config contract locked (2026-05-28)

### Summary
Resolved the one standing MODERATE design item from Sprint 5: how secrets reach a config-loaded handler. Added a `secret_env:` field to `HandlerConfig` — a `kwarg_name -> ENV_VAR_NAME` map the framework resolves from the environment at build time and passes as constructor kwargs. This mirrors the validator `secret_env` contract already shipped. The `handlers:` contract is now **locked** for the consumer to build against. 200 tests (+7), registry at 100% coverage.

### The decision (and why)
Brad delegated the technical call after an explainer. Three options were on the table:
1. **`secret_env` injection (chosen)** — framework reads env → passes as kwargs.
2. **Handler reads `os.environ` itself** — freeze the contract as-is.
3. **`${VAR}` interpolation** in any `config:` value.

Chose (1) because:
- **Consistency** — identical to the validator `secret_env` contract. One secrets story across the whole framework, not two.
- **Sets the authoring pattern** — the consumer's SMS handler is the first real handler; every future handler copies its shape. (1) produces handlers that take dependencies as constructor args (testable: `SmsHandler(from_number=..., api_token="fake")`, no env stub) rather than reaching into `os.environ` (ambient, needs monkeypatch).
- **Sharp point that decided it:** under the as-shipped schema, since secrets can't live in YAML, the *only* secret path was option (2)'s ambient env-read — i.e. freezing as-is would have *forced* the less-testable pattern on the consumer. (1) was the cheap fix-it-now-before-it's-load-bearing call.
- (1) is forward-compatible — could have shipped (2) and added (1) later non-breakingly — but doing it now avoids a consumer refactor.

Option (3) rejected: blurs secret vs non-secret values, bigger parse surface, fail-loud edge cases (missing var, literal `$`).

### Decisions (implementation)
- **`secret_env: dict[str, str]` on `HandlerConfig`**, default empty (`extra="forbid"` retained). Resolved in `handlers/registry._resolve_secret_kwargs(cfg, env=...)`; `env` is an injectable seam (defaults to `os.environ`), threaded through `build_handler` and `build_handler_manager` — consistent with the `time_source`/`subprocess_factory` seam philosophy.
- **Fail loud, fail early.** An unset env var or a kwarg present in *both* `config` and `secret_env` (ambiguous) raises `HandlerConfigError` at build time (app startup / CLI), naming the offending kwarg + env var. Not a mid-dispatch surprise.
- **Secret values redacted from error messages.** `build_handler`'s construction-failure error previously echoed `cfg.config!r`; since kwargs may now carry resolved secrets, it no longer prints kwargs. Pinned by `test_build_handler_construction_error_does_not_leak_secret_value` (asserts the secret string is absent from the raised error).
- **No change to `create_app`/CLI call sites** — they call `build_handler_manager` without `env`, defaulting to `os.environ`. Existing behavior preserved; all prior tests green.

### Tests
- **7 new** in `tests/test_handlers/test_registry.py` (covering: env→kwarg injection + merge with config, empty secret_env is a config copy, missing env fails loud, config/secret_env collision errors, build-time fail-before-construct, the secret-leak guard, and env-threading through the manager). `handlers/registry.py` 100%. Cumulative **200 tests, all green.**
- Tested `_resolve_secret_kwargs` directly (where the new logic lives) rather than fighting test-module import paths to get a kwarg-accepting handler — the value-lands-correctly proof is the pure-function assertion; `build_handler` just forwards `**kwargs` (already exercised).

### Docs updated
- `config/config.example.yaml` — worked consumer example with `secret_env`.
- `README.md` — config block, the secrets paragraph, and a "Secrets and config" subsection in the handler-authoring guide (take deps as constructor args; don't read env inside the handler).
- `SPEC.md` — §6 handlers YAML + amendment table row #1 (contract marked locked).

### Confidence
**HIGH.** The contract is small, internally consistent with the validator precedent, fully covered, and the security-redaction edge is pinned. This was the last design-judgment blocker for the consumer; its early sprints can now proceed as "write a `WebhookHandler` subclass taking `from_number` + `api_token` kwargs."

---

## Post-v1 — Lint + type debt cleared (2026-05-28)

### Summary
The SPEC §4 quality gates (`ruff check`, `mypy --strict`) had drifted — only the coverage gate was enforced through v1, so the tree carried 32 ruff + 8 mypy-strict errors. Cleared all of them in a dedicated pass (kept isolated from the feature commits). Now green on all three gates: **ruff clean, mypy --strict clean (34 files), 200 tests at 94%.** No behavioral change intended; full suite green throughout.

### Decisions / non-trivial fixes
- **44 ruff issues auto-fixed** (`ruff --fix`, safe fixes only): `timezone.utc`→`datetime.UTC`, `asyncio.TimeoutError`→`TimeoutError`, unused imports, `.encode("utf-8")`→`.encode()`, `typing.AsyncIterator`→`collections.abc`. Suite stayed green after.
- **`TunnelStatus(str, Enum)` → `StrEnum`** (UP042). Verified safe before applying: `/healthz` serializes `health.status.value` (routes.py:259), and `.value` is identical under `StrEnum`; the only other `{status}` f-strings are an unrelated CLI handler-run label. Behavior unchanged, tests green.
- **`zip(self._handlers, raw)` → `zip(..., strict=True)`** (B905). `raw` is `gather` over one coro per handler, so it's 1:1 by construction — `strict=True` asserts that invariant rather than silently truncating if it ever isn't.
- **`list`-shadowing (mypy `valid-type`)**: `StorageBackend.list` / `AsyncSQLiteStorage.list` shadow the `list` builtin in class annotation scope (under `from __future__ import annotations`), so `get_handler_runs(...) -> list[HandlerResult]` failed to resolve. Fixed with `builtins.list[HandlerResult]` + an explanatory comment — preserves the exact ABC return contract (vs. renaming the public `list` method or widening to `Sequence`).
- **`no-any-return` ×4** (`log.get_logger`, `ngrok` url, `cloudflare._spawn` factory branch, `cli._fmt_dt`): structlog / pyngrok / the injected factory return `Any`. Fixed with a localized `cast` (or a typed local) at each boundary — no `# type: ignore`, so `warn_unused_ignores` stays meaningful.
- **`routes._parse_body_json -> dict | None`** → `dict[str, object] | None` (type-arg), matching the file's existing `dict[str, object]` idiom.
- **`types-PyYAML`** added to `[dev]` extras + installed — resolves the `config/loader.py` untyped-import error properly rather than ignoring it.

### Confidence
**HIGH.** Purely mechanical/structural; every change verified against the full suite (200 green) plus both static gates. The one judgment call with behavioral surface (`StrEnum`) was checked against the actual serialization site before applying. A future agent can now trust `ruff check src tests && mypy src` as a real gate.

---

## Post-v1 — Pre-commit gate (2026-05-28)

### Summary
Root-cause fix for the debt cleared above: the gates drifted because nothing enforced them. Added a versioned pre-commit hook (`.githooks/pre-commit`) that runs ruff + mypy --strict + pytest and blocks the commit on any failure.

### Decisions
- **`core.hooksPath = .githooks` over `.git/hooks/` or the `pre-commit` framework.** `.githooks/` is committed (so the hook travels with the repo + future agents get it), needs no extra dependency, and runs the project `.venv` interpreter directly — which `mypy --strict` requires (an isolated pre-commit-framework tool env wouldn't see fastapi/pydantic and would mis-report). Trade-off: each clone runs `git config core.hooksPath .githooks` once (documented in README). `core.hooksPath` is local config, not stored in-repo — that's a git constraint, not a choice.
- **Interpreter autodetect** in the hook (`.venv/Scripts/python.exe` → `.venv/bin/python` → `python`) so it works on Windows or POSIX clones.
- **All three gates in pre-commit, not pytest-at-pre-push.** The suite runs in ~1.4s; no reason to split. `set -e` fails fast on the first failing gate.
- **Verified it blocks**, not just runs: staged a file with an F401, attempted a real commit, confirmed ruff rejected it and no commit object was created.

### Confidence
**HIGH.** Tested both the pass path (clean tree → commit proceeds) and the fail path (lint error → commit blocked, HEAD unchanged).

---

## Post-v1 — Quo (OpenPhone) signature validator (2026-05-29)

### Summary
Added a `quo` signature validator — the first validator added on behalf of a
consumer (whose inbound SMS comes via Quo/OpenPhone webhooks).
Quo's `generic_hmac` can't be verified by the generic validator: its
`openphone-signature` header is structured (`<scheme>;<version>;<timestamp>;<base64sig>`),
the signed payload is `<timestamp>.<rawbody>`, the digest is base64 (not hex),
and the signing key is base64-encoded. New `ValidatorType` member `"quo"`.
**215 tests (+15), ruff + mypy --strict clean.**

### Decisions
- **Modeled on `StripeValidator`** (the existing timestamp-prefixed validator) —
  same `<ts>.<body>` signing shape and `time_source` seam. Divergences from
  Stripe: semicolon-delimited header parsed **positionally** (timestamp = field
  2, signature = field 3); base64 digest, not hex; the **key is base64-decoded**
  before HMAC (validated at construction — a non-base64 secret raises).
- **Scheme/version tokens (fields 0/1) are NOT asserted** against literals —
  Quo's docs don't guarantee them as constants, so the validator relies only on
  the timestamp + signature positions. (Test pins this: a `xyz;9;...` header
  still validates.)
- **Replay/freshness check is OPTIONAL and off by default** (`tolerance_seconds`
  defaults to `None` for quo, vs Stripe's 300s). The timestamp's unit (s vs ms)
  isn't yet confirmed against a live Quo webhook, so enforcing a tolerance on a
  units guess could reject valid deliveries; the HMAC alone still gives
  integrity + authenticity. Enable once confirmed live.
- **Registry/schema wiring** mirrors the other types: `secret_env` resolution,
  `build_validator` branch, `__init__` export. `tolerance_seconds` doc updated
  to cover both stripe + quo semantics.

### Tests
- `tests/test_validators/test_quo.py` (15): valid pass; missing/short/malformed
  header; wrong sig; tampered body; wrong secret; scheme/version-agnostic;
  empty + non-base64 secret raise; negative tolerance raises; no-tolerance
  accepts old ts; tolerance rejects stale / accepts fresh / rejects non-int ts;
  case-insensitive header.

### Confidence
**HIGH** on the implementation (covered, gates green). **MODERATE** on the wire
format details (header token literals, timestamp unit, that the signing secret
is base64) until a real Quo webhook is observed — flagged in the consumer's
reconciliation notes as live-smoke items. The validator is written to be
robust to those unknowns (positional parsing, optional freshness).

---

## Post-v1 — `hooky stop` orphaned cloudflared on Windows (2026-05-30)

### Summary
A live Cloudflare tunnel smoke (the first consumer) found that `hooky stop` left an orphaned
`cloudflared` child process on Windows — reproduced 2/2, still running >24s
later, had to `Stop-Process -Force` it by hand. The main server exited and port
8000 freed, but the tunnel subprocess leaked. Root-caused, fixed, verified
against a real process tree, and covered by a regression test.
**218 tests (+3 net), ruff + mypy --strict clean.**

### Root cause (was MODERATE in the report; now confirmed HIGH)
`hooky stop` terminates the server out-of-band via the pidfile with
`os.kill(pid, signal.SIGTERM)`. On Windows there are no POSIX signals — Python
maps `SIGTERM` to `TerminateProcess`, a **hard kill** of the uvicorn process. A
hard kill does not run uvicorn's graceful shutdown, so the FastAPI `lifespan`
`finally` block (`app.py`) never executes — and that block is the *only* thing
that calls `tunnel_provider.stop()`, which is what reaps `cloudflared`
(SIGTERM→grace→SIGKILL). The provider teardown logic itself was correct and
unit-tested; it simply never got a chance to run on this path. So `cloudflared`
— a direct child of the uvicorn process — was orphaned.

The mock-based provider tests (`test_cloudflare.py`) all passed because they
exercise `stop()` directly. The gap was exactly the cross-process
`hooky start` → `hooky stop` teardown, which no test covered.

Secondary bug found along the way: on the hard-kill path, `start()`'s `finally`
that unlinks the pidfile also never runs → stale pidfile left behind after every
Windows `hooky stop`.

### Fix
Platform-split the termination in `cli/main.py` behind a new `_terminate_server(pid)`
helper, branching on a module-level `_IS_WINDOWS` flag:
- **POSIX (unchanged):** `os.kill(pid, SIGTERM)` → uvicorn graceful shutdown →
  lifespan teardown → `tunnel.stop()` reaps cloudflared. This is also the
  production path (`docker compose down` sends SIGTERM to uvicorn as PID 1), so
  Docker/Linux behavior is deliberately untouched — **no regression** to the
  shipping deployment.
- **Windows:** kill the whole process **tree** with `taskkill /PID <pid> /T /F`
  so the cloudflared child is reaped alongside its parent. `taskkill` exit 128
  ("process not found") maps onto the existing stale-pidfile cleanup path
  (mirrors POSIX `ProcessLookupError`); any other non-zero exit surfaces as a
  failure (exit 1, pidfile preserved — server may still be running). After a
  successful tree-kill, `stop()` removes the pidfile itself, since `start`'s
  `finally` can't run after a hard kill (fixes the secondary stale-pidfile bug).

### Decisions
- **`taskkill /T` tree-kill over a graceful Windows shutdown.** Triggering
  uvicorn's graceful shutdown on Windows would need CTRL_C/CTRL_BREAK console
  events sent to a process group — notoriously finicky for a detached server
  (requires `CREATE_NEW_PROCESS_GROUP`, console attachment, and the target then
  *ignores* Ctrl-C by default). The tree-kill is the robust, industry-standard
  Windows answer. Trade-off: no graceful async cleanup on Windows (SQLite isn't
  closed cleanly) — acceptable, because the main process was *already* being
  hard-killed before this fix; we're only extending that same hard kill to reap
  the child. SQLite tolerates this (WAL/rollback journal).
- **Branch on a patchable `_IS_WINDOWS` module flag, not a direct `os.name`
  read.** Tests need to drive both branches deterministically regardless of host
  OS. Patching the real `os.name` would also flip `pathlib`'s flavour mid-test
  (PosixPath can't be instantiated on Windows), breaking path handling. A
  dedicated module symbol is patched in isolation.
- **Job Object (kill-on-close) considered and deferred.** A Windows Job Object
  with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` assigned at spawn time would reap
  the child even if the parent is killed via Task Manager — more robust, but
  heavier (ctypes/win32 plumbing into `asyncio.create_subprocess_exec`) and hard
  to unit-test in CI. The `hooky stop` tree-kill directly fixes the reported
  repro; the job object is a possible future hardening, noted here.

### Tests
- `tests/test_cli/test_lifecycle.py`: replaced the two OS-agnostic stop tests
  with five platform-explicit ones. New regression test
  `test_stop_windows_kills_process_tree` asserts the Windows branch invokes
  `taskkill` with `/T` (tree kill) and *not* the single-process `os.kill` — i.e.
  it would have caught the orphan. Plus: POSIX sends SIGTERM + leaves the
  pidfile; Windows removes the pidfile; taskkill-128 → stale cleanup; taskkill
  non-zero → exit 1 with pidfile preserved.
- **Real-process verification (not just mocks):** stood up a real parent→child
  process tree on Windows (parent stands in for uvicorn, a long-lived `ping`
  child stands in for cloudflared), ran the *real* `hooky stop` against the
  pidfile, and confirmed parent dead + **child dead** + pidfile removed. This is
  the cross-process proof the mock tests structurally can't give.

### Confidence
**HIGH.** Root cause confirmed by reading the code path end-to-end; fix verified
against a real process tree on Windows (the exact failing scenario); POSIX/Docker
path provably unchanged; full suite + ruff + mypy --strict green.

### Follow-ups / notes
- The `cloudflare-smoke.md` runbook's pass criterion "`hooky stop` terminates
  cloudflared with no orphan" should now hold on Windows — worth re-running the
  live smoke to tick it.
- Minor latent robustness gap noticed (not fixed, out of scope): `hooky stop`
  rejects a pidfile written with a UTF-8 BOM (`int()` parse fails → graceful
  exit 1). Real `hooky start` writes the pidfile via Python `write_text` with no
  BOM, so this only bites if a non-hooky tool writes the pidfile. Left as-is.

---

## Post-v1 — `secret_token` header-equality validator (2026-05-31)

### Summary
Added a `secret_token` validator — the second validator added on behalf of a
consumer (whose Telegram surface needs webhook authenticity).
Unlike every existing validator (`github`/`stripe`/`generic_hmac`/`quo`), this one
does **not** HMAC the request body: it checks that a named header equals a shared
secret verbatim, in constant time. Telegram's `setWebhook` `secret_token` is
exactly this shape — Telegram echoes the secret in `X-Telegram-Bot-Api-Secret-Token`
on every update; there is no body signature to verify. New `ValidatorType` member
`"secret_token"`. **229 tests (+14), ruff + mypy clean on the touched files.**

### Decisions
- **Not modeled on any HMAC validator** — it's a different category (header-equals,
  body-agnostic). Kept it deliberately small: `__init__(secret, header=…)` +
  `validate`. Reuses `base._get_header` (case-insensitive) and
  `base.constant_time_compare` (a naive `==` would leak the secret byte-by-byte —
  the same OWASP footgun the HMAC validators guard against).
- **Header is configurable, defaults to Telegram's.** The default
  `X-Telegram-Bot-Api-Secret-Token` covers the motivating case with no config;
  the `header` field (already on `ValidatorConfig`, previously generic_hmac-only)
  lets any other "echo-the-secret" service reuse it. Registry passes `header`
  through only when set, else takes the default.
- **Body explicitly ignored** (documented in the validate docstring) — this is the
  one validator where the body plays no role; flagged so a future reader doesn't
  mistake it for an incomplete HMAC check.
- **Fail-closed:** empty secret rejected at construction (matches github/generic);
  missing header → `False`, never a match.
- Registry/schema wiring mirrors the other types: `secret_env` resolution,
  `build_validator` branch, `__init__` export, `ValidatorType` Literal, and the
  `type`/`header` field descriptions updated to mention `secret_token`.

### Tests
- `tests/test_validators/test_secret_token.py` (10): matching header passes; wrong
  value / missing header / empty value fail; case-insensitive lookup; body ignored
  (any/empty body still passes); custom header honoured (and default not consulted
  then); default header is Telegram's; empty-secret construction raises.
- `tests/test_validators/test_registry.py` (+2): builds with the default Telegram
  header when none configured; honours a custom `header`.

### Confidence
**HIGH.** The mechanism is trivial (constant-time string equality) and fully
covered; the only "wire format" unknown is whether Telegram's header name/behaviour
matches the docs, which the consumer confirms at its live smoke.

---

## Post-v1 — `py.typed` marker (PEP 561) (2026-06-01)

### Summary
Added `src/hooky/py.typed` (empty marker) so Hooky advertises its inline types to
downstream type-checkers. Without it, any consumer importing `hooky.*` under mypy
got `import-untyped` → the imported symbol degrades to `Any` → e.g. a subclass of
`WebhookHandler` becomes "cannot subclass (has type Any)". This surfaced in
the first consumer: 6 errors across its two `WebhookHandler` subclasses, blocking a
mypy-strict-clean tree there despite Hooky itself being fully typed.

### Why this is correct / safe
- Hooky's own `mypy --strict` is clean (36 source files), so the "this package is
  typed" claim the marker makes is **sound** — we're not exporting type holes.
- Hatchling's wheel target (`packages = ["src/hooky"]`) includes non-`.py` package
  files by default, so the marker ships in real builds with **no pyproject change**.
- Inert at runtime; affects type-checkers only.
- Verified downstream: with the marker in place, the consumer's full tree is
  mypy-strict-clean (the 6 handler errors cleared, no new mismatches surfaced —
  i.e. Hooky's actual exported types line up with how it subclasses them).

### Confidence
**HIGH.** One-file, additive, runtime-inert; both sides re-verified green.

---

## Handler-router seam — plugins may contribute auxiliary HTTP routes (2026-06-01)

**Why:** a consuming app wanted to serve a read-only status page on the
same FastAPI app, but Hooky had no seam for it — handlers only *process stored
webhooks*; routes were fixed in `build_router`. Rather than bake an app-specific
route into Hooky, added a small **general** extension point.

**What:** `WebhookHandler.routers() -> list[APIRouter]`, default `[]` (non-abstract,
so every existing handler is unaffected). `create_app` iterates `manager.handlers`,
collects each handler's routers, and `include_router`s them after the core router.

**Isolation (the load-bearing decision):** collecting routers is wrapped per-handler
in try/except — a handler that raises from `routers()` is logged
(`handler_routers_failed`) and **skipped**, never propagated. An auxiliary-route bug
must not stop the app from coming up and receiving webhooks: Hooky's prime directive
is to never drop a webhook. Mounted *after* the core router; the `POST
/webhook/{path}` catch-all can't collide with a handler's `GET` route.

**Contract for authors** (documented on the method): keep contributed routes
**read-only**, **own their auth** (the framework mounts them verbatim — it does not
authenticate them), and a router-only provider may implement `process()` as a no-op.

**Files:** `handlers/base.py` (+`routers()` + `fastapi.APIRouter` import), `app.py`
(collect+mount loop), `tests/test_handlers/test_routers_seam.py` (mounts a contributed
route; default handler mounts nothing + core routes intact; a broken `routers()`
doesn't sink the app and a sibling's route still mounts + webhook POST still 200s).

### Verification
`test_routers_seam.py` + `test_routes.py` green (19); ruff clean on changed files;
`mypy src/hooky` clean on the changes (the only error is the pre-existing
`types-PyYAML`-missing note in `config/loader.py`, unrelated).

### Confidence
**HIGH.** Additive + default-`[]` (no existing handler changes behavior); the failure
path is isolated and tested; consumed live by the consumer's admin status page.

## Windows: exclusive port bind — close the silent socket-shadow footgun (2026-06-05)

**Why (a live incident, not a hypothetical):** Hooky's production consumer
went silent for **~2 days** and nobody noticed. Root cause was *not* a crash: a stray
`python -m http.server 8000`, launched to preview an unrelated static demo, co-bound
the consumer's port. On Windows a socket that sets `SO_REUSEADDR` is allowed to bind a port
another process is **already actively listening on** — silently co-binding and stealing
inbound connections — *unless* the original socket claimed the port with
`SO_EXCLUSIVEADDRUSE`. uvicorn's default socket sets `SO_REUSEADDR` and **not** the
exclusive flag, so Hooky left its own door unlocked: the squatter won the OS's
last-binder-wins lottery and answered the tunnel's webhooks with 404/501 while the real
server sat healthy and shadowed. (On POSIX this can't happen — `SO_REUSEADDR` there does
not permit stealing an actively-listening socket.)

**The principle:** you can't fix this by enumerating forbidden ports (an unbounded set
that lives in human memory — a fool's errand). You harden the **one** thing that must be
protected: the server claims its port exclusively, and the OS turns every future
collision into the *newcomer's* loud `WSAEADDRINUSE` instead of our silent displacement.

**What:**
- `_bind_exclusive_socket(host, port)` (Windows): sets `SO_EXCLUSIVEADDRUSE` *before*
  `bind`, returns the bound (un-`listen`ed) socket. A short bind-retry loop
  (`_BIND_ATTEMPTS=5` × `_BIND_BACKOFF_S=0.5s`) absorbs the race on a fast restart where
  the previous process hasn't fully released the port yet. Address family is resolved via
  `getaddrinfo` so an IPv6 bind host still works.
- `_run_server(app, host, port)` platform-splits, mirroring the existing
  `_terminate_server` precedent: **Windows** pre-binds exclusively and hands the socket to
  `uvicorn.Server(...).run(sockets=[sock])` (so uvicorn skips its own `SO_REUSEADDR`
  bind); **POSIX** keeps the untouched `uvicorn.run(...)` path, because the footgun
  doesn't exist there and the production `docker`/`SIGTERM` graceful-shutdown path depends
  on uvicorn owning the socket lifecycle.
- `start()` now calls `_run_server(...)` instead of `uvicorn.run(...)` directly.

**Files:** `cli/main.py` (+`socket`/`time` imports, `_BIND_ATTEMPTS`/`_BIND_BACKOFF_S`,
`_bind_exclusive_socket`, `_run_server`; `start()` rewired; dropped the now-unused local
`import uvicorn`). `tests/test_cli/test_lifecycle.py` (split the start test into explicit
POSIX/Windows variants; added a **real-socket regression** that holds a port via
`_bind_exclusive_socket` then asserts a second binder using `SO_REUSEADDR` — exactly what
`python -m http.server` does — is *rejected*, Windows-only via `skipif`).

### Verification
ruff clean + `mypy --strict` clean on `cli/main.py`; full suite **234 passed** (was 232:
+1 Windows-bind wiring test, +1 real-socket intruder-rejection regression).

### Confidence
**HIGH.** Windows-only behavior change; POSIX path byte-for-byte unchanged. The platform
split follows the established `_terminate_server` pattern, and the core property is proven
by a real-socket test, not a mock. **Note:** this hardens *every* Hooky-hosted server, but
only takes effect on a service **restart** — a running instance keeps its old non-exclusive
socket until then.
