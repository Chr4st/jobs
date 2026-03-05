# CLAUDE.md — JarvisForJobs

Complete context for AI assistants working on this codebase.

## Project Overview

**JarvisForJobs** is a 100% local, free, API-first automation system that discovers internship roles on Greenhouse job boards, deterministically maps form fields to user profile data, fills applications via Playwright, and tracks everything in SQLite + Excel.

**Constraints**: No cloud LLMs (Ollama only), no CAPTCHA bypass, no paid APIs. macOS Apple Silicon (M-series).

**User**: Christ Xu — UMich student, USAMO/Putnam background. Primary resume: `resumes/CX2025_Q-21.pdf`.

## Environment Setup

```bash
source .venv/bin/activate          # ALWAYS use the project venv
pip install -r requirements.txt
python -m playwright install chromium
```

All CLI commands run from `jobbot/` (the directory containing `src/`).

## CLI Commands

```bash
# ── Primary Jarvis interface ──
python -m src.main gmi                           # one cycle: scan → map → fill (pause_at_submit)
python -m src.main gmi --dry-run                 # fill forms but do NOT submit
python -m src.main gmi --policy auto_if_safe     # actually submits
python -m src.main gmi --daemon                  # loop continuously
python -m src.main gmi --daemon --interval 20    # loop every 20 minutes
python -m src.main ngmi                          # stop the daemon (SIGTERM via PID file)

python -m src.main scan                          # scan boards only (no apply)
python -m src.main scan --max 200                # scan with custom limit
python -m src.main status                        # show all tracked applications
python -m src.main summary                       # daily summary

# ── Greenhouse-specific ──
python -m src.main apply-greenhouse --dry-run
python -m src.main apply-greenhouse --policy pause_at_submit
python -m src.main apply-greenhouse --policy auto_if_safe
python -m src.main gh-fill --app-id <app_id>
python -m src.main gh-fill --all-ready --policy pause_at_submit
python -m src.main scan-gh --max 200             # legacy alias for 'scan'

# ── Utilities ──
python -m src.main rebuild-excel                 # rebuild applications.xlsx from DB
python -m src.main resume --app-id <id>          # resume a NEEDS_HUMAN application
```

```bash
# ── Backend & Frontend ──
uvicorn src.server:app --reload                  # FastAPI (port 8000)
cd frontend && npm run dev                       # Next.js dashboard (port 3000)
```

```bash
# ── Tests ──
python -m pytest tests/ -v
python -m pytest tests/test_role_classify.py -v
```

## Architecture

### Data Flow

```
config/greenhouse_sources.yaml (45 company slugs)
        ↓
scanner.py ── boards-api.greenhouse.io ──→ jobs_seen (SQLite)
        ↓
schema_extract.py ── API-first, Playwright fallback ──→ form_schemas (SQLite)
        ↓
auto_map.py ── 6-level resolution hierarchy ──→ resolved field answers
        ↓
apply_playwright.py ── visible Chromium ──→ proofs/{filled,submitted,skipped}/
        ↓
applications (SQLite) ──→ applications.xlsx
```

### Field Resolution Hierarchy (auto_map.py)

1. **DB lookup** — `field_mappings` table (schema-specific → company → global)
2. **Profile match** — keys in `profile.yaml` / `jarvis_profile.yaml`
3. **Heuristic** — pattern-matched answers for common questions (GPA, work auth, sponsorship, EEO, etc.)
4. **Context inheritance** — `unknown_*` fields inherit from preceding labeled field
5. **Select/radio option matching** — deterministic best-option picker with EEO defaults (`_match_select_option`)
6. **LLM fallback** — Ollama local model (default: `mistral`); uncertain answers → `NEEDS_REVIEW`

When a required field cannot be resolved, `get_missing_required()` returns a descriptive reason:
- `"No profile key, heuristic, or LLM match for '<label>'"` (unmapped)
- `"LLM uncertain for '<label>' — needs human review"` (needs_review)

### Application Pipeline States

```
DISCOVERED → MAPPABLE → FILLING → FILLED_AWAITING_SUBMIT → APPLYING → APPLIED
                ↓                                                        ↓
          NEEDS_USER_DATA                                          OA_RECEIVED → OA_COMPLETED
                ↓                                                        ↓
          SKIPPED_UNANSWERABLE                              INTERVIEW_SCHEDULED → INTERVIEW_DONE
                                                                         ↓
                                                                   OFFER / REJECTED
                                                                         ↓
                                                              NEEDS_HUMAN / ERROR / WITHDRAWN
```

