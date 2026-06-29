"""
DB 초기화 + 시드 적재.
근거: PRD v3.1 §4.2(가중치·근원 정의), §11/§10.1(확정 월별 지수).

실행:  python data/seed.py
결과:  data/cpi.db 생성(있으면 시드 테이블 재적재)
"""
import csv
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "cpi.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
ITEM_WEIGHTS_CSV = Path(__file__).parent / "item_weights_2022.csv"

# ───────────────────────────────────────────────────────────
# 버킷 가중치 + 근원 포함 플래그 — PRD §4.2 매핑표 (2022년 기준, 합=1000.0)
#   (code, name, weight, in_core1, in_core2, note)
# 주의(§4.2): 농산물 38.4는 곡물 + 곡물제외농산물의 합. Tier 1에서는 한 버킷으로 두고,
#   곡물/비곡물 분리(근원②가 곡물은 포함, 비곡물 농산물은 제외)는 Tier 2 품목코드에서 확정.
#   따라서 Tier 1 농산물 버킷의 in_core2는 '근사(0)'이며, 공표 근원합으로 보정한다(아래 PUBLISHED 참조).
# ───────────────────────────────────────────────────────────
BUCKETS = [
    # code            name                weight  c1 c2  note
    ("nonggsan",      "농산물(곡물 포함)",   38.4, 0, 0, "곡물은 근원②포함·비곡물은 제외. Tier1 근사, 공표합으로 보정"),
    ("chuksusan",     "축산물·수산물",       37.2, 0, 1, "축산26.4·수산10.8"),
    ("gagong",        "가공식품(주류 제외)", 82.7, 0, 1, ""),
    ("seokyu",        "석유류",              46.6, 0, 0, "근원①·② 모두 제외"),
    ("egw",           "전기·가스·수도",      33.7, 0, 1, "근원②는 도시가스만 제외(Tier2 분리)"),
    ("gongeop",       "기타 공업제품",      209.0, 1, 1, "주류·담배 포함"),
    ("jipse",         "집세",                99.1, 1, 1, ""),
    ("gonggong",      "공공서비스",         120.0, 1, 1, ""),
    ("gaein",         "개인서비스",         333.3, 1, 1, "가장 끈적함. 계절성·추세 의존"),
]
# 합 검증: 38.4+37.2+82.7+46.6+33.7+209.0+99.1+120.0+333.3 = 1000.0

# 공표 근원 가중치 합(§4.2 note) — Tier 1 재정규화의 권위 앵커값.
#   버킷 단순합과의 잔차(인삼·화초·주류 등 혼재 버킷분)는 진단으로 노출(§1.3 혼재버킷 경고).
PUBLISHED_CORE_WEIGHT = {
    "core1": 782.2,   # 식료품및에너지제외 (309품목)
    "core2": 909.8,   # 농산물및석유류제외 (401품목)
}

# ───────────────────────────────────────────────────────────
# 확정 월별 지수 — §11(6월 예시), §10.1(4월 기준), 모두 KOSTAT 260602 공표 기반
#   (ym, headline, core1, core2, confirmed, source)
# ───────────────────────────────────────────────────────────
MONTHLY = [
    ("2025-05", 116.27, 113.10, 115.06, 0, "§10.1 YoY로 역산(미확정)"),
    ("2025-06", 116.31, 113.17, 115.25, 1, "KOSTAT 260602"),
    ("2025-07", 116.52, 113.47, 115.29, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2025-08", 116.45, 112.84, 114.84, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2025-09", 117.06, 113.36, 115.56, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2025-10", 117.42, 113.79, 116.00, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2025-11", 117.20, 113.64, 115.82, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2025-12", 117.57, 113.83, 116.02, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2026-01", 118.03, 114.41, 116.56, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2026-02", 118.40, 114.87, 116.93, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2026-03", 118.80, 114.98, 117.00, 1, "KOSIS DT_1J22003/22009/22007"),
    ("2026-04", 119.37, 115.38, 117.38, 1, "KOSTAT 260602 (§10.1)"),
    ("2026-05", 119.92, 115.97, 117.97, 1, "KOSTAT 260602 (§11)"),
]

# §10.1 5월 백테스트의 품목군별 추정 MoM(%) — 버킷 코드 기준
#   (§10.1 '농축수산물 75.6'은 농산물38.4+축수산37.2 → 둘 다 -0.3%로 분해)
BACKTEST_MAY_MOM = {
    "seokyu": 2.0, "nonggsan": -0.3, "chuksusan": -0.3, "gagong": 0.2,
    "gongeop": 0.1, "egw": 0.0, "jipse": 0.1, "gonggong": 0.1, "gaein": 0.2,
}

# §10.1 백테스트 결과(전망 vs 실제 YoY, KOSTAT 260602) — 학습 루프 시드
#   (ym, metric, forecast_yoy, actual_yoy, error_pp, cause)
BACKTEST_RECORDS = [
    ("2026-05", "headline", 2.87, 3.14, -0.27, "MoM 과소추정(변동품목 방향은 맞음)"),
    ("2026-05", "core1",    2.17, 2.54, -0.37, "끈적한 서비스·근원재 MoM 과소추정(개인서비스)"),
    ("2026-05", "core2",    2.22, 2.53, -0.31, "서비스·가공식품 MoM 과소추정"),
]

