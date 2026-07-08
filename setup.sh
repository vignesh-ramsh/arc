#!/usr/bin/env bash
#
# arc-server-setup.sh
# ---------------------------------------------------------------------------
# Provisions a fresh Linux server with:
#   1. Python 3.12 (uses the system one if already present, else uv-managed)
#   2. uv, installed system-wide (symlinked into /usr/local/bin)
#   3. The `arc` CLI, built from the kernel repo, also system-wide
#
# After this script finishes, ANY user who logs into this server can run
# `arc init myproject` immediately — no per-user setup, no shell profile
# edits, no activating anything first.
#
# Usage:
#   sudo ARC_KERNEL_REPO=git@github.com:you/kernel.git ./arc-server-setup.sh
#   sudo ARC_KERNEL_REPO=/some/local/path ./arc-server-setup.sh
#
# Safe to re-run: pulls latest instead of re-cloning, re-installs editable
# in place, overwrites symlinks idempotently.
# ---------------------------------------------------------------------------

set -euo pipefail

ARC_KERNEL_REPO="${ARC_KERNEL_REPO:-}"
ARC_KERNEL_BRANCH="${ARC_KERNEL_BRANCH:-main}"
ARC_INSTALL_DIR="${ARC_INSTALL_DIR:-/opt/arc}"
PYTHON_VERSION="3.12"

if [ -t 1 ]; then
  C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
else
  C_GREEN=''; C_YELLOW=''; C_RED=''; C_RESET=''
fi
log()  { echo -e "${C_GREEN}[arc-setup]${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}[arc-setup]${C_RESET} $1"; }
die()  { echo -e "${C_RED}[arc-setup] ERROR:${C_RESET} $1" >&2; exit 1; }

# ln -sf fails with "are the same file" if the target already resolves to the
# exact same path as the source (e.g. a prior run already symlinked it, or it
# was pre-installed there some other way). That failure is otherwise fatal
# under `set -e`, so every symlink in this script goes through this guard.
safe_symlink() {
  local src="$1" dst="$2"
  if [ -e "$dst" ] && [ "$(readlink -f "$src")" = "$(readlink -f "$dst")" ]; then
    return 0
  fi
  ln -sf "$src" "$dst"
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  die "Run as root: sudo ARC_KERNEL_REPO=<url> $0"
fi

if [ -z "$ARC_KERNEL_REPO" ]; then
  die "ARC_KERNEL_REPO is not set.\n  Usage: sudo ARC_KERNEL_REPO=<git-url-or-path> $0"
fi

log "Target: $ARC_KERNEL_REPO (branch: $ARC_KERNEL_BRANCH)"
log "Install directory: $ARC_INSTALL_DIR"

# ---------------------------------------------------------------------------
# git (install via apt if missing and apt is available)
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing git via apt..."
    apt-get update -qq && apt-get install -y -qq git
  else
    die "git not found and apt-get isn't available. Install git manually first."
  fi
fi
log "git: $(git --version)"

# ---------------------------------------------------------------------------
# uv — installed once, then symlinked into /usr/local/bin so every user
# (not just root, and not just this login shell) can reach it.
# ---------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer puts it under $HOME/.local/bin — pick that up for this
  # script's own PATH before continuing.
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv installed but not found on PATH — check the installer output above."

safe_symlink "$(command -v uv)" /usr/local/bin/uv
log "uv: $(uv --version), symlinked to /usr/local/bin/uv"

# ---------------------------------------------------------------------------
# Python 3.12 — prefer whatever's already on the system; only fall back to
# uv's own managed download if nothing suitable is found. This matters on
# locked-down corporate networks where GitHub release-asset downloads may
# be blocked but apt/system Python is already present (e.g. Ubuntu 24.04+
# ships 3.12 out of the box).
# ---------------------------------------------------------------------------
if command -v python3.12 >/dev/null 2>&1; then
  log "Found system python3.12: $(python3.12 --version)"
elif uv python find "$PYTHON_VERSION" >/dev/null 2>&1; then
  log "uv already has a managed python $PYTHON_VERSION"
else
  log "No python3.12 found — asking uv to install one..."
  if ! uv python install "$PYTHON_VERSION"; then
    die "Could not install python $PYTHON_VERSION via uv (likely a network/firewall issue). \
On Ubuntu, try: apt-get install -y python${PYTHON_VERSION} (add the deadsnakes PPA on older releases), then re-run this script."
  fi
fi

# ---------------------------------------------------------------------------
# Kernel source — clone once, pull on re-run
# ---------------------------------------------------------------------------
mkdir -p "$ARC_INSTALL_DIR"
KERNEL_DIR="$ARC_INSTALL_DIR/kernel"

if [ -d "$KERNEL_DIR/.git" ]; then
  log "Kernel already cloned at $KERNEL_DIR — pulling latest on $ARC_KERNEL_BRANCH..."
  git -C "$KERNEL_DIR" fetch origin "$ARC_KERNEL_BRANCH"
  git -C "$KERNEL_DIR" checkout "$ARC_KERNEL_BRANCH"
  git -C "$KERNEL_DIR" pull origin "$ARC_KERNEL_BRANCH"
else
  log "Cloning kernel into $KERNEL_DIR..."
  git clone --branch "$ARC_KERNEL_BRANCH" "$ARC_KERNEL_REPO" "$KERNEL_DIR"
fi

# ---------------------------------------------------------------------------
# arc CLI — one shared venv at a fixed path, editable install, symlinked
# into /usr/local/bin so it's reachable exactly like any other system tool.
# ---------------------------------------------------------------------------
VENV_DIR="$ARC_INSTALL_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
  log "Creating venv at $VENV_DIR..."
  uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
fi

log "Installing arc (editable) into the shared venv..."
uv pip install --python "$VENV_DIR/bin/python" --editable "$KERNEL_DIR"

safe_symlink "$VENV_DIR/bin/arc" /usr/local/bin/arc

# Every user needs read+execute on this tree to actually run the venv's
# python and load the editable-installed source files.
chmod -R a+rX "$ARC_INSTALL_DIR"

log "arc: $(arc --help >/dev/null 2>&1 && echo OK), symlinked to /usr/local/bin/arc"

# ---------------------------------------------------------------------------
# ARC_KERNEL_REPO, system-wide — so no user ever needs --kernel-repo by hand
# ---------------------------------------------------------------------------
cat > /etc/profile.d/arc.sh << EOF
export ARC_KERNEL_REPO="$ARC_KERNEL_REPO"
EOF
chmod 644 /etc/profile.d/arc.sh

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo
log "Setup complete."
echo
echo "  Kernel source : $KERNEL_DIR"
echo "  Shared venv   : $VENV_DIR"
echo "  arc binary    : /usr/local/bin/arc -> $VENV_DIR/bin/arc"
echo "  uv binary     : /usr/local/bin/uv"
echo "  ARC_KERNEL_REPO set system-wide via /etc/profile.d/arc.sh"
echo
warn "Open a NEW shell (or 'source /etc/profile.d/arc.sh') for ARC_KERNEL_REPO to take effect."
echo
echo "Then, from any directory, any user can run:"
echo "  arc init myproject"