- `FILLED_AWAITING_SUBMIT` — default end state (`policy=pause_at_submit`). User reviews screenshot in `proofs/filled/` then triggers submit.
- `SKIPPED_UNANSWERABLE` — terminal state when `no_approval=true` and required fields can't be resolved. Skip reason stored in `applications.skip_reason`.

### Automation Mode (profile.yaml → `automation:`)

When `automation.no_approval: true`:
- Jobs with unresolvable required fields are auto-skipped to `SKIPPED_UNANSWERABLE` (no human pause)
- Descriptive skip reasons are aggregated and stored in the DB
- Daily cap (`max_applications_per_day`) enforced per cycle
- Rate limiting: random delay between `min_minutes_between_applications` and `max_minutes_between_applications`

### Daemon Process Control

- `gmi --daemon` writes PID to `data/.jarvis.pid`, loops every N minutes
- `ngmi` reads PID file, sends `SIGTERM` for graceful shutdown
- Signal handlers (`SIGTERM`, `SIGINT`) set a `running = False` flag for clean exit
- PID file cleaned up on stop

## Key Modules

| Module | Role |
|--------|------|
| `src/main.py` | Click CLI: `gmi`, `ngmi`, `scan`, `status`, `summary`, `gh-fill`, `resume`, `apply-greenhouse`, `run-once`, `run-daemon`, `scan-gh`, `rebuild-excel`. PID file management for daemon. |
| `src/agents/scout_applier.py` | Main orchestrator. `run_greenhouse_direct()` is the primary entry point: scan → extract schema → resolve fields → fill → submit. Reads `automation` config for no-approval mode, daily cap, rate limiting. |
| `src/agents/excel_tracker.py` | Polls SQLite events since last run, upserts Excel rows, tracks `data/.last_event_ts`. |
| `src/greenhouse/scanner.py` | Hits `boards-api.greenhouse.io` for each company in `config/greenhouse_sources.yaml`. Filters for intern/co-op roles. |
| `src/greenhouse/schema_extract.py` | API-first field extraction (`extract_schema_from_api`), Playwright fallback (`extract_form_schema`). Returns normalized JSON: `{field_key, label, type, options, required}`. |
| `src/greenhouse/apply_playwright.py` | Visible-browser form filler. Policies: `pause_at_submit` (screenshot only) or `auto_if_safe` (click submit). Proof dirs: `proofs/{filled,submitted,needs_human,skipped}/`. |
| `src/greenhouse/parse.py` | Parses Greenhouse job page HTML for metadata. |
| `src/mapping/auto_map.py` | `resolve_all_fields()` — runs the 6-level hierarchy. `get_missing_required()` — returns unresolved required fields with descriptive reasons. `_match_select_option()` — dropdown/radio matcher. |
| `src/llm/answer_gen.py` | Ollama-powered answer generator for open-ended questions. Disk cache at `data/llm_answer_cache.json`. `can_generate()` checks if Ollama is available. |
| `src/storage/db.py` | SQLite (`data/jobs.db`). Tables: `jobs_seen`, `applications`, `events`, `email_threads`, `form_schemas`, `field_mappings`. Idempotent migrations via `MIGRATIONS` list. `skip_reason` column for auto-skip tracking. |
| `src/storage/excel.py` | Upsert/rebuild `data/applications.xlsx`. `rebuild_excel()` drops and recreates from DB. |
| `src/storage/gsheet.py` | Google Sheets integration (optional). |
| `src/utils/role_classify.py` | Classifies job titles into 4 role families: `founding`, `fullstack`, `ml`, `quant`. `get_resume_path()` returns `resumes/CX2025_Q-21.pdf` (single resume). |
| `src/utils/scoring.py` | `compute_match_score()` — keyword-based relevance scoring. |
| `src/utils/dedupe.py` | `make_dedup_key()` — deterministic key from company+title+location. |
| `src/utils/sanitize.py` | `safe_filename()` — filesystem-safe names for proof files. |
| `src/utils/logging.py` | `setup_logging()` — consistent logger factory. |
| `src/server.py` | FastAPI REST + WebSocket backend. Engine runs in background thread. |
| `src/discovery/web_scraper.py` | Generic web scraping utilities. |
| `src/yc/discover.py` | YC Work at a Startup job discovery. `discover_greenhouse_direct()` scans boards. |
| `src/yc/apply_waas.py` | WAAS (Work at a Startup) application flow. |

