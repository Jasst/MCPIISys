"""Тесты чистой логики: пластичность, онлайн-обучение ранжирования, калибровка.
Запуск: python -m unittest tests.test_improvements -v   (без faiss/sentence_transformers)
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from GCN.plasticity import PlasticityParams, stdp_delta, stabilized_plasticity, normalize_strength  # noqa: E402
from GCN.learned_rerank import RerankLearner  # noqa: E402
from GCN.calibration import best_threshold, load_calibration, save_calibration  # noqa: E402

P = PlasticityParams(ltp=0.01, ltd=0.005, window=5.0, base_rate=0.02)


class TestSTDP(unittest.TestCase):
    def test_causal_order_potentiates(self):
        self.assertGreater(stdp_delta(1.0, 0.5, P), 0)

    def test_anticausal_order_depresses(self):
        self.assertLess(stdp_delta(-1.0, 0.5, P), 0)

    def test_closer_pairs_change_more(self):
        self.assertGreater(stdp_delta(0.5, 0.5, P), stdp_delta(4.0, 0.5, P))

    def test_outside_window_is_weak_counter_not_timing(self):
        d = stdp_delta(60.0, 0.5, P)
        self.assertGreater(d, 0)
        self.assertLess(d, stdp_delta(1.0, 0.5, P))

    def test_direction_is_asymmetric(self):
        # user->assistant (dt>0) и assistant->user (dt<0) получают разные знаки
        self.assertEqual(stdp_delta(2.0, 0.5, P) > 0, True)
        self.assertEqual(stdp_delta(-2.0, 0.5, P) > 0, False)


class TestMetaplasticity(unittest.TestCase):
    def test_plasticity_decreases_with_confirmations(self):
        vals = [stabilized_plasticity(n, P) for n in (0, 5, 20, 100)]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_floor_respected(self):
        self.assertEqual(stabilized_plasticity(10**6, P), P.floor)

    def test_new_synapse_keeps_initial(self):
        self.assertAlmostEqual(stabilized_plasticity(0, P), P.initial_plasticity)


class TestNormalizeStrength(unittest.TestCase):
    def test_hub_incoming_capped(self):
        w = {(i, 0): 1.0 for i in range(1, 6)}  # пять входящих к узлу 0
        normalize_strength(w, cap=2.0)
        self.assertLessEqual(sum(w.values()), 2.0 + 1e-9)

    def test_small_nodes_untouched(self):
        w = {(1, 2): 0.1, (3, 4): 0.2}
        changed = normalize_strength(w, cap=2.0)
        self.assertEqual(changed, set())
        self.assertEqual(w, {(1, 2): 0.1, (3, 4): 0.2})

    def test_min_weight_floor(self):
        w = {(i, 0): 0.05 for i in range(1, 100)}
        normalize_strength(w, cap=0.5, min_weight=0.01)
        self.assertTrue(all(v >= 0.01 - 1e-12 for v in w.values()))


PRIOR = {"semantic": 0.4, "graph": 0.3, "freshness": 0.15, "confidence": 0.05, "evidence": 0.1}


class TestRerankLearner(unittest.TestCase):
    def test_learns_to_trust_semantic_over_graph(self):
        L = RerankLearner(PRIOR, lr=0.2, l2=0.0)
        good = {"semantic": 0.9, "graph": 0.1, "freshness": 0.5, "confidence": 0.5, "evidence": 0.5}
        bad = {"semantic": 0.1, "graph": 0.9, "freshness": 0.5, "confidence": 0.5, "evidence": 0.5}
        before = L.weights()
        for _ in range(60):
            L.feedback(good, "confirm")
            L.feedback(bad, "refute")
        after = L.weights()
        self.assertGreater(after["semantic"], before["semantic"])
        self.assertLess(after["graph"], before["graph"])

    def test_weights_normalized_and_nonnegative(self):
        L = RerankLearner(PRIOR, lr=0.5, l2=0.0)
        for _ in range(20):
            L.feedback({k: 1.0 for k in PRIOR}, "refute")
        w = L.weights()
        self.assertAlmostEqual(sum(w.values()), 1.0, places=9)
        self.assertTrue(all(v >= 0 for v in w.values()))

    def test_l2_pulls_weights_toward_prior(self):
        x = {"semantic": 1.0, "graph": 0, "freshness": 0, "confidence": 0, "evidence": 0}
        free = RerankLearner(PRIOR, lr=0.05, l2=0.0)
        reg = RerankLearner(PRIOR, lr=0.05, l2=5.0)
        for _ in range(20):
            free.feedback(x, "confirm")
            reg.feedback(x, "confirm")
        self.assertLess(abs(reg.w["semantic"] - PRIOR["semantic"]),
                        abs(free.w["semantic"] - PRIOR["semantic"]))

    def test_persistence_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rerank.json")
            L = RerankLearner(PRIOR, path=path)
            L.feedback({k: 0.5 for k in PRIOR}, "confirm")
            L2 = RerankLearner(PRIOR, path=path)
            self.assertEqual(L2.updates, 1)
            self.assertEqual(L2.w, L.w)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            L3 = RerankLearner(PRIOR, path=path)
            self.assertEqual(L3.w, dict(PRIOR))

    def test_bad_verdict_rejected(self):
        with self.assertRaises(ValueError):
            RerankLearner(PRIOR).feedback({}, "maybe")

    def test_missing_feature_in_prior_rejected(self):
        with self.assertRaises(ValueError):
            RerankLearner({"semantic": 1.0})


class TestCalibration(unittest.TestCase):
    def test_separable_data_gets_perfect_f1(self):
        scores = [0.9, 0.85, 0.8, 0.3, 0.2, 0.1]
        labels = [True, True, True, False, False, False]
        thr, f1, p, r = best_threshold(scores, labels)
        self.assertAlmostEqual(f1, 1.0)
        self.assertTrue(0.3 < thr <= 0.8)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            best_threshold([], [])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cal.json")
            self.assertEqual(load_calibration(path), {})
            save_calibration(path, {"IDENTITY_HANDSHAKE_THRESHOLD": 0.62})
            self.assertEqual(load_calibration(path)["IDENTITY_HANDSHAKE_THRESHOLD"], 0.62)


if __name__ == "__main__":
    unittest.main()
