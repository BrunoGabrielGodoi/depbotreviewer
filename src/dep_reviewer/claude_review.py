"""The Claude agent loop: investigate with tools, then return a structured verdict."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .repo_tools import TOOL_DEFINITIONS, RepoTools, ToolError
from .schema import REVIEW_SCHEMA

# Server-side fallback: if the model declines for policy reasons, the API re-runs the
# request on Anthropic's recommended fallback model instead of returning a refusal.
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 64_000
# Turns allowed after the "give your verdict now" nudge before giving up.
GRACE_TURNS = 3

SERVER_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 20},
]

FINISH_NOW = (
    "You have reached the investigation limit. Do not call any more tools. Give your final "
    "review now as JSON matching the schema, based on what you have found, and lower your "
    "confidence for anything you could not verify."
)


class ReviewError(Exception):
    pass


@dataclass
class ReviewOutcome:
    review: dict | None
    unavailable_reason: str | None = None
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def risk(self) -> str:
        return self.review["risk"] if self.review else "unknown"

    @property
    def confidence(self) -> str:
        return self.review["confidence"] if self.review else "low"


def _add_usage(total: dict[str, int], usage: Any) -> None:
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[key] = total.get(key, 0) + (getattr(usage, key, None) or 0)


def parse_review(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ReviewError(f"final reply was not valid JSON: {e}") from None
    missing = [k for k in REVIEW_SCHEMA["required"] if k not in data]
    if missing:
        raise ReviewError(f"final reply is missing {', '.join(missing)}")
    return data


def run_review(
    client: anthropic.Anthropic,
    *,
    model: str,
    effort: str,
    max_turns: int,
    system: str,
    user_message: str,
    tools: RepoTools,
    log=print,
) -> ReviewOutcome:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    usage: dict[str, int] = {}
    nudged_at: int | None = None
    turns = 0

    while True:
        turns += 1
        if nudged_at is not None and turns > nudged_at + GRACE_TURNS:
            raise ReviewError("Claude did not give a verdict after being asked to finish")

        request: dict[str, Any] = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=[*TOOL_DEFINITIONS, *SERVER_TOOLS],
            # Caches the growing conversation so each turn re-reads it at cache rates.
            cache_control={"type": "ephemeral"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            betas=BETAS,
            fallbacks="default",
        )
        if nudged_at is not None:
            request["tool_choice"] = {"type": "none"}

        with client.beta.messages.stream(**request) as stream:
            msg = stream.get_final_message()
        _add_usage(usage, msg.usage)

        if msg.stop_reason == "refusal":
            category = getattr(msg.stop_details, "category", None) if msg.stop_details else None
            return ReviewOutcome(
                review=None,
                unavailable_reason=f"Claude declined to review this PR (category: {category or 'unspecified'}).",
                turns=turns,
                usage=usage,
            )
        if msg.stop_reason == "max_tokens":
            raise ReviewError(f"Claude's reply hit the {MAX_TOKENS}-token output limit")
        if msg.stop_reason == "pause_turn":
            # A long-running server tool (web search/fetch) paused; send the turn back to continue.
            messages.append({"role": "assistant", "content": msg.content})
            continue

        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            text_blocks = [b.text for b in msg.content if b.type == "text"]
            if not text_blocks:
                raise ReviewError(f"Claude ended without a verdict (stop_reason={msg.stop_reason})")
            review = parse_review(text_blocks[-1])
            log(f"Review finished in {turns} turns: risk={review['risk']} confidence={review['confidence']}")
            return ReviewOutcome(review=review, turns=turns, usage=usage)

        messages.append({"role": "assistant", "content": msg.content})
        results: list[dict[str, Any]] = []
        for tu in tool_uses:
            log(f"  turn {turns}: {tu.name} {json.dumps(tu.input)[:200]}")
            try:
                output = tools.execute(tu.name, tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})
            except ToolError as e:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(e), "is_error": True})

        if turns >= max_turns and nudged_at is None:
            results.append({"type": "text", "text": FINISH_NOW})
            nudged_at = turns
            log(f"  reached max-turns ({max_turns}); asking Claude for its verdict")
        messages.append({"role": "user", "content": results})
