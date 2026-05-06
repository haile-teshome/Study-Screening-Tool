#!/bin/bash
# SYNERGY Annotation Tool — setup script
# Run once: bash setup.sh

set -e

echo ""
echo "===  Annotation Tool Setup ==="
echo ""

# ── 1. Python check ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3.9+ from https://python3.org and re-run."
  exit 1
fi

PY=$(python3 --version 2>&1)
echo "Python: $PY"

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
echo "Virtual environment activated."

# ── 3. Dependencies ───────────────────────────────────────────────────────────
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ── 4. Directories ────────────────────────────────────────────────────────────
mkdir -p papers static

# ── 5. .env file (only if it doesn't exist) ───────────────────────────────────
if [ ! -f ".env" ]; then
  ACCESS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
  ADMIN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

  cat > .env <<EOF
# Shared with annotators — paste this into the login screen if ACCESS_TOKEN is set
ACCESS_TOKEN=$ACCESS

# Keep this private — used for /admin dashboard
ADMIN_TOKEN=$ADMIN

# Flask session signing key — keep private
FLASK_SECRET=$SECRET
EOF

  echo ""
  echo "Generated .env with tokens:"
  echo "  ACCESS_TOKEN = $ACCESS  (share with annotators)"
  echo "  ADMIN_TOKEN  = $ADMIN  (keep private)"
  echo ""
else
  echo ".env already exists, skipping token generation."
fi

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo "=== Setup complete ==="
echo ""
echo "To start the app:"
echo ""
echo "  source venv/bin/activate"
echo "  source .env && python app.py"
echo ""
echo "Then open: http://localhost:5050"
echo "Admin:     http://localhost:5050/admin?admin_token=\$(grep ADMIN_TOKEN .env | cut -d= -f2)"
echo ""
