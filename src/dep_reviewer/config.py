from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAIL_ON = ("fail", "needs-human", "never")
MERGE_METHODS = ("squash", "merge", "rebase")


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
    fail_on: str
    auto_merge: bool
    auto_merge_method: str
    allowed_authors: tuple[str, ...]
    pr_number: int | None
    repository: str
    api_url: str
    server_url: str
    workspace: Path
    event_path: Path | None
    runner_temp: Path
    event_name: str
    graphql_url: str

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

        fail_on = inp("FAIL_ON", "fail").lower() or "fail"
        if fail_on not in FAIL_ON:
            raise ConfigError(f"fail-on must be one of {', '.join(FAIL_ON)}, got {fail_on!r}")
        merge_method = inp("AUTO_MERGE_METHOD", "squash").lower() or "squash"
        if merge_method not in MERGE_METHODS:
            raise ConfigError(f"auto-merge-method must be one of {', '.join(MERGE_METHODS)}, got {merge_method!r}")
        api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

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
            label_prefix=inp("LABEL_PREFIX", "dependabot-review:") or "dependabot-review:",
            fail_on=fail_on,
            auto_merge=_bool(inp("AUTO_MERGE", "false"), "auto-merge"),
            auto_merge_method=merge_method,
            allowed_authors=tuple(
                a.strip().lower() for a in inp("ALLOWED_AUTHORS", "dependabot[bot]").split(",") if a.strip()
            ),
            pr_number=_int(pr_raw, "pr-number", 1) if pr_raw else None,
            repository=repository,
            api_url=api_url,
            server_url=env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
            workspace=Path(env.get("GITHUB_WORKSPACE") or os.getcwd()),
            event_path=Path(event_path) if event_path else None,
            runner_temp=Path(env.get("RUNNER_TEMP") or "/tmp"),
            event_name=env.get("GITHUB_EVENT_NAME", ""),
            graphql_url=env.get("GITHUB_GRAPHQL_URL") or f"{api_url}/graphql",
        )


def should_fail(verdict: str, fail_on: str) -> bool:
    """fail-on=fail: only a failing verdict fails the check (the default).
    fail-on=needs-human: anything short of a pass fails the check.
    fail-on=never: the check always succeeds; act on the `verdict` output instead."""
    if fail_on == "never":
        return False
    if fail_on == "needs-human":
        return verdict != "pass"
    return verdict == "fail"
