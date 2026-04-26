#!/bin/bash
# AI Financial Advisor — one-shot setup.
# Usage: cp .env.example .env  # fill in credentials
#        bash setup.sh
set -euo pipefail

ADVISOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV_PATH:-$HOME/venv}"
ENV_FILE="$ADVISOR_DIR/.env"

# ── helpers ────────────────────────────────────────────────────────────────────
ok()   { echo "[OK]  $*"; }
info() { echo "[..] $*"; }
fail() { echo "[ERR] $*" >&2; exit 1; }

# Read a key from .env (strips quotes, Windows line endings)
env_get() {
    grep -E "^${1}=" "$ENV_FILE" 2>/dev/null \
        | head -1 | cut -d= -f2- \
        | tr -d '"'"'" | tr -d '\r'
}

# Parse HH:MM -> "M H" for cron
time_to_cron() {
    local t="$1"
    local h m
    h=$(echo "$t" | cut -d: -f1)
    m=$(echo "$t" | cut -d: -f2)
    printf '%d %d' "$((10#$m))" "$((10#$h))"
}

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   AI Financial Advisor — Setup               ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Check .env ──────────────────────────────────────────────────────────────
info "Checking .env..."
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ADVISOR_DIR/.env.example" ]; then
        fail ".env not found. Run:  cp $ADVISOR_DIR/.env.example $ADVISOR_DIR/.env  then fill in credentials."
    else
        fail ".env not found."
    fi
fi
ok ".env found"

# ── 2. Check Python ────────────────────────────────────────────────────────────
info "Checking Python..."
PYTHON=$(command -v python3 || command -v python || true)
[ -z "$PYTHON" ] && fail "python3 not found. Install Python 3.8+."
PY_OK=$($PYTHON -c 'import sys; print(int(sys.version_info >= (3,8)))')
[ "$PY_OK" != "1" ] && fail "Python 3.8+ required (found: $($PYTHON --version))."
ok "Python: $($PYTHON --version)"

# ── 3. Create virtualenv ───────────────────────────────────────────────────────
info "Setting up virtualenv at $VENV..."
if [ ! -d "$VENV" ]; then
    $PYTHON -m venv "$VENV"
    ok "Virtualenv created"
else
    ok "Virtualenv already exists"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── 4. Install Python dependencies ────────────────────────────────────────────
info "Installing Python dependencies (this may take a few minutes)..."
pip install -q --upgrade pip
pip install -q -r "$ADVISOR_DIR/requirements.txt"
ok "Dependencies installed"

# ── 5. Database setup ─────────────────────────────────────────────────────────
info "Setting up PostgreSQL database..."
DB_HOST=$(env_get DB_HOST);  DB_HOST="${DB_HOST:-localhost}"
DB_PORT=$(env_get DB_PORT);  DB_PORT="${DB_PORT:-5432}"
DB_NAME=$(env_get DB_NAME);  DB_NAME="${DB_NAME:-advisor}"
DB_USER=$(env_get DB_USER);  DB_USER="${DB_USER:-advisor_user}"
DB_PASS=$(env_get DB_PASSWORD)

# Try to create user + database as the postgres superuser.
# This is a best-effort step — if it fails (no sudo / already exists) we continue.
if command -v sudo &>/dev/null && sudo -n -u postgres psql -c '\q' &>/dev/null 2>&1; then
    sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" \
        | grep -q 1 || sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
    sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" \
        | grep -q 1 || sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null || true
    ok "Database user and database ensured"
else
    info "Cannot run as postgres superuser — assuming DB user/database already exist."
fi

# Apply schema (idempotent — all tables use IF NOT EXISTS)
PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    -f "$ADVISOR_DIR/db/schema.sql" -q \
    && ok "Schema applied" \
    || fail "Failed to apply schema. Check DB credentials in .env."

# ── 6. Create log directory ────────────────────────────────────────────────────
mkdir -p "$ADVISOR_DIR/logs" "$ADVISOR_DIR/data/universe_cache"
ok "Directories ready"

# ── 7. Install cron jobs ───────────────────────────────────────────────────────
info "Installing cron schedule..."
SCHEDULE_DAYS=$(env_get SCHEDULE_DAYS);        SCHEDULE_DAYS="${SCHEDULE_DAYS:-1-5}"
SCHEDULE_DAILY=$(env_get SCHEDULE_DAILY_TIME); SCHEDULE_DAILY="${SCHEDULE_DAILY:-04:00}"
SCHEDULE_BRIEF=$(env_get SCHEDULE_BRIEF_TIME); SCHEDULE_BRIEF="${SCHEDULE_BRIEF:-07:00}"
SCHEDULE_MIDDAY=$(env_get SCHEDULE_MIDDAY_TIME); SCHEDULE_MIDDAY="${SCHEDULE_MIDDAY:-12:00}"

DAILY_CRON=$(time_to_cron "$SCHEDULE_DAILY")
BRIEF_CRON=$(time_to_cron "$SCHEDULE_BRIEF")
MIDDAY_CRON=$(time_to_cron "$SCHEDULE_MIDDAY")

CRON_DAILY="$DAILY_CRON * * $SCHEDULE_DAYS $ADVISOR_DIR/run_daily.sh >> $ADVISOR_DIR/logs/daily.log 2>&1"
CRON_BRIEF="$BRIEF_CRON * * $SCHEDULE_DAYS $ADVISOR_DIR/send_brief.sh >> $ADVISOR_DIR/logs/brief.log 2>&1"
CRON_MIDDAY="$MIDDAY_CRON * * $SCHEDULE_DAYS $ADVISOR_DIR/run_midday.sh >> $ADVISOR_DIR/logs/midday.log 2>&1"
CRON_BACKTEST="0 5 * * 0 source $VENV/bin/activate && cd $ADVISOR_DIR && python pipeline/backtester.py >> $ADVISOR_DIR/logs/backtest.log 2>&1"

# Remove old advisor cron lines, add fresh ones
( crontab -l 2>/dev/null | grep -v "$ADVISOR_DIR" || true
  echo "$CRON_DAILY"
  echo "$CRON_BRIEF"
  echo "$CRON_MIDDAY"
  echo "$CRON_BACKTEST"
) | crontab -

ok "Cron installed:"
echo "     Daily pipeline : $SCHEDULE_DAILY UTC on days $SCHEDULE_DAYS"
echo "     Morning brief  : $SCHEDULE_BRIEF UTC on days $SCHEDULE_DAYS"
echo "     Midday check   : $SCHEDULE_MIDDAY UTC on days $SCHEDULE_DAYS"
echo "     Backtest       : Sunday 05:00 UTC"

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Setup complete!                            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo "  1. Run the universe loader (first time):  SKIP_UNIVERSE=0 $ADVISOR_DIR/run_daily.sh"
echo "  2. Or trigger manually right now:         bash $ADVISOR_DIR/run_daily.sh"
echo ""
echo "To reconfigure the schedule or markets, edit .env and re-run setup.sh."
echo ""
