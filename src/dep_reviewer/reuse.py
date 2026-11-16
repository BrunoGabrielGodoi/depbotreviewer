"""Reuse a blocking verdict when Dependabot only rebased the PR (opt-in: reuse-blocking-verdict).

Dependabot rebases every open PR whose lockfile conflicts with the base branch, so each
dependency change merged into the base re-runs the whole gate on every open PR. For an
update that needs a migration the verdict doesn't change, and Claude is asked the same
question again.

With the option on, a run skips Claude when all of these hold:

- the PR's head moved (a re-run of the same head always gets a full review);
- Dependabot's commit metadata lists the same dependencies, versions and update types;
- the build and tests had the same outcome;
- the earlier review completed (an API error or limit is never reused);
- re-deciding with the earlier review's risk, confidence and untested call sites, and
  this run's tests and checkout, gives fail or needs-human.

A pass is never reused: it can turn on auto-merge, so it always comes from a full review.
That also bounds what a tampered state can do: it can only block or escalate a PR.

The state lives in a hidden line of the reviewer's own PR comment, and is read only from
a comment written by a bot account. Deleting the comment, or re-running the job on the
same head, forces a full review.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .claude_review import ReviewOutcome
from .prompts import CheckRun

STATE_PREFIX = "<!-- claude-dependabot-reviewer:state "
STATE_SUFFIX = " -->"
# Marks where Claude's analysis starts in the comment, so a reuse run can carry it over.
ANALYSIS_MARKER = "<!-- claude-dependabot-reviewer:analysis -->"
ANALYSIS_END = "<!-- claude-dependabot-reviewer:analysis-end -->"
VERSION = 1
MAX_STATE_CHARS = 1024
MAX_UNCOVERED = 1000

_SHA = re.compile(r"^[0-9a-f]{40}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_STATE_LINE = re.compile(re.escape(STATE_PREFIX) + r"(\{[^\n]*\})" + re.escape(STATE_SUFFIX))
TEST_STATUSES = ("passed", "failed", "timed-out", "none")
RISKS = ("low", "medium", "high")
CONFIDENCES = ("low", "medium", "high")

# Dependabot's metadata block in its commit message:
#   ---
#   updated-dependencies:
#   - dependency-name: "@scope/pkg"
#     dependency-version: 2.0.0
#     dependency-type: direct:production
#     update-type: version-update:semver-major
#   ...
_BLOCK = re.compile(r"^updated-dependencies:\s*$(.*?)^\.\.\.\s*$", re.M | re.S)
_ENTRY_FIELD = re.compile(r"^\s*-?\s*(dependency-name|dependency-version|update-type):\s*(.+?)\s*$")


def update_fingerprint(commit_message: str) -> str | None:
    """A hash of the dependencies, target versions and update types the commit says it updates.

    None when the commit has no metadata block, or an entry lacks a name, version or type:
    then nothing proves two heads propose the same update, so nothing is reused.
    """
    m = _BLOCK.search(commit_message or "")
    if not m:
        return None
    entries: list[dict[str, str]] = []
    for line in m.group(1).splitlines():
        if line.lstrip().startswith("- "):
            entries.append({})
        field = _ENTRY_FIELD.match(line)
        if field and entries:
            entries[-1][field.group(1)] = field.group(2).strip("'\"")
    keys = ("dependency-name", "dependency-version", "update-type")
    if not entries or any(not all(e.get(k) for k in keys) for e in entries):
        return None
    canonical = sorted(tuple(e[k] for k in keys) for e in entries)
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def test_status(test: CheckRun | None) -> str:
    if test is None:
        return "none"
    if test.timed_out:
        return "timed-out"
    return "passed" if test.passed else "failed"


@dataclass(frozen=True)
class ReviewState:
    head_sha: str  # the PR head the comment describes
    reviewed_sha: str  # the PR head Claude last reviewed in full
    fingerprint: str
    tests: str
    risk: str
    confidence: str
    uncovered: int  # affected call sites Claude did not find covered by tests


def state_from_review(head_sha: str, fingerprint: str | None, test: CheckRun | None,
                      outcome: ReviewOutcome) -> ReviewState | None:
    """The state to save after a full review, or None when it must not be reused."""
    r = outcome.review
    if r is None or fingerprint is None:
        return None
    uncovered = sum(1 for i in r["impacted_code"] if i["covered_by_tests"] != "yes")
    return ReviewState(head_sha, head_sha, fingerprint, test_status(test), r["risk"], r["confidence"],
                       min(uncovered, MAX_UNCOVERED))


def render_state(state: ReviewState) -> str:
    return STATE_PREFIX + json.dumps({"version": VERSION, **asdict(state)}, separators=(",", ":")) + STATE_SUFFIX


def parse_state(body: str) -> ReviewState | None:
    """The state saved in a comment body, or None if it is missing or not exactly well-formed."""
    matches = _STATE_LINE.findall(body or "")
    if len(matches) != 1 or len(matches[0]) > MAX_STATE_CHARS:
        return None
    try:
        data = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or set(data) != {"version", *ReviewState.__dataclass_fields__}:
        return None
    if data["version"] != VERSION or isinstance(data["version"], bool):
        return None
    strings = ("head_sha", "reviewed_sha", "fingerprint", "tests", "risk", "confidence")
    if not all(isinstance(data[k], str) for k in strings):
        return None
    uncovered = data["uncovered"]
    if isinstance(uncovered, bool) or not isinstance(uncovered, int) or not 0 <= uncovered <= MAX_UNCOVERED:
        return None
    if not (_SHA.match(data["head_sha"]) and _SHA.match(data["reviewed_sha"])
            and _FINGERPRINT.match(data["fingerprint"])):
        return None
    if data["tests"] not in TEST_STATUSES or data["risk"] not in RISKS or data["confidence"] not in CONFIDENCES:
        return None
    return ReviewState(**{k: data[k] for k in ReviewState.__dataclass_fields__})


def reusable_outcome(prior: ReviewState | None, head_sha: str, fingerprint: str | None,
                     test: CheckRun | None) -> ReviewOutcome | None:
    """Claude's earlier assessment, if this run may reuse it instead of asking again.

    The caller still re-decides with it and must do a full review if that gives a pass.
    """
    if prior is None or fingerprint is None:
        return None
    if prior.head_sha == head_sha:
        return None  # a re-run of the same head asks for a fresh review
    if prior.fingerprint != fingerprint or prior.tests != test_status(test):
        return None
    impacted = [{"covered_by_tests": "no"}] * prior.uncovered
    return ReviewOutcome(review={"risk": prior.risk, "confidence": prior.confidence, "impacted_code": impacted})


def extract_analysis(body: str) -> str | None:
    """Claude's analysis section from an earlier comment by this reviewer, without the markers."""
    start = body.find(ANALYSIS_MARKER)
    end = body.find(ANALYSIS_END)
    if start < 0 or end < start:
        return None
    return body[start + len(ANALYSIS_MARKER):end].strip("\n")
