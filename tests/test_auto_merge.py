import pytest

from dep_reviewer import __main__ as main
from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import Config, ConfigError
from dep_reviewer.gate import (
    Checkout,
    UpdateInfo,
    Verdict,
    auto_merge_decision,
    parse_dependabot_metadata,
)
from dep_reviewer.github_api import GitHubError

MINOR_COMMIT = """Bump ms from 2.0.0 to 2.1.3 in /testbed

Bumps [ms](https://github.com/vercel/ms) from 2.0.0 to 2.1.3.

---
updated-dependencies:
- dependency-name: ms
  dependency-version: 2.1.3
  dependency-type: direct:production
  update-type: version-update:semver-minor
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

GROUP_COMMIT = """Bump the npm group with 2 updates

---
updated-dependencies:
- dependency-name: a
  dependency-type: direct:production
  update-type: version-update:semver-patch
- dependency-name: b
  dependency-type: direct:development
  update-type: version-update:semver-major
...
"""

PASS = Verdict("pass", ["ok"])


def outcome(confidence="high"):
    return ReviewOutcome(review={"risk": "low", "confidence": confidence, "impacted_code": []})


def test_parse_single_and_grouped_metadata():
    assert parse_dependabot_metadata(MINOR_COMMIT) == UpdateInfo(["ms"], ["minor"])
    group = parse_dependabot_metadata(GROUP_COMMIT)
    assert group.names == ["a", "b"] and group.worst == "major"
    assert parse_dependabot_metadata("Bump x").worst == "unknown"
    # A dependency without an update type makes the whole PR unknown.
    assert UpdateInfo(["a", "b"], ["patch"]).worst == "unknown"


@pytest.mark.parametrize(
    "verdict, confidence, commit, merge, reason",
    [
        (PASS, "high", MINOR_COMMIT, True, "minor update"),
        (Verdict("needs-human", []), "high", MINOR_COMMIT, False, "verdict is needs-human"),
        (PASS, "medium", MINOR_COMMIT, False, "confidence is medium"),
        (PASS, "high", GROUP_COMMIT, False, "major update"),
        (PASS, "high", "Bump x from 1 to 2", False, "doesn't say the update type"),
    ],
)
def test_decision(verdict, confidence, commit, merge, reason):
    d = auto_merge_decision(verdict, outcome(confidence), parse_dependabot_metadata(commit), ("patch", "minor"))
    assert d.merge is merge and reason in d.reason


def test_patch_only_policy_blocks_minor():
    d = auto_merge_decision(PASS, outcome(), parse_dependabot_metadata(MINOR_COMMIT), ("patch",))
    assert not d.merge and "minor update" in d.reason


def test_config_update_types():
    env = {"INPUT_ANTHROPIC_API_KEY": "k", "INPUT_GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}
    assert Config.from_env(env).auto_merge_update_types == ("patch", "minor")
    assert Config.from_env({**env, "INPUT_AUTO_MERGE_UPDATE_TYPES": "Patch"}).auto_merge_update_types == ("patch",)
    with pytest.raises(ConfigError, match="auto-merge-update-types"):
        Config.from_env({**env, "INPUT_AUTO_MERGE_UPDATE_TYPES": "patch,breaking"})


# --- merging ---------------------------------------------------------------------------

PR = {"number": 2, "node_id": "N", "head": {"sha": "head1"}, "base": {"sha": "base1"}, "state": "open"}
TESTED = Checkout("merge1", True, "merged", base_sha="base1")


class FakeGitHub:
    def __init__(self, checks, current=None, auto_merge_error=True):
        self.checks = list(checks)  # successive (pending, failed) answers
        self.current = current or PR
        self.auto_merge_error = auto_merge_error
        self.merged = None
        self.disabled = False

    def enable_auto_merge(self, node_id, method):
        if self.auto_merge_error:
            raise GitHubError(200, "Pull request is in clean status")

    def disable_auto_merge(self, node_id):
        self.disabled = True

    def other_checks(self, sha, own_run_id):
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    def get_pr(self, number):
        if isinstance(self.current, list):
            return self.current.pop(0) if len(self.current) > 1 else self.current[0]
        return self.current

    def upsert_comment(self, number, body, marker):
        self.comment = body

    def merge_pr(self, number, sha, method):
        self.merged = (number, sha, method)


def run(gh, merge=True, pr=PR, checkout=TESTED, retest=None):
    t = [0.0]

    def sleep(s):
        t[0] += s

    return main.update_auto_merge(gh, pr, merge, "squash", checkout, retest=retest, sleep=sleep, clock=lambda: t[0])


def test_hands_off_to_github_auto_merge_when_available():
    gh = FakeGitHub([([], [])], auto_merge_error=False)
    assert run(gh) is False and gh.merged is None


def test_merges_directly_after_waiting_for_other_checks():
    gh = FakeGitHub([(["CI"], []), (["CI"], []), ([], [])])
    assert run(gh) is True
    assert gh.merged == (2, "head1", "squash")


def test_does_not_merge_when_another_check_fails():
    gh = FakeGitHub([(["CI"], []), ([], ["CI (failure)"])])
    assert run(gh) is False and gh.merged is None


def test_gives_up_when_checks_never_finish():
    gh = FakeGitHub([(["slow"], [])])
    assert run(gh) is False and gh.merged is None


@pytest.mark.parametrize(
    "current",
    [
        {**PR, "head": {"sha": "head2"}},  # new push since the review
        {**PR, "state": "closed"},
    ],
)
def test_does_not_merge_what_was_not_tested(current):
    gh = FakeGitHub([([], [])], current=current)
    assert run(gh) is False and gh.merged is None


def test_does_not_merge_when_tests_did_not_run_on_merge_result():
    gh = FakeGitHub([([], [])])
    assert run(gh, checkout=Checkout("head1", False, "head only")) is False and gh.merged is None


def test_disables_existing_auto_merge_when_no_longer_qualified():
    gh = FakeGitHub([([], [])])
    assert run(gh, merge=False, pr={**PR, "auto_merge": {"enabled_by": {}}}) is False
    assert gh.disabled and gh.merged is None


def test_moved_base_without_retest_does_not_merge():
    gh = FakeGitHub([([], [])], current={**PR, "base": {"sha": "base2"}})
    assert run(gh) is False and gh.merged is None


def test_moved_base_is_retested_then_merged():
    tested = []
    gh = FakeGitHub([([], [])], current=[{**PR, "base": {"sha": "base2"}}, {**PR, "base": {"sha": "base2"}}])
    assert run(gh, retest=lambda base: tested.append(base) or True) is True
    assert tested == ["base2"] and gh.merged == (2, "head1", "squash")


def test_retest_failure_blocks_merge_and_explains_on_the_pr():
    gh = FakeGitHub([([], [])], current={**PR, "base": {"sha": "base2"}})
    assert run(gh, retest=lambda base: False) is False
    assert gh.merged is None and "tests fail once this PR is merged" in gh.comment


def test_base_that_keeps_moving_gives_up():
    bases = iter(f"base{i}" for i in range(2, 20))
    gh = FakeGitHub([([], [])])
    gh.get_pr = lambda n: {**PR, "base": {"sha": next(bases)}}
    calls = []
    assert run(gh, retest=lambda base: calls.append(base) or True) is False
    assert len(calls) == main.MAX_RETESTS and gh.merged is None


# --- retest_on_base against real git repositories ----------------------------------------

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd: Path, name: str, content: str) -> str:
    (cwd / name).write_text(content)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """origin with main + a PR branch; a workspace cloned before main moved on."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "commit.gpgsign", "false")  # throwaway fixture repo
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    commit(origin, "base.txt", "v1")
    git(origin, "checkout", "-q", "-b", "pr")
    head = commit(origin, "pr.txt", "bump")
    git(origin, "checkout", "-q", "main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return origin, work, head


def cfg_for(work: Path, tmp_path: Path, test_command: str) -> Config:
    return Config.from_env({
        "INPUT_ANTHROPIC_API_KEY": "k", "INPUT_GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "o/r",
        "GITHUB_WORKSPACE": str(work), "RUNNER_TEMP": str(tmp_path), "INPUT_TEST_COMMAND": test_command,
    })


def test_retest_merges_pr_into_new_base_and_runs_tests(repos, tmp_path):
    origin, work, head = repos
    new_base = commit(origin, "other.txt", "another update merged first")
    cfg = cfg_for(work, tmp_path, "test -f base.txt && test -f pr.txt && test -f other.txt")
    assert main.retest_on_base(cfg, 2, head, new_base) is True
    parents = git(work, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents == [new_base, head]  # same shape as GitHub's merge ref


def test_retest_fails_when_tests_fail(repos, tmp_path):
    origin, work, head = repos
    new_base = commit(origin, "other.txt", "x")
    assert main.retest_on_base(cfg_for(work, tmp_path, "exit 1"), 2, head, new_base) is False


def test_retest_fails_on_merge_conflict(repos, tmp_path):
    origin, work, head = repos
    new_base = commit(origin, "pr.txt", "conflicting change on main")
    assert main.retest_on_base(cfg_for(work, tmp_path, "true"), 2, head, new_base) is False
    assert git(work, "status", "--porcelain") == ""  # merge aborted cleanly
