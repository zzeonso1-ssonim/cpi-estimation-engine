"""
백테스팅·학습 루프 — 발표 후 오차를 다음 추정에 되먹임.
근거: PRD v3.1 §10(R-1~R-7), §10.1(2026년 5월 사례).

R-1 오차 분해 / R-2 원인 귀속 / R-3 품목군 기여도 / R-4 반복오차 감지 /
R-5 계수 업데이트 / R-6 계절 프라이어 갱신 / R-7 신뢰도 재산정.
"""
from __future__ import annotations
from dataclasses import dataclass, field

METRICS = ("headline", "core1", "core2")
METRIC_KR = {"headline": "헤드라인", "core1": "근원①", "core2": "근원②"}


@dataclass
class BTEntry:
    ym: str
    metric: str
    forecast_yoy: float
    actual_yoy: float
    error_pp: float          # 오차(%p) = forecast − actual (음수 = 과소추정)
    cause: str = ""


# ── R-1: 오차 분해 ──────────────────────────────────────────
def r1_decompose(ym: str, forecast_yoy: dict, actual_yoy: dict,
                 cause: dict | None = None) -> list[BTEntry]:
    """헤드라인·근원①·근원² 각각의 YoY 오차(%p) = 전망 − 실제."""
    cause = cause or {}
    out = []
    for m in METRICS:
        if m in forecast_yoy and m in actual_yoy:
            err = forecast_yoy[m] - actual_yoy[m]
            out.append(BTEntry(ym, m, forecast_yoy[m], actual_yoy[m],
                               round(err, 4), cause.get(m, "")))
    return out


# ── R-2: 원인 귀속 (MoM 오판 / 기저 / 이벤트) ───────────────
def mom_from_index(prev_index: float, cur_index: float) -> float:
    return (cur_index / prev_index - 1) * 100


@dataclass
class CauseAttribution:
    metric: str
    mom_error_pp: float      # 전망 MoM − 실제 MoM (기저 동일 시 YoY 오차의 주원인)
    base_error_pp: float     # 전년동월 지수 개정으로 인한 오차(확정 시 0)
    event_error_pp: float    # 이벤트 조정 기여 오차
    dominant: str            # MoM오판 / 기저 / 이벤트


def r2_attribute(metric: str, forecast_mom: float, actual_mom: float,
                 base_revised_pp: float = 0.0, event_error_pp: float = 0.0) -> CauseAttribution:
    mom_err = forecast_mom - actual_mom
    comps = {"MoM오판": abs(mom_err), "기저": abs(base_revised_pp), "이벤트": abs(event_error_pp)}
    dominant = max(comps, key=comps.get)
    return CauseAttribution(metric, round(mom_err, 4), round(base_revised_pp, 4),
                            round(event_error_pp, 4), dominant)


# ── R-3: 근원 MoM 과소/과대추정 배수 ────────────────────────
def r3_core_mom_ratio(forecast_core_mom: float, actual_core_mom: float) -> float | None:
    """실제 근원 MoM / 전망 근원 MoM (3배 이상이면 서비스·근원재 과소추정 신호)."""
    if abs(forecast_core_mom) < 1e-9:
        return None
    return actual_core_mom / forecast_core_mom


# ── R-4: 반복오차(체계적 편향) 감지 ─────────────────────────
def r4_repeated_bias(entries: list[BTEntry], metric: str, min_n: int = 2) -> str:
    errs = [e.error_pp for e in entries if e.metric == metric]
    if len(errs) < min_n:
        return f"표본 부족(n={len(errs)})"
    mean = sum(errs) / len(errs)
    if all(e < 0 for e in errs):
        return f"체계적 과소추정(평균 {mean:+.3f}%p, n={len(errs)})"
    if all(e > 0 for e in errs):
        return f"체계적 과대추정(평균 {mean:+.3f}%p, n={len(errs)})"
    return f"편향 불명확(평균 {mean:+.3f}%p, n={len(errs)})"


