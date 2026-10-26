# Hooky_McHookface

> The internet, given the chance to name things, gave us Boaty McBoatface. This
> is what happens when a webhook receiver gets the same treatment. It catches
> webhooks and stores them *before* asking any questions.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A framework-grade, async-native **webhook receiver**: FastAPI + SQLite +
pluggable handlers / validators / tunnel providers, with a CLI and an on-demand
replay engine.

It's a **library, not a service**: you subclass `WebhookHandler`, point a config
file at it, and run `hooky start`. Webhooks are stored *before* any handler runs
(store-first invariant), signatures are verified per-path, and anything stored
can be replayed through the exact same dispatch path as a live delivery.

> **Status:** v1 complete (Sprints 1–5). Receive + store + dispatch, HMAC /
> GitHub / Stripe signature validation, none / ngrok / Cloudflare tunnels, and
> the `hooky` CLI with replay. 200 tests, 94% coverage; `ruff` + `mypy --strict`
> clean (enforced by a pre-commit gate). Running in production as the front door
> for a private downstream project.

---

## 🧑‍🚀 Not a coder?

Honest answer: this one's plumbing. There's nothing here to *use* unless you're
building software that needs to receive webhooks (those little "something
happened!" pings that services like Stripe, GitHub, or a text-message provider
send to your server).

But if you're building such a thing **with an AI assistant**, hand it this:

> *I need to receive webhooks reliably. Look at `github.com/berad217/Hooky_McHookface`
> — read its `README.md` and `onboarding.md`. Could I use it as the foundation
> for my project instead of writing a webhook server from scratch? Explain the
> tradeoffs in plain English before we decide.*

If you don't know what a webhook is, you almost certainly don't need this — and
that's fine. This is the plumbing other apps get *built on top of* — not
something you'd run on its own.

---

## 🤖 Coding agents start here.

You've been pointed at this repo. Before you change anything:

1. **Read [`onboarding.md`](onboarding.md)** — the office tour: what Hooky is,
   where things live, and the conventions.
2. **Read [`SPEC.md`](SPEC.md)** for the design and **[`DEVLOG.md`](DEVLOG.md)**
   for where the work actually landed.
3. **The "Design invariants" section below is non-negotiable** — they're the
   reason replay works and handlers stay isolated. Don't break them for
   convenience.

**Hard rules:** secrets never go in the config YAML or in git (they're named by
env var and resolved at startup); the ABCs (`WebhookHandler`,
`SignatureValidator`, `TunnelProvider`, `StorageBackend`) are the public
contract — changing them is a breaking change; `pytest` + `ruff` +
`mypy --strict` are all green before and after (the pre-commit gate enforces it).

---

## Install

```bash
pip install -e ".[dev]"        # + dev tooling (pytest, ruff, mypy)
pip install -e ".[ngrok]"      # optional: pyngrok, only if you use the ngrok tunnel
```

Python 3.11+. `cloudflared` (for the Cloudflare tunnel) is an external binary —
install separately if you use `provider: cloudflare`. Hooky refuses to start the
tunnel on anything older than `MIN_CLOUDFLARED_VERSION` (2026.10.2; older releases
trust compromised eMudhra certificates).

## Quickstart (under 60 seconds)

```bash
# 1. Start a receiver on localhost (no tunnel, no validators — just store + 200)
hooky start

# 2. In another terminal, post a webhook
curl -X POST http://localhost:8000/webhook/test -d '{"hello":"world"}'
#    -> {"status":"received","id":"01J..."}

# 3. See it
hooky list
hooky show <id>
```

That's the whole loop: receive → store → inspect. Add a config file to wire in
handlers, validators, and a public tunnel.

## Configuration

Everything is driven by a YAML config passed via `--config` (or run with
defaults). Full example:

```yaml
server:
  host: 0.0.0.0
  port: 8000
  log_level: INFO
  log_format: json            # json (default) | console

storage:
  backend: sqlite
  path: data/hooky.db
  retention_days: 30          # 0 = keep forever (enforcement: parking lot)

# Handlers are loaded by dotted import path and registered at startup.
# This is how a consuming app wires in its own handlers — no launcher code.
handlers:
  - path: "myapp.handlers:MyHandler"
    config:                   # non-secret kwargs -> MyHandler(**config)
      some_option: true
    secret_env:               # kwarg -> ENV_VAR; read from env, passed as kwargs
      api_token: MYAPP_API_TOKEN
  - path: "hooky.handlers.logger:LoggerHandler"

# Per-path signature validators. A request to a guarded path with a bad/missing
# signature is stored (signature_valid=0) and rejected with 401 — never silently
# dropped, so you can debug it.
validators:
  - path: /webhook/github
    type: github
    secret_env: GITHUB_WEBHOOK_SECRET
  - path: /webhook/stripe
    type: stripe
    secret_env: STRIPE_WEBHOOK_SECRET
    tolerance_seconds: 300
  - path: /webhook/custom
    type: generic_hmac
    secret_env: CUSTOM_WEBHOOK_SECRET
    header: x-signature
    algorithm: sha256         # sha256 | sha1
    prefix: ""                # e.g. "sha256=" for some providers

tunnel:
  provider: none              # none | ngrok | cloudflare
  ngrok:
    region: us
    # auth token from env NGROK_AUTH_TOKEN
  cloudflare:
    hostname: hooky.example.com   # public hostname routed to this server
    # token from env CLOUDFLARED_TOKEN
```

