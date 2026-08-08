"""arc.update.update_one — the safety-critical part of `arc update`: never
pull over a dirty tree, never guess past diverged history, never touch
something that isn't even a git repo. Exercised against REAL local git
repos (a bare "remote" + a working clone), not mocks — this is exactly
the class of logic where a mocked subprocess call could hide a real bug
in how git's own exit codes/output are interpreted."""

from __future__ import annotations

import subprocess
from pathlib import Path

from arc.update import update_one


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_remote_and_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (remote, seed, clone) — `seed` is a second working copy used
    to push new commits to `remote` independently of `clone`, simulating
    "someone else pushed while I wasn't looking"."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "test@example.com"], seed)
    _git(["config", "user.name", "Test"], seed)
    (seed / "file.txt").write_text("v1\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "main"], seed)

    clone = tmp_path / "clone"
    _git(["clone", str(remote), str(clone)], tmp_path)
    _git(["config", "user.email", "test@example.com"], clone)
    _git(["config", "user.name", "Test"], clone)
    return remote, seed, clone


def test_a_clean_fast_forward_pulls_and_reports_updated(tmp_path):
    _remote, seed, clone = _make_remote_and_clone(tmp_path)
    (seed / "file.txt").write_text("v2\n")
    _git(["commit", "-am", "second"], seed)
    _git(["push", "origin", "main"], seed)

    outcome = update_one("demo", clone)

    assert outcome.status == "updated"
    assert (clone / "file.txt").read_text() == "v2\n"


def test_already_up_to_date_is_reported_without_erroring(tmp_path):
    _remote, _seed, clone = _make_remote_and_clone(tmp_path)

    outcome = update_one("demo", clone)

    assert outcome.status == "up_to_date"


def test_a_dirty_working_tree_is_skipped_never_pulled_over(tmp_path):
    """The single most important guarantee here: a real remote change
    exists (a plain pull WOULD succeed), but local uncommitted work must
    win — nothing gets touched."""
    _remote, seed, clone = _make_remote_and_clone(tmp_path)
    (seed / "file.txt").write_text("v2-from-remote\n")
    _git(["commit", "-am", "second"], seed)
    _git(["push", "origin", "main"], seed)

    (clone / "file.txt").write_text("v1-with-local-edits\n")  # uncommitted

    outcome = update_one("demo", clone)

    assert outcome.status == "skipped_dirty"
    assert (clone / "file.txt").read_text() == "v1-with-local-edits\n"


def test_no_remote_configured_is_skipped(tmp_path):
    repo = tmp_path / "standalone"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "file.txt").write_text("v1\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)

    outcome = update_one("demo", repo)

    assert outcome.status == "skipped_no_remote"


def test_detached_head_is_skipped(tmp_path):
    _remote, _seed, clone = _make_remote_and_clone(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(["checkout", sha], clone)

    outcome = update_one("demo", clone)

    assert outcome.status == "skipped_detached"


def test_diverged_history_fails_cleanly_instead_of_merging(tmp_path):
    """clone has a local commit the remote doesn't have, AND the remote has
    moved on too — a real fork in history. --ff-only must refuse rather
    than silently create a merge commit."""
    _remote, seed, clone = _make_remote_and_clone(tmp_path)

    (clone / "file.txt").write_text("local-only-change\n")
    _git(["commit", "-am", "local commit never pushed"], clone)

    (seed / "file.txt").write_text("remote-only-change\n")
    _git(["commit", "-am", "remote commit"], seed)
    _git(["push", "origin", "main"], seed)

    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()

    outcome = update_one("demo", clone)

    assert outcome.status == "failed"
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert after == before  # no merge commit was created


def test_not_a_git_repository_is_skipped(tmp_path):
    plain_dir = tmp_path / "not-git"
    plain_dir.mkdir()

    outcome = update_one("demo", plain_dir)

    assert outcome.status == "skipped_no_git"


def test_a_missing_directory_is_skipped_not_raised(tmp_path):
    outcome = update_one("demo", tmp_path / "does-not-exist")

    assert outcome.status == "skipped_no_git"