# ───────────────────────────────────────────────────────────
# 2026년 실제 탐지 이벤트 — PRD §4.5 바 「실제 탐지 사례」
#   (ym, name, target_bucket, target_weight, shock_pct, lag_months, reversal_rate, importance, direction, reason)
#   ym = CPI 반영월(시차 적용 후). target_weight = 영향 품목 가중치(버킷의 일부).
#   shock_pct는 §4.5 라 예시·가중치 기준 추정치(단가 미확정분은 reason에 명시).
# ───────────────────────────────────────────────────────────
EVENTS = [
    # 6월 반영
    ("2026-06", "도시가스 5·6월 연속 단가 인상", "egw", 11.0, 3.0, 0, 0.0, "Medium", "up",
     "서울도시가스 공지. 단가 미확정 → 추정 +3%(가중치 11). 기여도 +0.033%p"),
    ("2026-06", "석유 최고가격제 시행(6/13)", "seokyu", 46.6, -1.0, 0, 0.0, "Medium", "down",
     "정유사→주유소 공급가 상한. 상한단가 미확정 → 추정 -1%. 기여도 -0.047%p"),
    ("2026-06", "WTI 하락($84~100, 5월 대비)", "seokyu", 46.6, -3.0, 0, 0.4, "High", "down",
     "호르무즈 위기 완화. 오피넷 반영 추정 -3%. 기여도 -0.14%p. 되돌림 가능성 0.4"),
    # 7월 반영(월 필터 검증용 — 6월 전망엔 미반영)
    ("2026-07", "이통3사 통합요금제(2만원대 신설)", "gonggong", 52.0, -2.0, 1, 0.0, "High", "down",
     "LGU+ 6/1·SKT 7/2·KT 7/1. 통신서비스 가중치 ~52, 전환율 시차. 기여도 -0.104%p"),
    ("2026-07", "도수치료 관리급여 전환(7/1)", "gonggong", 2.0, -65.0, 0, 0.0, "High", "down",
     "상한 4.385만원/회. 가중치 ~2, 비급여 대비 대폭 하락. 기여도 -0.13%p"),
]


def seed_items(con) -> None:
    """458개 품목 가중치(붙임2) + 버킷 매핑을 item_map(level='item')에 적재 (Tier 2).
    가중치는 item_weights_2022.csv에서, 버킷·core플래그는 data/map_items.py에서.
    버킷합을 공표값과 대조해 잔차를 진단 출력(§1.3)."""
    if not ITEM_WEIGHTS_CSV.exists():
        print(f"[seed] 품목 가중치 CSV 없음({ITEM_WEIGHTS_CSV.name}) — 품목 단위(Tier2) 적재 건너뜀.")
        return
    sys.path.insert(0, str(Path(__file__).parent))
    from map_items import classify, core_flags, PUBLISHED_BUCKET

    rows = list(csv.DictReader(ITEM_WEIGHTS_CSV.open(encoding="utf-8")))
    sums = {k: 0.0 for k in PUBLISHED_BUCKET}
    unmapped = []
    for r in rows:
        name, w = r["item"], float(r["w2022"])
        bucket = classify(name)
        if bucket is None:
            unmapped.append(name)
            continue
        c1, c2 = core_flags(bucket, name)
        sums[bucket] += w
        con.execute(
            "INSERT OR REPLACE INTO item_map(code,name,level,weight,in_core1,in_core2,parent_bucket,note) "
            "VALUES (?,?,'item',?,?,?,?,?)",
            (f"I_{name}", name, w, c1, c2, bucket, "붙임2 2022가중치"),
        )
    print(f"[seed] 품목 {len(rows)-len(unmapped)}개 적재(Tier2). 미분류 {len(unmapped)}개 {unmapped or ''}")
    for k, tgt in PUBLISHED_BUCKET.items():
        d = sums[k] - tgt
        if abs(d) >= 1.0:
            print(f"   · {k}: 품목합 {sums[k]:.1f} vs 공표 {tgt:.1f} → 잔차 {d:+.1f} (§1.3 혼재)")


def build(db_path: Path = DB_PATH) -> None:
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    con.executemany(
        "INSERT INTO item_map(code,name,level,weight,in_core1,in_core2,note) "
        "VALUES (?,?,'bucket',?,?,?,?)",
        BUCKETS,
    )
    seed_items(con)
    con.executemany(
        "INSERT INTO monthly_index(ym,headline,core1,core2,confirmed,source) "
        "VALUES (?,?,?,?,?,?)",
        MONTHLY,
    )
    con.executemany(
        "INSERT INTO event_coef(ym,name,target_bucket,target_weight,shock_pct,"
        "lag_months,reversal_rate,importance,direction,reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        EVENTS,
    )
    con.executemany(
        "INSERT INTO backtest(ym,metric,forecast_yoy,actual_yoy,error_pp,cause) "
        "VALUES (?,?,?,?,?,?)",
        BACKTEST_RECORDS,
    )
    # 공표 근원합을 메타로 저장(별도 1행 테이블)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL)")
    con.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                    list(PUBLISHED_CORE_WEIGHT.items()))
    # 앱 입력 저장 상태 테이블 (세션 간 입력 유지)
    con.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)")

    con.commit()

    total = con.execute("SELECT SUM(weight) FROM item_map WHERE level='bucket'").fetchone()[0]
    con.close()
    print(f"[seed] cpi.db 생성 완료. 버킷 가중치 합 = {total:.1f} (기대 1000.0)")
    assert abs(total - 1000.0) < 0.1, "버킷 가중치 합 != 1000"


if __name__ == "__main__":
    build()
