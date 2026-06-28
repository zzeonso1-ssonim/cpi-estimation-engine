"""
정합성 검증 — 검증 1~8(§8), 특히 재현 게이트 6·7.
근거: PRD v3.1 §8.

재현 게이트(검증 6·7)를 통과하지 못하면 전망치를 발행하지 않는다(§8, §13).
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Check:
    no: str
    name: str
    value: float | None
    passed: bool | None     # None = 게이트 아님(정보성)
    note: str = ""


def run_checks(headline_yoy, core1_yoy, core2_yoy,
               core_renorm=None,            # {'core1': sum_adj, 'core2': sum_adj}
               reproduce_gap=None,          # 재현 YoY vs 표기 YoY 괴리(%p) — 검증7
               consensus=None,              # {'headline':..,'core1':..,'core2':..} 또는 None
               service_contrib=None,        # 개인서비스 기여도(%p) — 검증4
               ) -> list[Check]:
    checks: list[Check] = []

    # 검증 1~3: 지표 간 갭(정보성)
    checks.append(Check("1", "Headline YoY − Core① YoY", headline_yoy - core1_yoy, None))
    checks.append(Check("2", "Headline YoY − Core② YoY", headline_yoy - core2_yoy, None))
    checks.append(Check("3", "Core② YoY − Core① YoY", core2_yoy - core1_yoy, None))

    # 검증 4: 개인서비스 기여도 / 근원 (정보성)
    if service_contrib is not None:
        checks.append(Check("4", "개인서비스 기여도(%p)", service_contrib, None))

    # 검증 6(게이트): Σ조정가중치 = 1000 ±0.1
    if core_renorm:
        for core, sum_adj in core_renorm.items():
            ok = abs(sum_adj - 1000) <= 0.1
            checks.append(Check("6", f"Σ조정가중치({core}) = 1000 ±0.1",
                                sum_adj, ok,
                                "" if ok else "재정규화 오류 — 발행 금지"))

    # 검증 7(게이트): 재현 YoY vs 표기 YoY 괴리 ≤ 0.05%p
    if reproduce_gap is not None:
        ok = abs(reproduce_gap) <= 0.05
        checks.append(Check("7", "재현 YoY vs 표기 YoY 괴리 ≤ 0.05%p",
                            reproduce_gap, ok,
                            "" if ok else "산식·표기 불일치 — 발행 금지(§8)"))

    # 검증 8: 컨센서스 갭 산출 여부(§9.3)
    if consensus is not None:
        for k, v in consensus.items():
            checks.append(Check("8", f"컨센서스 갭({k})", v, None))
    else:
        checks.append(Check("8", "컨센서스 갭", None, None, "컨센서스 없음 — 추세/한은전망 대비로 대체"))

    return checks


def gate_passed(checks: list[Check]) -> bool:
    """게이트(passed가 명시된 검증)가 모두 통과했는가 → 전망 발행 가능 여부."""
    gates = [c for c in checks if c.passed is not None]
    return all(c.passed for c in gates)
