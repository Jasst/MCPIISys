"""Тесты тормозных взаимодействий и стабилизатора (чистая логика, без numpy/FAISS/LLM).

Запуск: python -m unittest tests.test_inhibition_stabilizer
"""
import os
import tempfile
import unittest

from GCN import inhibition as inh
from GCN import stabilizer as stab


class LateralRerankTests(unittest.TestCase):
    def test_no_similar_pairs_keeps_order(self):
        items = [{"score": 0.9}, {"score": 0.7}, {"score": 0.5}]
        out = inh.lateral_rerank(items, lambda i, j: 0.1, threshold=0.9, lam=0.5)
        self.assertEqual([round(o["score"], 2) for o in out], [0.9, 0.7, 0.5])

    def test_duplicate_is_pushed_below_distinct_item(self):
        # 0 и 1 почти одинаковы, 2 — другая тема с заметно меньшим скором
        items = [{"score": 0.9}, {"score": 0.85}, {"score": 0.6}]
        sims = {(0, 1): 0.99, (0, 2): 0.1, (1, 2): 0.1}

        def sim(i, j):
            return sims.get((min(i, j), max(i, j)), 0.0)

        out = inh.lateral_rerank(items, sim, threshold=0.9, lam=0.6)
        self.assertEqual(out[0]["score"], 0.9)
        self.assertEqual(out[1]["score"], 0.6, "дубликат должен уступить место другой теме")

    def test_returns_same_items_without_mutating_inputs(self):
        items = [{"score": 0.5}, {"score": 0.4}]
        out = inh.lateral_rerank(items, lambda i, j: 0.95, threshold=0.9, lam=0.3)
        self.assertEqual(len(out), 2)
        self.assertNotIn("score_adj", items[0])

    def test_empty_input(self):
        self.assertEqual(inh.lateral_rerank([], lambda i, j: 0.0), [])

    def test_penalty_zero_below_threshold_and_bounded(self):
        self.assertEqual(inh.lateral_penalty(0.5, 0.9, 0.5, 1.0), 0.0)
        self.assertLessEqual(inh.lateral_penalty(1.0, 0.9, 0.5, 1.0), 0.5 + 1e-12)


class InhibitoryRuleTests(unittest.TestCase):
    def test_delta_sign(self):
        self.assertGreater(inh.inhibitory_delta(0.9, 0.5, 0.01), 0)  # перевозбуждён -> тормоз растёт
        self.assertLess(inh.inhibitory_delta(0.1, 0.5, 0.01), 0)     # тихий узел -> тормоз слабеет

    def test_clamp(self):
        self.assertEqual(inh.clamp_inhibitory(-0.2, 1.0), 0.0)
        self.assertEqual(inh.clamp_inhibitory(3.0, 1.0), 1.0)


class StabilizerMetricsTests(unittest.TestCase):
    def test_hub_share_and_inh_ratio(self):
        edges = [(1, 2, 1.0, 1), (3, 2, 1.0, 1), (4, 2, 1.0, 1), (5, 6, 1.0, 1), (5, 7, 1.0, -1)]
        m = stab.compute_metrics(edges, active_fraction=0.1)
        self.assertAlmostEqual(m.hub_share, 3.0 / 4.0)
        self.assertAlmostEqual(m.inh_ratio, 1.0 / 5.0)
        self.assertEqual(m.n_inh, 1)

    def test_empty_graph(self):
        m = stab.compute_metrics([], active_fraction=0.0)
        self.assertEqual((m.n_nodes, m.hub_share, m.inh_ratio), (0, 0.0, 0.0))


class StabilizerProposalTests(unittest.TestCase):
    def test_noisy_output_raises_lateral_lambda_within_step(self):
        m = stab.Metrics(frac_active=0.6, n_excit=10, hub_share=0.05)
        props = stab.propose(m, dict(stab.DEFAULT_TUNE))
        lam = [p for p in props if p["param"] == "lateral_lambda"]
        self.assertEqual(len(lam), 1)
        self.assertAlmostEqual(lam[0]["new"] - lam[0]["old"], stab.LAMBDA_STEP, places=4)

    def test_hub_lowers_cap(self):
        m = stab.Metrics(hub_share=0.5, n_excit=10, frac_active=0.1)
        props = stab.propose(m, dict(stab.DEFAULT_TUNE))
        self.assertTrue(any(p["param"] == "node_strength_cap" and p["new"] < p["old"] for p in props))

    def test_no_proposal_when_healthy(self):
        m = stab.Metrics(hub_share=0.05, n_excit=10, frac_active=0.15)
        self.assertEqual(stab.propose(m, dict(stab.DEFAULT_TUNE)), [])


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.m = stab.Metrics(frac_active=0.6, n_excit=10)
        self.props = stab.propose(self.m, dict(stab.DEFAULT_TUNE))

    def test_approve_applies_proposal(self):
        raw = '```json\n{"decisions": [{"id": 0, "approve": true}]}\n```'
        ids = stab.parse_review(raw, self.props)
        new = stab.apply_approved(stab.DEFAULT_TUNE, self.props, ids)
        self.assertGreater(new["lateral_lambda"], stab.DEFAULT_TUNE["lateral_lambda"])

    def test_garbage_reply_applies_nothing(self):
        for raw in ["", "не JSON", '{"decisions": "oops"}', "{broken"]:
            ids = stab.parse_review(raw, self.props)
            self.assertEqual(ids, [])
            self.assertEqual(stab.apply_approved(stab.DEFAULT_TUNE, self.props, ids),
                             stab.DEFAULT_TUNE)

    def test_ai_cannot_invent_or_truthy_approve(self):
        raw = '{"decisions": [{"id": 99, "approve": true}, {"id": 0, "approve": "yes"}]}'
        self.assertEqual(stab.parse_review(raw, self.props), [])

    def test_ai_cannot_exceed_bounds_even_if_proposal_is_wild(self):
        wild = [{"id": 0, "param": "lateral_lambda", "old": 0.3, "new": 99.0, "reason": "x"}]
        new = stab.apply_approved(stab.DEFAULT_TUNE, wild, [0])
        self.assertLessEqual(new["lateral_lambda"], stab.BOUNDS["lateral_lambda"][1])
        self.assertLessEqual(new["lateral_lambda"] - 0.3, stab.LAMBDA_STEP + 1e-9)

    def test_unknown_param_ignored(self):
        bogus = [{"id": 0, "param": "delete_all_facts", "old": 0, "new": 1, "reason": "x"}]
        self.assertEqual(stab.apply_approved(stab.DEFAULT_TUNE, bogus, [0]), stab.DEFAULT_TUNE)

    def test_prompt_contains_metrics_and_proposals(self):
        prompt = stab.build_review_prompt(self.m, self.props)
        self.assertIn("МЕТРИКИ", prompt)
        self.assertIn("lateral_lambda", prompt)
        self.assertIn('"decisions"', prompt)


class StatePersistenceTests(unittest.TestCase):
    def test_roundtrip_and_clamp_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stabilizer_state.json")
            self.assertEqual(stab.load_state(path), stab.DEFAULT_TUNE)
            stab.save_state(path, {"lateral_lambda": 0.4, "node_strength_cap": 2.5})
            self.assertEqual(stab.load_state(path)["lateral_lambda"], 0.4)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"lateral_lambda": 42}')
            self.assertLessEqual(stab.load_state(path)["lateral_lambda"], 0.6)


if __name__ == "__main__":
    unittest.main()
