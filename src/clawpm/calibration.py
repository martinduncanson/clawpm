"""Calibration metrics over resolved prediction rows (CLAWP-112-004).

Spec: docs/design/calibration-metrics-spec.md section 2.3. Pure functions: the
loader in ``reflect.py`` turns the JSONL corpus into rows and a closure ledger;
everything here is arithmetic over those, with no I/O and no model call.

Row dict keys: project_id, task_id, occurred_at (aware datetime or None),
confidence (1-5 or None), held ("correct"/"incorrect"/"unresolvable"),
duration_ratio (actual / registered prediction, or None), complexity,
miss_category, legacy.

Ledger dict keys: registered, resolved, voided, legacy_resolved.

Every cell reports ``n`` and, when ``n < MIN_N``, ``insufficient_data: True``
with null values. Closure and the miss-category counts are plain tallies and
are not value-gated (spec 4.3 reads 10 registered / 6 resolved as 0.6).
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

MIN_N = 20
CLOSURE_ALARM_BELOW = 0.6  # spec 2.3: closure alarm threshold, judged on the rolling 30-day window
METAREFLECTION_WINDOW_DAYS = 30
METAREFLECTION_MIN_COUNT = 3
VELOCITY_WINDOWS = {"90d": 90, "365d": 365}
VELOCITY_STEP_DAYS = 30
VELOCITY_MIN_WINDOWS = 3

# Spec 2.2 confidence -> probability map (shared with gbrain).
P_BY_CONFIDENCE = {1: 0.55, 2: 0.65, 3: 0.75, 4: 0.85, 5: 0.95}

_RESOLVED = ("correct", "incorrect")
_HELD_VALUES = ("correct", "incorrect", "unresolvable")


def _r4(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_row(row: Any) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(row, malformed)``. A malformed field is nulled, never raised on.

    The corpus is operator-writable JSONL, so any field can hold any JSON
    type. Fields are rejected one by one and the caller counts the rows.
    """
    if not isinstance(row, dict):
        return None, True
    bad = False
    conf = row.get("confidence")
    if conf is not None and not (
        isinstance(conf, int) and not isinstance(conf, bool) and conf in P_BY_CONFIDENCE
    ):
        bad, conf = True, None
    held = row.get("held")
    if held is not None and not (isinstance(held, str) and held in _HELD_VALUES):
        bad, held = True, None
    ratio = row.get("duration_ratio")
    if ratio is not None and not (_is_number(ratio) and ratio > 0):
        bad, ratio = True, None
    complexity = row.get("complexity")
    if complexity is not None and not isinstance(complexity, str):
        bad, complexity = True, None
    miss = row.get("miss_category")
    if miss is not None and not isinstance(miss, str):
        bad, miss = True, None
    profile = row.get("agent_profile")
    if profile is not None and not isinstance(profile, str):
        bad, profile = True, None
    project = row.get("project_id")
    if not isinstance(project, str):
        bad = bad or project is not None
        project = ""
    at = row.get("occurred_at")
    if at is not None and not isinstance(at, datetime):
        bad, at = True, None
    return {
        "project_id": project,
        "task_id": row.get("task_id"),
        "occurred_at": at,
        "confidence": conf,
        "held": held,
        "duration_ratio": ratio,
        "complexity": complexity,
        "agent_profile": profile,
        "miss_category": miss,
        "legacy": bool(row.get("legacy")),
    }, bad


def _gate(n: int) -> bool:
    return n < MIN_N


