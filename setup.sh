#!/usr/bin/env bash
# Arc setup script — stands up a project end to end.
#   ./setup.sh [project_dir] [app_name]
set -euo pipefail

PROJECT_DIR="${1:-.}"
APP_NAME="${2:-arc-app}"
ARC_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Arc setup"
echo "    project: ${PROJECT_DIR}"
echo "    app:     ${APP_NAME}"

# 1. Python check (3.12+).
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "error: python3.12 not found"; exit 1
fi
PYV="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "==> Python ${PYV}"

# 2. Virtualenv.
mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

command -v python3.12 >/dev/null 2>&1 || {
  echo "ERROR: python3.12 not found"
  exit 1
}

if [ ! -d .venv ]; then
  echo "==> Creating .venv with Python 3.12"
  python3.12 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# 3. Install Arc (editable) + deps.
echo "==> Installing Arc and dependencies"
pip install --quiet --upgrade pip
pip install --quiet -e "${ARC_SRC}"

# 4. Scaffold the project (arc.toml + arc.lock with db/api/http).
if [ ! -f arc.lock ]; then
  echo "==> Initialising project"
  arc init . --name "${APP_NAME}"
else
  echo "==> arc.lock already present — skipping init"
fi

# 5. .env template.
if [ ! -f .env.example ]; then
  cat > .env.example <<'ENV'
# Copy to .env and fill in. asyncpg only.
DATABASE_URL=postgresql+asyncpg://arcuser:arcpass@localhost:5432/arcdb
ENV
fi

# 6. Resolve the graph as a sanity check.
echo "==> Resolving plugin graph"
arc doctor || true

echo ""
echo "==> Done. Next:"
echo "    cd ${PROJECT_DIR} && source .venv/bin/activate"
echo "    export DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db"
echo "    arc new-plugin finance     # scaffold a plugin"
echo "    arc db migrate             # create tables"
echo "    arc run                    # serve on :8000  (GET /health)"
