# Chenyme grok2api Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each successful Grok registration, optionally login to chenyme grok2api admin, import the SSO token, and convert web accounts to build.

**Architecture:** Parallel independent module beside existing `grok2api_*` pools. New `chenyme_grok2api_*` config, token cache, login/import/convert helpers, and one orchestrator called from GUI/CLI success paths. Failures are logged only and never break registration.

**Tech Stack:** Python 3.9+, unittest, curl_cffi `requests` via existing `http_post`, Tkinter GUI.

## Global Constraints

- Keep existing `grok2api_*` local/remote behavior unchanged.
- Import file body is pure SSO, one line per token (no email wrapper).
- Trigger: every successful account → import then convert (if convert enabled).
- Convert body: `{"all": true, "strategy": "<config>"}`; default strategy `missing`.
- Never raise from `add_token_to_chenyme_grok2api` into registration loops.
- Follow existing test style in `tests/test_grok2api_remote_pool.py`.

## File Structure

| File | Responsibility |
|------|----------------|
| `grok_register_ttk.py` | DEFAULT_CONFIG, chenyme helpers, GUI fields, CLI/GUI call sites |
| `config.example.json` | Document new keys |
| `tests/test_chenyme_grok2api.py` | Unit tests with mocked HTTP |
| `README.md` | Config docs for chenyme section |

---

### Task 1: Core chenyme API helpers + unit tests

**Files:**
- Modify: `grok_register_ttk.py` (DEFAULT_CONFIG + functions after `add_token_to_grok2api_pools`)
- Create: `tests/test_chenyme_grok2api.py`
- Modify: `config.example.json`

**Interfaces:**
- Produces:
  - `chenyme_clear_token_cache() -> None`
  - `chenyme_login(log_callback=None) -> str` (accessToken)
  - `chenyme_get_access_token(log_callback=None, force_refresh=False) -> str`
  - `chenyme_import_sso(raw_token, log_callback=None) -> bool`
  - `chenyme_convert_to_build(log_callback=None) -> bool`
  - `add_token_to_chenyme_grok2api(raw_token, email="", log_callback=None) -> bool`

- [ ] **Step 1: Write failing tests**

Create `tests/test_chenyme_grok2api.py` with DummyResponse (status, json, text, raise_for_status, iter_lines optional) covering:

1. `test_disabled_skips_http` — enabled false → no http_post
2. `test_login_and_cache` — login once, second get_access_token no second login
3. `test_import_multipart_sends_pure_sso` — files content is normalized sso line
4. `test_import_401_refreshes_and_retries` — first 401, re-login, second 200
5. `test_convert_body` — json `all=true`, strategy from config
6. `test_missing_config_skips` — empty base → False, no crash

- [ ] **Step 2: Run tests — expect FAIL**

```bash
python -m unittest tests.test_chenyme_grok2api -v
```

Expected: import/attribute errors for missing functions.

- [ ] **Step 3: Implement helpers**

Add to `DEFAULT_CONFIG` and `config.example.json`:

```json
"chenyme_grok2api_enabled": false,
"chenyme_grok2api_base": "",
"chenyme_grok2api_username": "",
"chenyme_grok2api_password": "",
"chenyme_grok2api_convert": true,
"chenyme_grok2api_convert_strategy": "missing"
```

Implement after `add_token_to_grok2api_pools` (~line 708):

- Module cache: `_chenyme_access_token`, `_chenyme_access_token_expires_at`
- `_chenyme_normalize_base(base)` → strip, rstrip `/`
- `chenyme_clear_token_cache()`
- `chenyme_login` → POST `{base}/api/admin/v1/auth/login`, parse `data.tokens.accessToken` and optional `accessTokenExpiresAt`; default TTL 50 min if no expiry
- `chenyme_get_access_token` → reuse if expires > now+60s; else login
- `chenyme_import_sso` → Bearer + multipart `files` field, filename `grok-web-sso-tokens.txt`, body pure sso; on 401 clear cache + force refresh + retry once; consume response text/stream
- `chenyme_convert_to_build` → POST convert-to-build with `{"all":true,"strategy":...}`; 401 retry once
- `add_token_to_chenyme_grok2api` → if not enabled return False; if missing base/user/pass log debug return False; try import then optional convert; catch all exceptions

Use existing `http_post` and `_normalize_sso_token`.

- [ ] **Step 4: Run tests — expect PASS**

```bash
python -m unittest tests.test_chenyme_grok2api -v
```

- [ ] **Step 5: Commit**

```bash
git add grok_register_ttk.py config.example.json tests/test_chenyme_grok2api.py
git commit -m "feat: add chenyme grok2api login import convert helpers"
```

---

### Task 2: Wire GUI, CLI call sites, README

**Files:**
- Modify: `grok_register_ttk.py` (GUI rows 10+, start_registration save, success call sites)
- Modify: `README.md`

**Interfaces:**
- Consumes: `add_token_to_chenyme_grok2api(raw_token, email="", log_callback=None)`

- [ ] **Step 1: GUI fields after grok2api remote app_key (row 9)**

Add rows 10–13:
- enabled checkbox + convert checkbox
- base entry
- username entry
- password entry (`show="*"`)

- [ ] **Step 2: Persist on start_registration**

Save enabled/base/username/password/convert into `config` before `save_config()`.

- [ ] **Step 3: Call after existing pool write**

GUI (~3140) and CLI (~3300):

```python
add_token_to_grok2api_pools(sso, email=email, log_callback=...)
add_token_to_chenyme_grok2api(sso, email=email, log_callback=...)
```

- [ ] **Step 4: README**

Add chenyme config table section next to existing grok2api remote docs.

- [ ] **Step 5: Re-run all related tests**

```bash
python -m unittest tests.test_chenyme_grok2api tests.test_grok2api_remote_pool -v
```

- [ ] **Step 6: Commit**

```bash
git add grok_register_ttk.py README.md
git commit -m "feat: wire chenyme grok2api import into GUI and CLI"
```

---

## Spec coverage checklist

- [x] Independent config set
- [x] Login + Bearer auth + cache
- [x] Multipart import pure sso
- [x] Convert-to-build after each import
- [x] 401 refresh once
- [x] Fail soft in registration
- [x] GUI + CLI
- [x] Tests + README + example config
