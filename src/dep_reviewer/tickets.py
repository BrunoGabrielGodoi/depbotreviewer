"""Ready-to-paste ticket text (for Linear, Jira or any tracker) for PRs that don't pass."""

from __future__ import annotations

from dataclasses import dataclass

from .report import neutralize

MAX_UNTESTED = 20
MAX_DESCRIPTION = 20000


@dataclass(frozen=True)
class TicketContent:
    title: str
    description: str  # Markdown, which both Linear and Jira accept when pasted


def build_content(*, repository: str, pr: dict, verdict: str, reasons: list[str], summary: str | None,
                  untested: list[str], pr_url: str, run_url: str | None) -> TicketContent:
    pr_title = pr.get("title", "").strip() or f"PR #{pr.get('number')}"
    label = {"fail": "breaks the build", "needs-human": "needs human review"}.get(verdict, verdict)
    title = f"Dependabot: {pr_title} {label} ({repository}#{pr.get('number')})"
    lines = [
        f"The dependency gate gave **{verdict}** for [{repository}#{pr.get('number')}]({pr_url}).",
        "",
        "**Why:**",
        *[f"- {r}" for r in reasons],
    ]
    if summary:
        lines += ["", "**Summary:**", summary.strip()]
    if untested:
        lines += ["", "**Affected code without test coverage:**", *[f"- {u}" for u in untested[:MAX_UNTESTED]]]
    lines += ["", f"Pull request: {pr_url}"]
    if run_url:
        lines.append(f"Workflow run: {run_url}")
    description = "\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 20] + "\n\n(truncated)"
    return TicketContent(title=title[:250], description=description)


def _fence(text: str) -> str:
    """A backtick fence longer than any run of backticks in `text`."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def render_ticket_section(content: TicketContent) -> str:
    """Collapsible PR-comment section with the title and description in copyable code blocks."""
    title, description = neutralize(content.title), neutralize(content.description)
    tf, df = _fence(title), _fence(description)
    return "\n".join([
        "",
        "<details>",
        "<summary>📋 Ticket text to paste into Linear or Jira</summary>",
        "",
        "**Title**",
        "",
        tf,
        title,
        tf,
        "",
        "**Description**",
        "",
        f"{df}markdown",
        description,
        df,
        "",
        "</details>",
        "",
    ])
