"""The factory's memory across issues: stage documents kept in the thoughts repo.

Each stage document is saved under `repos/<target-repo>/shared/factory/<folder>/`
of the thoughts repo, and that path alone is committed and pushed after each run.
"""

import contextlib
import logging
import subprocess
from pathlib import Path

from factory import config
from factory.stages import Stage
from factory.workspace import _git

log = logging.getLogger(__name__)

FOLDERS: dict[Stage, str] = {
    "research": "research",
    "plan": "plans",
    "implement": "plans",
    "validate": "validation",
}


def save(folder: str, vm_path: str, document: str) -> Path:
    """Write the document under the repo's factory folder, keeping the VM's filename."""
    path = config.factory_docs() / folder / Path(vm_path).name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document)
    return path


def _pull_rebase(root: Path) -> None:
    """Rebase on the remote; a rebase that stops is aborted, so the repo stays as it was."""
    try:
        _git("pull", "--rebase", "-q", cwd=root)
    except subprocess.CalledProcessError:
        with contextlib.suppress(subprocess.CalledProcessError):  # no rebase in progress
            _git("rebase", "--abort", cwd=root)
        raise


def backup(issue: int) -> None:
    """Commit and push only the factory folder; any other change stays untouched.

    Never raises: a failed push leaves the commit for the next push or sync.
    """
    root = config.THOUGHTS_DIR
    rel = str(config.factory_docs().relative_to(root))
    name = config.REPO.split("/")[1]
    if not (root / rel).is_dir():  # nothing saved yet
        return
    try:
        _git("add", "--", rel, cwd=root)
        if not _git("diff", "--cached", "--name-only", "--", rel, cwd=root):
            return
        # No hooks: a post-commit sync hook would commit unrelated changes as factory.
        _git("-c", "user.name=factory", "-c", "user.email=factory@localhost",
             "-c", "core.hooksPath=/dev/null", "commit", "-q",
             "-m", f"docs: factory documents for {name}#{issue}", "--", rel, cwd=root)  # fmt: skip
        try:
            _git("push", "-q", cwd=root)
        except subprocess.CalledProcessError:
            _pull_rebase(root)  # fails cleanly on a dirty tree
            _git("push", "-q", cwd=root)
    except (subprocess.CalledProcessError, OSError):
        log.warning("issue #%d: thoughts backup incomplete", issue, exc_info=True)
