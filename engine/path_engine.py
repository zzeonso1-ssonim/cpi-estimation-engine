"""
MoM 경로 시나리오 엔진.
고정(또는 이달만 다른) MoM 가정 → 12개월 YoY 경로 산출.
엑셀 '소비자물가추정.xlsx' 수기모델의 Python 재현.
"""
from __future__ import annotations
from dataclasses import dataclass

from engine.index_engine import project_index, yoy as calc_yoy

# ── 확정 공표 YoY — 통계청 발표치 (과거 1년 차트 기준) ─────────────────────
# 헤드라인: 소비자물가 (2020=100, w=1000)
# 근원②  : 농산물및석유류제외 (2020=100, w=909.8)
HIST_YOY: dict[str, dict[str, float]] = {
    "headline": {
        "2025-06": 2.170, "2025-07": 2.094, "2025-08": 1.668,
        "2025-09": 2.102, "2025-10": 2.380, "2025-11": 2.448,
        "2025-12": 2.315, "2026-01": 2.005, "2026-02": 1.999,
        "2026-03": 2.158, "2026-04": 2.569, "2026-05": 3.139,
    },
    "core2": {
        "2025-06": 2.390, "2025-07": 2.325, "2025-08": 1.854,
        "2025-09": 2.383, "2025-10": 2.510, "2025-11": 2.324,
        "2025-12": 2.256, "2026-01": 2.264, "2026-02": 2.462,
        "2026-03": 2.300, "2026-04": 2.194, "2026-05": 2.529,
    },
}


def add_months(ym: str, n: int) -> str:
    """'YYYY-MM' 에 n개월 가감."""
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


def to_date_str(ym: str) -> str:
    """Plotly 날짜 축용 'YYYY-MM-01' 변환."""
    return f"{ym}-01"


@dataclass
class ScenarioPath:
    name: str
    color: str
    dash: str
    moms: list[float]
    months: list[str]          # YYYY-MM, 12개월
    headline_yoys: list[float]
    core2_yoys: list[float]


def build_path(
    name: str,
    color: str,
    dash: str,
    moms: list[float],         # 12개월 MoM 가정 (%)
    start_ym: str,             # 마지막 확정월 (예: "2026-05")
    start_h: float,            # 헤드라인 지수 at start_ym
    start_c2: float,           # 근원② 지수 at start_ym
    base_h: dict[str, float],  # {ym: 지수} — 전년동월 헤드라인 분모
    base_c2: dict[str, float], # {ym: 지수} — 전년동월 근원② 분모
) -> ScenarioPath:
    months = [add_months(start_ym, i + 1) for i in range(len(moms))]
    h, c2 = start_h, start_c2
    h_yoys, c2_yoys = [], []
    for i, mom in enumerate(moms):
        h  = project_index(h,  mom)
        c2 = project_index(c2, mom)
        base_ym_key = add_months(months[i], -12)
        h_yoys.append(calc_yoy(h,  base_h [base_ym_key]))
        c2_yoys.append(calc_yoy(c2, base_c2[base_ym_key]))
    return ScenarioPath(name, color, dash, moms, months, h_yoys, c2_yoys)


def default_scenarios(this_month_mom: float = 0.1) -> list[dict]:
    """기본 4개 시나리오 정의.
    this_month_mom: 「이달↓ 시나리오」의 이달 MoM (기본 0.1%)."""
    return [
        {
            "name": "0.1% (하방)",
            "color": "#5B9BD5", "dash": "dash",
            "moms": [0.1] * 12,
        },
        {
            "name": "0.2% (베이스)",
            "color": "#70AD47", "dash": "solid",
            "moms": [0.2] * 12,
        },
        {
            "name": "0.3% (상방)",
            "color": "#FF4444", "dash": "dash",
            "moms": [0.3] * 12,
        },
        {
            "name": f"이달 {this_month_mom:+.1f}% → 이후 0.2%",
            "color": "#ED7D31", "dash": "dot",
            "moms": [this_month_mom] + [0.2] * 11,
        },
    ]
