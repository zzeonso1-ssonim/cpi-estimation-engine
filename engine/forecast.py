"""
전망 오케스트레이터 — 입력을 받아 §5 계산 → §8 검증 → §9 신뢰도 → §12 리포트까지 묶는다.

두 가지 전망 모드:
  1) direct  : 공표 근원 총지수 + 근원 MoM 가정을 직접 지수법에 투입(§5.1). §11 6월 예시가 이 경로.
  2) bottomup: 버킷별 MoM 가정 → 재정규화(§5.3)로 근원 MoM 합성 → 지수법.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from engine.index_engine import forecast_metric, MetricForecast
from engine.core_engine import renormalize, renormalize_tier2, core_mom, Bucket
from engine.validation import run_checks, gate_passed, Check
from engine.uncertainty import (confidence_grade, interval, scenario_probabilities,
                                net_event_risk)
from engine.report import build_report
from engine.event_engine import (Event, bucket_event_adj, event_breakdown,
                                 event_warnings, EventRow)


@dataclass
class ForecastResult:
    ym_label: str
    headline: MetricForecast
    core1: MetricForecast
    core2: MetricForecast
    checks: list[Check]
    gate_ok: bool
    grade: str
    report: str
    renorm: dict = field(default_factory=dict)   # core -> RenormResult (bottomup 모드)
    event_rows: list = field(default_factory=list)     # EventRow (이벤트 기여도 분해)
    event_warnings: list = field(default_factory=list) # §7 경고 문자열
    scenarios: dict | None = None                # 헤드라인 Base/Upside/Downside 확률(§9.1)
    pred_interval: tuple | None = None           # 헤드라인 예측구간(점추정±MAE, §9)
    bucket_contrib: list = field(default_factory=list)  # key factor: (품목군, 가중치, 최종MoM, 기여도%p)


def run_direct(ym_label,
               prev, base,                 # prev={'headline','core1','core2'}, base=동일 키
               mom,                         # {'headline','core1','core2'} MoM(%)
               confirmed_base=True,
               consensus=None,
               core_renorm_sums=None,       # {'core1':sum_adj,'core2':sum_adj} 있으면 검증6 포함
               mae_pp=None,                 # 헤드라인 백테스트 MAE(%p) → 예측구간·시나리오
               mean_error_pp=0.0,           # 헤드라인 평균오차(%p, R-4 편향) → 시나리오 스큐
               risk_score=0.0) -> ForecastResult:  # 품목군 리스크 점수[-1,1]
    h = forecast_metric("헤드라인", prev["headline"], base["headline"], mom["headline"], confirmed_base)
    c1 = forecast_metric("근원①", prev["core1"], base["core1"], mom["core1"], confirmed_base)
    c2 = forecast_metric("근원②", prev["core2"], base["core2"], mom["core2"], confirmed_base)

    checks = run_checks(
        h.yoy, c1.yoy, c2.yoy,
        core_renorm=core_renorm_sums,
        reproduce_gap=0.0,                  # direct 모드: 산식=표기, 괴리 0(검증7 통과)
        consensus=consensus,
    )
    gate_ok = gate_passed(checks)
    renorm_ok = core_renorm_sums is None or all(abs(v - 1000) <= 0.1 for v in core_renorm_sums.values())
    grade = confidence_grade(confirmed_base, renorm_ok, reproduce_gap=0.0)

    gaps = None
    if consensus:
        gaps = {"headline": h.yoy - consensus.get("headline", h.yoy),
                "core1": c1.yoy - consensus.get("core1", c1.yoy),
                "core2": c2.yoy - consensus.get("core2", c2.yoy)}

    scenarios = (scenario_probabilities(h.yoy, mae_pp, mean_error_pp, risk_score)
                 if (mae_pp is not None or risk_score) else None)
    pred_int = interval(h.yoy, mae_pp)

    report = build_report(ym_label, h.mom_pct, h.yoy, c1.yoy, c2.yoy, gaps, grade,
                          gate_ok=gate_ok, scenarios=scenarios, pred_interval=pred_int)
    return ForecastResult(ym_label, h, c1, c2, checks, gate_ok, grade, report,
                          scenarios=scenarios, pred_interval=pred_int)


def run_bottomup(ym_label,
                 prev, base,
                 buckets: list[Bucket],
                 bucket_mom: dict,           # code -> 기본 MoM(%) (이벤트 제외)
                 headline_mom: float | None = None,  # None이면 버킷 가중합으로 산출
                 published=None,             # {'core1','core2'} 공표 근원합
                 confirmed_base=True,
                 consensus=None,
                 mae_pp=None,
                 mean_error_pp=0.0,
                 risk_score=None,            # None이면 이벤트 순기여도로 자동 산정(§9.1)
                 events: list[Event] | None = None,  # 이벤트 계수(§7)
                 target_ym: str | None = None,
                 service_result=None,        # 개인서비스 §5.4
                 items: dict | None = None,  # Tier2 품목가중치 → 정밀 재정규화
                 macro_risk: float = 0.0) -> ForecastResult:  # 거시지표(환율·유가) 리스크[-1,1] §9.1
    published = published or {}
    weights = {b.code: b.weight for b in buckets}
    tot = sum(weights.values())
    bucket_mom = dict(bucket_mom)

    # 개인서비스(§5.4): service_result가 있으면 gaein MoM을 그 값으로 고정.
    #   service_result는 gaein 이벤트(당월·되돌림)를 이미 포함 → 이벤트 레이어에서 gaein 제외(이중계상 방지).
    service_owns_gaein = service_result is not None
    if service_owns_gaein:
        bucket_mom["gaein"] = service_result.private_service_mom

    # 이벤트 → 버킷 MoM 조정(§7). 기본 MoM에 가산해 최종 MoM 구성.
    ev_rows: list[EventRow] = []
    ev_adj: dict = {}
    if events and target_ym:
        ev_adj = bucket_event_adj(events, target_ym, weights)
        ev_rows = event_breakdown(events, target_ym)
        if service_owns_gaein:
            ev_adj.pop("gaein", None)   # 개인서비스 이벤트는 §5.4에서 이미 반영
    final_mom = {c: bucket_mom.get(c, 0.0) + ev_adj.get(c, 0.0) for c in weights}

    def _headline(mom_map):
        return sum(weights[c] / tot * mom_map.get(c, 0.0) for c in weights)

    headline_base = _headline(bucket_mom)
    headline_with = headline_mom if headline_mom is not None else _headline(final_mom)

    # 재정규화: items 주어지면 품목 단위(Tier2, 정밀), 아니면 버킷 단위(Tier1)
    if items:
        rn1 = renormalize_tier2(items, "core1", published.get("core1"))
        rn2 = renormalize_tier2(items, "core2", published.get("core2"))
    else:
        rn1 = renormalize(buckets, "core1", published.get("core1"))
        rn2 = renormalize(buckets, "core2", published.get("core2"))
    c1_mom = core_mom(rn1, final_mom)
    c2_mom = core_mom(rn2, final_mom)

    # 리스크 점수: (이벤트 순기여도 또는 지정값) + 거시지표(환율·유가) 리스크 → 합산 클램프(§9.1)
    base_risk = risk_score if risk_score is not None else (net_event_risk(ev_rows) if ev_rows else 0.0)
    rscore = max(-1.0, min(1.0, base_risk + macro_risk))

    res = run_direct(
        ym_label, prev, base,
        mom={"headline": headline_with, "core1": c1_mom, "core2": c2_mom},
        confirmed_base=confirmed_base, consensus=consensus,
        core_renorm_sums={"core1": rn1.sum_adj, "core2": rn2.sum_adj},
        mae_pp=mae_pp, mean_error_pp=mean_error_pp, risk_score=rscore,
    )
    res.renorm = {"core1": rn1, "core2": rn2}
    res.event_rows = ev_rows
    res.event_warnings = event_warnings(headline_with, headline_base, ev_rows)
    # key factor: 품목군별 헤드라인 기여도(%p) = 가중치/1000 × 최종MoM, 절대값 순 정렬
    contrib = [(b.name, b.weight, round(final_mom.get(b.code, 0.0), 3),
                round(b.weight / 1000 * final_mom.get(b.code, 0.0), 4)) for b in buckets]
    res.bucket_contrib = sorted(contrib, key=lambda x: -abs(x[3]))
    return res
