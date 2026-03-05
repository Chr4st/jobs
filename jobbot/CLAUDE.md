# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment Setup

Always use the project virtualenv — system Python lacks the dependencies:

```bash
source .venv/bin/activate
# or prefix commands:
.venv/bin/python -m src.main <command>
```

Install/update dependencies:
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Common Commands (Jarvis CLI)

All CLI commands run from `jobbot/` (the directory containing `src/`):

```bash
# ── Jarvis commands (primary interface) ──
python -m src.main gmi                           # one cycle: scan → map → fill (pause_at_submit)
python -m src.main gmi --dry-run                 # fill forms but do NOT submit
python -m src.main gmi --policy auto_if_safe     # actually submits
python -m src.main gmi --daemon                  # loop continuously
python -m src.main gmi --daemon --interval 20    # loop every 20 minutes
python -m src.main ngmi                          # stop the daemon

python -m src.main scan                          # scan boards only (no apply)
python -m src.main scan --max 200                # scan with custom limit
python -m src.main status                        # show all tracked applications

# ── Legacy commands (still work) ──
python -m src.main apply-greenhouse --dry-run
python -m src.main apply-greenhouse --policy pause_at_submit
python -m src.main apply-greenhouse --policy auto_if_safe

# Fill a specific application or all MAPPABLE ones
python -m src.main gh-fill --app-id <app_id>
python -m src.main gh-fill --all-ready --policy pause_at_submit

# Status / reporting
python -m src.main summary         # daily summary
python -m src.main rebuild-excel   # rebuild applications.xlsx from DB

# Scan Greenhouse boards only (no apply)
python -m src.main scan-gh --max 200

# Resume a blocked application after manual CAPTCHA/OTP completion
python -m src.main resume --app-id <id>
```

FastAPI backend (port 8000):
```bash
uvicorn src.server:app --reload
```

Next.js dashboard (port 3000):
```bash
cd frontend && npm run dev
```

Tests:
```bash
python -m pytest tests/ -v
python -m pytest tests/test_role_classify.py -v   # single file
```

## Architecture

### Data Flow

```
greenhouse_sources.yaml → scanner.py (API) → jobs_seen (SQLite)
                                                  ↓
                           schema_extract.py (API first, Playwright fallback)
                                                  ↓
                           auto_map.py → field answers (deterministic > LLM)
                                                  ↓
                           apply_playwright.py → proofs/
                                                  ↓
                           applications (SQLite) → applications.xlsx
```

### Field Resolution Hierarchy (auto_map.py)

1. **DB lookup** — `field_mappings` table (schema-specific → company → global)
2. **Profile match** — keys in `profile.yaml` / `jarvis_profile.yaml`
3. **Heuristic** — pattern-matched answers for common questions (GPA, work auth, etc.)
4. **Context inheritance** — `unknown_*` fields inherit from preceding field
5. **Select/radio option matching** — deterministic best-option picker with EEO defaults
6. **LLM fallback** — Ollama (local); uncertain answers → `NEEDS_REVIEW`

### Key Modules

| Module | Role |
|--------|------|
| `src/agents/scout_applier.py` | Main orchestrator: scan → extract → map → fill. `run_greenhouse_direct()` is the primary entry point. |
| `src/greenhouse/scanner.py` | Hits Greenhouse board API (`boards-api.greenhouse.io`) for each company in `config/greenhouse_sources.yaml`. |
| `src/greenhouse/schema_extract.py` | Playwright headless visit → extracts form fields (label, type, options, required) into a normalized JSON schema. |
| `src/greenhouse/apply_playwright.py` | Playwright visible-browser form filler. Key param: `policy` (`pause_at_submit` or `auto_if_safe`). |
| `src/mapping/auto_map.py` | Resolves field values via hierarchy: DB (schema_hash → company → global) → profile.yaml → heuristic answers. |
| `src/storage/db.py` | Single SQLite file (`data/jobs.db`). All schema + migrations live here. `get_connection()` is idempotent. |
| `src/storage/excel.py` | Upsert/rebuild `data/applications.xlsx`. `rebuild_excel()` drops and recreates from DB. |
| `src/server.py` | FastAPI: REST + WebSocket. Engine runs in a background thread; events broadcast via `sync_broadcast()`. |
| `src/agents/excel_tracker.py` | Polls SQLite events since last run, upserts Excel rows, tracks `data/.last_event_ts`. |

### Application Pipeline States

```
DISCOVERED → MAPPABLE → FILLING → FILLED_AWAITING_SUBMIT
                ↓                         ↓
          NEEDS_USER_DATA              APPLYING → APPLIED
                                          ↓
                                     NEEDS_HUMAN / ERROR
```

`FILLED_AWAITING_SUBMIT` is the default end state (policy=`pause_at_submit`). The user reviews the filled screenshot in `proofs/filled/` and then triggers submit manually or via `gh-fill --policy auto_if_safe`.

### DB Schema

Tables: `jobs_seen`, `applications`, `events`, `email_threads`, `form_schemas`, `field_mappings`.

Migrations are additive `ALTER TABLE ... ADD COLUMN` statements in `MIGRATIONS` list in `db.py` — they run idempotently on every `get_connection()` call (errors are silently swallowed).

The `field_mappings` table has both `scope` (legacy) and `scope_type` (new alias); they always hold the same value. Valid scope values: `schema`, `company`, `global`.

### Configuration Files

- `profile.yaml` — personal info used to fill forms (copy from `profile.yaml.example`)
- `config/greenhouse_sources.yaml` — company slug list for the scanner
- `config/targets.yaml` — role family patterns and scoring keywords
- `.env` — `MATCH_SCORE_THRESHOLD`, `RUN_INTERVAL_MINUTES`, `LOG_LEVEL`

### Role Families

Exactly 4 families: `founding`, `fullstack`, `ml`, `quant`. Each maps to a resume in `resumes/<family>.pdf`. Roles that don't match any family are skipped entirely.

### Proof Files

- `proofs/filled/` — pre-submit screenshots (policy=`pause_at_submit`)
- `proofs/submitted/` — post-submission screenshots + HTML
- `proofs/needs_human/` — blocked submissions needing manual intervention

### API Endpoints (server.py)

Key endpoints added recently:
- `POST /api/jobs/{id}/fill` — triggers fill with `pause_at_submit`
- `POST /api/jobs/{id}/apply` — triggers fill with `auto_if_safe`
- `GET /api/jobs/{id}/missing-fields` — returns required fields with no mapped answer
- `GET /api/applications`, `PATCH /api/applications/{id}/stage` — pipeline management
- `GET /api/mappings`, `POST /api/mappings` — field mapping CRUD
- `WebSocket /ws` — real-time engine events to the dashboard

### Frontend (Next.js)

Located in `frontend/`. Components in `frontend/app/components/`: `StatsBar`, `AppTable`, `ActivityFeed`, `EngineControl`, `MappingsPanel`. Connects to FastAPI at `http://localhost:8000`.