Secrets are **never** in the config file — they're named by env var (validator
`secret_env`, handler `secret_env`, `NGROK_AUTH_TOKEN`, `CLOUDFLARED_TOKEN`) and
read at startup. A missing env var fails loud at startup with a message naming
the var, not silently at first dispatch.

```bash
hooky start --config config.yaml
```

## Writing a handler

The handler ABC is the entire API surface. Two things: a `name` and an async
`process`.

```python
from hooky import WebhookHandler, StoredWebhook


class MyHandler(WebhookHandler):
    @property
    def name(self) -> str:
        return "my_handler"          # unique; keys handler_runs + replay

    async def process(self, webhook: StoredWebhook) -> dict:
        # webhook.body (bytes), webhook.body_json (dict | None),
        # webhook.headers (lowercased), webhook.path, webhook.signature_valid
        return {"processed": webhook.id}
```

Rules of the road:

- **Async-native.** If you must call blocking code, push it through
  `asyncio.to_thread` — don't block the event loop.
- **Isolation is free.** If your handler raises, the exception is caught,
  logged with a traceback, and recorded as a failed run. Other handlers are
  unaffected. The HTTP response is already 200 by the time handlers run.
- **Return JSON-serialisable values** (or `None`). The return value is stored in
  `handler_runs.return_value`.

**Secrets and config.** Give your handler whatever it needs as plain
constructor arguments — don't read `os.environ` inside the handler:

```python
class MyHandler(WebhookHandler):
    def __init__(self, from_number: str, api_token: str) -> None:
        self.from_number = from_number
        self.api_token = api_token   # just a value — no env-reading here
    ...
```

Non-secret args come from `config:`; secrets come from `secret_env:` (a
`kwarg -> ENV_VAR` map the framework resolves from the environment at startup).
This keeps secrets out of `config.yaml` *and* out of your handler code, and
keeps the handler trivially testable — `MyHandler(from_number="x",
api_token="fake")` with no environment to stub.

Register it via config (`handlers:` above) or programmatically. With config,
secrets flow through `secret_env`; programmatically, your launcher passes them
as constructor args (read env there — not inside the handler):

```python
import os
from hooky import HandlerManager
from hooky.app import create_app

manager = HandlerManager()
manager.register(
    MyHandler(from_number="+15551234567", api_token=os.environ["MYAPP_API_TOKEN"])
)
app = create_app(handler_manager=manager)
```

## CLI

```text
hooky start [--config PATH] [--host H] [--port P] [--pidfile PATH]
hooky stop  [--pidfile PATH]
hooky list  [--config PATH] [--db PATH] [--limit N] [--path /webhook/x]
hooky show  <id> [--config PATH] [--db PATH]
hooky replay <id> [--handler NAME]... [--config PATH] [--db PATH]
```

- `list` / `show` are read-only — point them at any DB with `--db`.
- `replay` re-dispatches a stored webhook through the **same** `HandlerManager.dispatch`
  path as live receive (no divergent code), so behaviour is identical. Use
  `--handler NAME` (repeatable) to replay to a subset. Replays append new
  `handler_runs` rows; they never mutate the original.

## Design invariants

- **Store-first.** Every received webhook is persisted before validation and
  before any handler dispatch. An invalid signature still stores (with
  `signature_valid=0`) and returns 401 — debuggable, replayable.
- **Handler isolation.** One handler failing never affects another
  (`asyncio.gather(..., return_exceptions=True)`).
- **Replay == live.** Replay reuses live dispatch. No second code path to drift.
- **ABCs are the public contract.** `WebhookHandler`, `SignatureValidator`,
  `TunnelProvider`, `StorageBackend`. Backwards-compat matters here.

## Development

```bash
pytest --cov=hooky          # run the suite + coverage
ruff check src tests        # lint
mypy src                    # type-check
```

A pre-commit gate runs all three (ruff + mypy + pytest) and blocks the commit
on failure. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

The hook (`.githooks/pre-commit`) uses the project `.venv` interpreter so
`mypy --strict` sees the installed deps. Bypass with `git commit --no-verify`
only when you mean it.

## License

[MIT](LICENSE). Build whatever you want on it. No warranty.
