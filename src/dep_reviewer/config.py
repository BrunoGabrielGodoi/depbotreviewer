from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unknown": 1}
FAIL_THRESHOLDS = ("none", "medium", "high")


class ConfigError(Exception):
    pass


def _bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no", ""):
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _int(value: str, name: str, minimum: int) -> int:
    try:
        n = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    if n < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return n


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    github_token: str
    model: str
    effort: str
    max_turns: int
    test_command: str
    test_timeout_minutes: int
    post_comment: bool
    add_labels: bool
    label_prefix: str
    fail_on_risk: str
    allowed_authors: tuple[str, ...]
    pr_number: int | None
    repository: str
    api_url: str
    server_url: str
    workspace: Path
    event_path: Path | None
    runner_temp: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        def inp(name: str, default: str = "") -> str:
            return env.get(f"INPUT_{name}", default).strip()

        api_key = inp("ANTHROPIC_API_KEY")
        if not api_key:
            raise ConfigError(
                "anthropic-api-key is empty. Dependabot-triggered runs only see Dependabot "
                "secrets: add ANTHROPIC_API_KEY under Settings > Secrets and variables > Dependabot."
            )
        token = inp("GITHUB_TOKEN")
        if not token:
            raise ConfigError("github-token is empty")

        effort = inp("EFFORT", "high").lower()
        if effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {effort!r}")

        fail_on = inp("FAIL_ON_RISK", "none").lower()
        if fail_on not in FAIL_THRESHOLDS:
            raise ConfigError(f"fail-on-risk must be one of {', '.join(FAIL_THRESHOLDS)}, got {fail_on!r}")

        pr_raw = inp("PR_NUMBER")
        repository = env.get("GITHUB_REPOSITORY", "")
        if not repository:
            raise ConfigError("GITHUB_REPOSITORY is not set; run this inside GitHub Actions")

        event_path = env.get("GITHUB_EVENT_PATH")
        return cls(
            anthropic_api_key=api_key,
            github_token=token,
            model=inp("MODEL", "claude-opus-5-5") or "claude-opus-5-5",
            effort=effort,
            max_turns=_int(inp("MAX_TURNS", "30") or "30", "max-turns", 1),
            test_command=inp("TEST_COMMAND"),
            test_timeout_minutes=_int(inp("TEST_TIMEOUT_MINUTES", "20") or "20", "test-timeout-minutes", 1),
            post_comment=_bool(inp("POST_COMMENT", "true"), "post-comment"),
            add_labels=_bool(inp("ADD_LABELS", "true"), "add-labels"),
            label_prefix=inp("LABEL_PREFIX", "dependabot-risk:") or "dependabot-risk:",
            fail_on_risk=fail_on,
            allowed_authors=tuple(
                a.strip().lower() for a in inp("ALLOWED_AUTHORS", "dependabot[bot]").split(",") if a.strip()
            ),
            pr_number=_int(pr_raw, "pr-number", 1) if pr_raw else None,
            repository=repository,
            api_url=env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            server_url=env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
            workspace=Path(env.get("GITHUB_WORKSPACE") or os.getcwd()),
            event_path=Path(event_path) if event_path else None,
            runner_temp=Path(env.get("RUNNER_TEMP") or "/tmp"),
        )


def should_fail(risk: str, threshold: str) -> bool:
    if threshold == "none":
        return False
    return RISK_ORDER.get(risk, 1) >= RISK_ORDER[threshold]
