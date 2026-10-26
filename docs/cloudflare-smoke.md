# Cloudflare Tunnel — manual smoke runbook

The one v1 success criterion not yet exercised against real infrastructure
(SPEC §7, Sprint 4): with a real Cloudflare Tunnel token, `hooky start` brings
up a working public tunnel, `/healthz` reports it connected, an external POST
is stored, and `hooky stop` tears it down cleanly.

The subprocess **failure** paths (binary-not-on-PATH, spawn failure, readiness
timeout, post-start crash, SIGTERM→SIGKILL escalation, watcher crash) are now
covered by unit tests in `tests/test_tunnel/test_cloudflare.py`. This runbook
covers the **happy path** that only real `cloudflared` can prove — line 125 of
`cloudflare.py` (the real `asyncio.create_subprocess_exec`) is the code this
exercises.

## Prerequisites (one-time)

1. A domain registered on / managed by Cloudflare (Cloudflare Registrar is the
   cheapest path — registering there auto-creates the zone).
2. A **named tunnel** created in Zero Trust → Networks → Tunnels (connector
   type "Cloudflared"). Copy its **token** (the long `eyJ...` string from the
   install command — this is the `--token` value, NOT a Cloudflare API token).
3. A **public hostname** on that tunnel routing `hooky.<yourdomain>` → service
   `HTTP` → `localhost:8000`.
4. `cloudflared` installed locally and on PATH:
   `winget install --id Cloudflare.cloudflared` (reopen the shell afterward).
   Verify: `cloudflared --version` reports at least 2026.10.2. `start()` refuses
   anything older (`MIN_CLOUDFLARED_VERSION` in `cloudflare.py`).

## Step 1 — smoke config

Save as `config/smoke.cloudflare.yaml` (gitignored / local-only — it names no
secrets, but keep your real hostname out of commits if you prefer):

```yaml
server:
  host: 0.0.0.0
  port: 8000
  log_level: INFO
  log_format: console          # readable for a live smoke

storage:
  backend: sqlite
  path: data/smoke.db

handlers:
  - path: "hooky.handlers.logger:LoggerHandler"

tunnel:
  provider: cloudflare
  cloudflare:
    hostname: hooky.<yourdomain>.com   # the public hostname you routed above
    # token sourced from env CLOUDFLARED_TOKEN
```

## Step 2 — put the token in the environment

```powershell
# PowerShell (current session only — do NOT commit the token anywhere)
$env:CLOUDFLARED_TOKEN = "eyJ...your-token..."
```

```bash
# bash equivalent
export CLOUDFLARED_TOKEN="eyJ...your-token..."
```

## Step 3 — start

```powershell
hooky start --config config/smoke.cloudflare.yaml
```

**Expect in the logs:** a `cloudflared_stdout` line containing
`Connection registered` (or `Registered tunnel connection`), then
`cloudflare_tunnel_started` with `public_url=https://hooky.<yourdomain>.com`.
If startup hangs ~30s then fails with "Readiness marker not seen", cloudflared
never registered — check the token and the tunnel's status in the dashboard.

## Step 4 — confirm health

In another shell:

```powershell
curl http://localhost:8000/healthz
```

**Pass:** the `tunnel` block reads
`{"status": "connected", "public_url": "https://hooky.<yourdomain>.com", ...}`.

## Step 5 — external round-trip (the real proof)

POST through the **public** hostname, not localhost — this proves the tunnel
actually carries traffic:

```powershell
curl -X POST https://hooky.<yourdomain>.com/webhook/test -d '{"smoke":"ok"}'
# -> {"status":"received","id":"01J..."}

hooky list --db data/smoke.db
hooky show <id> --db data/smoke.db
```

**Pass:** the webhook from the public URL appears in `hooky list`, and `show`
displays the body + a LoggerHandler run.

## Step 6 — clean teardown

```powershell
hooky stop
```

**Pass:** process exits; no orphaned `cloudflared` process remains
(`Get-Process cloudflared` returns nothing). `/healthz` is no longer reachable.

## Pass criteria checklist

- [ ] `cloudflared` spawns and registers (readiness marker seen in logs).
- [ ] `cloudflare_tunnel_started` logged with the configured public URL.
- [ ] `/healthz` tunnel block → `status: connected` + correct `public_url`.
- [ ] A POST to the **public** hostname is stored (verifiable via `hooky list`).
- [ ] `hooky stop` terminates cloudflared with no orphan process.

When this passes clean, promote the Sprint 4 confidence in DEVLOG.md from
MODERATE to HIGH and tick the last open SPEC §6 "still outstanding" item.

## Troubleshooting → which code path

| Symptom | Likely cause | Where |
|---|---|---|
| `'cloudflared' not found on PATH` at start | binary not installed / shell not reopened | `cloudflare.py:_require_on_path` |
| `cloudflared X is below the minimum` or `Could not read a version` | old or dev-build cloudflared; the floor fails closed | `_check_version`; upgrade with `winget upgrade --id Cloudflare.cloudflared` |
| Hang ~30s then "Readiness marker not seen" | bad token, tunnel down on CF side, or a cloudflared version with a 3rd marker phrasing | `start()` timeout; `_READY_MARKERS` |
| Starts then `/healthz` flips to `error` | cloudflared crashed after registering | `_watch_output` sets `_last_error`, clears `_url` |
| `hooky stop` slow (~5s) then exits | cloudflared ignored SIGTERM; escalated to SIGKILL | `_terminate_proc` grace → kill |