def _brier_of(scored: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in scored:
        o = 1.0 if row["held"] == "correct" else 0.0
        total += (P_BY_CONFIDENCE[row["confidence"]] - o) ** 2
    return total / len(scored)


def _median_factor(ratios: list[float]) -> float:
    return math.exp(statistics.median(math.log(r) for r in ratios))


def _brier(scored: list[dict[str, Any]], n_unscored: int) -> dict[str, Any]:
    n = len(scored)
    cell: dict[str, Any] = {"n": n, "n_unscored": n_unscored}
    if _gate(n):
        cell.update(insufficient_data=True, value=None)
    else:
        cell.update(insufficient_data=False, value=_r4(_brier_of(scored)))
    return cell


def _curve(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = []
    for conf, p in P_BY_CONFIDENCE.items():
        bucket = [r for r in scored if r["confidence"] == conf]
        n = len(bucket)
        if _gate(n):
            observed = None
        else:
            observed = _r4(sum(r["held"] == "correct" for r in bucket) / n)
        cells.append({
            "confidence": conf,
            "p": p,
            "n": n,
            "observed": observed,
            "insufficient_data": _gate(n),
        })
    return cells


def _overconfidence(curve: list[dict[str, Any]], n_scored: int) -> dict[str, Any]:
    by_conf: dict[str, Any] = {}
    headline = None
    for cell in curve:
        ratio = None
        if cell["observed"] is not None:
            ratio = _r4(cell["observed"] / cell["p"])
        by_conf[str(cell["confidence"])] = {
            "n": cell["n"],
            "ratio": ratio,
            "insufficient_data": cell["insufficient_data"],
        }
        # Highest qualifying bucket wins; 5 is the headline when it qualifies.
        if ratio is not None:
            headline = {"confidence": cell["confidence"], "ratio": ratio, "n": cell["n"]}
    if headline is None:
        return {"n": n_scored, "insufficient_data": True, "headline": None,
                "by_confidence": by_conf}
    return {"n": headline["n"], "insufficient_data": False,
            "headline": {"confidence": headline["confidence"], "ratio": headline["ratio"]},
            "by_confidence": by_conf}


def _bias(ratios: list[float]) -> dict[str, Any]:
    n = len(ratios)
    if _gate(n):
        return {"n": n, "insufficient_data": True, "factor": None, "direction": None}
    factor = _median_factor(ratios)
    if factor < 1:
        direction = "inflated"
    elif factor > 1:
        direction = "optimistic"
    else:
        direction = "calibrated"
    return {"n": n, "insufficient_data": False, "factor": _r4(factor),
            "direction": direction}


def _bias_by_bucket(dur_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Spec 2.3 per-bucket bias: ``_bias`` applied to each slice, same gate."""
    dims = {
        "by_complexity": lambda r: r["complexity"] or "unknown",
        "by_confidence": lambda r: (
            "unset" if r.get("confidence") is None else str(r["confidence"])),
        "by_agent_profile": lambda r: r.get("agent_profile") or "unspecified",
        "by_project": lambda r: r["project_id"] or "unknown",
    }
    out: dict[str, Any] = {}
    for name, key_of in dims.items():
        groups: dict[str, list[float]] = {}
        for row in dur_rows:
            groups.setdefault(key_of(row), []).append(row["duration_ratio"])
        out[name] = {k: _bias(groups[k]) for k in sorted(groups)}
    return out


def _bucket_bias(dur_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-complexity factors at FULL precision; ``_serialise_bucket_bias``
    rounds them for output only, so debiasing never sees a rounded factor."""
    all_ratios = [r["duration_ratio"] for r in dur_rows]
    global_n = len(all_ratios)
    global_factor = None if _gate(global_n) else _median_factor(all_ratios)
    out: dict[str, Any] = {
        "global": {"factor": global_factor, "n": global_n, "source": "global"}
    }
    buckets: dict[str, list[float]] = {}
    for row in dur_rows:
        buckets.setdefault(row["complexity"] or "unknown", []).append(row["duration_ratio"])
    for key in sorted(buckets):
        ratios = buckets[key]
        if not _gate(len(ratios)):
            out[key] = {"factor": _median_factor(ratios), "n": len(ratios),
                        "source": f"complexity={key}"}
        else:
            out[key] = {"factor": global_factor, "n": global_n, "source": "global"}
    return out


def _serialise_bucket_bias(bucket_bias: dict[str, Any]) -> dict[str, Any]:
    return {k: {**v, "factor": _r4(v["factor"])} for k, v in bucket_bias.items()}


_BAND_LOG_TOL = 1e-9


def _in_band(ratio: float, factor: float = 1.0) -> bool:
    """Inclusive 0.5..2.0 band test in log space, tolerant of exp/log float error."""
    return abs(math.log(ratio) - math.log(factor)) <= math.log(2.0) + _BAND_LOG_TOL


def _debiased(dur_rows: list[dict[str, Any]], bucket_bias: dict[str, Any]) -> dict[str, Any]:
    n = len(dur_rows)
    if _gate(n):
        return {"n": n, "insufficient_data": True, "hit_rate": None, "raw_hit_rate": None}
    raw_hits = debiased_hits = 0
    for row in dur_rows:
        r = row["duration_ratio"]
        raw_hits += _in_band(r)
        factor = (bucket_bias.get(row["complexity"] or "unknown") or bucket_bias["global"])["factor"]
        if factor is None or not factor > 0:
            factor = 1.0  # no usable factor: score the raw ratio, never divide by zero
        debiased_hits += _in_band(r, factor)
    return {"n": n, "insufficient_data": False,
            "hit_rate": _r4(debiased_hits / n), "raw_hit_rate": _r4(raw_hits / n)}


def _learning_velocity(scored: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    dated = [r for r in scored if r.get("occurred_at") is not None]
    oldest = min((r["occurred_at"] for r in dated), default=None)
    out: dict[str, Any] = {}
    for key, width in VELOCITY_WINDOWS.items():
        series: list[tuple[int, float]] = []  # (chronological index, brier)
        if oldest is not None:
            k = 0
            while True:
                end = now - timedelta(days=k * VELOCITY_STEP_DAYS)
                if end <= oldest:
                    break
                start = end - timedelta(days=width)
                inside = [r for r in dated if start < r["occurred_at"] <= end]
                if not _gate(len(inside)):
                    series.append((-k, _brier_of(inside)))
                k += 1
        series.reverse()  # oldest -> newest
        if len(series) < VELOCITY_MIN_WINDOWS:
            out[key] = {"n": len(series), "insufficient_data": True, "slope": None,
                        "series": [_r4(b) for _, b in series]}
            continue
        xs = [x for x, _ in series]
        ys = [y for _, y in series]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        var = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in series) / var
        out[key] = {"n": len(series), "insufficient_data": False, "slope": _r4(slope),
                    "series": [_r4(b) for b in ys]}
    return out


def _metareflection(missed: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=METAREFLECTION_WINDOW_DAYS)
    counts: dict[tuple[str, str], int] = {}
    for row in missed:
        at = row.get("occurred_at")
        if at is None or at < cutoff or at > now:
            continue
        key = (row["project_id"] or "", row["miss_category"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"project_id": proj, "miss_category": cat, "count": c}
        for (proj, cat), c in sorted(counts.items())
        if c >= METAREFLECTION_MIN_COUNT
    ]


def _closure(ledger: dict[str, Any]) -> dict[str, Any]:
    registered = int(ledger.get("registered", 0))
    resolved = int(ledger.get("resolved", 0))
    voided = int(ledger.get("voided", 0))
    w_reg = int(ledger.get("registered_30d", 0))
    w_res = int(ledger.get("resolved_30d", 0))
    w_void = int(ledger.get("voided_30d", 0))
    w_rate = round(w_res / w_reg, 4) if w_reg > 0 else None
    base = {
        "registered": registered,
        "resolved": resolved,
        "voided": voided,
        "legacy_resolved": int(ledger.get("legacy_resolved", 0)),
        "window_30d": {"registered": w_reg, "resolved": w_res,
                       "voided": w_void, "rate": w_rate},
    }
    if registered <= 0:
        return {**base, "rate": None, "open_predictions": 0, "void_rate": None,
                "insufficient_data": True, "alarm": None, "alarm_basis": None}
    rate = round(resolved / registered, 4)
    alarm = None
    basis = None
    # Spec 2.3: the alarm judges the rolling 30 days when it has any
    # registrations; an empty window falls back to the all-time rate.
    # Comparison is strict (< threshold): 0.59 alarms, exactly 0.60 does not.
    judged, basis_name = (w_rate, "window_30d") if w_rate is not None else (rate, "all_time")
    if judged < CLOSURE_ALARM_BELOW:
        basis = basis_name
        span = "30-day " if basis_name == "window_30d" else ""
        alarm = (f"{span}closure {judged:.2f} is below {CLOSURE_ALARM_BELOW}: most "
                 "predictions never resolve, so the other numbers are survivors only")
    return {**base, "rate": rate,
            "open_predictions": max(0, registered - resolved - voided),
            "void_rate": round(voided / registered, 4),
            "insufficient_data": False, "alarm": alarm, "alarm_basis": basis}


def compute_calibration(
    rows: list[dict[str, Any]],
    ledger: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the spec 2.3 ``calibration`` block from rows and the closure ledger."""
    now = now or datetime.now(timezone.utc)
    cleaned: list[dict[str, Any]] = []
    rejected_rows = 0
    for raw in rows:
        row, bad = _clean_row(raw)
        rejected_rows += bad
        if row is not None:
            cleaned.append(row)
    rows = cleaned
    resolved = [r for r in rows if r.get("held") in _RESOLVED]
    scored = [r for r in resolved if r.get("confidence") in P_BY_CONFIDENCE]
    n_unscored = len(resolved) - len(scored)
    dur_rows = [r for r in rows
                if r.get("duration_ratio") is not None and r["duration_ratio"] > 0]
    ratios = [r["duration_ratio"] for r in dur_rows]

    curve = _curve(scored)
    bucket_bias = _bucket_bias(dur_rows)

    mae = {"n": len(ratios)}
    if _gate(len(ratios)):
        mae.update(insufficient_data=True, value=None)
    else:
        mae.update(insufficient_data=False,
                   value=_r4(statistics.fmean(abs(math.log(r)) for r in ratios)))

    missed = [r for r in resolved if r["held"] == "incorrect" and r.get("miss_category")]
    dist: dict[str, int] = {}
    for row in missed:
        dist[row["miss_category"]] = dist.get(row["miss_category"], 0) + 1

    return {
        "brier": _brier(scored, n_unscored),
        "calibration_curve": curve,
        "overconfidence_ratio": _overconfidence(curve, len(scored)),
        "mae_log": mae,
        "bias": {**_bias(ratios), **_bias_by_bucket(dur_rows)},
        "bucket_bias_factor": _serialise_bucket_bias(bucket_bias),
        "duration_debiased": _debiased(dur_rows, bucket_bias),
        "learning_velocity": _learning_velocity(scored, now),
        "miss_category_distribution": {"n": len(missed), "counts": dist},
        "metareflection_due": _metareflection(missed, now),
        "closure": _closure(ledger),
        "data_quality": {
            "malformed_lines": int(ledger.get("malformed_lines", 0)),
            "non_object": int(ledger.get("non_object", 0)),
            "unknown_events": int(ledger.get("unknown_events", 0)),
            "rejected_rows": rejected_rows,
        },
    }


def render_data_quality(block: dict[str, Any]) -> list[str]:
    """One clause when any record was skipped or rejected, else none.

    ``render_scorecard`` folds it into the Brier line: the scorecard is exactly
    five lines."""
    dq = block.get("data_quality") or {}
    parts = [f"{dq[k]} {label}" for k, label in (
        ("malformed_lines", "malformed lines"),
        ("non_object", "non-object records"),
        ("unknown_events", "unknown events"),
        ("rejected_rows", "rejected rows"),
    ) if dq.get(k)]
    if not parts:
        return []
    return ["data quality: skipped " + ", ".join(parts)]


def render_scorecard(block: dict[str, Any]) -> list[str]:
    """Five ASCII lines: closure, bias, Brier + n, overconfidence, top miss."""
    cl = block["closure"]
    if cl["rate"] is None:
        line1 = "closure: n/a (no registered predictions)"
    else:
        line1 = (f"closure: {cl['rate']:.2f} ({cl['resolved']}/{cl['registered']} resolved, "
                 f"{cl['open_predictions']} open, void rate {cl['void_rate']:.2f})")
        if cl["alarm"]:
            line1 += f" ALARM: below {CLOSURE_ALARM_BELOW:.2f}"
            if cl.get("alarm_basis") == "window_30d":
                line1 += " (30d)"

    b = block["bias"]
    if b["factor"] is None:
        line2 = f"bias: insufficient data (n={b['n']}, need {MIN_N})"
    else:
        line2 = f"bias: factor {b['factor']:.2f}, {b['direction']} (n={b['n']})"

    br = block["brier"]
    if br["value"] is None:
        line3 = f"brier: insufficient data (n={br['n']}, need {MIN_N})"
    else:
        line3 = f"brier: {br['value']:.4f} (n={br['n']})"
    line3 += "".join("; " + part for part in render_data_quality(block))

    oc = block["overconfidence_ratio"]
    if oc["headline"] is None:
        line4 = f"overconfidence: insufficient data (n={oc['n']}, need {MIN_N})"
    else:
        h = oc["headline"]
        line4 = (f"overconfidence: confidence {h['confidence']} observed/p "
                 f"{h['ratio']:.2f} (n={oc['n']})")

    due = block["metareflection_due"]
    counts = block["miss_category_distribution"]["counts"]
    parts = []
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        parts.append(f"top miss {top[0]} x{top[1]}")
    else:
        parts.append("top miss: none recorded")
    if due:
        parts.append("metareflection due: " + ", ".join(
            f"{d['project_id']}/{d['miss_category']} x{d['count']}" for d in due))
    else:
        parts.append("metareflection due: none")
    line5 = "miss: " + "; ".join(parts)

    return [line1, line2, line3, line4, line5]
