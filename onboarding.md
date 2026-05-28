# Onboarding — Hooky_McHookface

> Welcome. You're an AI agent (Claude Code, Cursor, GPT, whatever) arriving in this repo. Read this first.

## What this is

**Hooky_McHookface** is an async-native, framework-grade webhook receiver: local FastAPI server + tunnel + persistent SQLite storage + plugin handler system + signature validation + replay engine. Reusable across personal projects.

Python import: `hooky`. Tone: quietly competent infrastructure. Boring on purpose.

**Status (2026-05-28): v1 complete.** Sprints 1–5 shipped + a post-v1 hardening pass. 200 tests, 94% coverage, `ruff` + `mypy --strict` clean (enforced by a pre-commit gate). The handler config contract — including `secret_env` for secrets — is **locked**. The one open item is the Cloudflare real-infra tunnel smoke, gated on a domain (runbook: `docs/cloudflare-smoke.md`). Everything else is done; this is no longer mid-build.

**First real consumer**: a separate private project consumes Hooky as a config-only dependency — it writes a `WebhookHandler` subclass taking constructor args like `from_number` + `api_token`, wired in via the `handlers:` config block (`config:` for non-secrets, `secret_env:` for the token).

**This is a library, not a service.** API ergonomics matter more than UI polish. The handler/validator/tunnel ABCs are the public contract.

## Read in this order

1. **[SPEC.md](SPEC.md)** — source of truth for what to build. Sprint-organized.
2. **[DEVLOG.md](DEVLOG.md)** — what's been built so far + why. Check the latest entry first.
3. **`docs/.agents/current-handover.md`** if it exists — ephemeral session context.
4. This file — navigation + workflow rules.
5. **Reference (do not modify)**: an earlier Flask-based attempt, kept locally as a behavioral spec source + algorithm reference (HMAC math, schema design, handler ABC concept). Do NOT inherit code from it directly.

## Document locations

| Doc | Location |
|---|---|
| Spec | `SPEC.md` (project root) |
| DEVLOG | `DEVLOG.md` (project root) |
| Onboarding | `onboarding.md` (you are here) |
| Handover | `docs/.agents/current-handover.md` (created on demand) |
| Reference impl | an earlier Flask-based attempt, kept locally (read-only, do not edit) |

## About the human

The author: systems thinker, builds infrastructure for personal use. Direct feedback preferred — hedging wastes time. Out-of-scope ideas go to §10 Parking Lot.

**Important framing**: This is a **rewrite, not a migration**. The old `webhook-reciever/` had zero production users; given the agent-era rewrite-vs-migrate tradeoffs, the right call is fresh code with the old as reference. Don't apologize for not preserving old structure. Read old tests as behavioral docs, lift algorithms, redesign interfaces.

## Workflow

This project uses the sprint workflow:

1. **Orient** — read SPEC, latest DEVLOG entry, any handover. ~5 minutes.
2. **Implement** — one sprint at a time, per the spec. Write tests in the same sprint.
3. **Test** — sprint not done until tests pass + coverage holds.
4. **DEVLOG** — write the entry while it's fresh. Decisions + their *why*.
5. **Confidence check** — HIGH = ship; MODERATE = ship + document; LOW = stop and ask.

If stuck for 3 attempts on the same problem, stop and surface it.

## Project-specific quirks

- **This is a library.** No persona, no SMS, no consumer-specific logic. Core stays consumer-agnostic.
- **The ABCs are the public API.** `WebhookHandler`, `SignatureValidator`, `TunnelProvider`, `StorageBackend`. Backwards-compat matters here in a way it doesn't for services.
- **Async-native throughout.** No `asyncio.run()` inside the framework. If you find yourself reaching for it, redesign.
- **Store-first invariant.** Webhook is stored before any handler runs, before any validator runs (validator failure still stores). Critical for debugging + replay.
- **Handler isolation.** One handler failing must not affect others. Use `asyncio.gather(..., return_exceptions=True)`.
- **Replay uses live dispatch.** Replay engine calls the same `HandlerManager.dispatch` as live receive — no divergent code paths.
- **Logs are structured JSON to stdout by default.** Never log raw secrets.
- **The old `webhook-reciever/` folder is reference, not source.** Read its tests as behavior spec; lift HMAC math + schema design; do not inherit Flask code.
- **First consumer's needs are valid signal but not gospel.** The first consumer's requirements informed v1, but Hooky_McHookface's API stays generic. If a consumer wants something provider-specific, it goes in that consumer, not here.

## How to write a handover

When wrapping a session that didn't naturally end at a sprint boundary, write `docs/.agents/current-handover.md` with:

- **Where we are**: current sprint + last completed step.
- **What's not committed**: uncommitted files, half-finished thoughts.
- **What I'd do next**: 1-3 concrete next actions.
- **Open questions for Brad**: blockers.
- **Gotchas discovered**: subtle stuff not yet in DEVLOG.

Keep it short. Durable stuff goes to DEVLOG.

## Getting started

```bash
# Install
pip install -e ".[dev]"

# Enable the pre-commit gate (ruff + mypy + pytest) — once per clone
git config core.hooksPath .githooks

# Run tests
pytest --cov=hooky

# Start the server (copy config/config.example.yaml -> config.yaml first)
hooky start --config config.yaml

# Useful CLI
hooky list
hooky show <id>
hooky replay <id>
```

Full setup + the handler-authoring guide live in `README.md`. A ready-to-edit
config is at `config/config.example.yaml`.
