"""
arc.update
-----------------
`arc update` — git-pulls the latest code for the arc kernel and every
ENABLED plugin (--ff-only, never a plain pull), skipping anything with
uncommitted changes, a detached HEAD, or no configured upstream tracking
branch. Deliberately narrow, same posture as arc.deploy: opt-in tooling
the operator runs explicitly, never something arc.boot() or any other
core command depends on.

What this does NOT do, on purpose:
  * Sync dependencies for you beyond a single `uv sync --all-packages`
    after anything actually changed — cli.py's own update() does that.
  * Run `arc psqldb migrate` — a schema change pulled in from git needs
    the SAME reviewed plan/confirm flow as any other migration; silently
    auto-running it would skip that review entirely.
  * Restart the running process — new code on disk doesn't change what
    an already-imported Python module does until something restarts it.

Both are cli.py's job to remind the operator about, never this module's
job to do automatically.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UpdateOutcome:
    name: str
    # updated | up_to_date | skipped_dirty | skipped_detached |
    # skipped_no_remote | skipped_no_git | failed
    status: str
    detail: str = ""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _is_dirty(repo: Path) -> bool:
    return bool(_git(["status", "--porcelain"], repo).stdout.strip())


def _on_a_branch(repo: Path) -> bool:
    # Fails (non-zero) in a detached HEAD — there's no branch name to ask
    # a remote-tracking question about at all in that state.
    return _git(["symbolic-ref", "-q", "HEAD"], repo).returncode == 0


def _has_upstream(repo: Path) -> bool:
    # @{u} resolves only when the current branch has a configured
    # upstream (git clone sets this up automatically) — this is
    # deliberately never a check against a specific remote NAME (this
    # project's own repos mix "origin" and "upstream"); whatever the
    # branch is already tracking is what `git pull` with no args uses.
    return _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo).returncode == 0


def update_one(name: str, repo: Path) -> UpdateOutcome:
    """One repo's worth of the whole command — never forces anything,
    never touches a dirty tree, never guesses past diverged history.
    `repo` not existing at all, or not being a git repo, is reported the
    same as any other skip rather than raising — arc update's own loop
    keeps going either way."""
    if not repo.is_dir() or not (repo / ".git").is_dir():
        return UpdateOutcome(name, "skipped_no_git", "not a git repository")
    if _is_dirty(repo):
        return UpdateOutcome(name, "skipped_dirty", "uncommitted changes — commit or stash first")
    if not _on_a_branch(repo):
        return UpdateOutcome(name, "skipped_detached", "detached HEAD, not on a branch")
    if not _has_upstream(repo):
        return UpdateOutcome(name, "skipped_no_remote", "no remote configured for this branch")

    result = _git(["pull", "--ff-only"], repo)
    if result.returncode != 0:
        # --ff-only's own failure message (diverged history, network
        # error, ...) is already a clean one-liner in practice; last line
        # of whichever stream has content is a safe, simple way to surface
        # it without dumping git's full, sometimes-noisy raw output.
        raw = (result.stderr or result.stdout).strip()
        detail = raw.splitlines()[-1] if raw else "git pull failed"
        return UpdateOutcome(name, "failed", detail)

    output = result.stdout.strip()
    if "Already up to date." in output:
        return UpdateOutcome(name, "up_to_date")
    summary = output.splitlines()[-1] if output else ""
    return UpdateOutcome(name, "updated", summary)
