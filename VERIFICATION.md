# Current verification — OpenAI migration, 2026-11-04

The earlier authentication blockers below are **resolved**. Current evidence:

| Check | Result |
| --- | --- |
| Final mocked suite, isolated SQLite | **54 passed**, 4.73 seconds |
| Final mocked suite, isolated PostgreSQL | **54 passed**, 5.39 seconds |
| Corrected development SQLAlchemy connection | **PASS**, user `diassuleimenov`, database `castle_agents` |
| Additive development migration | **PASS**, revision `0001_versioned_backend` |
| Original data preservation | **13 tasks / 7 messages**, all original fields verified unchanged |
| Development `alembic check` | **No new upgrade operations detected** |
| Selected OpenAI model metadata | **Accessible**: `gpt-6-luna`, `gpt-6.1-terra` |
| Single authorized live generation | **PASS**, `gpt-6-luna` structured router output, cap 128 tokens, no retries |
| Syntax compilation and `pip check` | **PASS** |

No executor-generation comparison or full live scenario was run. Both dependency-choice paths pass with mocked OpenAI. No remaining credential blocker is known. All `.env` credentials were preserved; only provider/model selection variables were changed. Three unfinished legacy task records were preserved without starting the development scheduler, to respect the one-request live limit. The temporary PostgreSQL test cluster is separate from development data.

See [OPENAI_MIGRATION_REPORT.md](OPENAI_MIGRATION_REPORT.md) for the complete migration, pricing, setup and API report. The prior-provider evidence below is historical, not the current deployment status.

---

# Historical pre-migration verification — 2026-11-04

## Initial state

The repository had FastAPI endpoints, message persistence and mention detection, a Claude JSON router, an asyncio worker semaphore, cancellation, mutable task revisions, and an unused TaskVersion/backfill foundation. Execution was simulated. Dependencies, version choices, durable events, a test suite and project documentation were absent. The original `test_db.py` performed database writes on import.

## Implemented backend

TaskVersion is now authoritative. The backend has exact-version dependencies, transactional version decisions, retained historical executions, execution-token stale-result guards, configurable asynchronous Claude execution, persisted routing, a single-process scheduler, cancellation/recovery, ordered chat events, polling APIs, retention maintenance, additive Alembic migration and documented setup. No frontend or market-data integration was added.

The source structure and endpoint contract are documented in README.md. Development secrets were neither printed nor modified. No commit or push was performed.

## Executed checks

| Check | Result |
| --- | --- |
| `python -m pytest -q`, temporary SQLite databases | **40 passed**, 4.60 seconds |
| Same suite with isolated PostgreSQL and one random schema per test | **40 passed**, 4.77 seconds |
| Alice/Bob/Jack HTTP scenario with mocked Claude | **Passed for keep_original and use_updated**, on both database engines |
| Fresh and legacy migration, including preservation of old results | **Passed**, including legacy adoption on PostgreSQL |
| Repeating Alembic initialization | **Passed** |
| `alembic check` against migrated isolated PostgreSQL | **No new upgrade operations detected** |
| Actual Uvicorn HTTP startup and requests | **Passed**: health, DB health, OpenAPI, ordinary-message acknowledgement/persistence, event polling |
| Syntax compilation | **Passed** |

Tests mock all Claude calls. They cover independent overlap, capacity limits, cancellations before and during execution, exact input selection, both dependency choices, invalid references/ownership/cycles, simultaneous revisions, late-result rejection, historical preservation, restart recovery, publication/cursor idempotency, retention, provider retries/timeouts, malformed routing and orphan-worker recovery.

The PostgreSQL integration cluster was created separately under `/tmp/castle-backend-pg` on port 55439, with database `castle_test_backend`. No destructive tests were run against the configured development database. The real server smoke test used this isolated database and did not call Claude.

## Historical live acceptance status

A real-Claude smoke attempt was made against the isolated PostgreSQL database. The first routing request was rejected with `AuthenticationError`; no successful live model response or live end-to-end result is claimed. The user offered to replace the key. The backend's persisted failure path handled the rejection.

A user-requested retry on 2026-11-04 again reached Claude but returned HTTP 401 at the first routing request. The development database retry also still returned SQLSTATE `28000`. The temporary PostgreSQL server was stopped after the check; no development data or secrets were modified.

The configured development PostgreSQL connection separately failed with `InvalidAuthorizationSpecificationError`, SQLSTATE `28000`. Its data was not changed and its migration was not run. The additive migration was successfully exercised on isolated PostgreSQL instead.

Previously required external setup (resolved by the OpenAI migration):

1. Save a valid Anthropic key in local `.env` and verify model access.
2. Correct the development PostgreSQL credentials/role configuration in local `.env` or PostgreSQL as appropriate.
3. Run `python init_db.py`, then the opt-in live smoke scenario while the normal API server is stopped.

```bash
source .venv/bin/activate
python init_db.py
python -m scripts.smoke_demo --live --choice keep_original
# Optional verification of the second path with real Claude:
python -m scripts.smoke_demo --live --choice use_updated
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

## Limits

This is a single-process demonstration backend, not a distributed queue. It has no identity authentication or external research tools. Provider work can repeat after a crash, while published answers remain deduplicated. Readiness scans the graph and retention runs offline. README.md documents these trade-offs and production follow-ups.
