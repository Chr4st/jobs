#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo "  Jobbot — macOS Installation Script"
echo "  (Apple Silicon / M-series)"
echo "============================================"

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# ── 1. Homebrew ──────────────────────────────────────────
echo ""
echo "[1/7] Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
else
    echo "Homebrew found: $(brew --version | head -1)"
fi

# ── 2. System dependencies ──────────────────────────────
echo ""
echo "[2/7] Installing system dependencies..."
brew install python@3.12 node 2>/dev/null || true
echo "Python: $(python3 --version)"
echo "Node: $(node --version)"

# ── 3. Ollama ────────────────────────────────────────────
echo ""
echo "[3/7] Installing Ollama..."
if ! command -v ollama &>/dev/null; then
    brew install ollama
else
    echo "Ollama already installed: $(ollama --version 2>/dev/null || echo 'installed')"
fi

# ── 4. Python virtual environment ────────────────────────
echo ""
echo "[4/7] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Python packages installed."

# ── 5. Playwright browsers ──────────────────────────────
echo ""
echo "[5/7] Installing Playwright browsers..."
python -m playwright install chromium
python -m playwright install-deps chromium 2>/dev/null || true
echo "Playwright browsers installed."

# ── 6. Setup config files ───────────────────────────────
echo ""
echo "[6/7] Setting up configuration..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
else
    echo ".env already exists"
fi

if [ ! -f "profile.yaml" ]; then
    cp profile.yaml.example profile.yaml
    echo "Created profile.yaml from profile.yaml.example"
    echo ""
    echo "  *** IMPORTANT: Edit profile.yaml with your personal details ***"
else
    echo "profile.yaml already exists"
fi

# Create data directories
mkdir -p data proofs/submitted proofs/needs_human

# ── 7. Ollama model ─────────────────────────────────────
echo ""
echo "[7/7] Setting up Ollama model..."

# Start ollama server in background if not running
if ! pgrep -x "ollama" > /dev/null 2>&1; then
    echo "Starting Ollama server..."
    ollama serve &>/dev/null &
    sleep 3
fi

# Pull the default model (mistral is fast on Apple Silicon)
echo "Pulling Mistral model (this may take a few minutes on first run)..."
ollama pull mistral 2>/dev/null || echo "Warning: Could not pull model. Run 'ollama pull mistral' manually."

echo ""
echo "============================================"
echo "  Installation Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit profile.yaml with your details"
echo "  2. Drop your resumes into resumes/"
echo "     - resumes/founding.pdf"
echo "     - resumes/fullstack.pdf"
echo "     - resumes/ml.pdf"
echo "     - resumes/quant.pdf"
echo "  3. Run a smoke test:"
echo "     source .venv/bin/activate"
echo "     python -m src.main run-once --dry-run"
echo ""
