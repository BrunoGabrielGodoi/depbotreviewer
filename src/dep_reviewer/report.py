"""Render the review as a PR comment."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .claude_review import ReviewOutcome
from .prompts import CheckRun

COMMENT_MARKER = "<!-- claude-dependabot-reviewer -->"
# GitHub rejects comments over 65,536 characters.
MAX_COMMENT_CHARS = 60_000
MAX_ROWS = 40

RISK_BADGE = {
    "low": "🟢 Low risk",
    "medium": "🟡 Medium risk",
    "high": "🔴 High risk",
    "unknown": "⚪ Review unavailable",
}
LABEL_COLORS = {"low": "0e8a16", "medium": "fbca04", "high": "d93f0b", "unknown": "8b949e"}
AFFECTS = {"yes": "⚠️ yes", "no": "no", "unclear": "❓ unclear"}


@dataclass(frozen=True)
class RenderContext:
    server_url: str
    repository: str
    head_sha: str
    model: str
    test: CheckRun | None
    run_url: str | None = None


def neutralize(text: str) -> str:
    """Stop model-written text from @-mentioning people or teams."""
    return re.sub(r"@(?=[A-Za-z0-9_-])", "@​", text)


def cell(text: str) -> str:
    return neutralize(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def file_link(ctx: RenderContext, path: str, line: int) -> str:
    path = path.removeprefix("./").lstrip("/")
    url = f"{ctx.server_url}/{ctx.repository}/blob/{ctx.head_sha}/{urllib.parse.quote(path)}"
    label = path
    if line > 0:
        url += f"#L{line}"
        label += f":{line}"
    return f"[`{cell(label)}`]({url})"


def _safe_url(url: str) -> str:
    return url if re.match(r"^https?://[^\s)]+$", url) else ""


def _rows(items: list, limit: int) -> tuple[list, int]:
    return items[:limit], max(0, len(items) - limit)


def render_comment(outcome: ReviewOutcome, ctx: RenderContext, max_rows: int = MAX_ROWS) -> str:
    out: list[str] = [COMMENT_MARKER, f"## {RISK_BADGE[outcome.risk]} · Claude dependency review", ""]
    r = outcome.review

    if r is None:
        out += [
            neutralize(outcome.unavailable_reason or "The review could not be completed."),
            "",
            "Review this update manually.",
        ]
    else:
        out += [f"**Confidence:** {r['confidence']}", "", neutralize(r["summary"]).strip(), ""]

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
            shown, hidden = _rows(relevant, max_rows)
            out += ["### Upstream changes that matter", "", "| Package | Kind | Change | Affects this repo |", "|---|---|---|---|"]
            for c in shown:
                change = cell(c["description"])
                url = _safe_url(c["source_url"])
                if url:
                    change += f" ([source]({url}))"
                out.append(f"| {cell(c['dependency'])} | {c['kind']} | {change} | {AFFECTS[c['affects_repo']]} |")
            if hidden:
                out.append(f"\n_{hidden} more not shown._")
            out.append("")

        if r["impacted_code"]:
            shown, hidden = _rows(r["impacted_code"], max_rows)
            out += ["### Where this repo is affected", ""]
            for i in shown:
                out.append(f"- {file_link(ctx, i['file'], i['line'])}: {neutralize(i['description']).strip()}")
            if hidden:
                out.append(f"- _{hidden} more not shown._")
            out.append("")

        out += ["### Tests", ""]
        if ctx.test is not None:
            out.append(f"`{ctx.test.command}` {ctx.test.describe()}.")
            out.append("")
        out += [neutralize(r["test_assessment"]).strip(), ""]

        if r["recommendations"]:
            out += ["### Recommendations", ""]
            out += [f"- {neutralize(x).strip()}" for x in r["recommendations"]]
            out.append("")

    footer = f"<sub>Reviewed by {cell(ctx.model)} at {ctx.head_sha[:7]} in {outcome.turns} turns"
    if ctx.run_url:
        footer += f" · [workflow run]({ctx.run_url})"
    footer += ". This is an automated assessment, so check it before you merge.</sub>"
    out.append(footer)

    body = "\n".join(out)
    if len(body) > MAX_COMMENT_CHARS and max_rows > 5:
        return render_comment(outcome, ctx, max_rows=max_rows // 2)
    return body
