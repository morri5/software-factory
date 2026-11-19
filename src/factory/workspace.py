"""The VM's working tree on the host: a fresh clone plus the session's saved patch.

The patch (`workspace_saved`) is the only way out of the VM. It is a binary diff
against the session's starting commit and includes untracked files, so the stage
documents under `thoughts/` arrive in it together with any code change.
"""

import os
import subprocess
import tempfile
from pathlib import Path

VM_REPO = "/lab/repo"  # where the repo lives inside the VM


class ArtifactError(Exception):
    """The artifact path is outside the tree or the file does not exist."""


def _git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def materialize(repo: str, base_commit: str | None, patch: bytes) -> Path:
    """Clone `repo` (a URL or path) at `base_commit` into a new temp dir, apply `patch`.

    The caller removes the directory. Without a base commit the default branch is used.
    """
    tree = Path(tempfile.mkdtemp(prefix="factory-")) / "repo"
    _git("clone", "-q", "--", repo, str(tree))
    if base_commit:
        _git("checkout", "-q", base_commit, cwd=tree)
    if patch:
        _git("apply", "--binary", "-", cwd=tree, stdin=patch)
    return tree


def read_artifact(tree: Path, rel_path: str) -> str:
    """The text of `rel_path` in `tree`; a path in the VM's repo is taken as relative."""
    if rel_path.startswith(VM_REPO + "/"):
        rel_path = rel_path[len(VM_REPO) + 1 :]
    root = tree.resolve()
    path = (root / rel_path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ArtifactError(f"artifact path outside the repository: {rel_path!r}")
    if not path.is_file():
        raise ArtifactError(f"artifact not in the workspace: {rel_path!r}")
    return path.read_text()
