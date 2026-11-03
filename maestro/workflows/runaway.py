"""P13 W13: runaway detection.

An attempt that failed after spending more than `factor` x the p90 of its role, task class
and planner complexity band gets no more retries: it enters the P12B ladder at the
diagnoser. `shadow` records the trigger and changes nothing; a band with fewer than
`MIN_SAMPLES` measured attempts never triggers.

Spend is what W5 already compares, the frozen relative cost times the attempt's complete
token count. It measures spend, not context, and this module launches nothing: the runner's
ordinary `fail_quality` path opens the existing failure edge.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping

MIN_SAMPLES = 10
DEFAULT_FACTOR = 2.0
MODES = ("on", "shadow", "off")

#: Where an attempt's band is frozen in its `binding_json`. W5's band lives in the pace
#: decision, which only the `pace` objective records, so W13 keeps its own.
BINDING_KEY = "runaway_band"
TRIGGER_EVENT = "RunawayTriggered"
FORECAST_EVENT = "PlannerForecast"
FORECAST_FILE = "forecast.json"
FALSE_ALARM = "runaway_false_alarm"
PLANNER_ROLE = "planner"


def valid_band(value: Any) -> int | None:
    """A forecast is exactly an integer 1-5; `3.0`, `"3"` and `True` are not forecasts."""
    return value if type(value) is int and 1 <= value <= 5 else None


def effective_mode(routing: Any) -> str:
    """The operator's `/runaway` control, read now, else the configured mode."""
    try:
        control = routing.runaway_control_reader()
    except Exception:  # noqa: BLE001 - an unreadable control keeps the configured mode
        control = None
    return control if control in MODES else routing.runaway_mode


def forecast(routing: Any, task_id: str) -> int | None:
    """This task's forecast while detection is on. Off never asks the reader."""
    if effective_mode(routing) == "off":
        return None
    try:
        return valid_band(routing.planner_complexity_reader(task_id))
    except Exception:  # noqa: BLE001 - an unreadable forecast is no forecast
        return None


def binding_stamp(routing: Any, node: Any, task_id: str) -> dict | None:
    """The band this attempt is compared in. The class is the task spec's: a route's
    `shape_key.task_class` is `ordinary` for every task, so it would pool them all."""
    band = forecast(routing, task_id)
    role = getattr(node, "agent_definition_ref", None)
    if band is None or not role:
        return None
    try:
        task_class = routing.task_spec(task_id).task_class
    except Exception:  # noqa: BLE001 - detection is advisory; an unstamped attempt is unmeasured
        return None
    return {"role": str(role), "task_class": str(task_class), "complexity": band}


def forecast_contract(routing: Any, node: Any) -> str | None:
    """The forecast's brief and schema, which exist only while detection is on (B10)."""
    if getattr(node, "agent_definition_ref", None) != PLANNER_ROLE:
        return None
    if effective_mode(routing) == "off":
        return None
    from maestro.workflows.telemetry import forecast_prompt

    return forecast_prompt(FORECAST_FILE)


def _document(value: Any) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _spend(row: Mapping) -> float | None:
    from maestro.workflows.pace import attempt_cost_units

    return attempt_cost_units(_document(row["binding_json"]),
                              _document(row["usage_settle_json"]))


def p90(values: Iterable[float]) -> float:
    """Nearest rank, so the answer is a spend that actually happened."""
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def assess(rows: Iterable[Mapping], *, attempt: Mapping, factor: float) -> dict | None:
    """The trigger's facts when `attempt` passes the threshold, otherwise `None`."""
    stamp = _document(attempt["binding_json"]).get(BINDING_KEY)
    spend = _spend(attempt)
    if not isinstance(stamp, dict) or spend is None:
        return None
    samples = []
    for row in rows:
        if row["attempt_id"] == attempt["attempt_id"]:
            continue
        if _document(row["binding_json"]).get(BINDING_KEY) != stamp:
            continue
        cost = _spend(row)
        if cost is not None:
            samples.append(cost)
    if len(samples) < MIN_SAMPLES:
        return None
    baseline = p90(samples)
    threshold = factor * baseline
    if spend <= threshold:
        return None
    return {**stamp, "spend": spend, "p90": baseline, "factor": factor,
            "threshold": threshold, "samples": len(samples)}


def stamped_attempts(store: Any) -> list:
    return store.conn.execute(
        "SELECT attempt_id, binding_json, usage_settle_json FROM attempts"
        " WHERE binding_json LIKE ? AND usage_settle_json IS NOT NULL",
        (f'%"{BINDING_KEY}"%',),
    ).fetchall()


def record_forecast(store: Any, task_id: str, complexity: int, *, run_id: str,
                    attempt_id: str, at: str) -> None:
    store.append_event(
        FORECAST_EVENT, run_id, payload={"task_id": str(task_id), "complexity": complexity},
        run_id=run_id, attempt_id=attempt_id, recorded_at=at,
    )


def latest_forecast(store: Any, task_id: str) -> int | None:
    """The newest forecast a planner node made for this task, across its runs."""
    rows = store.conn.execute(
        "SELECT payload FROM events WHERE type = ? ORDER BY sequence DESC", (FORECAST_EVENT,),
    ).fetchall()
    for row in rows:
        payload = _document(row["payload"])
        if payload.get("task_id") == str(task_id):
            return valid_band(payload.get("complexity"))
    return None


def contract_text(trigger: Mapping) -> str:
    """What a diagnosis the trigger opened early is told, and its extra verdict."""
    return (
        "RUNAWAY DETECTION opened this diagnosis early. The attempt failed after spending "
        f"{trigger['spend']:g} cost units; the threshold was {trigger['threshold']:g} "
        f"({trigger['factor']:g} x the p90 of {trigger['samples']} attempts by a "
        f"{trigger['role']} on {trigger['task_class']} work at complexity "
        f"{trigger['complexity']}). Its remaining retries were not used. Look for why it "
        "cost this much as well as why it failed. If you find a fault, give your verdict as "
        f'usual. If the spend was justified and nothing is wrong, use "verdict": "{FALSE_ALARM}" '
        'with a one-line "reason": the node runs once more, unchanged.'
    )
