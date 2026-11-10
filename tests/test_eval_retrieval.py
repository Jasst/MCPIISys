"""Тесты метрик hit@k и MRR (без зависимостей проекта).

Запуск: python -m unittest tests.test_eval_retrieval
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tools import eval_retrieval as ev  # noqa: E402


class MetricTests(unittest.TestCase):
    def test_hit_and_rank(self):
        ranked = ["Пьёт кофе утром", "Работает в офисе на Сенной", "Живёт в Амстердаме"]
        self.assertTrue(ev.hit_at_k(ranked, ["офис"], k=5))
        self.assertEqual(ev.first_relevant_rank(ranked, ["офис"], k=5), 2)
        self.assertAlmostEqual(ev.reciprocal_rank(ranked, ["офис"], k=5), 0.5)

    def test_case_insensitive(self):
        self.assertTrue(ev.is_relevant("Работает В ОФИСЕ", ["офисе"]))
        self.assertTrue(ev.is_relevant("Работает В ОФИСЕ", ["работает в"]))

    def test_k_cuts_results(self):
        ranked = ["a", "b", "нужный факт"]
        self.assertFalse(ev.hit_at_k(ranked, ["нужный"], k=2))
        self.assertTrue(ev.hit_at_k(ranked, ["нужный"], k=3))
        self.assertEqual(ev.reciprocal_rank(ranked, ["нужный"], k=2), 0.0)

    def test_no_relevant_or_empty(self):
        self.assertFalse(ev.hit_at_k([], ["x"], k=5))
        self.assertFalse(ev.hit_at_k(["abc"], ["zzz"], k=5))
        self.assertFalse(ev.is_relevant("abc", [""]))  # пустая подстрока не должна совпадать

    def test_summary(self):
        per = [{"hit": True, "rr": 1.0}, {"hit": True, "rr": 0.5}, {"hit": False, "rr": 0.0}]
        s = ev.summarize(per)
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["hit_at_k"], round(2 / 3, 4))
        self.assertAlmostEqual(s["mrr"], 0.5)
        self.assertEqual(ev.summarize([])["n"], 0)

    def test_load_questions_rejects_incomplete_rows(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write('{"query": "вопрос"}\n')
            name = fh.name
        try:
            with self.assertRaises(ValueError):
                ev.load_questions(name)
        finally:
            os.unlink(name)


if __name__ == "__main__":
    unittest.main()
