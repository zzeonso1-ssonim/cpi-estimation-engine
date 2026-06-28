"""
골든 테스트 — 2026년 6월 예시 재현(PRD §11) + 재정규화 검증6(§8).
이 테스트가 통과해야 엔진이 PRD와 정합한다(재현 게이트).

실행:  python tests/test_golden_june.py   (또는 pytest)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.index_engine import project_index, yoy
from engine.core_engine import renormalize, core_mom, Bucket
from data.seed import BUCKETS, PUBLISHED_CORE_WEIGHT

# §11 입력(확정)
PREV = {"headline": 119.92, "core1": 115.97, "core2": 117.97}   # 5월
BASE = {"headline": 116.31, "core1": 113.17, "core2": 115.25}   # 2025.6


GATE = 0.05  # 검증7(§8): 재현 YoY vs 표기 YoY 괴리 허용 ≤ 0.05%p


def _yoy_range(metric, mom_lo, mom_hi):
    lo = yoy(project_index(PREV[metric], mom_lo), BASE[metric])
    hi = yoy(project_index(PREV[metric], mom_hi), BASE[metric])
    return lo, hi


def _assert_gate(metric, mom_lo, mom_hi, prd_lo, prd_hi, label):
    """재현치가 PRD 표기 범위와 검증7 허용오차(0.05%p) 이내인지 확인."""
    lo, hi = _yoy_range(metric, mom_lo, mom_hi)
    assert abs(lo - prd_lo) <= GATE, f"{label} 하한 재현 {lo:.4f} vs PRD {prd_lo} (괴리 {abs(lo-prd_lo):.4f} > {GATE})"
    assert abs(hi - prd_hi) <= GATE, f"{label} 상한 재현 {hi:.4f} vs PRD {prd_hi} (괴리 {abs(hi-prd_hi):.4f} > {GATE})"


def test_june_headline():
    # §11: MoM +0.1~+0.2% → YoY 3.21~3.31%
    _assert_gate("headline", 0.1, 0.2, 3.21, 3.31, "헤드라인")


def test_june_core1():
    # §11: MoM +0.15~+0.25% → YoY 2.62~2.73%
    _assert_gate("core1", 0.15, 0.25, 2.62, 2.73, "근원①")


def test_june_core2():
    # §11: MoM +0.20~+0.30% → YoY 2.57~2.66% (v3.0 표기 2.7~2.8%는 과대 → 재현치로 정정)
    _assert_gate("core2", 0.20, 0.30, 2.57, 2.66, "근원②")


def test_renorm_sum_1000():
    """검증6(§8): Σ조정가중치 = 1000 ±0.1."""
    buckets = [Bucket(c, n, w, c1, c2) for (c, n, w, c1, c2, _note) in BUCKETS]
    for core in ("core1", "core2"):
        rn = renormalize(buckets, core, PUBLISHED_CORE_WEIGHT[core])
        assert abs(rn.sum_adj - 1000) <= 0.1, f"{core} Σ조정가중치 {rn.sum_adj} != 1000"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
    # 진단 출력
    buckets = [Bucket(c, n, w, c1, c2) for (c, n, w, c1, c2, _note) in BUCKETS]
    print("\n[진단] 혼재버킷 잔차(§1.3):")
    for core in ("core1", "core2"):
        rn = renormalize(buckets, core, PUBLISHED_CORE_WEIGHT[core])
        print(f"  {core}: 버킷단순합 {rn.included_raw_sum:.1f} vs 공표합 {rn.published_sum:.1f} "
              f"→ 잔차 {rn.residual_vs_published:+.1f} (인삼·화초·주류 등)")
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
