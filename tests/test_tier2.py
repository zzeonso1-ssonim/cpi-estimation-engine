"""
Tier 2 골든 테스트 — 458 품목 가중치 적재·매핑·자동충격 가드레일.
근거: 2022 가중치 개편 보도자료(붙임2) + PRD §4.2/§4.5.
실행: python tests/test_tier2.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.db import connect, load_items, load_buckets, published_core_sum
from engine.core_engine import renormalize, renormalize_tier2
from web_factor_scan import (match_item, detect_direction, _strip_source,
                             DRIVERS, contribution_pp, item_in_headline,
                             _macro_direction, macro_risk_score, DriverSignal)
from web_update import parse_life_prices, classify_life_price_bucket, life_price_bucket_mom

FAIL = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAIL.append(name)


def main():
    con = connect()
    items = load_items(con)

    print("[품목 적재 무결성]")
    check("458개 품목 적재", len(items) == 458)
    total = round(sum(v["weight"] for v in items.values()), 1)
    check(f"가중치 합 1000.0 (실제 {total})", abs(total - 1000.0) < 0.05)

    print("[개별 품목 가중치·버킷·core (보도자료 붙임2)]")
    check("양파 w=0.7 / nonggsan", items["양파"]["weight"] == 0.7 and items["양파"]["bucket"] == "nonggsan")
    check("휘발유 w=24.1 / seokyu", items["휘발유"]["weight"] == 24.1 and items["휘발유"]["bucket"] == "seokyu")
    check("도시가스 w=11.5 / egw", items["도시가스"]["weight"] == 11.5 and items["도시가스"]["bucket"] == "egw")
    # core 플래그 (공식 정의, 붙임1):
    #   근원②=농산물(곡물 제외)·도시가스·석유류 제외 → 곡물 포함, 도시가스 제외
    #   근원①=식료품+에너지(석유류·전기료·도시가스·지역난방비) 제외 → 상수도료 포함
    check("쌀 곡물 → core2 포함", items["쌀"]["c2"] == 1 and items["쌀"]["c1"] == 0)
    check("양파 비곡물농산물 → core2 제외", items["양파"]["c2"] == 0)
    check("도시가스 → core2 제외(붙임1)", items["도시가스"]["c2"] == 0)
    check("상수도료 → core1 포함(에너지 아님)", items["상수도료"]["c1"] == 1)
    check("전기료 → core1 제외(에너지)", items["전기료"]["c1"] == 0)
    check("휘발유 → core1·2 모두 제외", items["휘발유"]["c1"] == 0 and items["휘발유"]["c2"] == 0)

    print("[자동충격 가드레일 (§4.5)]")
    # 정상 매칭: 제목 앞쪽 주어 우선
    h = match_item("양파값 1년 새 60% 하락…무·감자 공급", items, bucket="nonggsan")
    check("양파값 헤드라인 → 양파 매칭", h is not None and h[0] == "양파")
    # 오매칭 차단: '양산신문'의 '신문'(공업제품)이 석유류 검색에 끼지 않음
    h2 = match_item(_strip_source("양산 기름값 하락 더뎌", "양산신문"), items, bucket="seokyu")
    check("'양산신문'→신문 오매칭 차단", h2 is None)
    # 버킷 불일치 차단
    h3 = match_item("커피 가격 인상", items, bucket="seokyu")
    check("버킷 불일치 시 매칭 안 함", h3 is None)

    print("[헤드라인 % 파싱 → 충격값]")
    d, s = detect_direction("양파값 60% 하락")
    check("'60% 하락' → down -60", d == "down" and s == -60.0)
    d2, s2 = detect_direction("도시가스 요금 인상")
    check("%없는 인상 → up placeholder +1.0", d2 == "up" and s2 == 1.0)
    d3, s3 = detect_direction("물가 안정세 지속")
    check("방향어 없음 → ? 0", d3 == "?" and s3 == 0.0)

    print("[결정요인 프레임워크 (사용자 제공 우선순위)]")
    # 드라이버의 모든 매핑품목이 458품목에 존재
    bad = [(d["name"], it) for d in DRIVERS for it in d["items"] if it not in items]
    check(f"드라이버 매핑품목 전부 458에 존재 (불일치 {len(bad)})", not bad)
    # 우선순위: ⭐⭐⭐(석유류·공공요금/통신) 2개
    top = [d["name"] for d in DRIVERS if d["importance"] == 3]
    check("⭐⭐⭐ 결정적 드라이버 = 석유류·공공요금 2개", len(top) == 2)
    # 환율은 거시지표(CPI 품목 아님)로 분리
    fx = next((d for d in DRIVERS if d["key"] == "fx"), None)
    check("환율 = 거시지표(macro), 매핑품목 없음", fx and fx["macro"] and not fx["items"])
    # 기여도식: 휘발유(24.1) +3% → +0.0723%p
    check("기여도(%p) = 가중치/1000×충격", abs(contribution_pp(24.1, 3.0) - 0.0723) < 1e-6)
    # 별칭 매칭: 뉴스 표현(통신비/전기요금/버스요금) → KOSIS 품목명(휴대전화료/전기료/시내버스료)
    check("'통신비 인하' → 휴대전화료", item_in_headline("휴대전화료", "통신비 인하 경쟁"))
    check("'전기요금 인상' → 전기료", item_in_headline("전기료", "전기요금 인상 검토"))
    check("'버스 요금 300원' → 시내버스료", item_in_headline("시내버스료", "서울 버스 요금 300원 인상"))
    check("무관 헤드라인은 매칭 안 함", not item_in_headline("전기료", "양파값 급등"))

    print("[거시지표(환율·국제유가) → 시나리오 리스크 연동 (§9.1)]")
    check("환율 약세 → 상방(+1)", _macro_direction("fx", "원화 약세 1500원 돌파") == 1)
    check("환율 강세/안정 → 하방(-1)", _macro_direction("fx", "환율 하락 원화 강세 안정") == -1)
    check("국제유가 급등 → 상방(+1)", _macro_direction("oil", "국제유가 급등 100달러 돌파") == 1)
    check("국제유가 하회 → 하방(-1)", _macro_direction("oil", "국제유가 70달러 하회") == -1)
    sg = [DriverSignal(1, "석유류", 3, False, [], key="oil", macro_headline="유가 급등"),
          DriverSignal(5, "환율", 2, True, [], key="fx", macro_headline="원화 약세")]
    score, _det = macro_risk_score(sg)
    check("유가↑·환율↑ 집계 = +1.0 강한 상방", abs(score - 1.0) < 1e-9)
    sg[0].macro_headline = "유가 급락 하락"
    score2, _ = macro_risk_score(sg)
    check("유가↓·환율↑ 집계 = -0.2(가중평균)", abs(score2 - (-0.2)) < 1e-9)
    check("거시 헤드라인 없으면 0", macro_risk_score([])[0] == 0.0)

    print("[참가격 생필품 → bottom-up 보조 신호]")
    sample_price_html = """
    <h3>생필품 주간정보</h3>
    <a>품목별가격정보 더보기</a>
    <p>당근(흙당근, 100g)</p><span>금주</span><b>485</b><span>2주전 대비</span><b>42</b><span>전년대비</span><b>485</b>
    <p>깻잎(100g)</p><span>금주</span><b>3,121</b><span>2주전 대비</span><b>238</b><span>전년대비</span><b>3,121</b>
    <p>크리넥스 클린케어 3겹(30롤)</p><span>금주</span><b>27,213</b><span>2주전 대비</span><b>-2,268</b><span>전년대비</span><b>27,213</b>
    <p>벡셀 알카라인 건전지 AA(4개입)</p><span>금주</span><b>8,200</b><span>2주전 대비</span><b>0</b><span>전년대비</span><b>8,200</b>
    <h3>주요소식</h3>
    """
    life = parse_life_prices(sample_price_html)
    check("참가격 HTML에서 생필품 4개 파싱", len(life) == 4)
    check("당근 → 농산물 버킷", classify_life_price_bucket("당근(흙당근, 100g)") == "nonggsan")
    check("크리넥스 → 기타 공업제품 버킷", classify_life_price_bucket("크리넥스 클린케어 3겹") == "gongeop")
    sig = life_price_bucket_mom(life)
    check("농산물 2개 표본 → MoM 제안 생성", "nonggsan" in sig and sig["nonggsan"]["count"] == 2)
    check("기타 공업제품 2개 표본 → MoM 제안 생성", "gongeop" in sig and sig["gongeop"]["count"] == 2)

    print("[Tier2 재정규화 — 공식 근원정의(붙임1)로 잔차 축소]")
    buckets = load_buckets(con)
    pub = published_core_sum(con)
    for core, p in [("core1", pub["core1"]), ("core2", pub["core2"])]:
        t1 = renormalize(buckets, core, p)
        t2 = renormalize_tier2(items, core, p)
        check(f"{core} Tier2 잔차({t2.residual_vs_published:+.1f}) ≤ Tier1({t1.residual_vs_published:+.1f})",
              abs(t2.residual_vs_published) <= abs(t1.residual_vs_published))
        check(f"{core} 검증6 Σ조정=1000", abs(t2.sum_adj - 1000) < 0.1)
    # core2는 곡물 포함·도시가스 제외로 거의 정확(공표 909.8, 이론 909.7)
    t2c2 = renormalize_tier2(items, "core2", pub["core2"])
    check(f"core2 Tier2 잔차 |{t2c2.residual_vs_published:+.1f}| < 4 (곡물/도시가스 분리)",
          abs(t2c2.residual_vs_published) < 4.0)

    con.close()
    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
