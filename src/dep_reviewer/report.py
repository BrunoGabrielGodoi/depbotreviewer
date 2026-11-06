"""Render the review as a PR comment."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .claude_review import ReviewOutcome
from .gate import FAIL, NEEDS_HUMAN, PASS, Checkout, Verdict
from .prompts import CheckRun

COMMENT_MARKER = "<!-- claude-dependabot-reviewer -->"
# GitHub rejects comments over 65,536 characters.
MAX_COMMENT_CHARS = 60_000
MAX_ROWS = 40

VERDICT_HEADING = {
    PASS: "✅ Verified: safe to merge",
    NEEDS_HUMAN: "👀 Needs human review",
    FAIL: "❌ Do not merge",
}
LABEL_COLORS = {PASS: "0e8a16", NEEDS_HUMAN: "fbca04", FAIL: "d93f0b"}
LABEL_DESCRIPTIONS = {
    PASS: "Dependency update verified by build, tests and Claude review",
    NEEDS_HUMAN: "Dependency update needs a human decision before merging",
    FAIL: "Dependency update breaks the build, tests or code",
}
RISK = {"low": "🟢 low", "medium": "🟡 medium", "high": "🔴 high", "unknown": "⚪ unknown"}
AFFECTS = {"yes": "⚠️ yes", "no": "no", "unclear": "❓ unclear"}
COVERED = {"yes": "✅ tested", "no": "❌ no test", "unknown": "❓ test coverage unknown"}


@dataclass(frozen=True)
class RenderContext:
    server_url: str
    repository: str
    model: str
    checkout: Checkout
    test: CheckRun | None
    run_url: str | None = None


def neutralize(text: str) -> str:
    """Stop model-written text from @-mentioning people or teams."""
    return re.sub(r"@(?=[A-Za-z0-9_-])", "@​", text)


def cell(text: str) -> str:
    return neutralize(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def file_link(ctx: RenderContext, path: str, line: int) -> str:
    path = path.removeprefix("./").lstrip("/")
    url = f"{ctx.server_url}/{ctx.repository}/blob/{ctx.checkout.sha}/{urllib.parse.quote(path)}"
    label = path
    if line > 0:
        url += f"#L{line}"
        label += f":{line}"
    return f"[`{cell(label)}`]({url})"


def _safe_url(url: str) -> str:
    return url if re.match(r"^https?://[^\s)]+$", url) else ""


def render_comment(outcome: ReviewOutcome, verdict: Verdict, ctx: RenderContext, max_rows: int = MAX_ROWS) -> str:
    out: list[str] = [COMMENT_MARKER, f"## {VERDICT_HEADING[verdict.status]}", ""]
    out += [f"- {neutralize(r)}" for r in verdict.reasons]
    out.append("")

    tested = "no build or tests run"
    if ctx.test is not None:
        tested = f"`{ctx.test.command}` {ctx.test.describe()}"
    out += [f"**Tested:** {tested}, on {ctx.checkout.description}.  ", f"**Claude risk rating:** {RISK[outcome.risk]}"]
    r = outcome.review
    if r is not None:
        out[-1] += f" (confidence: {r['confidence']})"
    out.append("")

    if r is not None:
        out += [neutralize(r["summary"]).strip(), ""]

        if r["dependencies"]:
            out += ["### Updates", "", "| Package | Ecosystem | From | To | Type | Scope |", "|---|---|---|---|---|---|"]
            for d in r["dependencies"]:
                name = cell(d["name"]) + ("" if d["direct"] else " (transitive)")
                out.append(
                    f"| {name} | {cell(d['ecosystem'])} | {cell(d['from_version'])} | {cell(d['to_version'])} "
                    f"| {d['update_type']} | {d['scope']} |"
                )
            out.append("")

        relevant = [c for c in r["upstream_changes"] if c["kind"] not in ("feature", "other") or c["affects_repo"] != "no"]
        if relevant:
            out += ["### Upstream changes that matter", "", "| Package | Kind | Change | Affects this repo |", "|---|---|---|---|"]
            for c in relevant[:max_rows]:
                change = cell(c["description"])
                url = _safe_url(c["source_url"])
                if url:
                    change += f" ([source]({url}))"
                out.append(f"| {cell(c['dependency'])} | {c['kind']} | {change} | {AFFECTS[c['affects_repo']]} |")
            if len(relevant) > max_rows:
                out.append(f"\n_{len(relevant) - max_rows} more not shown._")
            out.append("")

        if r["impacted_code"]:
            # Untested call sites first: they are what stands between this PR and a pass.
            sites = sorted(r["impacted_code"], key=lambda i: i["covered_by_tests"] == "yes")
            out += ["### Affected call sites", ""]
            for i in sites[:max_rows]:
                out.append(
                    f"- {COVERED[i['covered_by_tests']]} · {file_link(ctx, i['file'], i['line'])}: "
                    f"{neutralize(i['description']).strip()}"
                )
            if len(sites) > max_rows:
                out.append(f"- _{len(sites) - max_rows} more not shown._")
            out.append("")

        out += ["### Test assessment", "", neutralize(r["test_assessment"]).strip(), ""]

        if r["recommendations"]:
            out += ["### Recommendations", ""]
            out += [f"- {neutralize(x).strip()}" for x in r["recommendations"]]
            out.append("")

    footer = f"<sub>Reviewed by {cell(ctx.model)} in {outcome.turns} turns"
    if ctx.run_url:
        footer += f" · [workflow run]({ctx.run_url})"
    footer += ". Only passing tests on the merge result can produce a pass. Claude's review can only block or escalate.</sub>"
    out.append(footer)

    body = "\n".join(out)
    if len(body) > MAX_COMMENT_CHARS and max_rows > 5:
        return render_comment(outcome, verdict, ctx, max_rows=max_rows // 2)
    return body
