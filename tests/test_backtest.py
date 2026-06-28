"""
백테스트 학습 루프 테스트 — §10(R-1~R-7), §10.1(5월 사례).

(A) 저장된 §10.1 결과로 R-1 오차분해가 PRD 표기 오차와 정확히 일치하는지.
(B) 엔진을 실제로 돌린 라이브 백테스트가 5월 괴리(헤드라인 −0.27, 근원① −0.37)를
    재현하고, 근원 MoM 과소추정(R-3, 실제/전망 ≥ 3배)을 잡아내는지.

실행:  python tests/test_backtest.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.backtest import (r1_decompose, r4_repeated_bias, r7_mae,
                             backtest_from_forecast, mom_from_index)
from engine.core_engine import Bucket
from engine.forecast import run_bottomup
from data.seed import BACKTEST_RECORDS, BACKTEST_MAY_MOM, BUCKETS, PUBLISHED_CORE_WEIGHT

# 4월 확정(직전월) / 2025-05 역산(전년동월) / 2026-05 실제
PREV = {"headline": 119.37, "core1": 115.38, "core2": 117.38}
BASE = {"headline": 116.27, "core1": 113.10, "core2": 115.06}
ACTUAL = {"headline": 119.92, "core1": 115.97, "core2": 117.97}
BUCKET_OBJS = [Bucket(c, n, w, c1, c2) for (c, n, w, c1, c2, _note) in BUCKETS]


def test_r1_matches_prd():
    """(A) 저장된 §10.1 전망/실제 → R-1 오차가 PRD 표기와 일치."""
    fc = {m: f for (_ym, m, f, _a, _e, _c) in BACKTEST_RECORDS}
    act = {m: a for (_ym, m, _f, a, _e, _c) in BACKTEST_RECORDS}
    entries = r1_decompose("2026-05", fc, act)
    err = {e.metric: round(e.error_pp, 2) for e in entries}
    assert err["headline"] == -0.27, err
    assert err["core1"] == -0.37, err
    assert err["core2"] == -0.31, err


def test_live_backtest_reproduces_may():
    """(B) 엔진 라이브 백테스트가 5월 헤드라인·근원① 괴리를 재현."""
    res = run_bottomup("2026-05", PREV, BASE, BUCKET_OBJS, dict(BACKTEST_MAY_MOM),
                       published={"core1": PUBLISHED_CORE_WEIGHT["core1"],
                                  "core2": PUBLISHED_CORE_WEIGHT["core2"]})
    bt = backtest_from_forecast(res, ACTUAL, PREV)
    err = {e.metric: e.error_pp for e in bt["entries"]}
    assert abs(err["headline"] - (-0.27)) <= 0.05, f"헤드라인 {err['headline']}"
    assert abs(err["core1"] - (-0.37)) <= 0.05, f"근원① {err['core1']}"
    # 근원② 라이브는 정밀 재정규화로 더 큰 과소추정(< -0.2)
    assert err["core2"] < -0.2, f"근원② {err['core2']}"


def test_r3_core_underestimate():
    """(R-3) 실제 근원 MoM / 전망 근원 MoM ≥ 3배(서비스 과소추정 신호, §10.1)."""
    res = run_bottomup("2026-05", PREV, BASE, BUCKET_OBJS, dict(BACKTEST_MAY_MOM),
                       published={"core1": PUBLISHED_CORE_WEIGHT["core1"],
                                  "core2": PUBLISHED_CORE_WEIGHT["core2"]})
    bt = backtest_from_forecast(res, ACTUAL, PREV)
    for m in ("core1", "core2"):
        assert bt["core_mom_ratio"][m] >= 3.0, f"{m} ratio {bt['core_mom_ratio'][m]}"


def test_r4_r7_bias_mae():
    """(R-4/R-7) 저장된 §10.1 3건은 모두 과소추정 → 체계적 편향, MAE 산출."""
    entries = r1_decompose("2026-05",
                           {m: f for (_y, m, f, _a, _e, _c) in BACKTEST_RECORDS},
                           {m: a for (_y, m, _f, a, _e, _c) in BACKTEST_RECORDS})
    # 단일월이라도 헤드라인 표본 1개 — 다월 누적 가정 테스트는 동일 metric 2개로
    two = entries + r1_decompose("2026-04", {"core1": 2.0}, {"core1": 2.3})
    bias = r4_repeated_bias(two, "core1")
    assert "과소추정" in bias, bias
    assert r7_mae(entries, "headline") is not None


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1; print(f"  FAIL  {name}: {e}")
    # 진단
    res = run_bottomup("2026-05", PREV, BASE, BUCKET_OBJS, dict(BACKTEST_MAY_MOM),
                       published={"core1": PUBLISHED_CORE_WEIGHT["core1"],
                                  "core2": PUBLISHED_CORE_WEIGHT["core2"]})
    bt = backtest_from_forecast(res, ACTUAL, PREV)
    print("\n[진단] 라이브 5월 백테스트:")
    for e in bt["entries"]:
        print(f"  {e.metric:9} 전망 {e.forecast_yoy:.2f}% / 실제 {bt['actual_yoy'][e.metric]:.2f}% "
              f"→ 오차 {e.error_pp:+.3f}%p")
    for m in ("core1", "core2"):
        am = mom_from_index(PREV[m], ACTUAL[m])
        print(f"  {m} MoM: 전망 {res.__dict__[m].mom_pct:.3f}% vs 실제 {am:.3f}% "
              f"(실제/전망 {bt['core_mom_ratio'][m]:.1f}배)")
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
