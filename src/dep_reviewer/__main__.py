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
from .gate import PASS, decide, describe_checkout, pr_number_from_queue_ref
from .github_api import GitHub, GitHubError
from .prompts import SYSTEM_PROMPT, CheckRun, build_user_message
from .repo_tools import RepoTools
from .report import COMMENT_MARKER, LABEL_COLORS, LABEL_DESCRIPTIONS, RenderContext, render_comment

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


def load_event(cfg: Config) -> dict:
    if cfg.event_path and cfg.event_path.exists():
        return json.loads(cfg.event_path.read_text(encoding="utf-8"))
    return {}


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
            # The test command does not need the Anthropic key or the GitHub token.
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


def run_merge_queue(cfg: Config, gh: GitHub, event: dict) -> int:
    """In a merge queue, re-run only the build and tests on the exact commit that will land.

    Claude already reviewed the PR when it was opened or updated, so it is not re-run here.
    """
    ref = (event.get("merge_group") or {}).get("head_ref", "")
    number = pr_number_from_queue_ref(ref)
    if number is not None:
        author = gh.get_pr(number)["user"]["login"].lower()
        if author not in cfg.allowed_authors:
            print(f"Merge queue entry for PR #{number} by {author}; not a dependency PR, skipping.")
            set_output("skipped", "true")
            return 0
    set_output("skipped", "false")
    if not cfg.test_command:
        print("::warning::Merge queue run with no test-command: nothing to verify.")
        set_output("verdict", "needs-human")
        return 1 if cfg.fail_on == "needs-human" else 0
    test = run_checks(cfg, cfg.runner_temp / "dep-review-merge-queue.log")
    verdict = PASS if test.passed else "fail"
    set_output("verdict", verdict)
    append_summary(f"Merge queue check: `{test.command}` {test.describe()} on {ref or 'the queued commit'}.")
    return 1 if should_fail(verdict, cfg.fail_on) else 0


def review_pr(cfg: Config, gh: GitHub, event: dict) -> int:
    number = cfg.pr_number or (event.get("pull_request") or {}).get("number")
    if not number:
        raise ConfigError("no pull request in the triggering event; set the pr-number input")
    pr = gh.get_pr(int(number))
    author = pr["user"]["login"].lower()
    if author not in cfg.allowed_authors:
        print(f"PR #{number} is by {author}, not one of {', '.join(cfg.allowed_authors)}; skipping.")
        set_output("skipped", "true")
        return 0
    set_output("skipped", "false")

    head_sha, base_sha, base_ref = pr["head"]["sha"], pr["base"]["sha"], pr["base"]["ref"]
    files = gh.list_pr_files(number)
    print(f"Reviewing PR #{number}: {pr['title']} ({len(files)} files changed)")

    test_log = cfg.runner_temp / f"dep-review-test-{number}.log"
    tools = RepoTools(cfg.workspace, base_sha, head_sha)
    tools.ensure_commits()
    checkout = describe_checkout(cfg.workspace, head_sha, base_ref)
    print(f"Workspace: {checkout.description}")
    if not checkout.is_merge_result:
        print(
            "::warning::The workspace is not the PR's merge result, so this can't verify what will land. "
            "Use actions/checkout without `ref:` on the pull_request event."
        )

    test = run_checks(cfg, test_log) if cfg.test_command else None
    if test is None:
        print("::warning::No test-command set: the verdict can't be a pass without a build and tests.")
    tools.test_log = test_log if test else None

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    try:
        outcome = run_review(
            client,
            model=cfg.model,
            effort=cfg.effort,
            max_turns=cfg.max_turns,
            system=SYSTEM_PROMPT,
            user_message=build_user_message(pr, files, test, checkout.description),
            tools=tools,
        )
    except ReviewError as e:
        print(f"::warning::Claude review incomplete: {e}")
        outcome = ReviewOutcome(review=None, unavailable_reason=f"Claude's review could not be completed: {e}")
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

    verdict = decide(outcome, test, checkout)
    print(f"Verdict: {verdict.status}")
    for reason in verdict.reasons:
        print(f"  - {reason}")

    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{cfg.server_url}/{cfg.repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = render_comment(outcome, verdict, RenderContext(cfg.server_url, cfg.repository, cfg.model, checkout, test, run_url))
    append_summary(body)

    try:
        if cfg.post_comment:
            print(f"Comment: {gh.upsert_comment(number, body, COMMENT_MARKER)}")
        if cfg.add_labels:
            label = f"{cfg.label_prefix}{verdict.status}"
            gh.ensure_label(label, LABEL_COLORS[verdict.status], LABEL_DESCRIPTIONS[verdict.status])
            gh.set_exclusive_label(number, [l["name"] for l in pr.get("labels", [])], cfg.label_prefix, label)
    except GitHubError as e:
        if e.status == 403:
            print(
                "::error::GitHub refused to post the comment or label (403). Give the workflow "
                "`pull-requests: write` and `issues: write` permissions."
            )
        raise

    if cfg.auto_merge:
        update_auto_merge(gh, pr, verdict.status == PASS, cfg.auto_merge_method)

    set_output("verdict", verdict.status)
    set_output("risk", outcome.risk)
    set_output("confidence", outcome.confidence)

    if should_fail(verdict.status, cfg.fail_on):
        print(f"::error::Verdict is {verdict.status} (fail-on: {cfg.fail_on})")
        return 1
    return 0


def update_auto_merge(gh: GitHub, pr: dict, passed: bool, method: str) -> None:
    try:
        if passed:
            gh.enable_auto_merge(pr["node_id"], method)
            print(f"Auto-merge enabled ({method}); GitHub merges once all required checks pass.")
        elif pr.get("auto_merge"):
            gh.disable_auto_merge(pr["node_id"])
            print("Auto-merge disabled: this push no longer passes the gate.")
    except GitHubError as e:
        # Common causes: auto-merge not allowed in repo settings, no branch protection
        # (nothing to wait for), or missing contents: write permission.
        print(f"::warning::Could not change auto-merge: {e}")


def main() -> int:
    try:
        cfg = Config.from_env()
        gh = GitHub(cfg.github_token, cfg.api_url, cfg.repository, cfg.graphql_url)
        event = load_event(cfg)
        if cfg.event_name == "merge_group":
            return run_merge_queue(cfg, gh, event)
        return review_pr(cfg, gh, event)
    except ConfigError as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
