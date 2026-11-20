import subprocess
from pathlib import Path

import pytest

from factory import config, memory

DOCS = "repos/factory-playground/shared/factory"


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.rstrip()


@pytest.fixture
def thoughts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A thoughts repo with a bare remote, an unrelated modified file and an unrelated
    staged file, like Morris's own work in progress."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    repo = tmp_path / "thoughts"
    git("clone", "-q", str(remote), str(repo), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    (repo / "notes.md").write_text("v1\n")
    (repo / "other.md").write_text("v1\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / "notes.md").write_text("v2, not staged\n")
    (repo / "other.md").write_text("v2, staged\n")
    git("add", "other.md", cwd=repo)
    monkeypatch.setattr(config, "THOUGHTS_DIR", repo)
    return repo


def test_save_keeps_the_vm_filename(thoughts: Path) -> None:
    path = memory.save("plans", "/lab/repo/thoughts/shared/plans/2026-11-20-x.md", "# Plan\n")
    assert path == thoughts / DOCS / "plans" / "2026-11-20-x.md"
    assert path.read_text() == "# Plan\n"


def test_backup_commits_and_pushes_only_the_factory_folder(thoughts: Path) -> None:
    memory.save("research", "thoughts/shared/research/r.md", "# Research\n")
    memory.backup(7)
    assert git("log", "-1", "--format=%an %s", cwd=thoughts) == (
        "factory docs: factory documents for factory-playground#7"
    )
    assert git("show", "--name-only", "--format=", "HEAD", cwd=thoughts) == f"{DOCS}/research/r.md"
    assert git("status", "--short", cwd=thoughts).splitlines() == [" M notes.md", "M  other.md"]
    assert git("rev-parse", "origin/main", cwd=thoughts) == git("rev-parse", "HEAD", cwd=thoughts)
    remote = thoughts.parent / "remote.git"
    assert git("rev-parse", "main", cwd=remote) == git("rev-parse", "HEAD", cwd=thoughts)


def test_backup_without_a_change_makes_no_commit(thoughts: Path) -> None:
    memory.save("research", "r.md", "# Research\n")
    memory.backup(7)
    head = git("rev-parse", "HEAD", cwd=thoughts)
    memory.backup(7)
    assert git("rev-parse", "HEAD", cwd=thoughts) == head


def test_backup_with_nothing_saved_makes_no_commit(thoughts: Path) -> None:
    head = git("rev-parse", "HEAD", cwd=thoughts)
    memory.backup(7)
    assert git("rev-parse", "HEAD", cwd=thoughts) == head


def test_a_rejected_push_keeps_the_commit_and_does_not_raise(
    thoughts: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hook = thoughts.parent / "remote.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    memory.save("validation", "v.md", "# Validation\n")
    memory.backup(7)
    committed = git("show", "--name-only", "--format=", "HEAD", cwd=thoughts)
    assert committed == f"{DOCS}/validation/v.md"
    assert git("rev-parse", "origin/main", cwd=thoughts) != git("rev-parse", "HEAD", cwd=thoughts)
    assert "thoughts backup incomplete" in caplog.text
    assert git("status", "--short", cwd=thoughts).splitlines() == [" M notes.md", "M  other.md"]


def test_backup_skips_the_repo_hooks(thoughts: Path) -> None:
    hook = thoughts / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\ngit add -A && git commit -q -m sync\n")
    hook.chmod(0o755)
    memory.save("research", "r.md", "# Research\n")
    memory.backup(7)
    assert git("log", "-1", "--format=%s", cwd=thoughts) == (
        "docs: factory documents for factory-playground#7"
    )
    assert git("status", "--short", cwd=thoughts).splitlines() == [" M notes.md", "M  other.md"]


def test_seed_files_only_for_allowlisted_repos(
    thoughts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory.save("research", "r.md", "# Research\n")
    memory.save("plans", "p.md", "# Plan\n")
    memory.save("validation", "v.md", "# Validation\n")
    (thoughts / DOCS / "plans" / "notes.txt").write_text("not a document\n")
    monkeypatch.setattr(config, "SEED_REPOS", set())
    assert memory.seed_files("morri5/factory-playground") == {}
    monkeypatch.setattr(config, "SEED_REPOS", {"morri5/factory-playground"})
    assert memory.seed_files("morri5/factory-playground") == {
        "thoughts/shared/factory/plans/p.md": "# Plan\n",
        "thoughts/shared/factory/research/r.md": "# Research\n",
        "thoughts/shared/factory/validation/v.md": "# Validation\n",
    }
    assert memory.seed_files("morri5/other") == {}  # not allowlisted, no folder either
