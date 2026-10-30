"""The strategy under test: Claude reads the snapshot and returns target weights.

The decision comes back through structured outputs (output_config.format with a JSON
schema). Forcing a tool call with tool_choice is rejected by claude-sonnet-5-5, so the
schema is the reliable way to get exactly one well-formed decision. The response is
then validated with pydantic; an invalid response is retried once with the validation
error included, and a second failure means no trades for this strategy this run.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import anthropic

from ..costs import usage_dict
from .base import DecisionError, InvalidDecision, Snapshot, Strategy, TargetDecision, decision_json_schema, parse_model_decision

PROMPT_PATH = Path(__file__).parent / "prompts" / "claude_system.txt"
MAX_ATTEMPTS = 2


def _fmt(x: float) -> str:
    return f"{x:g}"


def render_system_prompt(market: str, rules: dict) -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").format(
        market=market,
        max_position_weight=_fmt(rules["max_position_weight"]),
        max_invested_weight=_fmt(rules["max_invested_weight"]),
        round_trip_cost_bps=_fmt(rules["round_trip_cost_bps"]),
    )


def _to_dict(resp) -> dict:
    for attr in ("to_dict", "model_dump"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            return fn()
    return {"repr": repr(resp)}


def extract_decision_json(resp) -> dict:
    if resp.stop_reason == "refusal":
        raise InvalidDecision("the model declined to answer (stop_reason refusal)")
    if resp.stop_reason == "max_tokens":
        raise InvalidDecision("the response was cut off at max_tokens before the decision was complete")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise InvalidDecision("the response contained no decision")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidDecision(f"the response was not valid JSON ({e.msg})") from e


class ClaudeStrategy(Strategy):
    is_claude = True

    def __init__(self, cfg, *, config, market: str, cost_tracker, client=None):
        super().__init__(cfg)
        self.model = cfg.model
        self.settings = config.claude
        self.market = market
        self.cost_tracker = cost_tracker
        self._client = client

    @property
    def client(self):
        if self._client is None:
            # The SDK retries rate-limit (429), overloaded (529), server (5xx) and connection
            # errors with exponential backoff, max_retries times.
            self._client = anthropic.Anthropic(
                timeout=self.settings.request_timeout_seconds,
                max_retries=self.settings.max_retries,
            )
        return self._client

    def _meta(self, attempts: list[dict]) -> dict:
        usage = {k: sum(a.get("usage", {}).get(k, 0) for a in attempts) for k in usage_dict({})}
        return {
            "model": self.model,
            "effort": self.settings.effort,
            "attempts": len(attempts),
            "usage": usage,
            "cost_usd": sum(a.get("cost_usd", 0.0) for a in attempts),
            "latency_ms": sum(a.get("latency_ms", 0) for a in attempts),
            "responses": attempts,
        }

    def decide(self, snapshot: Snapshot) -> TargetDecision:
        system = render_system_prompt(snapshot.market, snapshot.risk)
        user = json.dumps(snapshot.payload(), separators=(",", ":"))
        schema = decision_json_schema(snapshot.universe)
        attempts: list[dict] = []
        error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            content = user
            if error is not None:
                content += (
                    f"\n\nYour previous response was invalid: {error}\n"
                    "Return a corrected decision that follows the schema and the rules."
                )
            estimate = self.cost_tracker.estimate(self.model, len(system) + len(content), self.settings.max_tokens)
            self.cost_tracker.check(snapshot.now, estimate)  # raises BudgetExceeded

            started = time.monotonic()
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.settings.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    output_config={
                        "effort": self.settings.effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                )
            except anthropic.APIError as e:
                attempts.append({"attempt": attempt, "error": f"{type(e).__name__}: {e}"})
                raise DecisionError(f"Claude API error: {type(e).__name__}: {e}", meta=self._meta(attempts)) from e
            latency_ms = int((time.monotonic() - started) * 1000)

            usage = usage_dict(resp.usage)
            cost = self.cost_tracker.record(
                now=snapshot.now,
                run_id=snapshot.run_id,
                market=self.market,
                strategy_id=self.id,
                model=self.model,
                usage=usage,
                request_id=getattr(resp, "id", None),
            )
            record = {
                "attempt": attempt,
                "model": getattr(resp, "model", self.model),
                "request_id": getattr(resp, "id", None),
                "stop_reason": resp.stop_reason,
                "usage": usage,
                "cost_usd": cost,
                "latency_ms": latency_ms,
                "raw_response": _to_dict(resp),
            }
            attempts.append(record)

            try:
                decision = parse_model_decision(extract_decision_json(resp), snapshot.universe, source="claude")
            except InvalidDecision as e:
                error = str(e)
                record["validation_error"] = error
                continue
            decision.meta = self._meta(attempts)
            return decision

        raise DecisionError(f"invalid response after {MAX_ATTEMPTS} attempts: {error}", meta=self._meta(attempts))
