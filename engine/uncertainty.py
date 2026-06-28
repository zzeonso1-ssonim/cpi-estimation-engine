"""
불확실성·확률 출력 — 시나리오 확률, 예측구간, 신뢰도 등급.
근거: PRD v3.1 §9(출력 5종), §9.1(시나리오 확률 산정), §9.2(신뢰도 A/B/C).

기본 산정(§9.1): "백테스트 오차분포(정규근사) + 품목군 리스크 점수" 혼합,
Base/Upside/Downside 합 = 100%로 정규화.
"""
from __future__ import annotations
import math


def confidence_grade(all_index_confirmed: bool,
                     renorm_ok: bool,
                     reproduce_gap: float | None,
                     mae_stable: bool | None = None) -> str:
    """
    신뢰도 등급 산정(§9.2):
      A: 전월·전년동월 지수 모두 확정 · Σ=1000 통과 · 재현괴리 ≤0.03%p · 최근 MAE 안정
      B: 주요 지수 확정이나 일부 미확정 · 재현괴리 ≤0.05%p
      C: 전년동월 미확정 · 이벤트 비중 과다 · 또는 오차편향 확대
    """
    gap = abs(reproduce_gap) if reproduce_gap is not None else 1.0
    if all_index_confirmed and renorm_ok and gap <= 0.03 and (mae_stable in (True, None)):
        return "A"
    if renorm_ok and gap <= 0.05:
        return "B"
    return "C"


def interval(point_yoy: float, mae_pp: float | None) -> tuple[float, float] | None:
    """예측구간(§9): 점추정 ± 과거 백테스트 MAE(%p). MAE 없으면 None."""
    if mae_pp is None:
        return None
    return (point_yoy - mae_pp, point_yoy + mae_pp)


# ── 시나리오 확률 (§9.1) ────────────────────────────────────
_MAE_TO_SIGMA = math.sqrt(math.pi / 2)   # 정규분포에서 σ = MAE × √(π/2)


def _norm_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 0.0 if x < mu else 1.0
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def probs_from_distribution(point_yoy: float, mae_pp: float,
                            mean_error_pp: float = 0.0, band_pp: float = 0.1) -> dict:
    """
    백테스트 오차분포 정규근사(§9.1):
      · σ = MAE × √(π/2)
      · 편향 반영: error = 전망 − 실제 이므로, 평균오차가 음수(과소추정)면
        실제는 전망보다 높게 나옴 → 분포 중심을 point − mean_error 로 이동(상방 스큐).
      · Base = 중심 ±band, Upside = +band 초과, Downside = −band 미만.
    """
    center = point_yoy - mean_error_pp
    sigma = max(mae_pp, 1e-6) * _MAE_TO_SIGMA
    p_down = _norm_cdf(point_yoy - band_pp, center, sigma)
    p_up = 1 - _norm_cdf(point_yoy + band_pp, center, sigma)
    p_base = max(0.0, 1 - p_up - p_down)
    return {"base": p_base, "upside": p_up, "downside": p_down}


def probs_from_risk(risk_score: float) -> dict:
    """
    품목군 리스크 점수 기반(§9.1). risk_score ∈ [−1,+1] (+=상방).
    중립 1/3씩에서 점수만큼 상·하방으로 질량 이동.
    """
    r = max(-1.0, min(1.0, risk_score))
    base = 1 / 3
    up = base + r * base
    down = base - r * base
    return {"base": base, "upside": up, "downside": down}


def scenario_probabilities(point_yoy: float, mae_pp: float | None,
                           mean_error_pp: float = 0.0, risk_score: float = 0.0,
                           band_pp: float = 0.1,
                           w_dist: float = 0.6, w_risk: float = 0.4) -> dict:
    """
    §9.1 기본값: 분포 기반 + 리스크 점수 혼합 → 합 100% 정규화.
    MAE가 없으면 리스크 점수만 사용.
    """
    if mae_pp is None:
        mixed = probs_from_risk(risk_score)
    else:
        d = probs_from_distribution(point_yoy, mae_pp, mean_error_pp, band_pp)
        k = probs_from_risk(risk_score)
        mixed = {s: w_dist * d[s] + w_risk * k[s] for s in ("base", "upside", "downside")}
    total = sum(mixed.values()) or 1.0
    up = round(mixed["upside"] / total * 100, 1)
    down = round(mixed["downside"] / total * 100, 1)
    base = round(100.0 - up - down, 1)   # 잔차로 잡아 합 정확히 100% 보장
    return {"base": base, "upside": up, "downside": down}


def net_event_risk(event_rows, scale: float = 0.2) -> float:
    """이벤트 순기여도 → 리스크 점수[−1,1]. 상방 이벤트 우세면 +."""
    net = sum(r.contrib_pp for r in event_rows)
    return max(-1.0, min(1.0, net / scale))


def bucket_sensitivity(buckets, bucket_mom: dict, low_conf: set | None = None,
                       top_n: int = 3) -> list[tuple[str, float]]:
    """
    품목군별 오차 민감도(§9): 가중치 × 가정 MoM 크기 기준 위험 가정 순위.
    low_conf(신뢰도 낮은 버킷)면 가중. 개인서비스가 통상 최상위(§10.1).
    """
    low_conf = low_conf or set()
    scored = []
    for b in buckets:
        sens = b.weight / 1000 * (abs(bucket_mom.get(b.code, 0.0)) + 0.1)
        if b.code in low_conf:
            sens *= 1.5
        scored.append((b.name, round(sens, 4)))
    return sorted(scored, key=lambda x: -x[1])[:top_n]
