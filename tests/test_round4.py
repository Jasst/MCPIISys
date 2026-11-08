"""Тесты: JSON-схемы, время события, NLI, предиктор переходов (numpy).
Запуск: python -m unittest tests.test_round4 -v
"""
import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from GCN.json_schema import (CONTRADICTION_VERDICT_SCHEMA, FACT_LIST_SCHEMA, validate)  # noqa: E402
from GCN.temporal import normalize_event_time  # noqa: E402
from GCN.nli import NLIJudge, softmax  # noqa: E402
from GCN.transition_model import TransitionModel, fit_with_gate, nearest_candidate  # noqa: E402


class TestSchema(unittest.TestCase):
    def test_valid_verdict(self):
        ok, err = validate({"relation": "true_contradiction", "keep": "B", "reason": "x"},
                           CONTRADICTION_VERDICT_SCHEMA)
        self.assertTrue(ok, err)

    def test_bad_enum_rejected(self):
        ok, _ = validate({"relation": "maybe", "keep": "B", "reason": "x"}, CONTRADICTION_VERDICT_SCHEMA)
        self.assertFalse(ok)

    def test_missing_required_rejected(self):
        ok, _ = validate({"relation": "false_positive", "keep": "both"}, CONTRADICTION_VERDICT_SCHEMA)
        self.assertFalse(ok)

    def test_extra_field_rejected(self):
        ok, _ = validate({"relation": "false_positive", "keep": "both", "reason": "", "x": 1},
                         CONTRADICTION_VERDICT_SCHEMA)
        self.assertFalse(ok)

    def test_fact_list(self):
        good = {"facts": [{"text": "Сервер запущен в 2024 году", "source": "a", "date": "2024"}]}
        self.assertTrue(validate(good, FACT_LIST_SCHEMA)[0])
        self.assertFalse(validate({"facts": [{"source": "a"}]}, FACT_LIST_SCHEMA)[0])
        self.assertFalse(validate({"facts": "нет"}, FACT_LIST_SCHEMA)[0])


class TestTemporal(unittest.TestCase):
    def test_canonical_forms(self):
        self.assertEqual(normalize_event_time("2024-03-05"), "2024-03-05")
        self.assertEqual(normalize_event_time("2024-3"), "2024-03")
        self.assertEqual(normalize_event_time("2024"), "2024")

    def test_unknown_and_invalid(self):
        for raw in ("unknown", "", None, "вчера", "2024-13", "2024-02-31x", "1800"):
            self.assertIsNone(normalize_event_time(raw), raw)


class TestNLI(unittest.TestCase):
    def test_softmax_sums_to_one(self):
        self.assertAlmostEqual(sum(softmax([1.0, 2.0, 3.0])), 1.0)

    def test_decide_returns_label_above_threshold(self):
        judge = NLIJudge(scorer=lambda a, b: [0.0, 5.0, 0.0])  # entailment
        self.assertEqual(judge.decide("a", "b", threshold=0.8), "entailment")

    def test_decide_none_below_threshold(self):
        judge = NLIJudge(scorer=lambda a, b: [0.0, 0.0, 0.0])  # равномерно
        self.assertIsNone(judge.decide("a", "b", threshold=0.8))

    def test_unavailable_returns_none(self):
        judge = NLIJudge()  # без модели
        self.assertFalse(judge.available)
        self.assertIsNone(judge.decide("a", "b", threshold=0.5))

    def test_wrong_logit_count_rejected(self):
        with self.assertRaises(ValueError):
            NLIJudge(scorer=lambda a, b: [1.0, 2.0]).probabilities("a", "b")


class TestTransitionModel(unittest.TestCase):
    def test_gradient_matches_finite_difference(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(8, 5))
        Y = rng.normal(size=(8, 5))
        m = TransitionModel(dim=5, hidden=4, seed=2)

        def loss_of(model):
            from GCN.transition_model import _normalize_rows
            P = model.predict(X)
            T = _normalize_rows(Y)
            return float(np.mean(1.0 - np.sum(P * T, axis=1)))

        base = loss_of(m)
        eps = 1e-6
        i, j = 1, 2
        m.W2[i, j] += eps
        numeric = (loss_of(m) - base) / eps
        m.W2[i, j] -= eps
        # аналитический градиент через один шаг fit с lr=1 (dW = -lr * grad)
        before = m.W2.copy()
        m.fit(X, Y, epochs=1, lr=1.0, l2=0.0)
        analytic = -(m.W2 - before)[i, j]
        self.assertAlmostEqual(analytic, numeric, delta=1e-4)

    def test_learns_structured_transitions_and_passes_gate(self):
        rng = np.random.default_rng(0)
        K, dim = 6, 16
        centers = rng.normal(size=(K, dim))
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        nxt = rng.permutation(K)  # детерминированный переход: кластер i -> nxt[i]
        cur = rng.integers(0, K, size=300)
        X = centers[cur] + rng.normal(0, 0.05, size=(300, dim))
        Y = centers[nxt[cur]] + rng.normal(0, 0.05, size=(300, dim))
        model, report = fit_with_gate(X, Y, centers, k=1, epochs=400, seed=0)
        self.assertTrue(report["accepted"], report)
        self.assertGreater(report["model_hit"], report["baseline_hit"])

    def test_random_transitions_rejected_by_gate(self):
        rng = np.random.default_rng(3)
        K, dim = 6, 16
        centers = rng.normal(size=(K, dim))
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        X = rng.normal(size=(200, dim))
        Y = centers[rng.integers(0, K, size=200)]  # цель не зависит от входа
        model, report = fit_with_gate(X, Y, centers, k=1, epochs=200, seed=0)
        self.assertIsNone(model)
        self.assertFalse(report["accepted"])

    def test_small_data_not_accepted(self):
        model, report = fit_with_gate(np.zeros((5, 4)), np.zeros((5, 4)), np.eye(4), k=1)
        self.assertIsNone(model)

    def test_save_load_roundtrip(self):
        import tempfile
        m = TransitionModel(dim=4, hidden=3, seed=5)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.npz")
            m.save(path)
            m2 = TransitionModel.load(path)
        x = np.ones((1, 4))
        np.testing.assert_allclose(m.predict(x), m2.predict(x))


if __name__ == "__main__":
    unittest.main()
