"""Conservative composition: a positive weight requires material evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


COVERAGE_FLOAT_TOLERANCE = 1e-9
SCHOOL_COVERAGE_FLOOR = 0.95
SAFETY_COVERAGE_FLOOR = 0.95


@dataclass(frozen=True)
class Dimension:
    name: str
    weight: float
    score: float | None
    evidenced: bool


@dataclass(frozen=True)
class CompositeResult:
    score: float | None
    unsupported: tuple[str, ...]


def _finite_number_within(value: object, *, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < low or number > high:
        return None
    return number


def _meets_coverage_floor(coverage: float, floor: float) -> bool:
    return coverage >= floor - COVERAGE_FLOAT_TOLERANCE


def school_evidence_is_material(explanation: object) -> bool:
    """Require nearly complete pre-renormalization assignment geometry."""
    if not isinstance(explanation, Mapping):
        return False
    coverage = _finite_number_within(explanation.get("coverage_fraction_raw"), low=0.0, high=1.0)
    return coverage is not None and _meets_coverage_floor(coverage, SCHOOL_COVERAGE_FLOOR)


def safety_evidence_is_material(explanation: object) -> bool:
    """Require complete retained geometry and aligned contributing results."""
    if not isinstance(explanation, Mapping):
        return False
    overlaps = explanation.get("zip_overlap_fractions")
    weights = explanation.get("contributing_zip_weights")
    results = explanation.get("contributing_zip_results")
    if not all(isinstance(value, Mapping) for value in (overlaps, weights, results)):
        return False
    if not all(isinstance(key, str) for key in (*overlaps, *weights, *results)):
        return False
    if not (set(overlaps) == set(weights) == set(results)):
        return False

    retained_total = 0.0
    for key, recorded_overlap in overlaps.items():
        overlap = _finite_number_within(recorded_overlap, low=0.0, high=1.0)
        weight = _finite_number_within(weights.get(key), low=0.0, high=1.0)
        result = results.get(key)
        if overlap is None or weight is None or not isinstance(result, Mapping):
            return False
        result_overlap = _finite_number_within(result.get("overlap_fraction"), low=0.0, high=1.0)
        result_weight = _finite_number_within(result.get("weight"), low=0.0, high=1.0)
        score = _finite_number_within(result.get("composite_score"), low=0.0, high=100.0)
        if result_overlap is None or result_weight is None or score is None:
            return False
        if abs(result_overlap - overlap) > COVERAGE_FLOAT_TOLERANCE:
            return False
        if abs(result_weight - weight) > COVERAGE_FLOAT_TOLERANCE:
            return False
        retained_total += overlap
    return _meets_coverage_floor(min(1.0, max(0.0, retained_total)), SAFETY_COVERAGE_FLOOR)


def qualified_weighted_mean(dimensions: Sequence[Dimension]) -> CompositeResult:
    """Compose only after every positively weighted dimension is supported."""
    checked: list[tuple[Dimension, float, float | None]] = []
    for dimension in dimensions:
        if not dimension.name:
            raise ValueError("dimension name must be non-empty")
        weight = _finite_number_within(dimension.weight, low=0.0, high=float("inf"))
        if weight is None:
            raise ValueError(f"dimension {dimension.name!r} has an invalid weight")
        score = (
            None
            if dimension.score is None
            else _finite_number_within(dimension.score, low=0.0, high=100.0)
        )
        checked.append((dimension, weight, score))

    unsupported = tuple(
        dimension.name
        for dimension, weight, score in checked
        if weight > 0 and (score is None or dimension.evidenced is not True)
    )
    if unsupported:
        return CompositeResult(score=None, unsupported=unsupported)

    weighted = [(weight, score) for _dimension, weight, score in checked if weight > 0 and score is not None]
    if not weighted:
        return CompositeResult(score=50.0, unsupported=())
    scale = max(weight for weight, _score in weighted)
    scaled = [(weight / scale, value) for weight, value in weighted]
    total_weight = sum(weight for weight, _score in scaled)
    score = sum(weight * value for weight, value in scaled) / total_weight
    return CompositeResult(score=score, unsupported=())
