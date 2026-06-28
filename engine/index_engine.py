"""
지수법 본체 + 기저효과 검산.
근거: PRD v3.1 §5.1, §5.2.
"""
from __future__ import annotations
from dataclasses import dataclass


def project_index(prev_index: float, mom_pct: float) -> float:
    """지수법(§5.1): Index_t = Index_{t-1} × (1 + MoM/100)."""
    return prev_index * (1 + mom_pct / 100)


def yoy(index_t: float, index_t_minus_12: float) -> float:
    """YoY(§5.1): (Index_t / Index_{t-12} − 1) × 100."""
    return (index_t / index_t_minus_12 - 1) * 100


def base_effect_yoy(prev_yoy: float, mom_t: float, mom_t_minus_12: float) -> float:
    """기저효과 교차검증(§5.2, 검산용): YoY_t ≈ YoY_{t-1} + (MoM_t − MoM_{t-12})."""
    return prev_yoy + (mom_t - mom_t_minus_12)


@dataclass
class MetricForecast:
    """단일 지표(헤드라인/근원①/근원②)의 전망 결과."""
    name: str
    prev_index: float       # 직전월 지수
    base_index: float       # 전년동월 지수(YoY 분모)
    mom_pct: float          # 가정 MoM(%)
    confirmed_base: bool    # 전년동월 지수 확정 여부(§1.5)

    @property
    def proj_index(self) -> float:
        return project_index(self.prev_index, self.mom_pct)

    @property
    def yoy(self) -> float:
        return yoy(self.proj_index, self.base_index)


def forecast_metric(name, prev_index, base_index, mom_pct, confirmed_base=True) -> MetricForecast:
    return MetricForecast(name, prev_index, base_index, mom_pct, confirmed_base)
