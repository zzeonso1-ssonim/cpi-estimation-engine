"""
KOSIS OpenAPI 월별 지수 자동수집 (무키 불가 — 인증키 필요).
근거: PRD §4.3 monthly_index. 헤드라인·근원①·근원②를 KOSIS에서 받아 upsert.

키: .env 파일의 KOSIS_API_KEY 또는 환경변수.  발급: https://kosis.kr/openapi/
통계표(orgId=101, 2020=100):
  · 헤드라인 : DT_1J22003 (시도별 소비자물가지수) objL1=T10(전국)
  · 근원①   : DT_1J22009 (식료품및에너지제외)   objL1=DB
  · 근원②   : DT_1J22007 (농산물및석유류제외)   objL1=QB
  · 품목별   : DT_1J22001 (지출목적별 품목포함)  objL1=T10, objL2=품목코드

실행:  python kosis_fetch.py [start_ym end_ym]   (기본 최근 13개월)
"""
from __future__ import annotations
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "cpi.db"
ENV_PATH = Path(__file__).parent / ".env"
PARAM_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

# (지표, 통계표, objL1 분류코드)
SERIES = [
    ("headline", "DT_1J22003", "T10"),
    ("core1",    "DT_1J22009", "DB"),
    ("core2",    "DT_1J22007", "QB"),
]


def load_api_key() -> str | None:
    """환경변수 → st.secrets → .env 순으로 KOSIS_API_KEY 탐색."""
    key = os.environ.get("KOSIS_API_KEY")
    if key:
        return key.strip()
    try:
        import streamlit as st
        key = st.secrets.get("KOSIS_API_KEY")
        if key:
            return str(key).strip()
    except Exception:
        pass
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("KOSIS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def _req(tbl: str, objL1: str, start_ym: str, end_ym: str, api_key: str) -> list[dict]:
    params = {
        "method": "getList", "apiKey": api_key, "format": "json", "jsonVD": "Y",
        "orgId": "101", "tblId": tbl, "itmId": "T", "objL1": objL1,
        "prdSe": "M", "startPrdDe": start_ym, "endPrdDe": end_ym,
    }
    url = PARAM_URL + "?" + urllib.parse.urlencode(params)
    raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):  # {"err":..,"errMsg":..}
        raise RuntimeError(f"KOSIS 오류({tbl}): {data}")
    return data


def fetch_monthly_index(start_ym: str, end_ym: str, api_key: str | None = None) -> dict[str, dict]:
    """헤드라인·근원①·근원② 월별 지수 수집. 반환: {ym: {headline, core1, core2}}.
    start_ym/end_ym: 'YYYYMM'."""
    api_key = api_key or load_api_key()
    if not api_key:
        raise RuntimeError(
            "KOSIS_API_KEY 미설정.\n"
            "  1) https://kosis.kr/openapi/ 에서 키 발급\n"
            "  2) .env 파일에 KOSIS_API_KEY=... 또는 환경변수 설정\n"
            "  (키 없이도 data/seed.py 확정 지수로 6월/5월 재현은 동작함)"
        )
    merged: dict[str, dict] = {}
    for metric, tbl, objL1 in SERIES:
        for r in _req(tbl, objL1, start_ym, end_ym, api_key):
            ym = f"{r['PRD_DE'][:4]}-{r['PRD_DE'][4:6]}"
            merged.setdefault(ym, {})[metric] = float(r["DT"])
    return merged


def upsert_index(con: sqlite3.Connection, ym, headline, core1, core2, source="KOSIS"):
    con.execute(
        "INSERT INTO monthly_index(ym,headline,core1,core2,confirmed,source) "
        "VALUES (?,?,?,?,1,?) "
        "ON CONFLICT(ym) DO UPDATE SET headline=?,core1=?,core2=?,confirmed=1,source=?",
        (ym, headline, core1, core2, source, headline, core1, core2, source),
    )


def sync(start_ym: str, end_ym: str, db_path: Path = DB_PATH) -> int:
    """수집 → monthly_index upsert. 반환: upsert 행수."""
    data = fetch_monthly_index(start_ym, end_ym)
    con = sqlite3.connect(db_path)
    n = 0
    for ym in sorted(data):
        v = data[ym]
        if {"headline", "core1", "core2"} <= v.keys():
            upsert_index(con, ym, v["headline"], v["core1"], v["core2"])
            n += 1
    con.commit()
    con.close()
    return n


def _default_range() -> tuple[str, str]:
    today = date.today()
    end = f"{today.year}{today.month:02d}"
    y = today.year - 1
    start = f"{y}{today.month:02d}"
    return start, end


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        start, end = _default_range()
    key = load_api_key()
    print(f"[KOSIS] 키 {'로드됨' if key else '없음'} · 수집범위 {start}~{end}")
    data = fetch_monthly_index(start, end)
    for ym in sorted(data):
        v = data[ym]
        print(f"  {ym}: H {v.get('headline')} / C1 {v.get('core1')} / C2 {v.get('core2')}")
    n = sync(start, end)
    print(f"[KOSIS] monthly_index {n}개월 upsert 완료.")
