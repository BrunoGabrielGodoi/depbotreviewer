"""The merge gate: turn evidence (tests, checkout state, Claude's review) into a verdict.

Only evidence can produce a pass. Claude's opinion can block a merge or send it to a
human, but it can never pass a PR on its own.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .claude_review import ReviewOutcome
from .prompts import CheckRun

PASS, NEEDS_HUMAN, FAIL = "pass", "needs-human", "fail"
VERDICTS = (PASS, NEEDS_HUMAN, FAIL)


@dataclass(frozen=True)
class Checkout:
    sha: str
    is_merge_result: bool
    description: str


def describe_checkout(root: Path, head_sha: str, base_ref: str) -> Checkout:
    """Work out whether the workspace holds the PR merged into its base branch."""
    out = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    sha, parents = out[0], out[1:]
    if len(parents) == 2 and parents[1] == head_sha:
        return Checkout(sha, True, f"the PR merged into {base_ref} at {parents[0][:7]} (merge commit {sha[:7]})")
    if sha == head_sha:
        return Checkout(sha, False, f"the PR's own commit {sha[:7]}, NOT merged with the latest {base_ref}")
    return Checkout(sha, False, f"commit {sha[:7]}, which is neither the PR head nor its merge result")


@dataclass(frozen=True)
class Verdict:
    status: str
    reasons: list[str]


def decide(outcome: ReviewOutcome, test: CheckRun | None, checkout: Checkout) -> Verdict:
    fail: list[str] = []
    human: list[str] = []

    if test is None:
        human.append("No build or tests were run (set `test-command`), so nothing proves the update works.")
    elif test.timed_out:
        fail.append(f"`{test.command}` timed out.")
    elif not test.passed:
        fail.append(f"`{test.command}` failed (exit code {test.exit_code}).")

    if not checkout.is_merge_result:
        human.append(f"Tests ran on {checkout.description}, so they do not prove the result of merging.")

    r = outcome.review
    if r is None:
        human.append(outcome.unavailable_reason or "Claude's review could not be completed.")
    else:
        if r["risk"] == "high":
            fail.append("Claude rated the update high risk.")
        elif r["risk"] == "medium":
            human.append("Claude rated the update medium risk.")
        if r["confidence"] == "low":
            human.append("Claude has low confidence in its review.")
        uncovered = [i for i in r["impacted_code"] if i["covered_by_tests"] != "yes"]
        if uncovered:
            human.append(
                f"{len(uncovered)} affected call site{'s are' if len(uncovered) != 1 else ' is'} "
                "not proven to be covered by tests."
            )

    if fail:
        return Verdict(FAIL, fail + human)
    if human:
        return Verdict(NEEDS_HUMAN, human)
    return Verdict(
        PASS,
        [
            "The build and tests passed on the merge result.",
            "Every affected call site Claude found is covered by a passing test.",
        ],
    )


_QUEUE_REF = re.compile(r"/pr-(\d+)-[0-9a-f]+$")


def pr_number_from_queue_ref(ref: str) -> int | None:
    """gh-readonly-queue/main/pr-123-<sha> -> 123 (the last PR in the queue group)."""
    m = _QUEUE_REF.search(ref)
    return int(m.group(1)) if m else None
