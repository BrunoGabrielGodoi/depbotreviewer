from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.gate import FAIL, NEEDS_HUMAN, Checkout, Verdict
from dep_reviewer.prompts import CheckRun, build_user_message, is_lockfile
from dep_reviewer.report import COMMENT_MARKER, RenderContext, render_comment

MERGE = Checkout("abcdef1234567", True, "the PR merged into main at 1111111 (merge commit abcdef1)")
PASSED = CheckRun("npm test", 0, False, 30.0, 100, "ok")
CTX = RenderContext("https://github.com", "acme/app", "claude-opus-5-5", MERGE, PASSED)

REVIEW = {
    "risk": "medium",
    "confidence": "high",
    "summary": "Major bump of left-pad. @everyone should look.",
    "dependencies": [
        {
            "name": "left-pad",
            "ecosystem": "npm",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "update_type": "major",
            "direct": True,
            "scope": "runtime",
        }
    ],
    "upstream_changes": [
        {
            "dependency": "left-pad",
            "kind": "breaking",
            "description": "Default fill | changed",
            "affects_repo": "unclear",
            "source_url": "https://github.com/x/left-pad/releases",
        },
        {
            "dependency": "left-pad",
            "kind": "feature",
            "description": "New option",
            "affects_repo": "no",
            "source_url": "javascript:alert(1)",
        },
    ],
    "impacted_code": [
        {"file": "./src/util.js", "line": 9, "description": "Wraps pad()", "covered_by_tests": "yes"},
        {"file": "./src/index.js", "line": 2, "description": "Calls pad()", "covered_by_tests": "no"},
    ],
    "test_assessment": "No tests cover pad() in index.js.",
    "recommendations": ["Add a test for pad()."],
}

HUMAN = Verdict(NEEDS_HUMAN, ["Claude rated the update medium risk."])


def test_render_full_review():
    body = render_comment(ReviewOutcome(review=REVIEW, turns=4), HUMAN, CTX)
    assert body.startswith(COMMENT_MARKER)
    assert "👀 Needs human review" in body
    assert "- Claude rated the update medium risk." in body
    assert "**Tested:** `npm test` passed (exit code 0, 30s), on the PR merged into main" in body
    assert "@​everyone" in body  # mention neutralized
    assert "Default fill \\| changed" in body  # table cell escaped
    assert "New option" not in body  # unaffecting feature filtered out
    assert "javascript:" not in body
    assert "https://github.com/acme/app/blob/abcdef1234567/src/index.js#L2" in body
    # untested call sites are listed first
    assert body.index("❌ no test") < body.index("✅ tested")


def test_render_unavailable():
    v = Verdict(FAIL, ["`npm test` failed (exit code 1).", "API down"])
    body = render_comment(ReviewOutcome(review=None, unavailable_reason="API down"), v, CTX)
    assert "❌ Do not merge" in body and "API down" in body and "⚪ unknown" in body


def test_render_shrinks_huge_review():
    sites = [{"file": "a.js", "line": i, "description": "x" * 2000, "covered_by_tests": "no"} for i in range(1, 200)]
    body = render_comment(ReviewOutcome(review=dict(REVIEW, impacted_code=sites)), HUMAN, CTX)
    assert len(body) <= 60_000
    assert "more not shown" in body


def test_lockfiles():
    assert is_lockfile("frontend/package-lock.json")
    assert is_lockfile("Cargo.lock")
    assert not is_lockfile("package.json")


def test_user_message_routes_lockfiles_to_tool():
    pr = {"title": "Bump x", "user": {"login": "dependabot[bot]"}, "body": "notes", "labels": []}
    files = [
        {"filename": "package.json", "status": "modified", "additions": 1, "deletions": 1, "patch": "-a\n+b"},
        {"filename": "package-lock.json", "status": "modified", "additions": 9, "deletions": 9, "patch": "-c\n+d"},
    ]
    test = CheckRun("npm test", 1, False, 12.0, 300, "FAIL x")
    msg = build_user_message(pr, files, test, "the PR merged into main")
    assert '<diff file="package.json">' in msg
    assert '<diff file="package-lock.json">' not in msg
    assert "package-lock.json (modified, +9 -9) - lockfile; read with show_pr_diff" in msg
    assert "FAILED (exit code 1" in msg
    assert "Workspace (what your file tools read and what the tests ran on): the PR merged into main" in msg
