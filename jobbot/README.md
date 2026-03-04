# Jobbot — Local Job Application Automation

A 100% free, 100% local job application automation system for macOS (Apple Silicon).

**What it does:**
- Discovers internship/job postings from YC pages
- Filters to Greenhouse-hosted applications only
- Classifies roles into 4 families: Founding Engineer, Full Stack, AI/ML, Quant
- Scores matches deterministically
- Auto-fills and submits Greenhouse applications via Playwright
- Tracks everything in a local Excel spreadsheet
- Stops on CAPTCHA/OTP/login — saves screenshot for manual completion

**Stack:** Python, Playwright, SQLite, openpyxl, Ollama (optional LLM), BeautifulSoup

---

## Quick Start (macOS)

### 1. Clone and install

```bash
git clone <this-repo> jobbot
cd jobbot
chmod +x scripts/*.sh
./scripts/install_macos.sh
```

### 2. Configure your profile

```bash
cp profile.yaml.example profile.yaml
# Edit with your details:
nano profile.yaml
```

### 3. Add your resumes

Drop your PDFs into `resumes/`:
```
resumes/founding.pdf    # For founding engineer roles
resumes/fullstack.pdf   # For full-stack / SWE roles
resumes/ml.pdf          # For AI/ML roles
resumes/quant.pdf       # For quantitative trading roles
```

### 4. Run

```bash
source .venv/bin/activate

# Dry run (fills forms but does NOT submit):
python -m src.main run-once --dry-run

# Real run:
python -m src.main run-once

# Daemon mode (runs every 30 min):
python -m src.main run-daemon

# After completing a manual step (CAPTCHA, etc.):
python -m src.main resume --app-id <id>

# View summary:
python -m src.main summary

# View all tracked applications:
python -m src.main status
```

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   CLI (main.py)                  │
├──────────────────────────────────────────────────┤
│                                                  │
│  ┌─────────────────┐    ┌──────────────────┐     │
│  │  ScoutApplier    │    │  ExcelTracker    │     │
│  │  Agent           │──▶ │  Agent           │     │
│  └────────┬─────────┘    └────────┬─────────┘     │
│           │                       │               │
│  ┌────────▼─────────┐    ┌───────▼──────────┐    │
│  │ YC Discover      │    │ Excel Upsert     │    │
│  │ GH Parse         │    │ (openpyxl)       │    │
│  │ GH Apply (PW)    │    └──────────────────┘    │
│  │ Role Classify    │                             │
│  │ Scoring          │                             │
│  └────────┬─────────┘                             │
│           │                                       │
│  ┌────────▼─────────────────────────────────┐    │
│  │           SQLite (event bus + dedupe)     │    │
│  └──────────────────────────────────────────┘    │
└──────────────────────────────────────────────────┘
```

### Two-Agent Pipeline

1. **ScoutApplier** — discovers → parses → classifies → scores → applies
2. **ExcelTracker** — reads SQLite events → upserts Excel rows → computes follow-ups

### Status Flow

```
DISCOVERED → READY_TO_APPLY → APPLYING → SUBMITTED → INTERVIEW
                                  ↓                      ↓
                            NEEDS_HUMAN              REJECTED
```

---

## Configuration

### `.env`
```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=mistral
RUN_INTERVAL_MINUTES=30
MATCH_SCORE_THRESHOLD=0.6
DRY_RUN=false
LOG_LEVEL=INFO
```

### `profile.yaml`
Your personal information used to fill application forms. See `profile.yaml.example`.

### `config/targets.yaml`
Role family definitions, keyword patterns, scoring weights. Usually no changes needed.

### `config/openclaw.yaml`
OpenClaw agent runtime configuration. The system works without OpenClaw via the built-in local orchestrator in `main.py`.

---

## Compliance & Safety

- **No CAPTCHA bypass.** If a CAPTCHA, OTP, login, or phone verification appears, automation stops immediately. A screenshot and URL are saved to `proofs/needs_human/`, and the application is marked `NEEDS_HUMAN`.
- **Manual resume.** After you complete the human step in your browser, run `python -m src.main resume --app-id <id>` to continue.
- **Only Greenhouse.** Non-Greenhouse application targets are ignored.
- **Only 4 role families.** Roles that don't match founding/fullstack/ml/quant are skipped.

---

## Output Files

| Path | Description |
|---|---|
| `data/jobs.db` | SQLite database with jobs, applications, events |
| `data/applications.xlsx` | Excel tracker with all applications |
| `proofs/submitted/` | Screenshots + HTML of successful submissions |
| `proofs/needs_human/` | Screenshots + HTML of blocked submissions |

---

## Tests

```bash
source .venv/bin/activate
pip install pytest
python -m pytest tests/ -v
```

---

## OpenClaw Integration

OpenClaw config is at `config/openclaw.yaml`. It defines the two agents and their tool permissions. If OpenClaw is not installed or is unstable, the system falls back to the local orchestrator in `src/main.py` which runs both agents sequentially.

---

## Scoring

Match scores are computed deterministically (no LLM required):

| Factor | Weight | Description |
|---|---|---|
| Title match | 40% | How well the job title matches the role family patterns |
| Keyword density | 30% | Relevant keywords found in the job description |
| Location match | 15% | Proximity to your preferred location |
| Recency | 15% | Newer postings score higher |

Default threshold: 0.6 (configurable via `MATCH_SCORE_THRESHOLD` in `.env`)

---

## Troubleshooting

**Playwright fails to launch:**
```bash
python -m playwright install chromium
python -m playwright install-deps chromium
```

**Ollama not running:**
```bash
ollama serve &
ollama pull mistral
```

**Resume not found:**
Ensure your PDFs are in `resumes/` with the exact names: `founding.pdf`, `fullstack.pdf`, `ml.pdf`, `quant.pdf`.

**Application stuck in NEEDS_HUMAN:**
1. Check `proofs/needs_human/` for the screenshot
2. Open the URL in your browser and complete the manual step
3. Run `python -m src.main resume --app-id <id>`
