"""reuse-blocking-verdict, the stale-run guard, and the reason shown when a test result is not used."""

import copy
import json

import pytest

from dep_reviewer import __main__ as main
from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import Config
from dep_reviewer.gate import NEEDS_HUMAN, Checkout, decide
from dep_reviewer.prompts import CheckRun
from dep_reviewer.report import RenderContext, render_comment
from dep_reviewer.reuse import (
    STATE_PREFIX,
    STATE_SUFFIX,
    ReviewState,
    parse_state,
    render_state,
    update_fingerprint,
)
from tests.test_report import REVIEW

A, B, C = "a" * 40, "b" * 40, "c" * 40
MERGE = Checkout("m" * 40, True, "the PR merged into main (merge commit mmmmmmm)", "1" * 40)
NOT_MERGE = Checkout("h" * 40, False, "the PR's own commit hhhhhhh, NOT merged with the latest main")
PASSED = CheckRun("npm test", 0, False, 1.0, 10, "ok")
FAILED = CheckRun("npm test", 1, False, 1.0, 10, "boom")

PASSING_REVIEW = copy.deepcopy(REVIEW)
PASSING_REVIEW.update(risk="low", confidence="high")
for item in PASSING_REVIEW["impacted_code"]:
    item["covered_by_tests"] = "yes"


def commit(*deps: tuple[str, str, str]) -> str:
    lines = ["Bump things", "", "---", "updated-dependencies:"]
    for name, version, kind in deps:
        lines += [f'- dependency-name: "{name}"', f"  dependency-version: {version}",
                  "  dependency-type: direct:production", f"  update-type: version-update:semver-{kind}"]
    return "\n".join(lines + ["...", "", "Signed-off-by: dependabot[bot]"])


UUID_14 = commit(("uuid", "14.0.3", "major"))


# --- update_fingerprint ------------------------------------------------------------------

def test_fingerprint_is_stable_and_order_insensitive():
    one = commit(("a", "1.0.0", "minor"), ("b", "2.0.0", "patch"))
    two = commit(("b", "2.0.0", "patch"), ("a", "1.0.0", "minor"))
    assert update_fingerprint(one) == update_fingerprint(two)
    assert len(update_fingerprint(one)) == 64


def test_fingerprint_changes_with_the_target_version():
    assert update_fingerprint(UUID_14) != update_fingerprint(commit(("uuid", "14.0.4", "major")))


@pytest.mark.parametrize("message", [
    "Bump uuid from 8.3.2 to 14.0.3",  # no metadata block
    "---\nupdated-dependencies:\n- dependency-name: uuid\n  update-type: version-update:semver-major\n...",  # no version
    "---\nupdated-dependencies:\n...",  # empty block
])
def test_no_fingerprint_without_complete_metadata(message):
    assert update_fingerprint(message) is None


# --- state line ----------------------------------------------------------------------------

STATE = ReviewState(A, A, "f" * 64, "passed", "medium", "high", 1)


def test_state_round_trip():
    assert parse_state("comment\n" + render_state(STATE)) == STATE


def tampered(**changes) -> str:
    data = json.loads(render_state(STATE).removeprefix(STATE_PREFIX).removesuffix(STATE_SUFFIX))
    data.update(changes)
    return STATE_PREFIX + json.dumps(data) + STATE_SUFFIX


@pytest.mark.parametrize("body", [
    "",
    tampered(extra=1),
    tampered(version=2),
    tampered(head_sha="not-a-sha"),
    tampered(fingerprint="f" * 63),
    tampered(tests="skipped"),
    tampered(risk="none"),
    tampered(uncovered=True),
    tampered(uncovered=-1),
    render_state(STATE) + "\n" + render_state(STATE),  # two state lines: ambiguous
    STATE_PREFIX + "{not json}" + STATE_SUFFIX,
])
def test_malformed_state_is_ignored(body):
    assert parse_state(body) is None


# --- the reason shown when a test result is not used ------------------------------------

def test_discarded_test_result_says_why():
    reason = "the test job's result was not used (the tests ran on PR head aaaaaaa, not the reviewed head bbbbbbb)"
    verdict = decide(ReviewOutcome(review=PASSING_REVIEW), None, MERGE, reason)
    assert verdict.status == NEEDS_HUMAN
    assert verdict.reasons[0].startswith("The test job's result was not used")
    assert "set `test-command`" not in verdict.reasons[0]
    body = render_comment(ReviewOutcome(review=PASSING_REVIEW), verdict,
                          RenderContext("https://github.com", "o/r", "m", MERGE, None, None, reason))
    assert f"**Tested:** {reason}" in body


# --- review_pr end to end, with GitHub and Claude faked ---------------------------------

class FakeGitHub:
    def __init__(self, messages: dict[str, str]):
        self.messages = messages
        self.pr: dict = {}
        self.comment: dict | None = None

    def get_pr(self, number):
        return self.pr

    def list_pr_files(self, number):
        return []

    def get_commit_message(self, sha):
        return self.messages[sha]

    def find_comment(self, number, marker):
        return self.comment

    def upsert_comment(self, number, body, marker):
        self.comment = {"id": 1, "body": body, "user": {"login": "github-actions[bot]", "type": "Bot"}}
        return "comment-url"

    def ensure_label(self, *a):
        pass

    def set_exclusive_label(self, *a):
        pass


