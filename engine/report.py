"""
리포트 출력 모듈 — 채권전략팀장용 표준 문장.
근거: PRD v3.1 §12.

숫자 칸은 §8 재현 게이트(검증 6·7)를 통과한 값만 채운다(§12).
"""
from __future__ import annotations


def fmt(x, nd=2):
    return f"{x:+.{nd}f}" if x is not None else "—"


def fmt_yoy(x, nd=2):
    return f"{x:.{nd}f}%" if x is not None else "—"


def build_report(ym_label: str,
                 headline_mom, headline_yoy,
                 core1_yoy, core2_yoy,
                 gaps: dict | None,
                 grade: str,
                 driver: str = "[유가/농산물/기저효과]",
                 core_judgement: str = "[둔화/고착/재가속]",
                 market_view: str = "[인하 명분 강화/동결 장기화/인하 기대 후퇴]",
                 gate_ok: bool = True,
                 scenarios: dict | None = None,
                 pred_interval: tuple | None = None) -> str:
    if not gate_ok:
        return ("⚠️ 재현 게이트(검증 6·7) 미통과 — 전망치를 발행하지 않습니다(§8/§13).\n"
                "   재정규화 Σ=1000 또는 재현 괴리 ≤0.05%p 조건을 먼저 충족하세요.")

    g = gaps or {}
    extra = ""
    if scenarios:
        extra += (f"\n\n시나리오 확률은 Base {scenarios['base']}% · "
                  f"Upside {scenarios['upside']}% · Downside {scenarios['downside']}%이다.")
    if pred_interval:
        extra += f"\n헤드라인 예측구간(점추정±MAE)은 {pred_interval[0]:.2f}~{pred_interval[1]:.2f}%이다."
    return f"""**{ym_label} CPI 전망**
헤드라인은 MoM {fmt(headline_mom)}%, YoY {fmt_yoy(headline_yoy)}로 추정한다.
근원① 식료품및에너지제외는 YoY {fmt_yoy(core1_yoy)}, 근원② 농산물및석유류제외는 YoY {fmt_yoy(core2_yoy)}로 추정한다.

이번 전망의 핵심은 헤드라인보다 근원이다.
헤드라인은 {driver} 영향으로 변동하지만, 근원은 개인서비스·가공식품·공공요금 때문에 {core_judgement}으로 판단한다.

컨센서스 갭은 헤드라인 {fmt(g.get('headline')) if g.get('headline') is not None else '컨센서스 없음'}%p, \
근원① {fmt(g.get('core1')) if g.get('core1') is not None else '없음'}%p, \
근원② {fmt(g.get('core2')) if g.get('core2') is not None else '없음'}%p이다.
채권시장 관점에서는 {market_view}에 가깝다.
(신뢰도 등급: {grade}){extra}"""
