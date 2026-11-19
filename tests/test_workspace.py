import subprocess
from pathlib import Path

import pytest

from factory.workspace import ArtifactError, materialize, read_artifact


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "app.py").write_text("print('hi')\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=repo)
    return repo


def snapshot(origin: Path, tmp_path: Path) -> tuple[str, bytes]:
    """A workspace patch made like the VM makes it: untracked files included, binary."""
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    base = git("rev-parse", "HEAD", cwd=work)
    doc = work / "thoughts" / "shared" / "research" / "doc.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Research\n\nFindings.\n")
    (work / "blob.bin").write_bytes(bytes(range(256)))
    (work / "app.py").write_text("print('hello')\n")
    git("-c", "core.excludesFile=/dev/null", "add", "-A", cwd=work)  # no global ignores
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary", base], cwd=work, check=True, capture_output=True
    ).stdout
    return base, patch


def test_materialize_applies_the_binary_patch(origin: Path, tmp_path: Path) -> None:
    base, patch = snapshot(origin, tmp_path)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "later",
        cwd=origin)  # fmt: skip
    tree = materialize(str(origin), base, patch)
    assert git("rev-parse", "HEAD", cwd=tree) == base
    assert read_artifact(tree, "thoughts/shared/research/doc.md") == "# Research\n\nFindings.\n"
    assert (tree / "blob.bin").read_bytes() == bytes(range(256))
    assert (tree / "app.py").read_text() == "print('hello')\n"


def test_read_artifact_accepts_the_vm_path(origin: Path, tmp_path: Path) -> None:
    base, patch = snapshot(origin, tmp_path)
    tree = materialize(str(origin), base, patch)
    assert read_artifact(tree, "/lab/repo/thoughts/shared/research/doc.md").startswith("# Research")


OUTSIDE = ["../secret.md", "thoughts/../../x", "/etc/passwd", ".", "nope.md"]


@pytest.mark.parametrize("path", OUTSIDE)
def test_read_artifact_refuses(origin: Path, tmp_path: Path, path: str) -> None:
    (tmp_path / "secret.md").write_text("no")
    tree = materialize(str(origin), None, b"")
    with pytest.raises(ArtifactError):
        read_artifact(tree, path)