class FakeTools:
    def __init__(self, *a):
        self.test_log = None

    def ensure_commits(self):
        pass


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """run(head, ...) reviews the PR at `head`; returns (outputs, Claude calls so far, comment body)."""
    out = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(main, "RepoTools", FakeTools)
    gh = FakeGitHub({A: UUID_14, B: UUID_14, C: UUID_14})
    calls: list[str] = []
    state = {"checkout": MERGE, "test": PASSED, "review": REVIEW}

    monkeypatch.setattr(main, "describe_checkout", lambda root, head, ref: state["checkout"])
    monkeypatch.setattr(main, "run_checks", lambda cfg, log: state["test"])

    def fake_claude(client, **k):
        calls.append("claude")
        review = state["review"]
        return ReviewOutcome(review=review) if review else ReviewOutcome(review=None, unavailable_reason="HTTP 429")

    # mode: all with an API key: this job runs test-command (faked) and Claude (faked).
    monkeypatch.setattr(main, "run_review", fake_claude)

    def run(head, event_head=None, **inputs):
        out.write_text("")
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_WORKSPACE": str(tmp_path), "RUNNER_TEMP": str(tmp_path),
               "GITHUB_EVENT_NAME": "pull_request", "INPUT_ANTHROPIC_API_KEY": "key",
               "INPUT_GITHUB_TOKEN": "ghs", "INPUT_TEST_COMMAND": "npm test",
               "INPUT_REUSE_BLOCKING_VERDICT": "true"}
        env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
        gh.pr = {"number": 7, "node_id": "N", "title": "Bump uuid", "body": "", "html_url": "https://x/pr/7",
                 "user": {"login": "dependabot[bot]"}, "labels": [], "head": {"sha": head},
                 "base": {"sha": "1" * 40, "ref": "main"}}
        event = {"pull_request": {"number": 7, "head": {"sha": event_head or head}}}
        main.review_pr(Config.from_env(env), gh, event)
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        return outputs, len(calls), (gh.comment or {}).get("body", "")

    return run, gh, state


def test_rebase_reuses_a_blocking_verdict_and_the_state_survives(gate):
    run, gh, _ = gate
    outputs, calls, body = run(A)
    assert (outputs["verdict"], calls) == (NEEDS_HUMAN, 1)
    assert parse_state(body).reviewed_sha == A

    for head in (B, C):  # two rebases in a row
        outputs, calls, body = run(head)
        assert (outputs["verdict"], calls) == (NEEDS_HUMAN, 1)
        assert "Claude was not asked again" in body
        assert "Major bump of left-pad" in body  # Claude's analysis carried over
        state = parse_state(body)
        assert (state.head_sha, state.reviewed_sha) == (head, A)


def test_rerun_of_the_same_head_reviews_again(gate):
    run, _, _ = gate
    run(A)
    assert run(A)[1] == 2


def test_new_target_version_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.messages[B] = commit(("uuid", "14.0.4", "major"))
    assert run(B)[1] == 2


def test_changed_test_outcome_reviews_again(gate):
    run, _, state = gate
    run(A)
    state["test"] = FAILED
    assert run(B)[1] == 2


def test_an_unavailable_review_is_never_reused(gate):
    run, _, state = gate
    state["review"] = None  # e.g. the API returned 429
    _, _, body = run(A)
    assert parse_state(body) is None
    state["review"] = REVIEW
    assert run(B)[1] == 2


def test_reuse_never_produces_a_pass(gate):
    run, _, state = gate
    # Claude found nothing, but the tests didn't run on the merge result: needs-human.
    state.update(review=PASSING_REVIEW, checkout=NOT_MERGE)
    assert run(A)[0]["verdict"] == NEEDS_HUMAN
    # Now the same update and test outcome on the merge result would re-decide to a pass,
    # so Claude must review in full instead.
    state["checkout"] = MERGE
    outputs, calls, _ = run(B, auto_merge="true")
    assert calls == 2
    assert outputs["verdict"] == "pass"


def test_state_in_a_persons_comment_is_ignored(gate):
    run, gh, _ = gate
    run(A)
    gh.comment["user"] = {"login": "someone", "type": "User"}
    assert run(B)[1] == 2


def test_no_state_when_the_option_is_off(gate):
    run, _, _ = gate
    _, _, body = run(A, reuse_blocking_verdict="false")
    assert STATE_PREFIX not in body
    assert run(B, reuse_blocking_verdict="false")[1] == 2


def test_run_for_an_older_head_changes_nothing(gate):
    run, gh, _ = gate
    run(A)
    before = gh.comment
    outputs, calls, _ = run(B, event_head=A)  # a re-run of A's workflow after a rebase to B
    assert outputs == {"skipped": "true"}
    assert calls == 1
    assert gh.comment is before
