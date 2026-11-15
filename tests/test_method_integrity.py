"""Регрессия: каждый вызов self.метод(...) в модулях должен иметь определение.
Ловит случаи, когда при правке файла случайно удалён соседний метод (так и случилось с
_compute_prediction_error: из-за этого ответ переставал выводиться в чат)."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["routes/ai_assistant.py", "GCN/memory_graph.py", "GCN/llm_client.py"]


def _defs(src):
    return set(re.findall(r"^\s*(?:async )?def (\w+)\(", src, re.M))


class TestMethodIntegrity(unittest.TestCase):
    def test_every_self_call_is_defined(self):
        for rel in FILES:
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            defined = _defs(src) | set(re.findall(r"self\.(\w+)\s*=", src))
            called = set(re.findall(r"self\.(\w+)\(", src))
            missing = sorted(c for c in called if c not in defined)
            self.assertEqual(missing, [], f"{rel}: вызываются, но не определены: {missing}")


if __name__ == "__main__":
    unittest.main()
