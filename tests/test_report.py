from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import should_fail
from dep_reviewer.prompts import CheckRun, build_user_message, is_lockfile
from dep_reviewer.report import COMMENT_MARKER, RenderContext, render_comment

CTX = RenderContext("https://github.com", "acme/app", "abcdef1234567", "claude-opus-5-5", None)

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
    "impacted_code": [{"file": "./src/index.js", "line": 2, "description": "Calls pad()"}],
    "test_assessment": "No tests cover pad().",
    "recommendations": ["Add a test for pad()."],
}


def test_render_full_review():
    body = render_comment(ReviewOutcome(review=REVIEW, turns=4), CTX)
    assert body.startswith(COMMENT_MARKER)
    assert "🟡 Medium risk" in body
    assert "@​everyone" in body  # mention neutralized
    assert "Default fill \\| changed" in body  # table cell escaped
    assert "New option" not in body  # unaffecting feature filtered out
    assert "javascript:" not in body
    assert "https://github.com/acme/app/blob/abcdef1234567/src/index.js#L2" in body


def test_render_unavailable():
    body = render_comment(ReviewOutcome(review=None, unavailable_reason="API down"), CTX)
    assert "⚪ Review unavailable" in body and "API down" in body


def test_render_shrinks_huge_review():
    big = dict(REVIEW, impacted_code=[{"file": "a.js", "line": i, "description": "x" * 2000} for i in range(1, 200)])
    body = render_comment(ReviewOutcome(review=big), CTX)
    assert len(body) <= 60_000
    assert "more not shown" in body


def test_should_fail():
    assert not should_fail("high", "none")
    assert should_fail("high", "high") and not should_fail("medium", "high")
    assert should_fail("medium", "medium") and should_fail("unknown", "medium")
    assert not should_fail("low", "medium")


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
    msg = build_user_message(pr, files, test)
    assert '<diff file="package.json">' in msg
    assert '<diff file="package-lock.json">' not in msg
    assert "package-lock.json (modified, +9 -9) - lockfile; read with show_pr_diff" in msg
    assert "FAILED (exit code 1" in msg