# ── R-7: MAE → 예측구간/신뢰도 ──────────────────────────────
def r7_mae(entries: list[BTEntry], metric: str) -> float | None:
    errs = [abs(e.error_pp) for e in entries if e.metric == metric]
    return sum(errs) / len(errs) if errs else None


def mae_stable(entries: list[BTEntry], metric: str, threshold: float = 0.4) -> bool | None:
    mae = r7_mae(entries, metric)
    return None if mae is None else mae <= threshold


# ── DB ──────────────────────────────────────────────────────
def save_entries(con, entries: list[BTEntry]) -> None:
    con.executemany(
        "INSERT INTO backtest(ym,metric,forecast_yoy,actual_yoy,error_pp,cause) "
        "VALUES (?,?,?,?,?,?)",
        [(e.ym, e.metric, e.forecast_yoy, e.actual_yoy, e.error_pp, e.cause) for e in entries],
    )
    con.commit()


def load_entries(con, metric: str | None = None) -> list[BTEntry]:
    q = "SELECT ym,metric,forecast_yoy,actual_yoy,error_pp,cause FROM backtest"
    rows = (con.execute(q + " WHERE metric=? ORDER BY ym", (metric,))
            if metric else con.execute(q + " ORDER BY ym, metric")).fetchall()
    return [BTEntry(r["ym"], r["metric"], r["forecast_yoy"], r["actual_yoy"],
                    r["error_pp"], r["cause"]) for r in rows]


# ── 라이브 백테스트: ForecastResult vs 실제 지수 ────────────
def backtest_from_forecast(res, actual_index: dict, prev_index: dict,
                           save_con=None) -> dict:
    """
    엔진 전망(res) vs 실제 발표 지수로 R-1~R-3 산출.
      actual_index/prev_index: {'headline','core1','core2'} 실제·직전월 지수.
    """
    fc_yoy = {"headline": res.headline.yoy, "core1": res.core1.yoy, "core2": res.core2.yoy}
    bases = {"headline": res.headline.base_index, "core1": res.core1.base_index,
             "core2": res.core2.base_index}
    act_yoy = {m: (actual_index[m] / bases[m] - 1) * 100 for m in METRICS}

    entries = r1_decompose(res.ym_label, fc_yoy, act_yoy)
    # R-2/R-3 (근원 중심)
    attributions, ratios = {}, {}
    fc_mom = {"headline": res.headline.mom_pct, "core1": res.core1.mom_pct, "core2": res.core2.mom_pct}
    for m in METRICS:
        act_mom = mom_from_index(prev_index[m], actual_index[m])
        attributions[m] = r2_attribute(m, fc_mom[m], act_mom)
        if m in ("core1", "core2"):
            ratios[m] = r3_core_mom_ratio(fc_mom[m], act_mom)
    if save_con is not None:
        save_entries(save_con, entries)
    return {"entries": entries, "attributions": attributions, "core_mom_ratio": ratios,
            "actual_yoy": act_yoy}


# ── 학습 피드백 요약 (R-4~R-6) ──────────────────────────────
def learning_feedback(entries: list[BTEntry]) -> list[str]:
    """저장된 백테스트로부터 다음 추정용 피드백 생성(§10.1 학습 피드백 형식)."""
    fb = []
    for m in METRICS:
        bias = r4_repeated_bias(entries, m)
        mae = r7_mae(entries, m)
        if mae is not None:
            fb.append(f"{METRIC_KR[m]}: {bias} · MAE {mae:.3f}%p")
    # §10.1 핵심 교훈
    under = [e for e in entries if e.metric in ("core1", "core2") and e.error_pp < -0.2]
    if under:
        fb.append("→ 근원 과소추정 반복: 개인서비스 디폴트 상향(+0.1~0.2% → +0.3~0.4%) 검토(§10.1 R-5)")
        fb.append("→ 근원① MoM ≥ 헤드라인 MoM 시 서비스 기조 강세 신호 → 서비스 가정 재검토 트리거(R-4)")
    return fb
