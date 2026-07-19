# Build Liveness Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After chenyme convert-to-build, probe each account with a real `/v1/responses` "hi" call using grok2api CLI headers and the registration proxy; log + write `liveness_*.jsonl` without changing main account saves.

**Architecture:** Pure probe helpers in `build_liveness.py`; CLI profile from chenyme `GET /settings` `recommendedProviderBuild` (cached); wire into `add_token_to_chenyme_grok2api` after bot_flag; proxy from `last_expanded_proxy` / account bind map.

**Tech Stack:** Python 3.9+, curl_cffi requests, existing chenyme admin auth, unittest.

## Global Constraints

- Do not purge chenyme accounts; do not change `accounts_*.txt` success semantics.
- Probe failure must not make `add_token_to_chenyme_grok2api` return False.
- Default model `grok-4.5`, prompt `hi`, base `https://cli-chat-proxy.grok.com/v1`.
- CLI defaults: version `0.2.103`, UA `grok-shell/0.2.103 (linux; x86_64)`, identifier `grok-shell`, tokenAuth `xai-grok-cli`.
- Protocol aligned with chenyme/grok2api `cli.Adapter.applyHeaders`.

---

### Task 1: Config + pure probe module + unit tests

**Files:**
- Create: `build_liveness.py`
- Create: `tests/test_build_liveness.py`
- Modify: `app_config.py` (defaults + validation)
- Modify: `config.example.json`

**Produces:** `resolve_cli_profile`, `build_cli_headers`, `extract_output_text`, `classify_liveness`, `probe_build_responses`, `append_liveness_jsonl`, `liveness_path_for_accounts`

- [ ] Add config keys and implement `build_liveness.py` + tests (TDD for pure functions).
- [ ] Run: `python -m pytest tests/test_build_liveness.py -v`
- [ ] Commit

### Task 2: Proxy account bind

**Files:**
- Modify: `proxy_manager.py`
- Modify: `tests/test_proxy_manager.py` or `tests/test_build_liveness.py`

**Produces:** `remember_proxy_for_account(email)`, `get_proxy_for_account(email)`, `clear_account_proxy_bindings()`

- [ ] Store expanded proxy per email; get falls back to `last_expanded_proxy()`.
- [ ] Tests + commit

### Task 3: Chenyme integration

**Files:**
- Modify: `grok_register_ttk.py` (`chenyme_check_bot_flag` share export, probe hook, liveness file path)
- Modify: `tests/test_chenyme_grok2api.py`

**Produces:** `chenyme_fetch_build_cli_profile`, `chenyme_find_build_account`, `chenyme_probe_build_liveness`; call from `add_token_to_chenyme_grok2api`; set liveness path in `run_registration_common`

- [ ] Implement + mock tests
- [ ] Run full related tests
- [ ] Commit

### Task 4: Verify + push optional

- [ ] `python -m pytest tests/test_build_liveness.py tests/test_chenyme_grok2api.py tests/test_proxy_manager.py -q`
- [ ] Fix regressions

---

## Spec coverage

| Spec | Task |
|------|------|
| G1–G4 probe protocol | 1, 3 |
| G5 jsonl + no purge | 1, 3 |
| G6 CLI from settings | 3 |
| Proxy bind | 2 |
| Config keys | 1 |
