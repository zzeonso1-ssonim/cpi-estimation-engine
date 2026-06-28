"""
이벤트 계수 레이어 테스트 — §4.5 라(기여도식)·다(중요도)·§7(경고).
§4.5 라의 예시 3건을 그대로 골든값으로 검증.

실행:  python tests/test_event_engine.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.event_engine import (Event, headline_contrib, classify_importance,
                                  bucket_event_adj, event_breakdown, effective_events,
                                  prev_month)


def test_contrib_formula():
    # §4.5 라 예시: 도시가스 w11 ×+5% = +0.055%p / 통신 w52 ×−2% = −0.104%p / 도수치료 w2 ×−65% = −0.13%p
    assert round(headline_contrib(11, 5.0), 3) == 0.055
    assert round(headline_contrib(52, -2.0), 3) == -0.104
    assert round(headline_contrib(2, -65.0), 3) == -0.130


def test_importance():
    # §4.5 다: High≥0.1 / Medium 0.03~0.1 / Low<0.03
    assert classify_importance(0.055) == "Medium"
    assert classify_importance(-0.104) == "High"
    assert classify_importance(-0.13) == "High"
    assert classify_importance(0.02) == "Low"
    assert classify_importance(-0.10) == "High"     # 경계 0.1 → High
    assert classify_importance(0.03) == "Medium"    # 경계 0.03 → Medium


def test_bucket_adj_consistency():
    """버킷 기여도(버킷가중치/1000×버킷MoM조정) == 헤드라인 기여도 (이중계상 없음)."""
    ev = Event("도시가스", "egw", target_weight=11.0, shock_pct=5.0, ym="2026-06")
    bw = {"egw": 33.7}
    adj = bucket_event_adj([ev], "2026-06", bw)
    bucket_contrib = bw["egw"] / 1000 * adj["egw"]
    assert round(bucket_contrib, 6) == round(headline_contrib(11.0, 5.0), 6)


def test_month_filter():
    """6월 전망에는 7월 이벤트가 들어오지 않는다."""
    june = Event("석유", "seokyu", 46.6, -3.0, "2026-06")
    july = Event("통신", "gonggong", 52.0, -2.0, "2026-07")
    rows = event_breakdown([june, july], "2026-06")
    names = {r.name for r in rows}
    assert names == {"석유"}, f"6월에 7월 이벤트 누출: {names}"


def test_reversal():
    """전월 이벤트의 되돌림이 다음달에 역방향으로 적용(§5.4 중복금지)."""
    ev = Event("WTI급락", "seokyu", 46.6, -3.0, "2026-06", reversal_rate=0.4)
    # 7월: 되돌림 = −0.4 × (−3.0) = +1.2%
    eff = effective_events([ev], "2026-07")
    assert len(eff) == 1
    _e, shock, kind = eff[0]
    assert kind == "되돌림" and round(shock, 3) == 1.2
    assert prev_month("2026-07") == "2026-06"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1; print(f"  FAIL  {name}: {e}")
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
