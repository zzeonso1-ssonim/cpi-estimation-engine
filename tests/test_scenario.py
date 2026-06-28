"""
시나리오 확률 테스트 — §9.1.

실행:  python tests/test_scenario.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.uncertainty import (scenario_probabilities, probs_from_distribution,
                                probs_from_risk, net_event_risk, interval)


def _sum100(p):
    return abs(p["base"] + p["upside"] + p["downside"] - 100.0) < 0.05


def test_sum_100():
    assert _sum100(scenario_probabilities(3.14, 0.3, mean_error_pp=0.0, risk_score=0.0))
    assert _sum100(scenario_probabilities(3.14, 0.3, mean_error_pp=-0.27, risk_score=0.3))
    assert _sum100(scenario_probabilities(3.14, None, risk_score=0.5))   # MAE 없음


def test_symmetric_when_neutral():
    """편향0·리스크0 → 상·하방 대칭."""
    p = scenario_probabilities(3.14, 0.3, mean_error_pp=0.0, risk_score=0.0)
    assert abs(p["upside"] - p["downside"]) < 0.1


def test_underestimate_skews_upside():
    """과소추정 편향(mean_error<0) → 상방 확률 > 하방(§10.1 R-4 연결)."""
    p = scenario_probabilities(3.14, 0.3, mean_error_pp=-0.27, risk_score=0.0)
    assert p["upside"] > p["downside"], p


def test_risk_score_tilts():
    """상방 리스크 점수 → 상방 확률 증가."""
    up = probs_from_risk(0.6)
    assert up["upside"] > up["downside"]
    down = probs_from_risk(-0.6)
    assert down["downside"] > down["upside"]


def test_net_event_risk_sign():
    class R:  # event_rows 모사
        def __init__(self, c): self.contrib_pp = c
    # 상방 우세
    assert net_event_risk([R(0.05), R(0.03)]) > 0
    # 하방 우세(유가 하락 등)
    assert net_event_risk([R(-0.14), R(-0.05)]) < 0


def test_interval():
    lo, hi = interval(3.14, 0.3)
    assert (round(lo, 2), round(hi, 2)) == (2.84, 3.44)
    assert interval(3.14, None) is None


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1; print(f"  FAIL  {name}: {e}")
    # 진단: 5월 편향 반영 헤드라인 시나리오
    p = scenario_probabilities(3.14, 0.31, mean_error_pp=-0.27, risk_score=-0.7)
    print(f"\n[진단] 6월 헤드라인 시나리오(MAE0.31·과소편향·유가하락 리스크): "
          f"Base {p['base']}% / Upside {p['upside']}% / Downside {p['downside']}%")
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
