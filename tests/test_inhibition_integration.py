"""Интеграционные проверки тормозных синапсов и стабилизатора на реальных методах
CognitiveMemory (с заглушками тяжёлых зависимостей, как в smoke_memory_stubs.py).

Запуск: python -m unittest tests.test_inhibition_integration
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
st = types.ModuleType("sentence_transformers"); st.SentenceTransformer = object
sys.modules.setdefault("sentence_transformers", st)
sys.modules.setdefault("faiss", types.ModuleType("faiss"))

import GCN.memory_graph as mg  # noqa: E402
from GCN import stabilizer as stab  # noqa: E402


def make_obj(base_dir=None):
    obj = mg.CognitiveMemory.__new__(mg.CognitiveMemory)
    obj.synapses, obj.facts_by_id, obj.semantic_facts = {}, {}, []
    obj.graph = mg.defaultdict(set)
    obj.gcn_store = None
    obj._dirty = False
    obj._sync_synapse_to_gcn = lambda *a, **k: None
    obj._lock = asyncio.Lock()
    obj.user_id = "test-user"
    if base_dir is not None:
        obj.base_dir = Path(base_dir)
    return obj


def add_fact(obj, fid, access=0, activation=0.0):
    f = mg.Fact(id=fid, text=f"fact {fid}", type="claim", timestamp=0.0, keywords=[],
                access_count=access, activation=activation)
    obj.facts_by_id[fid] = f
    obj.semantic_facts.append(f)
    return f


def link(obj, src, tgt, w, sign=1):
    obj.synapses[(src, tgt)] = mg.Synapse(src, tgt, weight=w, sign=sign)
    obj.graph[src].add(tgt)


class InhibitionInSpreadingTests(unittest.TestCase):
    def setUp(self):
        self.obj = make_obj()
        for i in (1, 2, 3):
            add_fact(self.obj, i)
        mg.INHIBITORY_SYNAPSES_ENABLED = True

    def tearDown(self):
        mg.INHIBITORY_SYNAPSES_ENABLED = False

    def test_inhibitory_edge_suppresses_active_neighbour(self):
        # seeds 1 и 2 активны; 1 тормозит 2 => 2 должен падать по сравнению с отсутствием тормоза
        link(self.obj, 1, 2, 0.9, sign=-1)
        link(self.obj, 2, 1, 0.9, sign=-1)
        amap = asyncio.run(self.obj.spread_activation([1, 2]))
        self.assertLess(amap[2], 1.0)
        self.assertLess(amap[1], 1.0)

    def test_inhibitory_edge_does_not_excite(self):
        # 3 соседствует с 1 только тормозом: активации 3 появиться не должно
        link(self.obj, 1, 3, 1.0, sign=-1)
        amap = asyncio.run(self.obj.spread_activation([1]))
        self.assertNotIn(3, amap)

    def test_excitatory_path_unchanged_when_flag_off(self):
        mg.INHIBITORY_SYNAPSES_ENABLED = False
        link(self.obj, 1, 2, 0.9, sign=1)
        amap = asyncio.run(self.obj.spread_activation([1]))
        self.assertIn(2, amap)


class HomeostasisTests(unittest.TestCase):
    def test_busy_node_strengthens_inhibition_quiet_node_weakens(self):
        obj = make_obj()
        add_fact(obj, 1, access=100)   # самый активный
        add_fact(obj, 2, access=1)     # тихий
        add_fact(obj, 3, access=50)
        link(obj, 9, 1, 0.5, sign=-1)  # тормоз на активный узел
        link(obj, 9, 2, 0.5, sign=-1)  # тормоз на тихий узел
        obj.facts_by_id[9] = mg.Fact(id=9, text="x", type="claim", timestamp=0, keywords=[])
        before = {k: s.weight for k, s in obj.synapses.items()}
        obj._inhibitory_homeostasis()
        self.assertGreater(obj.synapses[(9, 1)].weight, before[(9, 1)])
        self.assertLess(obj.synapses[(9, 2)].weight, before[(9, 2)])

    def test_weights_stay_within_bounds(self):
        obj = make_obj()
        add_fact(obj, 1, access=1000)
        add_fact(obj, 2, access=0)
        link(obj, 2, 1, 0.99, sign=-1)
        for _ in range(500):
            obj._inhibitory_homeostasis()
        w = obj.synapses[(2, 1)].weight
        self.assertGreaterEqual(w, 0.0)
        self.assertLessEqual(w, mg.INHIBITORY_MAX_WEIGHT)

    def test_excitatory_synapses_untouched(self):
        obj = make_obj()
        add_fact(obj, 1, access=100)
        add_fact(obj, 2, access=0)
        link(obj, 2, 1, 0.4, sign=1)
        obj._inhibitory_homeostasis()
        self.assertEqual(obj.synapses[(2, 1)].weight, 0.4)


class ScalingTests(unittest.TestCase):
    def test_inhibitory_weight_not_counted_in_node_cap(self):
        obj = make_obj()
        obj._tune = {"lateral_lambda": 0.3, "node_strength_cap": 1.0}
        link(obj, 1, 5, 0.6, sign=1)
        link(obj, 2, 5, 5.0, sign=-1)  # огромный тормоз не должен урезать возбуждающие связи
        obj._scale_synapses()
        self.assertEqual(obj.synapses[(1, 5)].weight, 0.6)


class StabilizerIntegrationTests(unittest.TestCase):
    def test_deterministic_then_ai_approval_applies_bounded_change(self):
        with tempfile.TemporaryDirectory() as d:
            obj = make_obj(d)
            obj._get_tune()  # загрузит значения по умолчанию из конфига
            add_fact(obj, 1, activation=0.9)
            add_fact(obj, 2, activation=0.9)
            add_fact(obj, 3, activation=0.9)
            add_fact(obj, 4, activation=0.0)
            link(obj, 1, 2, 0.5)
            obj._stabilize_deterministic()
            props = obj._stab_proposals
            self.assertTrue(any(p["param"] == "lateral_lambda" for p in props))

            async def fake_llm(messages, temp=0.0, max_tokens=0):
                ids = [p["id"] for p in props]
                return '{"decisions": [' + ",".join(
                    f'{{"id": {i}, "approve": true}}' for i in ids) + ']}'

            mg.call_llm = fake_llm
            asyncio.run(obj._ai_review_stabilizer())
            new = obj._get_tune()
            self.assertGreater(new["lateral_lambda"], stab.DEFAULT_TUNE["lateral_lambda"] - 1e-9)
            self.assertLessEqual(new["lateral_lambda"] - 0.3, stab.LAMBDA_STEP + 1e-9)
            self.assertTrue(os.path.exists(os.path.join(d, "stabilizer_state.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "stabilizer_log.jsonl")))

    def test_no_llm_reply_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            obj = make_obj(d)
            before = dict(obj._get_tune())
            add_fact(obj, 1, activation=0.9)
            add_fact(obj, 2, activation=0.9)
            obj._stabilize_deterministic()

            async def broken_llm(messages, temp=0.0, max_tokens=0):
                raise RuntimeError("LM Studio недоступна")

            mg.call_llm = broken_llm
            asyncio.run(obj._ai_review_stabilizer())
            self.assertEqual(obj._get_tune(), before)
            self.assertFalse(os.path.exists(os.path.join(d, "stabilizer_state.json")))

    def test_ai_cannot_see_user_text_only_numbers(self):
        m = stab.Metrics(frac_active=0.9, n_excit=3)
        prompt = stab.build_review_prompt(m, stab.propose(m, dict(stab.DEFAULT_TUNE)))
        self.assertNotIn("fact", prompt)


class LateralInhibitionTests(unittest.TestCase):
    def test_near_duplicates_are_spread_apart(self):
        obj = make_obj()
        # два почти одинаковых эмбеддинга и один другой
        vecs = np.array([[1.0, 0.0], [0.999, 0.045], [0.0, 1.0]], dtype=float)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

        class FakeEmb:
            def encode(self, texts, convert_to_numpy=True, batch_size=1):
                return vecs[:len(texts)]

        obj.use_embeddings = True
        obj.embedder = FakeEmb()
        obj._tune = {"lateral_lambda": 0.6, "node_strength_cap": 3.0}
        result = [
            {"text": "a", "score": 0.9, "gcn_id": "1"},
            {"text": "a2", "score": 0.85, "gcn_id": "2"},
            {"text": "b", "score": 0.6, "gcn_id": "3"},
        ]
        mg.EMBEDDING_PASSAGE_PREFIX = ""
        out = obj._lateral_inhibit(result)
        self.assertEqual([r["gcn_id"] for r in out], ["1", "3", "2"])
        self.assertEqual(len(out), 3, "набор кандидатов не должен меняться")
        self.assertNotIn("score_adj", out[0])


if __name__ == "__main__":
    unittest.main()
