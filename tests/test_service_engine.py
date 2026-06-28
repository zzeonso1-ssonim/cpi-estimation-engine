"""
개인서비스 이중계상 방지 테스트 — §1.4(규칙)·§5.4(산식).

실행:  python tests/test_service_engine.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.service_engine import compute_private_service, service_event_components
from engine.event_engine import Event


def test_sum_components():
    """4항 단순 합 = private_service_mom."""
    r = compute_private_service(0.30, 0.10, -0.05, 0.05, trend_mom=0.20)
    assert round(r.private_service_mom, 3) == round(0.30 + 0.10 - 0.05 + 0.05, 3)
    assert round(r.sum_abs, 3) == 0.50


def test_overadjust_warning():
    """Σ|각 항| > k×|기조추세| → 과조정 경고(§5.4)."""
    # Σ|각 항| = 0.9, 기조 0.1, k=2 → 0.9 > 0.2 → 경고
    r = compute_private_service(0.4, 0.3, -0.1, 0.1, trend_mom=0.1, k_overadjust=2.0)
    assert r.overadjust_warning is not None
    # 충분히 큰 기조면 경고 없음
    r2 = compute_private_service(0.1, 0.05, 0.0, 0.0, trend_mom=0.5, k_overadjust=2.0)
    assert r2.overadjust_warning is None


def test_double_count_flag():
    """규칙1: 계절 프라이어 이벤트 미보정 + 당월 이벤트 존재 → 이중계상 경고."""
    r = compute_private_service(0.3, 0.2, 0.0, 0.0, trend_mom=0.3,
                                seasonal_event_corrected=False)
    assert any("이중계상" in f for f in r.double_count_flags)
    # 보정된 경우 경고 없음
    r2 = compute_private_service(0.3, 0.2, 0.0, 0.0, trend_mom=0.3,
                                 seasonal_event_corrected=True)
    assert not any("이중계상" in f for f in r2.double_count_flags)


def test_sticky_reversal_offset():
    """규칙3: 되돌림(−)·하방경직(+) 부호 충돌 → 상계량 보고."""
    r = compute_private_service(0.2, 0.0, -0.3, 0.2, trend_mom=0.2)
    assert round(r.sticky_offset, 3) == 0.2     # min(0.3, 0.2)
    # 같은 부호면 상계 없음
    r2 = compute_private_service(0.2, 0.0, -0.3, -0.1, trend_mom=0.2)
    assert r2.sticky_offset == 0.0


def test_event_reversal_separation():
    """규칙2: effective_events가 당월/되돌림을 다른 월 이벤트로 분리 → 중복 없음."""
    ev = Event("외식 성수기 급등", "gaein", target_weight=50.0, shock_pct=0.6,
               ym="2026-05", reversal_rate=0.5)
    # 5월: 당월만
    e5, r5 = service_event_components([ev], "2026-05", gaein_weight=333.3)
    assert r5 == 0.0 and e5 > 0
    # 6월: 되돌림만(= −0.5×0.6 환산)
    e6, r6 = service_event_components([ev], "2026-06", gaein_weight=333.3)
    assert e6 == 0.0 and r6 < 0


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
