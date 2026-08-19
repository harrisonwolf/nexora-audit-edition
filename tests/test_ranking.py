from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexora_audit.ranking import Dimension, qualified_weighted_mean, safety_evidence_is_material, school_evidence_is_material


def _safety(overlap: float = 1.0) -> dict[str, object]:
    return {
        "zip_overlap_fractions": {"00001": overlap},
        "contributing_zip_weights": {"00001": 1.0},
        "contributing_zip_results": {
            "00001": {
                "composite_score": 72.0,
                "overlap_fraction": overlap,
                "weight": 1.0,
            }
        },
    }


class QualifiedRankingTests(unittest.TestCase):
    def test_positive_weighted_missing_evidence_suppresses_the_composite(self) -> None:
        result = qualified_weighted_mean(
            (
                Dimension("schools", 50.0, 80.0, False),
                Dimension("safety", 50.0, 70.0, True),
            )
        )
        self.assertIsNone(result.score)
        self.assertEqual(result.unsupported, ("schools",))

    def test_zero_weighted_missing_evidence_does_not_answer_an_unasked_question(self) -> None:
        result = qualified_weighted_mean(
            (
                Dimension("schools", 0.0, None, False),
                Dimension("safety", 100.0, 70.0, True),
            )
        )
        self.assertEqual(result.score, 70.0)
        self.assertEqual(result.unsupported, ())

    def test_weighted_mean_is_exact_after_qualification(self) -> None:
        result = qualified_weighted_mean(
            (
                Dimension("schools", 3.0, 80.0, True),
                Dimension("safety", 1.0, 60.0, True),
            )
        )
        self.assertEqual(result.score, 75.0)

    def test_school_coverage_is_fail_closed_for_malformed_values(self) -> None:
        self.assertTrue(school_evidence_is_material({"coverage_fraction_raw": 0.95}))
        for value in (0.949, None, True, "1", math.nan, math.inf, -1.0, 1.1):
            with self.subTest(value=value):
                self.assertFalse(school_evidence_is_material({"coverage_fraction_raw": value}))

    def test_safety_requires_complete_cumulative_and_key_aligned_evidence(self) -> None:
        self.assertTrue(safety_evidence_is_material(_safety(0.95)))
        self.assertFalse(safety_evidence_is_material(_safety(0.949)))
        missing_result = _safety()
        missing_result["contributing_zip_results"] = {}
        self.assertFalse(safety_evidence_is_material(missing_result))
        inconsistent = _safety()
        inconsistent["contributing_zip_results"]["00001"]["overlap_fraction"] = 0.5  # type: ignore[index]
        self.assertFalse(safety_evidence_is_material(inconsistent))

    def test_safety_coverage_is_cumulative_across_aligned_components(self) -> None:
        evidence = {
            "zip_overlap_fractions": {"00001": 0.5, "00002": 0.45},
            "contributing_zip_weights": {"00001": 0.6, "00002": 0.4},
            "contributing_zip_results": {
                "00001": {"composite_score": 72.0, "overlap_fraction": 0.5, "weight": 0.6},
                "00002": {"composite_score": 65.0, "overlap_fraction": 0.45, "weight": 0.4},
            },
        }
        self.assertTrue(safety_evidence_is_material(evidence))
        evidence["zip_overlap_fractions"]["00002"] = 0.44
        evidence["contributing_zip_results"]["00002"]["overlap_fraction"] = 0.44
        self.assertFalse(safety_evidence_is_material(evidence))

    def test_rejects_negative_and_non_finite_weights(self) -> None:
        for weight in (-1.0, math.nan, math.inf):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    qualified_weighted_mean((Dimension("schools", weight, 80.0, True),))

    def test_zero_total_weight_has_an_explicit_neutral_result(self) -> None:
        result = qualified_weighted_mean(
            (
                Dimension("schools", 0.0, None, False),
                Dimension("safety", 0.0, None, False),
            )
        )
        self.assertEqual(result.score, 50.0)
        self.assertEqual(result.unsupported, ())

    def test_huge_finite_weights_cannot_overflow_the_composite(self) -> None:
        result = qualified_weighted_mean(
            (
                Dimension("schools", 1e308, 80.0, True),
                Dimension("safety", 1e308, 20.0, True),
            )
        )
        self.assertEqual(result.score, 50.0)
        self.assertTrue(result.score is not None and math.isfinite(result.score))
