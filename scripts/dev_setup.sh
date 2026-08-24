#!/usr/bin/env bash
set -euo pipefail

# Bootstrap senza-agent for local development.
#
# Creates a venv at .venv/, installs the Senza SDK wheel (built from the
# Senza runtime repo pinned in runtime.lock), then installs senza-agent
# in editable mode.
#
# Usage:
#   ./scripts/dev_setup.sh
#   VENV=/path/to/venv ./scripts/dev_setup.sh
#   SENZA_REPO=/path/to/Senza ./scripts/dev_setup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV="${VENV:-$REPO_ROOT/.venv}"
SENZA_REPO="${SENZA_REPO:-$(cd "$REPO_ROOT/../Senza" && pwd)}"
LOCK_FILE="$REPO_ROOT/runtime.lock"

# ── venv ─────────────────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Creating venv at $VENV ..."
    for candidate in \
        "/opt/homebrew/bin/python3.12" \
        "/usr/local/bin/python3.12" \
        "/opt/homebrew/bin/python3.13" \
        "/usr/local/bin/python3.13" \
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12" \
        python3; do
        [ -x "$candidate" ] && break
    done
    "$candidate" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
fi
PYTHON="$VENV/bin/python"

# ── Build Senza wheel ────────────────────────────────────────────────
SHA=$(cat "$LOCK_FILE")
echo "==> Senza runtime pin: $SHA"

if [ ! -d "$SENZA_REPO" ]; then
    echo "ERROR: Senza repo not found at $SENZA_REPO"
    echo "       Set SENZA_REPO=/path/to/Senza ./scripts/dev_setup.sh"
    exit 1
fi

# Build the wheel from the Senza repo at the pinned commit.
(
    cd "$SENZA_REPO"
    git fetch --quiet origin 2>/dev/null || true
    # Try checkout; if it fails (detached HEAD etc.), just build from current.
    git checkout "$SHA" 2>/dev/null || true
    ./scripts/build_wheel.sh
)

WHEEL=$(ls -t "$SENZA_REPO"/dist/senza_sdk*.whl "$SENZA_REPO"/dist/senza*.whl 2>/dev/null | head -1)
if [ -z "$WHEEL" ]; then
    echo "ERROR: No wheel found in $SENZA_REPO/dist/"
    exit 1
fi

# ── Install ──────────────────────────────────────────────────────────
echo ""
echo "==> Installing Senza SDK wheel ..."
"$PYTHON" -m pip install "$WHEEL" --force-reinstall

echo ""
echo "==> Installing senza-agent (editable) ..."
"$PYTHON" -m pip install -e "$REPO_ROOT"

echo ""
echo "==> Setup complete."
echo "    Venv:  $VENV"
echo "    Wheel: $WHEEL"
echo ""
echo "Next steps:"
echo "    source $VENV/bin/activate"
echo "    senza-agent --nostop                    # interactive CLI"
echo "    senza-agent --web                       # web dashboard"
echo "    cd desktop && npm start                 # desktop app"
echo "    python -m pytest tests/ -v              # run tests"
