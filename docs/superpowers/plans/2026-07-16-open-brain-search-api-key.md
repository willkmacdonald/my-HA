# Open Brain: X-API-Key auth for /api/search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **⚠ TARGET REPO: `/Users/willmacdonald/Documents/Code/claude/open-brain` — NOT my-HA.** This plan lives in my-HA for Phase 2 cohesion, but every file path below is relative to the open-brain repo root. Execute it in a session opened in that repo (it has its own hooks — local server runs are blocked there; tests are fine). It is my-HA Phase 2's only cross-repo dependency (spec §Component 5).

**Goal:** `/api/search` accepts `X-API-Key: <key>` as an alternative to the session cookie, so my-HA's router can query Open Brain headlessly.

**Architecture:** The bypass lives in `SessionCookieMiddleware.dispatch` — before the cookie check, a constant-time key comparison admits `/api/search` only. The key follows the repo's existing Key Vault pattern: a `*_secret_name` Settings field, fetched at startup in the lifespan, stored on `app.state`. No other endpoint changes.

**Tech Stack:** FastAPI/Starlette middleware, pydantic-settings, azure-keyvault-secrets (all already in the repo), pytest via `uv run pytest`.

## Global Constraints

- Spec (my-HA `docs/superpowers/specs/2026-07-12-phase2-openbrain-timers-design.md` §Component 5): key stored as new Key Vault secret **`ob-search-api-key`**; **constant-time comparison**; **401 on mismatch as today**; **no other endpoint changes**. Unit tests follow this repo's patterns; deploy via the repo's documented `az acr build` + `az containerapp update` loop.
- **Deliberate deviation from one spec sentence:** the spec says "surfaced to the container app as an env var", but this repo's convention loads secrets from Key Vault at startup via `settings.key_vault_url` (see `src/open_brain/main.py:89-110`). Follow the repo convention — the spec's intent (key lives in Key Vault, service reads it) is preserved; record nothing else.
- Key absent/empty ⇒ feature off: header auth never matches, cookie auth unchanged. Dev mode (no `KEY_VAULT_URL`) runs with the feature off.
- Tests: `uv run pytest tests/test_search_api_key.py -v` (this repo's runner). Do NOT start servers locally (blocked by hook).

---

### Task 1: Settings field + startup secret fetch

**Files:**
- Modify: `src/open_brain/config.py` (Settings class, next to the other `*_secret_name` fields at lines 39-40)
- Modify: `src/open_brain/main.py` (lifespan secrets block, lines ~75-113)

**Interfaces:**
- Produces: `settings.search_api_key_secret_name` (default `"ob-search-api-key"`); `app.state.search_api_key: str` (`""` when unset/dev).

- [ ] **Step 1: Add the Settings field**

In `src/open_brain/config.py`, directly under `admin_password_hash_secret_name` (line 40):

```python
    search_api_key_secret_name: str = "ob-search-api-key"  # noqa: S105
```

- [ ] **Step 2: Fetch it in the lifespan**

In `src/open_brain/main.py`, initialize before the `if not settings.key_vault_url:` branch (line 82):

```python
        search_api_key_value = ""
```

Inside the Key Vault branch, after the `hash_secret = await kv_client.get_secret(...)` call (line 100) and before `validate_production_web_session_secrets`:

```python
                search_key_secret = await kv_client.get_secret(
                    settings.search_api_key_secret_name
                )
                search_api_key_value = search_key_secret.value or ""
```

And after `logger.info("Admin password hash fetched from Key Vault")` (line 106):

```python
                logger.info("Search API key fetched from Key Vault")
```

Then, next to the `app.state.session_signing_key` assignment (line 112):

```python
        app.state.search_api_key = search_api_key_value
```

- [ ] **Step 3: Run the existing suite to confirm nothing broke**

Run: `uv run pytest tests/ -x -q`
Expected: same pass count as before this change (the tests' `app` fixture in `tests/conftest.py` doesn't run the lifespan).

- [ ] **Step 4: Commit**

```bash
git add src/open_brain/config.py src/open_brain/main.py
git commit -m "feat: load ob-search-api-key secret into app.state.search_api_key"
```

---

### Task 2: Middleware bypass with constant-time comparison + tests

**Files:**
- Modify: `src/open_brain/middleware/session.py`
- Create: `tests/test_search_api_key.py`

**Interfaces:**
- Consumes: `app.state.search_api_key` (Task 1).
- Produces: `X-API-Key` header admits exactly `POST/GET /api/search`; everything else unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_api_key.py` (fixture style copied from `tests/test_web_auth.py`, which builds a local app + `add_middleware(SessionCookieMiddleware)` + httpx `ASGITransport`):

```python
"""X-API-Key auth for /api/search (my-HA Phase 2 integration)."""

from __future__ import annotations

import secrets

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from open_brain.middleware.session import SessionCookieMiddleware
from open_brain.session import mint_session_token

_KEY = "test-search-key-abc123"


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def app(signing_key: bytes) -> FastAPI:
    a = FastAPI()
    a.add_middleware(SessionCookieMiddleware)
    a.state.session_signing_key = signing_key
    a.state.search_api_key = _KEY

    @a.post("/api/search")
    async def search() -> dict:
        return {"thoughts": [], "total": 0, "query": "q"}

    @a.post("/api/capture")
    async def capture() -> dict:
        return {}

    return a


@pytest_asyncio.fixture
async def client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_valid_api_key_admits_search(client: AsyncClient) -> None:
    resp = await client.post("/api/search", json={"query": "q"},
                             headers={"X-API-Key": _KEY})
    assert resp.status_code == 200


async def test_wrong_api_key_is_401(client: AsyncClient) -> None:
    resp = await client.post("/api/search", json={"query": "q"},
                             headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


async def test_missing_api_key_is_401(client: AsyncClient) -> None:
    resp = await client.post("/api/search", json={"query": "q"})
    assert resp.status_code == 401


async def test_api_key_does_not_admit_other_api_routes(client: AsyncClient) -> None:
    resp = await client.post("/api/capture", json={}, headers={"X-API-Key": _KEY})
    assert resp.status_code == 401


async def test_unconfigured_key_never_matches(app: FastAPI) -> None:
    app.state.search_api_key = ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/search", json={"query": "q"}, headers={"X-API-Key": ""})
    assert resp.status_code == 401


async def test_session_cookie_still_admits_search(app: FastAPI, signing_key: bytes,
                                                  client: AsyncClient) -> None:
    from open_brain.middleware.session import SESSION_COOKIE_NAME

    token = mint_session_token(signing_key=signing_key, ttl_seconds=3600)
    resp = await client.post("/api/search", json={"query": "q"},
                             cookies={SESSION_COOKIE_NAME: token})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_search_api_key.py -v`
Expected: `test_valid_api_key_admits_search` FAILs with 401 (no bypass yet); cookie/401 tests already PASS.

- [ ] **Step 3: Write the implementation**

In `src/open_brain/middleware/session.py`, add `import secrets` to the imports, add a constant under `SESSION_COOKIE_NAME`:

```python
SEARCH_API_KEY_PATH = "/api/search"
```

and insert into `dispatch`, immediately after the `PUBLIC_PATHS` early-return (line 24) and before the signing-key/cookie block:

```python
        # my-HA Phase 2: header-key auth for the search endpoint ONLY.
        # Constant-time compare; unset key (dev mode / secret missing) never
        # matches, so cookie auth remains the only door.
        if path == SEARCH_API_KEY_PATH:
            supplied = request.headers.get("X-API-Key") or ""
            configured = getattr(request.app.state, "search_api_key", "") or ""
            if supplied and configured and secrets.compare_digest(supplied, configured):
                return await call_next(request)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_search_api_key.py tests/test_web_auth.py tests/test_api_search.py -v`
Expected: all PASS (web-auth and api-search regressions included).

- [ ] **Step 5: Full suite, lint per repo convention, commit**

```bash
uv run pytest -q && uv run ruff check .
git add src/open_brain/middleware/session.py tests/test_search_api_key.py
git commit -m "feat: X-API-Key auth for /api/search (constant-time, search-only)"
```

---

### Task 3: Deploy + verify (operator steps — Will's az session, not a subagent)

**Files:** none (Azure + my-HA `.env`).

- [ ] **Step 1: Create the secret** (generate, then store — pick the vault the container app already reads via its `KEY_VAULT_URL` env; confirm with `az containerapp show -n open-brain-web -g <resource-group> --query "properties.template.containers[0].env" -o table`):

```bash
openssl rand -hex 32   # this value is <KEY> below
az keyvault secret set --vault-name <vault-from-KEY_VAULT_URL> --name ob-search-api-key --value <KEY>
```

- [ ] **Step 2: Build + deploy** per the repo README's documented loop (README.md:369):

```bash
az acr build --registry openbrainwkm --image open-brain-web:vNN --file Dockerfile .
az containerapp update -n open-brain-web -g <resource-group> \
  --image openbrainwkm.azurecr.io/open-brain-web:vNN
```

- [ ] **Step 3: Verify against production**

```bash
DOMAIN=open-brain-web.yellowforest-4e567186.eastus2.azurecontainerapps.io
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://$DOMAIN/api/search" \
  -H "content-type: application/json" -d '{"query":"test","limit":1}'          # expect 401
curl -s -X POST "https://$DOMAIN/api/search" -H "X-API-Key: <KEY>" \
  -H "content-type: application/json" -d '{"query":"speaker driver","limit":1}' # expect 200 + thoughts JSON
```

- [ ] **Step 4: Wire my-HA** — add to my-HA's repo-root `.env` (edit the file directly; do NOT put `.env` in any git command — the security hook blocks it):

```
OPEN_BRAIN_URL=https://open-brain-web.yellowforest-4e567186.eastus2.azurecontainerapps.io
OPEN_BRAIN_API_KEY=<KEY>
```

- [ ] **Step 5: Push the open-brain commits** (`git push` in the open-brain repo).

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** header accepted as cookie alternative ✓ (Task 2); Key Vault secret `ob-search-api-key` ✓ (Tasks 1, 3); constant-time compare ✓; 401 on mismatch as today ✓ (falls through to the existing cookie path); unit tests per repo patterns ✓ (test_web_auth.py style); deploy per documented loop ✓; no other endpoint changes ✓ (path-equality guard + explicit negative test for `/api/capture`).
- **Deviation recorded:** Key Vault-at-startup instead of "env var" — repo convention, noted in Global Constraints.
- **Type consistency:** `app.state.search_api_key: str` produced in Task 1, consumed in Task 2 middleware and tests.