## DB Schema

Tables: `jobs_seen`, `applications`, `events`, `email_threads`, `form_schemas`, `field_mappings`.

Key columns on `applications`: `stage`, `skip_reason`, `applied_at`, `notes`, `policy`, `proof_path`.

Migrations are additive `ALTER TABLE ... ADD COLUMN` in `MIGRATIONS` list in `db.py` — run idempotently on every `get_connection()` call (errors silently swallowed).

The `field_mappings` table has both `scope` (legacy) and `scope_type` (new alias); they always hold the same value. Valid scope values: `schema`, `company`, `global`.

## Configuration Files

| File | Purpose |
|------|---------|
| `profile.yaml` | Personal info for form filling (name, email, phone, LinkedIn, GitHub, education, work auth, why_snippets per role family, **automation settings**) |
| `config/greenhouse_sources.yaml` | 45 company slugs for the Greenhouse board scanner |
| `config/targets.yaml` | Role family patterns and scoring keywords |
| `.env` | `MATCH_SCORE_THRESHOLD`, `RUN_INTERVAL_MINUTES`, `LOG_LEVEL`, `OLLAMA_MODEL` |

### profile.yaml automation block

```yaml
automation:
  no_approval: true                    # Auto-skip unresolvable jobs (no human pause)
  max_applications_per_day: 50         # Daily cap (0 = unlimited)
  min_minutes_between_applications: 7  # Min delay between apps
  max_minutes_between_applications: 15 # Max delay between apps
```

## Role Families

4 families: `founding`, `fullstack`, `ml`, `quant`. All now use a single resume: `resumes/CX2025_Q-21.pdf`. Each family has a `why_snippet` in `profile.yaml` for cover letter personalization.

## Proof Files

| Directory | Contents |
|-----------|----------|
| `proofs/filled/` | Pre-submit screenshots (`policy=pause_at_submit`) |
| `proofs/submitted/` | Post-submission screenshots + HTML |
| `proofs/needs_human/` | Blocked submissions needing manual intervention |
| `proofs/skipped/` | Screenshots/HTML of jobs skipped due to unresolvable fields |

## API Endpoints (server.py — port 8000)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/applications` | List all applications |
| GET | `/api/applications/{id}` | Get single application |
| PATCH | `/api/applications/{id}/stage` | Update pipeline stage |
| GET | `/api/stats` | Aggregate stats |
| GET | `/api/stages` | List valid pipeline stages |
| GET | `/api/engine/status` | Engine running state |
| POST | `/api/engine/start` | Start engine in background thread |
| POST | `/api/engine/stop` | Stop engine |
| GET | `/api/schemas` | List form schemas |
| GET | `/api/schemas/{hash}` | Get specific schema |
| GET | `/api/mappings` | List field mappings (filter by field_key, scope) |
| POST | `/api/mappings` | Create field mapping |
| DELETE | `/api/mappings/{id}` | Remove field mapping |
| POST | `/api/jobs/{id}/fill` | Fill application (`pause_at_submit`) |
| POST | `/api/jobs/{id}/apply` | Submit application (`auto_if_safe`) |
| GET | `/api/jobs/{id}/missing-fields` | Missing required fields with reasons |
| WebSocket | `/ws` | Real-time engine events |

## Frontend (Next.js — port 3000)

Located in `frontend/`. Components in `frontend/app/components/`:
- `StatsBar.tsx` — aggregate pipeline stats
- `AppTable.tsx` — application list with stage badges
- `ActivityFeed.tsx` — real-time event stream via WebSocket
- `EngineControl.tsx` — start/stop engine controls
- `MappingsPanel.tsx` — field mapping CRUD

API client: `frontend/app/lib/api.ts`. Connects to FastAPI at `http://localhost:8000`.

## Milestone History

| Milestone | Description |
|-----------|-------------|
| M1 | Single resume + profile consolidation |
| M2 | Jarvis CLI commands (gmi/ngmi/scan/status) |
| M3 | Greenhouse API-first schema extraction |
| M4 | Deterministic field mapping with 6-level hierarchy |
| M5 | CLAUDE.md docs + DB schema updates |
| M6 | Server, frontend dashboard, and discovery updates |
| Post-M6 | No-approval automation, skip reasons, daily cap, rate limiting, PID daemon control, PROOFS_SKIPPED |
