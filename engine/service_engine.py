"""
개인서비스 계산 — 이중계상 방지.
근거: PRD v3.1 §1.4(이중계상 위험·규칙), §5.4(산식).

PrivateService_MoM
  = SeasonalPrior(이벤트 보정 후 순수 계절성)
  + EventAdjustment(당월 신규 이벤트만)
  + ReversalAdjustment(= −f(전월 EventAdjustment), 중복 금지)
  + StickyServiceAdjustment(하방경직, 부호 충돌 시 상계)

가드:
  · 규칙1: SeasonalPrior는 이벤트 보정 후 값이어야 함(미보정 시 경고)
  · 규칙2: 동일 이벤트의 이벤트조정·되돌림 중복 적용 금지(되돌림은 전월 이벤트에서만)
  · 규칙3: 되돌림·하방경직 부호 충돌 시 상계량 보고
  · 경고: Σ|각 항| > k × |기조추세 MoM| → 과조정 경고
"""
from __future__ import annotations
from dataclasses import dataclass

from engine.event_engine import effective_events, Event

GAEIN = "gaein"


@dataclass
class ServiceComponents:
    seasonal_prior: float       # 순수 계절성(이벤트 보정 후)
    event_adj: float            # 당월 신규 이벤트
    reversal_adj: float         # 전월 이벤트의 되돌림(중복 금지)
    sticky_adj: float           # 하방경직


@dataclass
class ServiceResult:
    components: ServiceComponents
    private_service_mom: float
    sum_abs: float                  # Σ|각 항|
    sticky_offset: float            # 되돌림·하방경직 상계량(부호 충돌 시 >0)
    overadjust_warning: str | None
    double_count_flags: list[str]


def service_event_components(events: list[Event], target_ym: str,
                            gaein_weight: float, gaein_code: str = GAEIN):
    """
    개인서비스 버킷에 대한 당월 EventAdjustment / 되돌림 ReversalAdjustment(MoM %).
    effective_events가 '당월'과 '되돌림'을 이미 분리(다른 월의 이벤트) → 규칙2 자동 충족.
    """
    event_adj = reversal_adj = 0.0
    for ev, eff_shock, kind in effective_events(events, target_ym):
        if ev.target_bucket != gaein_code:
            continue
        contrib = ev.target_weight / gaein_weight * eff_shock   # 버킷 MoM 환산
        if kind == "당월":
            event_adj += contrib
        else:
            reversal_adj += contrib
    return event_adj, reversal_adj


def compute_private_service(seasonal_prior: float,
                            event_adj: float,
                            reversal_adj: float,
                            sticky_adj: float,
                            trend_mom: float,
                            seasonal_event_corrected: bool = True,
                            k_overadjust: float = 2.0) -> ServiceResult:
    comp = ServiceComponents(seasonal_prior, event_adj, reversal_adj, sticky_adj)
    mom = seasonal_prior + event_adj + reversal_adj + sticky_adj
    sum_abs = abs(seasonal_prior) + abs(event_adj) + abs(reversal_adj) + abs(sticky_adj)

    # 규칙3: 되돌림·하방경직 부호 충돌 시 상계량(투명 보고). 합산 자체가 net이므로 보고만.
    sticky_offset = (min(abs(reversal_adj), abs(sticky_adj))
                     if reversal_adj * sticky_adj < 0 else 0.0)

    flags: list[str] = []
    # 규칙1: 계절 프라이어 이벤트 미보정 시 이중계상 위험
    if not seasonal_event_corrected and event_adj != 0:
        flags.append("⚠️ 이중계상 위험: SeasonalPrior가 이벤트 미보정 — 당월 이벤트와 중복 가능(규칙1)")
    # 규칙2 보조 점검: 당월 이벤트와 되돌림이 동시에 큰 경우 동일 이벤트 중복 여부 확인 권고
    if event_adj != 0 and reversal_adj != 0 and (event_adj * reversal_adj) > 0:
        flags.append("ℹ️ 당월 이벤트와 되돌림이 같은 부호 — 동일 이벤트 중복 적용 아닌지 확인(규칙2)")

    # 경고: Σ|각 항| > k × |기조추세 MoM|
    warn = None
    if abs(trend_mom) > 1e-9 and sum_abs > k_overadjust * abs(trend_mom):
        warn = (f"⚠️ 과조정 경고: Σ|각 항| {sum_abs:.3f}% > {k_overadjust:g}×|기조추세 "
                f"{trend_mom:.3f}%| = {k_overadjust*abs(trend_mom):.3f}%")

    return ServiceResult(comp, mom, sum_abs, sticky_offset, warn, flags)
