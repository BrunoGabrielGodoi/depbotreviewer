"""Agent-loop tests with a fake Anthropic client (no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from dep_reviewer.claude_review import FINISH_NOW, ReviewError, run_review
from tests.test_report import REVIEW


class FakeTools:
    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return "result"


def usage():
    return NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool_msg(i):
    return NS(stop_reason="tool_use", stop_details=None, usage=usage(),
              content=[NS(type="tool_use", id=f"t{i}", name="search_code", input={"pattern": "x"})])


def final_msg(review=REVIEW):
    return NS(stop_reason="end_turn", stop_details=None, usage=usage(),
              content=[NS(type="text", text=json.dumps(review))])


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kwargs):
        # Snapshot messages: the loop keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.replies.pop(0)
        client = self

        class Ctx:
            def __enter__(self):
                return NS(get_final_message=lambda: reply)

            def __exit__(self, *a):
                return False

        return Ctx()


def run(client, tools=None, max_turns=10):
    return run_review(client, model="m", effort="high", max_turns=max_turns, system="s",
                      user_message="u", tools=tools or FakeTools(), log=lambda *_: None)


def test_tool_loop_then_verdict():
    tools = FakeTools()
    client = FakeClient([tool_msg(1), tool_msg(2), final_msg()])
    out = run(client, tools)
    assert out.risk == "medium" and out.turns == 3
    assert len(tools.calls) == 2
    assert out.usage["input_tokens"] == 30
    req = client.requests[0]
    assert req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in req


def test_turn_limit_nudges_then_disables_tools():
    client = FakeClient([tool_msg(1), tool_msg(2), final_msg()])
    out = run(client, max_turns=2)
    assert out.risk == "medium"
    last_user = client.requests[2]["messages"][-1]["content"]
    assert last_user[-1] == {"type": "text", "text": FINISH_NOW}
    assert client.requests[2]["tool_choice"] == {"type": "none"}


def test_refusal_gives_unknown():
    refusal = NS(stop_reason="refusal", stop_details=NS(category="cyber"), usage=usage(), content=[])
    out = run(FakeClient([refusal]))
    assert out.risk == "unknown" and "cyber" in out.unavailable_reason


def test_pause_turn_resumes():
    pause = NS(stop_reason="pause_turn", stop_details=None, usage=usage(), content=[NS(type="server_tool_use")])
    client = FakeClient([pause, final_msg()])
    assert run(client).risk == "medium"
    assert client.requests[1]["messages"][-1]["role"] == "assistant"


def test_bad_json_raises():
    bad = NS(stop_reason="end_turn", stop_details=None, usage=usage(), content=[NS(type="text", text="not json")])
    with pytest.raises(ReviewError):
        run(FakeClient([bad]))
