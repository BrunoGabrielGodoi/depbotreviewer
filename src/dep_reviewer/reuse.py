"""Reuse a blocking verdict when Dependabot only rebased the PR (opt-in: reuse-blocking-verdict).

Dependabot rebases every open PR whose lockfile conflicts with the base branch, so each
dependency change merged into the base re-runs the whole gate on every open PR. For an
update that needs a migration the verdict doesn't change, and Claude is asked the same
question again.

With the option on, a run skips Claude when all of these hold:

- this is not a re-run for the head the last run already handled;
- the head commit is the PR author's (Dependabot's), committed and verified by GitHub,
  and has the same PR title and the
  same dependencies, target versions, dependency types and update types;
- the build and tests had the same outcome;
- the last review completed (an API error or limit is never saved, so never reused);
- re-deciding with the earlier review's risk, confidence and untested call sites, and
  this run's tests and checkout, gives fail or needs-human.

A pass is never reused: it can turn on auto-merge, so it always comes from a full review.

The state is the last line of the reviewer's comment. It is read only from a comment
written by a bot account, and only if its HMAC verifies: the key is derived from the
Claude credential, so neither another bot nor a person editing the comment can forge
the state or the analysis it vouches for. Even a forged state could only keep a PR
blocked. Re-running the job for the same head, or deleting the comment, forces a full
review; so does rotating the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass

from .claude_review import ReviewOutcome
from .gate import parse_update_entries
from .prompts import CheckRun
from .report import ANALYSIS_END, ANALYSIS_MARKER, STATE_PREFIX, STATE_SUFFIX
from .test_result import SHA_RE

VERSION = 1
MAX_STATE_CHARS = 1024
# Only the count matters to the gate; capped so the state line stays small.
MAX_UNCOVERED = 1000
TESTS_OUTCOMES = ("passed", "failed", "timed-out", "none")
RISKS = ("low", "medium", "high")
CONFIDENCES = ("low", "medium", "high")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STATE_LINE = re.compile(re.escape(STATE_PREFIX) + r"(\{[^\n]*\})" + re.escape(STATE_SUFFIX))
_KEY_CONTEXT = b"claude-dependabot-reviewer reuse-blocking-verdict state v1"


def state_key(credential: str) -> bytes | None:
    """The HMAC key for the state, derived from the Claude credential; None without one."""
    if not credential:
        return None
    return hmac.new(credential.encode(), _KEY_CONTEXT, hashlib.sha256).digest()


def update_fingerprint(pr_title: str, commit_message: str) -> str | None:
    """A hash of what the PR proposes: its title and every dependency's name, target version,
    dependency type and update type, from Dependabot's commit metadata.

    The title carries the from-version and the directory. None when the metadata is
    missing or incomplete: then nothing proves two heads propose the same update.
    """
    entries = parse_update_entries(commit_message)
    if entries is None:
        return None
    canonical = [pr_title, sorted(
        (e["dependency-name"], e["dependency-version"], e["dependency-type"], e["update-type"]) for e in entries
    )]
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def tests_outcome(test: CheckRun | None) -> str:
    """The build and test result in one word, as saved in the state."""
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
    return ReviewState(head_sha, head_sha, fingerprint, tests_outcome(test), r["risk"], r["confidence"],
                       min(uncovered, MAX_UNCOVERED))


def _mac(key: bytes, state: ReviewState, analysis: str, repository: str, number: int) -> str:
    # The repository and PR number are signed too, so a state can't be copied to another PR
    # (or repository sharing the credential).
    payload = json.dumps([repository, number, asdict(state), analysis], sort_keys=True).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def render_state(state: ReviewState, analysis: str, key: bytes, repository: str, number: int) -> str:
    """The state line, signed together with the analysis section it was saved with."""
    data = {"version": VERSION, **asdict(state), "mac": _mac(key, state, analysis, repository, number)}
    return STATE_PREFIX + json.dumps(data, separators=(",", ":")) + STATE_SUFFIX


def extract_analysis(body: str) -> str | None:
    """The analysis section of a reviewer comment, without the markers."""
    start = body.find(ANALYSIS_MARKER)
    end = body.find(ANALYSIS_END)
    if start < 0 or end < start:
        return None
    return body[start + len(ANALYSIS_MARKER):end].strip("\n")


def parse_state(body: str, key: bytes, repository: str, number: int) -> tuple[ReviewState, str] | None:
    """The state and analysis saved in a comment body, or None unless exactly one
    well-formed state line is present and its signature verifies."""
    matches = _STATE_LINE.findall(body or "")
    analysis = extract_analysis(body or "")
    if len(matches) != 1 or len(matches[0]) > MAX_STATE_CHARS or analysis is None:
        return None
    try:
        data = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    fields = set(ReviewState.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != {"version", "mac", *fields}:
        return None
    # bool is an int subclass: never accept one where a number is expected.
    if isinstance(data["version"], bool) or data["version"] != VERSION:
        return None
    if not all(isinstance(data[k], str) for k in (*fields - {"uncovered"}, "mac")):
        return None
    uncovered = data["uncovered"]
    if isinstance(uncovered, bool) or not isinstance(uncovered, int) or not 0 <= uncovered <= MAX_UNCOVERED:
        return None
    if not (SHA_RE.match(data["head_sha"]) and SHA_RE.match(data["reviewed_sha"])
            and _HEX64.match(data["fingerprint"]) and _HEX64.match(data["mac"])):
        return None
    if data["tests"] not in TESTS_OUTCOMES or data["risk"] not in RISKS or data["confidence"] not in CONFIDENCES:
        return None
    state = ReviewState(**{k: data[k] for k in fields})
    if not hmac.compare_digest(data["mac"], _mac(key, state, analysis, repository, number)):
        return None
    return state, analysis


def reusable_outcome(prior: ReviewState | None, head_sha: str, fingerprint: str | None,
                     test: CheckRun | None) -> ReviewOutcome | None:
    """Claude's earlier assessment, if this run may reuse it instead of asking again.

    The caller still re-decides with it and must do a full review if that gives a pass.
    """
    if prior is None or fingerprint is None:
        return None
    if prior.head_sha == head_sha:
        return None  # a re-run of the same head asks for a fresh review
    if prior.fingerprint != fingerprint or prior.tests != tests_outcome(test):
        return None
    # decide() only counts call sites whose covered_by_tests isn't "yes", so the saved
    # count stands in for the call sites themselves.
    impacted = [{"covered_by_tests": "no"}] * prior.uncovered
    return ReviewOutcome(review={"risk": prior.risk, "confidence": prior.confidence, "impacted_code": impacted})
