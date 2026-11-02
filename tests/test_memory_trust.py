"""Правила доверия к памяти: источники, проверки, отрицания, насыщение важности, дрейф доверия.

Запуск из корня проекта:  python3 -m unittest tests.test_memory_trust -v
Не требует sentence-transformers / faiss: проверяется только GCN-хранилище и чистые правила.
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from GCN import memory_trust as mt  # noqa: E402
from GCN.GCN import MemoryStore  # noqa: E402


class TrustRulesTest(unittest.TestCase):
    def test_negation_by_whole_word_only(self):
        self.assertFalse(mt.has_negation("мне нравится кофе"))
        self.assertFalse(mt.has_negation("небо голубое"))
        self.assertFalse(mt.has_negation("менеджер проекта"))
        self.assertTrue(mt.has_negation("я не согласен"))
        self.assertTrue(mt.has_negation("это никогда не работало"))
        self.assertTrue(mt.has_negation("it is not true"))

    def test_access_importance_saturates(self):
        self.assertEqual(mt.access_importance(0), 0.0)
        self.assertEqual(mt.access_importance(None), 0.0)
        self.assertLess(mt.access_importance(1), mt.access_importance(10))
        self.assertLess(mt.access_importance(10), mt.access_importance(50))
        self.assertAlmostEqual(mt.access_importance(mt.ACCESS_SATURATION), 1.0)
        self.assertEqual(mt.access_importance(10_000), 1.0)  # не растёт бесконечно

    def test_sources_have_different_base_trust(self):
        self.assertLess(mt.base_confidence(mt.SOURCE_ASSISTANT, 1.0), mt.base_confidence(mt.SOURCE_USER, 1.0))
        self.assertEqual(mt.base_confidence(mt.SOURCE_ASSISTANT, 1.0), mt.ASSISTANT_UTTERANCE_CONFIDENCE)
        self.assertEqual(mt.base_confidence("unknown", 0.42), 0.42)

    def test_verification_raises_trust_and_refutation_drops_it(self):
        base = 0.5
        self.assertGreater(mt.trust(base, [mt.VERIFY_OK], 0), base)
        self.assertEqual(mt.trust(base, [mt.VERIFY_OK, mt.VERIFY_WRONG], 0), mt.REFUTED_CONFIDENCE)
        self.assertLess(mt.trust(base, [mt.VERIFY_DISPUTED], 0), base)

    def test_contradictions_penalise_and_bounds_hold(self):
        self.assertLess(mt.trust(0.5, [], 2), mt.trust(0.5, [], 0))
        self.assertEqual(mt.trust(0.99, [mt.VERIFY_OK] * 10, 0), 1.0)
        self.assertEqual(mt.trust(0.0, [], 100), 0.0)

    def test_unknown_statuses_are_not_verification(self):
        # llm_reflection пишет в status тип связи (например CONTRADICTS): это не подтверждение
        self.assertEqual(mt.trust(0.5, ["CONTRADICTS", "RESOLVED"], 0), mt.trust(0.5, [], 0))

    def test_trust_labels(self):
        self.assertEqual(mt.trust_label(mt.SOURCE_ASSISTANT, []), "ответ ассистента, не проверено")
        self.assertEqual(mt.trust_label(mt.SOURCE_USER, []), "со слов пользователя")
        self.assertEqual(mt.trust_label(mt.SOURCE_ASSISTANT, [mt.VERIFY_OK]), "подтверждено")
        self.assertEqual(mt.trust_label(None, [mt.VERIFY_WRONG]), "опровергнуто")
        self.assertIsNone(mt.trust_label(None, []))

    def test_similarity_synapses_threshold_and_cap(self):
        pairs = [(1, 0.95), (2, 0.9), (3, 0.85), (4, 0.7), (5, 0.6)]
        self.assertEqual([i for i, _ in mt.select_similar(pairs, 0.8, 2)], [1, 2])
        self.assertEqual([i for i, _ in mt.select_similar(pairs, 0.8, 10)], [1, 2, 3])
        self.assertEqual(mt.select_similar(pairs, 0.99, 5), [])


class GcnStoreTrustTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(embedding_dim=None)

    def test_assistant_reply_is_not_stored_as_certain(self):
        user_id, ass_id = self.store.add_episode("какой сегодня день?", "сегодня понедельник", "u1")
        self.assertAlmostEqual(self.store.get(ass_id).confidence, mt.ASSISTANT_UTTERANCE_CONFIDENCE)
        self.assertAlmostEqual(self.store.get(user_id).confidence, mt.USER_UTTERANCE_CONFIDENCE)
        self.assertEqual(self.store.get(ass_id).object["source"], mt.SOURCE_ASSISTANT)
        self.assertEqual(self.store.get(user_id).object["source"], mt.SOURCE_USER)

    def test_recomputing_confidence_does_not_drift(self):
        # старый код: бонус за проверку писался обратно в confidence и прибавлялся при каждом пересчёте
        obj_id = self.store.add_fact("факт про проект", "claim", "u1", confidence=0.6)
        self.store.verify(obj_id, "u1", mt.VERIFY_OK, "u1")
        first = self.store.compute_confidence(obj_id)
        for _ in range(20):
            self.store.update(obj_id, {"confidence": self.store.compute_confidence(obj_id)}, "u1")
        self.assertAlmostEqual(self.store.compute_confidence(obj_id), first)
        self.assertAlmostEqual(first, 0.6 + mt.OK_BONUS_EACH)

    def test_verification_and_refutation_through_store(self):
        obj_id = self.store.add_fact("утверждение", "claim", "u1", confidence=0.5)
        self.store.verify(obj_id, "u1", mt.VERIFY_OK, "u1")
        self.assertGreater(self.store.compute_confidence(obj_id), 0.5)
        self.store.verify(obj_id, "u1", mt.VERIFY_WRONG, "u1")
        self.assertAlmostEqual(self.store.compute_confidence(obj_id), mt.REFUTED_CONFIDENCE)

    def test_contradiction_lowers_confidence(self):
        a = self.store.add_fact("первое утверждение", "claim", "u1", confidence=0.7)
        b = self.store.add_fact("второе утверждение", "claim", "u1", confidence=0.7)
        before = self.store.compute_confidence(a)
        self.store.link(a, b, "CONTRADICTS", "u1")
        self.assertLess(self.store.compute_confidence(a), before)


if __name__ == "__main__":
    unittest.main()
