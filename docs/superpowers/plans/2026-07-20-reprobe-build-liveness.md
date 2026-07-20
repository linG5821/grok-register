# Reprobe Build Liveness Implementation Plan

> **For agentic workers:** Implement inline in this session.

**Goal:** CLI script that takes an email list, matches chenyme full export, runs 5-proxy → OAuth refresh → 5-proxy liveness cycle per account.

**Architecture:** `build_token_refresh.py` for official refresh; extend `build_liveness` with rotation helper; `scripts/reprobe_build_liveness.py` orchestrates.

**Tech Stack:** Python 3.9+, curl_cffi, existing build_liveness + proxy_manager.

## Global Constraints

- Email file input; full export once + local match
- max_proxies=5 per phase; first live stops
- Default no chenyme writeback; no purge
- Exit 2 on login/export failure

### Tasks

1. `build_token_refresh.py` + tests
2. Email load + export index + single-account cycle + tests
3. `scripts/reprobe_build_liveness.py` CLI
4. Run tests + commit
