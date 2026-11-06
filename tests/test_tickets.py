import pytest

from dep_reviewer.config import Config, ConfigError
from dep_reviewer.tickets import TicketContent, build_content, render_ticket_section

PR = {"number": 3, "title": "Bump chalk from 4.1.2 to 6.0.1 in /testbed", "html_url": "https://github.com/org/repo/pull/3"}
CONTENT = build_content(
    repository="org/repo", pr=PR, verdict="fail",
    reasons=["`npm test` failed (exit code 1)."], summary="chalk 5 is ESM-only.",
    untested=["src/a.js:3: calls x"], pr_url=PR["html_url"],
    run_url="https://github.com/org/repo/actions/runs/1",
)
BASE_ENV = {"INPUT_ANTHROPIC_API_KEY": "k", "INPUT_GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "org/repo"}


def test_content_mentions_pr_verdict_and_reasons():
    assert CONTENT.title == "Dependabot: Bump chalk from 4.1.2 to 6.0.1 in /testbed breaks the build (org/repo#3)"
    for text in ("**fail**", "failed (exit code 1)", "ESM-only", "src/a.js:3", "/actions/runs/1"):
        assert text in CONTENT.description


def test_section_is_collapsed_with_copyable_blocks():
    section = render_ticket_section(CONTENT)
    assert "<details>" in section and "</details>" in section
    assert f"```\n{CONTENT.title}\n```" in section
    assert "```markdown\n" in section


def test_fence_outgrows_backticks_in_the_text():
    content = TicketContent(title="t", description="before\n```js\ncode\n```\nafter")
    section = render_ticket_section(content)
    assert "````markdown\nbefore\n```js" in section  # 4-backtick fence, so the inner ``` can't close it
    assert section.count("````") == 2


def test_mentions_are_neutralized():
    section = render_ticket_section(TicketContent(title="hi @octocat", description="cc @org/team"))
    assert "@octocat" not in section and "@org/team" not in section


def test_ticket_text_follows_ticket_on():
    from dep_reviewer import __main__ as main
    from dep_reviewer.claude_review import ReviewOutcome
    from dep_reviewer.gate import Verdict

    outcome = ReviewOutcome(review={"summary": "s", "impacted_code": [
        {"file": "a.js", "line": 1, "description": "d", "covered_by_tests": "no"},
        {"file": "b.js", "line": 2, "description": "tested", "covered_by_tests": "yes"}]})
    default = Config.from_env(BASE_ENV)
    text = main.ticket_text(default, PR, Verdict("fail", ["tests failed"]), outcome, None)
    assert "a.js:1: d" in text and "b.js:2" not in text
    assert main.ticket_text(default, PR, Verdict("needs-human", []), outcome, None) == ""
    assert main.ticket_text(default, PR, Verdict("pass", []), outcome, None) == ""

    strict = Config.from_env({**BASE_ENV, "INPUT_TICKET_ON": "needs-human"})
    assert main.ticket_text(strict, PR, Verdict("needs-human", ["untested"]), outcome, None)
    off = Config.from_env({**BASE_ENV, "INPUT_TICKET_ON": "never"})
    assert main.ticket_text(off, PR, Verdict("fail", []), outcome, None) == ""


def test_config_rejects_bad_ticket_on():
    with pytest.raises(ConfigError, match="ticket-on"):
        Config.from_env({**BASE_ENV, "INPUT_TICKET_ON": "always"})
