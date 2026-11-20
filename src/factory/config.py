"""Settings from the environment (`make` loads `.env`; see `.env.example`)."""

import os
from pathlib import Path

from pydantic import BaseModel


class Limits(BaseModel):
    """Run limits for one stage, sent with the stage's message to the session API."""

    max_tool_calls: int
    run_timeout_s: int
    max_usd: float


API_URL = os.environ.get("LAB_API_URL", "http://127.0.0.1:8000")
REPO = os.environ.get("FACTORY_REPO", "morri5/factory-playground")
APPROVER = os.environ.get("FACTORY_APPROVER", "morri5")
COMMANDS_DIR = Path(os.environ.get("FACTORY_COMMANDS_DIR", "~/.claude/commands")).expanduser()
POLL_S = int(os.environ.get("FACTORY_POLL_S", "60"))
VAR_DIR = Path(os.environ.get("FACTORY_VAR_DIR", "var"))
# Langfuse from compose.yaml (`make langfuse-up`); its headless init creates these keys.
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3100")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-factory")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-factory")


def repo_url(repo: str = REPO) -> str:
    return f"https://github.com/{repo}.git"


def _limits(stage: str, calls: int, timeout_s: int, usd: float) -> Limits:
    """Defaults per stage; e.g. FACTORY_RESEARCH_MAX_USD=2 overrides one of them."""
    prefix = f"FACTORY_{stage.upper()}_"
    return Limits(
        max_tool_calls=int(os.environ.get(prefix + "MAX_TOOL_CALLS", calls)),
        run_timeout_s=int(os.environ.get(prefix + "RUN_TIMEOUT_S", timeout_s)),
        max_usd=float(os.environ.get(prefix + "MAX_USD", usd)),
    )


LIMITS = {
    "research": _limits("research", 300, 1800, 1.00),
    "plan": _limits("plan", 300, 1800, 1.00),
    "implement": _limits("implement", 500, 3600, 2.00),
    "validate": _limits("validate", 300, 1800, 1.00),
}
