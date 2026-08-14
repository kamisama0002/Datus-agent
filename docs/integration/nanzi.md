# NanZi bridge local runbook

The NanZi bridge runs from this Datus repository in its own Python 3.12 virtual environment. Keep it separate from the NanZi platform environment so the two repositories can use their required Python versions independently.

## Runtime contract

- Protocol: `nanzi-datus/v1`
- Datus listener: `127.0.0.1:8001`
- Worker count: exactly `--workers 1`; the project cache is process-local
- Project configuration: fetched from NanZi after authenticated requests and held in memory
- SQL permissions: read-only policy, immutable project configuration, and disabled Bash tools

## Prepare the Datus environment

From this repository in PowerShell:

```powershell
.\scripts\setup-nanzi-integration.ps1
```

The script creates `.venv` with Python 3.12 and installs Datus plus the required MySQL and semantic adapters. It does not configure or start either repository.

## Configure the process

Use [.env.nanzi.example](../../.env.nanzi.example) as the variable inventory. Set values in the current process or your secret manager; do not commit a completed environment file.

```powershell
$env:NANZI_CALLBACK_URL = "http://127.0.0.1:8000"
$env:NANZI_DATUS_INTERNAL_TOKEN = [Convert]::ToHexString(
    [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLowerInvariant()
```

Generate the token once and supply the same value to NanZi through its secret-management path. Angle-bracket values, `change-me`, `example-*`, and `replace-with-a-shared-secret` are rejected as placeholders.

The Datus YAML template is [conf/agent-nanzi.example.yml](../../conf/agent-nanzi.example.yml). It resolves both values from the process environment and pins `nanzi-datus/v1`.

## Run the non-starting readiness check

Run `scripts/check-nanzi-integration.ps1` from the repository root:

```powershell
.\scripts\check-nanzi-integration.ps1
```

The command reads only the local YAML and environment. It never calls NanZi, MySQL, or a model service. Output contains stable states only:

```json
{"liveness":"alive","nanzi":{"checks":{"callback_url":"configured","config":"compatible","service_token":"configured"},"protocol":"nanzi-datus/v1","ready":true}}
```

A nonzero exit means configuration is not ready. The output never includes configured values, callback bodies, credentials, or raw exceptions.

## Start Datus manually

After the readiness check passes, start the API in the foreground:

```powershell
.\scripts\start-nanzi-integration.ps1
```

The script executes the equivalent of:

```powershell
.\.venv\Scripts\python.exe -m datus.api.main --host 127.0.0.1 --port 8001 --workers 1 --config conf/agent-nanzi.example.yml
```

Stop it with `Ctrl+C`. Do not add extra workers because the authenticated project cache and eviction coordination are in memory.

## Health contract

`GET http://127.0.0.1:8001/health` remains a process health endpoint. In NanZi mode it returns `liveness: alive` separately from `capabilities.nanzi.ready`, exposes `capabilities.nanzi.protocol: nanzi-datus/v1`, and reports database/model probes as `not_checked`. The NanZi-mode health path performs no callback, datasource, or model request.

Readiness check values are limited to `configured`, `compatible`, `missing`, `invalid`, `placeholder`, `unreadable`, and `incompatible`. Investigate the named local setting rather than logging its value.

## Canonical protocol fixtures

The authoritative `nanzi-datus/v1` cross-repository fixtures are under `tests/fixtures/nanzi_datus/v1/`. Copy `request.json`, `sse.json`, and `contract-manifest.json` byte-for-byte into the NanZi repository, then verify the request/SSE SHA-256 values recorded in the manifest. The raw bearer is a validator-rejected redacted sentinel; contract tests replace it only in memory with a strong test-only value before authentication.
