from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anthropic

from .claude_review import ReviewError, ReviewOutcome, run_review
from .config import Config, ConfigError, should_fail
from .github_api import GitHub, GitHubError
from .prompts import SYSTEM_PROMPT, CheckRun, build_user_message
from .repo_tools import RepoTools
from .report import COMMENT_MARKER, LABEL_COLORS, RenderContext, render_comment

LOG_TAIL_LINES = 150


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def append_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def resolve_pr_number(cfg: Config) -> int:
    if cfg.pr_number:
        return cfg.pr_number
    if cfg.event_path and cfg.event_path.exists():
        event = json.loads(cfg.event_path.read_text(encoding="utf-8"))
        pr = event.get("pull_request") or {}
        if pr.get("number"):
            return int(pr["number"])
    raise ConfigError("no pull request in the triggering event; set the pr-number input")


def run_checks(cfg: Config, log_path: Path) -> CheckRun:
    print(f"::group::Running test-command: {cfg.test_command}")
    start = time.monotonic()
    timed_out = False
    exit_code: int | None
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            ["bash", "-o", "pipefail", "-c", cfg.test_command],
            cwd=cfg.workspace,
            stdout=log,
            stderr=subprocess.STDOUT,
            # The test command does not need the Anthropic key.
            env={k: v for k, v in os.environ.items() if not k.startswith("INPUT_")},
        )
        try:
            exit_code = proc.wait(timeout=cfg.test_timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            exit_code, timed_out = None, True
    duration = time.monotonic() - start
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines))
    print("::endgroup::")
    run = CheckRun(
        command=cfg.test_command,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=duration,
        log_line_count=len(lines),
        log_tail="\n".join(lines[-LOG_TAIL_LINES:]),
    )
    print(f"test-command {run.describe()}")
    return run


def main() -> int:
    try:
        cfg = Config.from_env()
        number = resolve_pr_number(cfg)
    except ConfigError as e:
        print(f"::error::{e}")
        return 1

    gh = GitHub(cfg.github_token, cfg.api_url, cfg.repository)
    pr = gh.get_pr(number)
    author = pr["user"]["login"].lower()
    if author not in cfg.allowed_authors:
        print(f"PR #{number} is by {author}, not one of {', '.join(cfg.allowed_authors)}; skipping.")
        set_output("skipped", "true")
        return 0
    set_output("skipped", "false")

    head_sha, base_sha = pr["head"]["sha"], pr["base"]["sha"]
    files = gh.list_pr_files(number)
    print(f"Reviewing PR #{number}: {pr['title']} ({len(files)} files changed)")

    test_log = cfg.runner_temp / f"dep-review-test-{number}.log"
    test = run_checks(cfg, test_log) if cfg.test_command else None

    tools = RepoTools(cfg.workspace, base_sha, head_sha, test_log if test else None)
    tools.ensure_commits()

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    try:
        outcome = run_review(
            client,
            model=cfg.model,
            effort=cfg.effort,
            max_turns=cfg.max_turns,
            system=SYSTEM_PROMPT,
            user_message=build_user_message(pr, files, test),
            tools=tools,
        )
    except ReviewError as e:
        print(f"::warning::Claude review incomplete: {e}")
        outcome = ReviewOutcome(review=None, unavailable_reason=f"The review could not be completed: {e}")
    except anthropic.APIError as e:
        # Report API failures on the PR rather than leaving it silently unreviewed.
        print(f"::warning::Anthropic API error: {e}")
        outcome = ReviewOutcome(review=None, unavailable_reason=f"The Claude API returned an error: {type(e).__name__}.")

    u = outcome.usage
    if u:
        print(
            f"Tokens: input={u.get('input_tokens', 0)} cache_read={u.get('cache_read_input_tokens', 0)} "
            f"cache_write={u.get('cache_creation_input_tokens', 0)} output={u.get('output_tokens', 0)}"
        )

    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{cfg.server_url}/{cfg.repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = render_comment(outcome, RenderContext(cfg.server_url, cfg.repository, head_sha, cfg.model, test, run_url))
    append_summary(body)

    try:
        if cfg.post_comment:
            print(f"Comment: {gh.upsert_comment(number, body, COMMENT_MARKER)}")
        if cfg.add_labels:
            label = f"{cfg.label_prefix}{outcome.risk}"
            gh.ensure_label(label, LABEL_COLORS[outcome.risk], f"Claude dependency review: {outcome.risk} risk")
            gh.set_exclusive_label(number, [l["name"] for l in pr.get("labels", [])], cfg.label_prefix, label)
    except GitHubError as e:
        if e.status == 403:
            print(
                "::error::GitHub refused to post the comment or label (403). Give the workflow "
                "`pull-requests: write` and `issues: write` permissions."
            )
        raise

    set_output("risk", outcome.risk)
    set_output("confidence", outcome.confidence)

    if should_fail(outcome.risk, cfg.fail_on_risk):
        print(f"::error::Dependency update risk is {outcome.risk} (fail-on-risk: {cfg.fail_on_risk})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
