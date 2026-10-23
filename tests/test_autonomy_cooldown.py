"""Кулдаун исследования целей и отсутствие ранних LLM-вызовов.
Проверяет логику без запуска LLM: методы вызываются на минимальном стабе."""
import sys, time, types
sys.path.insert(0, "/home/claude/proj")
from GCN import autonomy as A


def make_engine():
    e = types.SimpleNamespace(_goal_researched_at={})
    e.goal_on_cooldown = types.MethodType(A.AutonomyEngine.goal_on_cooldown, e)
    return e


def test_goal_cooldown():
    e = make_engine()
    assert e.goal_on_cooldown("цель") is False            # ещё не исследовалась
    e._goal_researched_at["цель"] = time.time()
    assert e.goal_on_cooldown("цель") is True             # только что исследована
    e._goal_researched_at["цель"] = time.time() - A.GOAL_RESEARCH_COOLDOWN_SECONDS - 1
    assert e.goal_on_cooldown("цель") is False            # кулдаун истёк
    assert e.goal_on_cooldown("другая") is False
    print("1 ok: кулдаун цели работает, истекает, не влияет на другие цели")


def test_timers_not_zero_at_start():
    src = open("/home/claude/proj/GCN/autonomy.py", encoding="utf-8").read()
    assert "self._last_goal_decompose: float = time.time()" in src
    assert "self._last_motivation_tick: float = time.time()" in src
    print("2 ok: таймеры декомпозиции и мотивации стартуют с текущего времени")


def test_activity_not_set_on_controller_creation():
    src = open("/home/claude/proj/routes/ai_assistant.py", encoding="utf-8").read()
    assert "self._last_activity_time = 0.0" in src
    # реальное сообщение по-прежнему ставит активность
    assert src.count("self._last_activity_time = time.time()") >= 2
    print("3 ok: открытие приложения не считается активностью, сообщение — считается")


if __name__ == "__main__":
    for t in [test_goal_cooldown, test_timers_not_zero_at_start, test_activity_not_set_on_controller_creation]:
        t()
    print("ALL PASSED")
