"""
이벤트 계수 레이어 — 정성 플래그를 수치 조정으로 전환.
근거: PRD v3.1 §7(이벤트 계수), §4.5(웹서치 체크리스트·기여도식·중요도·시차).

핵심 변환:
  · 헤드라인 기여도(%p) = 해당 품목 가중치/1000 × 품목 MoM 변동(%)         (§4.5 라)
  · 버킷 MoM 조정(%)    = 해당 품목 가중치/버킷 가중치 × 품목 MoM 변동(%)
    → 버킷 기여도(버킷가중치/1000 × 버킷MoM조정) = 헤드라인 기여도 와 항등(이중계상 없음)
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Event:
    name: str
    target_bucket: str          # 버킷 코드
    target_weight: float        # 영향 품목 가중치(천분비)
    shock_pct: float            # 당월 충격(해당 품목 MoM %)
    ym: str                     # CPI 반영월 'YYYY-MM' (시차 적용 후)
    lag_months: float = 0.0
    reversal_rate: float = 0.0  # 다음달 되돌림 비율(0~1)
    direction: str = ""         # up/down (참고)
    reason: str = ""            # 수동 조정 사유(필수)


# ── 기여도·중요도 (§4.5 라·다) ──────────────────────────────
def headline_contrib(target_weight: float, shock_pct: float) -> float:
    """헤드라인 기여도(%p) = 가중치/1000 × MoM변동(%)."""
    return target_weight / 1000 * shock_pct


def classify_importance(contrib_pp: float) -> str:
    """중요도 분류(§4.5 다): High ≥0.1 / Medium 0.03~0.1 / Low <0.03 (절대값 기준)."""
    a = abs(contrib_pp)
    if a >= 0.1:
        return "High"
    if a >= 0.03:
        return "Medium"
    return "Low"


# ── 월 이동 헬퍼 ────────────────────────────────────────────
def prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


# ── 당월 집계: 신규 충격 + 전월 이벤트의 되돌림 ──────────────
def effective_events(events: list[Event], target_ym: str):
    """
    target_ym에 적용되는 (Event, 유효충격%, 종류) 목록.
      · 당월   : ym == target_ym 인 이벤트의 shock_pct
      · 되돌림 : ym == target_ym 직전월 이고 reversal_rate>0 → −reversal_rate×shock (§5.4 중복금지)
    """
    out = []
    pm = prev_month(target_ym)
    for ev in events:
        if ev.ym == target_ym:
            out.append((ev, ev.shock_pct, "당월"))
        elif ev.ym == pm and ev.reversal_rate > 0:
            out.append((ev, -ev.reversal_rate * ev.shock_pct, "되돌림"))
    return out


def bucket_event_adj(events: list[Event], target_ym: str,
                     bucket_weight: dict[str, float]) -> dict[str, float]:
    """버킷별 이벤트 MoM 조정(%) 합. bucket_weight: code -> 버킷 원가중치."""
    adj: dict[str, float] = {}
    for ev, eff_shock, _kind in effective_events(events, target_ym):
        bw = bucket_weight.get(ev.target_bucket)
        if not bw:
            continue
        adj[ev.target_bucket] = adj.get(ev.target_bucket, 0.0) + ev.target_weight / bw * eff_shock
    return adj


@dataclass
class EventRow:
    name: str
    bucket: str
    kind: str               # 당월/되돌림
    eff_shock: float        # 유효 품목 MoM(%)
    contrib_pp: float       # 헤드라인 기여도(%p)
    importance: str
    reason: str


def event_breakdown(events: list[Event], target_ym: str) -> list[EventRow]:
    """이벤트별 헤드라인 기여도·중요도 표(리포트/UI용)."""
    rows = []
    for ev, eff_shock, kind in effective_events(events, target_ym):
        c = headline_contrib(ev.target_weight, eff_shock)
        rows.append(EventRow(ev.name, ev.target_bucket, kind, eff_shock, c,
                             classify_importance(c), ev.reason))
    return rows


# ── §7 경고 ────────────────────────────────────────────────
def event_warnings(headline_mom_with: float, headline_mom_base: float,
                   event_rows: list[EventRow], k_dependence: float = 0.5,
                   k_deviation: float = 0.2) -> list[str]:
    """
    §7 경고:
      · 이벤트 의존 전망 : Σ|이벤트 기여도| ≥ 50% × |헤드라인 MoM|
      · 기본 경로 이탈   : |이벤트 반영 MoM − 기본 MoM| ≥ 0.2%p
    """
    warns = []
    total_abs = sum(abs(r.contrib_pp) for r in event_rows)
    if abs(headline_mom_with) > 1e-9 and total_abs >= k_dependence * abs(headline_mom_with):
        warns.append(f"⚠️ 이벤트 의존 전망: Σ|이벤트 기여도| {total_abs:.3f}%p "
                     f"≥ 50%×|헤드라인 MoM {headline_mom_with:.3f}%|")
    if abs(headline_mom_with - headline_mom_base) >= k_deviation:
        warns.append(f"⚠️ 기본 경로 이탈: 이벤트 반영 MoM {headline_mom_with:.3f}% vs "
                     f"기본 {headline_mom_base:.3f}% (이탈 {headline_mom_with-headline_mom_base:+.3f}%p)")
    return warns


def load_events(con, ym_window: list[str] | None = None) -> list[Event]:
    """DB event_coef → Event 리스트. ym_window 주면 해당 월들만."""
    q = "SELECT name,target_bucket,target_weight,shock_pct,ym,lag_months,reversal_rate,direction,reason FROM event_coef"
    rows = con.execute(q).fetchall()
    evs = [Event(r["name"], r["target_bucket"], r["target_weight"], r["shock_pct"], r["ym"],
                 r["lag_months"], r["reversal_rate"], r["direction"], r["reason"]) for r in rows]
    if ym_window:
        evs = [e for e in evs if e.ym in ym_window]
    return evs
