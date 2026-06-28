"""
웹 라이브 데이터 수집 (무키).
근거: PRD v3.1 §4.4 — "CPI 석유류 = 국내 주유소가(오피넷) ≠ 국제유가".

오피넷(한국석유공사) 전국 평균 판매가를 API 키 없이 공개 페이지에서 수집한다.
  · 엔드포인트: /user/dopospdrg/dopOsPdrgSelect.do  (GET, 무키)
  · 페이지에 일별 전국평균가가 JSON 객체로 임베드됨: {"B027":..,"D047":..,"gb_nm":"YYYY년MM월DD일"}
  · 유종코드: B027 보통(일반)휘발유 · D047 자동차용경유 · B034 고급휘발유 · C004 실내등유 · C042 보일러등유

주의: 비공식 스크래핑이므로 페이지 구조 변경 시 깨질 수 있음(파싱 실패 시 명시적 예외).
"""
from __future__ import annotations
import calendar
import urllib.parse
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
import json
import re
import statistics
import urllib.request

OPINET_BASE    = "https://www.opinet.co.kr"
OPINET_URL     = OPINET_BASE + "/user/dopospdrg/dopOsPdrgSelect.do"
_HIST_CSV_URL  = OPINET_BASE + "/user/doop/doopOilHistoryCsv.do"
PRICE_GO_URL   = "https://www.price.go.kr/tprice/index.do"
PROD = {"B027": "보통휘발유", "D047": "자동차용경유", "B034": "고급휘발유",
        "C004": "실내등유", "C042": "보일러등유"}
_OBJ_RE  = re.compile(r'\{[^{}]*"gb_nm":"[^"]+"[^{}]*\}')
_DATE_RE = re.compile(r"(\d{4})년(\d{2})월(\d{2})일")
_NUM_RE  = re.compile(r"^-?\d[\d,]*$")
_HIST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": OPINET_BASE + "/user/doop/doopOilHistory.do",
}


def _csv_post(params: dict, timeout: int = 15) -> bytes:
    """doopOilHistoryCsv.do 에 POST 후 raw bytes 반환."""
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_HIST_CSV_URL, data=data, headers=_HIST_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _decode_csv(raw: bytes) -> list[list[str]]:
    """EUC-KR CSV bytes → [[col, ...], ...] (헤더 포함)."""
    text = raw.decode("euc-kr", errors="replace")
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append([c.strip() for c in line.split(",")])
    return rows


def fetch_opinet_monthly_csv(year_from: int, month_from: int,
                             year_to: int, month_to: int,
                             fuel_code: str = "B027") -> list[dict]:
    """오피넷 月별 전국 평균가 CSV 수집.

    반환: [{"ym": "YYYY-MM", "보통휘발유": float, ...}, ...]
    오피넷은 완성된 월만 반환하므로 진행 중인 당월은 포함되지 않는다.
    """
    params = {
        "TERM": "M",
        "STA_Y": str(year_from), "STA_M": f"{month_from:02d}",
        "END_Y": str(year_to),   "END_M": f"{month_to:02d}",
        f"OIL_CD_{fuel_code}_P": "Y",
    }
    rows = _decode_csv(_csv_post(params))
    # 행 형식: ["2026년01월", "1704.38"]
    _YM_RE = re.compile(r"(\d{4})년(\d{2})월")
    out = []
    for row in rows[1:]:  # 헤더 건너뜀
        if len(row) < 2:
            continue
        m = _YM_RE.match(row[0])
        if not m:
            continue
        try:
            ym = f"{m.group(1)}-{m.group(2)}"
            out.append({"ym": ym, PROD.get(fuel_code, fuel_code): float(row[1])})
        except ValueError:
            continue
    return out


def fetch_opinet_weekly_csv(year: int, month: int,
                            fuel_code: str = "B027") -> list[dict]:
    """오피넷 週별 전국 평균가 CSV 수집 (당월 진행 중 데이터 포함).

    반환: [{"ym": "YYYY-MM", "week": int, "보통휘발유": float}, ...]
    """
    params = {
        "TERM": "W",
        "STA_Y": str(year), "STA_M": f"{month:02d}", "STA_W": "1",
        "END_Y": str(year), "END_M": f"{month:02d}", "END_W": "5",
        f"OIL_CD_{fuel_code}_P": "Y",
    }
    rows = _decode_csv(_csv_post(params))
    # 행 형식: ["2026년06월1주", "2010.40"]
    _WK_RE = re.compile(r"(\d{4})년(\d{2})월(\d+)주")
    out = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        m = _WK_RE.match(row[0])
        if not m:
            continue
        try:
            ym = f"{m.group(1)}-{m.group(2)}"
            out.append({"ym": ym, "week": int(m.group(3)),
                        PROD.get(fuel_code, fuel_code): float(row[1])})
        except ValueError:
            continue
    return out


def _fetch_html(timeout: int = 15) -> str:
    req = urllib.request.Request(OPINET_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_opinet(html: str) -> list[dict]:
    """페이지 HTML → 일별 전국평균가 레코드 리스트(날짜 오름차순)."""
    recs = []
    for m in _OBJ_RE.finditer(html):
        try:
            o = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        dm = _DATE_RE.search(o.get("gb_nm", ""))
        if not dm:
            continue
        ymd = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
        rec = {"date": ymd}
        for code, name in PROD.items():
            if code in o:
                rec[name] = float(o[code])
        recs.append(rec)
    # 중복 날짜 제거 후 정렬
    uniq = {r["date"]: r for r in recs}
    return [uniq[d] for d in sorted(uniq)]


def fetch_opinet() -> dict:
    """오피넷 전국 평균 판매가 수집 (무키).

    반환 dict:
      "cur":  {"ym", "avg", "n_weeks", "source", "fuel"}  — 당월 주간평균
      "prev": {"ym", "avg", "source", "fuel"}             — 전월 월평균 (완성)
      "raw_daily": list[dict]                              — 기존 일별 GET 결과 (폴백용)
    """
    today = date.today()
    y, m = today.year, today.month
    prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
    fuel = "보통휘발유"
    result: dict = {"cur": None, "prev": None, "raw_daily": []}

    # 1) 당월 주별 CSV
    try:
        wks = fetch_opinet_weekly_csv(y, m)
        if wks:
            vals = [w[fuel] for w in wks if fuel in w]
            if vals:
                result["cur"] = {
                    "ym": f"{y}-{m:02d}",
                    "avg": round(statistics.mean(vals), 2),
                    "n_weeks": len(vals),
                    "weeks": wks,
                    "source": "주별CSV",
                    "fuel": fuel,
                }
    except Exception:
        pass

    # 2) 전월 및 올해 월별 CSV
    try:
        monthly = fetch_opinet_monthly_csv(y, 1, y, m)
        result["monthly_series"] = monthly
        prev_ym = f"{prev_y}-{prev_m:02d}"
        for row in monthly:
            if row["ym"] == prev_ym and fuel in row:
                result["prev"] = {
                    "ym": prev_ym,
                    "avg": row[fuel],
                    "source": "월별CSV",
                    "fuel": fuel,
                }
                break
    except Exception:
        pass

    # 3) 일별 GET (폴백 — 구 방식 호환)
    try:
        result["raw_daily"] = parse_opinet(_fetch_html())
    except Exception:
        pass

    # cur가 없으면 raw_daily로 대체
    if result["cur"] is None and result["raw_daily"]:
        recs = result["raw_daily"]
        cur_ym = recs[-1]["date"][:7]
        pts = [r[fuel] for r in recs if fuel in r and r["date"].startswith(cur_ym)]
        if pts:
            result["cur"] = {
                "ym": cur_ym,
                "avg": round(statistics.mean(pts), 2),
                "n_weeks": None,
                "source": f"일별GET({len(pts)}일)",
                "fuel": fuel,
            }

    if result["cur"] is None and result["prev"] is None:
        raise RuntimeError("오피넷 수집 실패 — 페이지 구조 변경 가능")
    return result


def gasoline_monthly_avg(recs: list[dict], ym: str | None = None,
                         fuel: str = "보통휘발유") -> dict | None:
    """수집된 일별 레코드에서 특정 월(YYYY-MM)의 평균가 산출 (하위 호환용)."""
    if not recs:
        return None
    if ym is None:
        ym = recs[-1]["date"][:7]
    pts = [(r["date"], r[fuel]) for r in recs if fuel in r and r["date"].startswith(ym)]
    if not pts:
        return None
    avg = statistics.mean(p for _, p in pts)
    return {
        "fuel": fuel,
        "ym": ym,
        "avg": round(avg, 2),
        "n_days": len(pts),
        "first_date": pts[0][0],
        "last_date": pts[-1][0],
    }


def gasoline_daily_change(recs: list[dict], fuel: str = "보통휘발유") -> dict | None:
    """가용 일자 기준 최신가·직전가·변동률(%) 반환."""
    pts = [(r["date"], r[fuel]) for r in recs if fuel in r]
    if not pts:
        return None
    latest_d, latest_p = pts[-1]
    if len(pts) >= 2:
        prev_d, prev_p = pts[-2]
        chg = (latest_p / prev_p - 1) * 100 if prev_p else 0.0
        return {"fuel": fuel, "latest_date": latest_d, "latest": latest_p,
                "prev_date": prev_d, "prev": prev_p, "change_pct": chg}
    return {"fuel": fuel, "latest_date": latest_d, "latest": latest_p,
            "prev_date": None, "prev": None, "change_pct": None}


@dataclass
class LifePrice:
    """참가격 생필품 주간정보 1개 상품."""
    name: str
    current_price: float
    diff_2w: float
    change_pct_2w: float
    bucket: str | None = None


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens: list[str] = []

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", unescape(data)).strip()
        if text:
            self.tokens.append(text)


def _html_tokens(html: str) -> list[str]:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.tokens


def _to_number(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if not re.match(r"^-?\d+(?:\.\d+)?$", text):
        return None
    return float(text)


LIFE_PRICE_BUCKET_KEYWORDS = {
    "gagong": [
        "소면", "국수", "만두", "아몬드", "땅콩", "포테토칩", "포카칩", "새우깡",
        "후라보노", "자일리톨", "오렌지", "녹차", "두유", "단무지", "라면",
        "우유", "커피", "식용유", "고춧가루", "김치", "고추장", "된장", "간장",
    ],
    "chuksusan": [
        "계란", "달걀", "유정란", "목초란", "갈치", "고등어", "오징어", "명태",
        "새우", "김 ", "올리브김", "들기름김",
    ],
    "gongeop": [
        "코디", "잘풀리는집", "크리넥스", "휴지", "화장지", "건전지", "에너자이저",
        "듀라셀", "벡셀", "세제", "샴푸", "치약", "칫솔", "주방세제",
    ],
    "nonggsan": [
        "고구마", "깻잎", "당근", "쌀(", "현미(", "배추", "무(", "양파", "대파",
        "감자", "오이", "상추", "시금치", "토마토", "사과", "배(", "귤",
    ],
}


def classify_life_price_bucket(name: str) -> str | None:
    """참가격 상품명 → 엔진 버킷 코드. 할인상품 노이즈를 줄이기 위한 보수적 키워드 매핑."""
    compact = re.sub(r"\s+", "", name)
    if "김밥" in compact and "단무지" in compact:
        return "gagong"
    for bucket, keys in LIFE_PRICE_BUCKET_KEYWORDS.items():
        if any(k.replace(" ", "") in compact for k in keys):
            return bucket
    return None


def parse_life_prices(html: str) -> list[LifePrice]:
    """price.go.kr 참가격 메인 HTML → 생필품 주간정보 상품 가격.

    메인에 노출되는 '금주'와 '2주전 대비'를 사용한다. change_pct_2w는
    diff_2w / (금주 - diff_2w)로 계산한 최근 2주 판매가격 모멘텀이다.
    """
    tokens = _html_tokens(html)
    if "품목별가격정보 더보기" in tokens:
        start = tokens.index("품목별가격정보 더보기") + 1
    elif "생필품 주간정보" in tokens:
        start = tokens.index("생필품 주간정보") + 1
    else:
        start = 0
    end = tokens.index("주요소식") if "주요소식" in tokens else len(tokens)
    ts = tokens[start:end]

    out: list[LifePrice] = []
    i = 0
    while i + 6 < len(ts):
        if ts[i + 1] == "금주" and ts[i + 3] == "2주전 대비" and ts[i + 5] == "전년대비":
            price = _to_number(ts[i + 2])
            diff = _to_number(ts[i + 4])
            if price is not None and diff is not None:
                prev = price - diff
                if prev > 0:
                    name = ts[i]
                    out.append(LifePrice(
                        name=name,
                        current_price=price,
                        diff_2w=diff,
                        change_pct_2w=round(diff / prev * 100, 2),
                        bucket=classify_life_price_bucket(name),
                    ))
            i += 7
            continue
        i += 1
    return out


def fetch_life_prices() -> list[LifePrice]:
    """한국소비자원 참가격 생필품 주간정보 수집(무키)."""
    req = urllib.request.Request(PRICE_GO_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    recs = parse_life_prices(html)
    if not recs:
        raise RuntimeError("참가격 생필품 주간정보 파싱 실패 — 페이지 구조가 변경되었을 수 있음.")
    return recs


def life_price_bucket_mom(recs: list[LifePrice], min_items: int = 2,
                          cap_abs: float = 3.0) -> dict[str, dict]:
    """참가격 상품별 2주 모멘텀 → 엔진 버킷별 MoM 제안.

    유통업체 할인 노이즈가 크므로 평균보다 중앙값을 쓰고, CPI 입력 기본값으로는
    절대값을 cap_abs 이내로 제한한다.
    """
    by_bucket: dict[str, list[LifePrice]] = {}
    for r in recs:
        if r.bucket is None:
            continue
        by_bucket.setdefault(r.bucket, []).append(r)

    out: dict[str, dict] = {}
    for bucket, rows in by_bucket.items():
        if len(rows) < min_items:
            continue
        vals = [r.change_pct_2w for r in rows]
        median = statistics.median(vals)
        out[bucket] = {
            "count": len(rows),
            "median_2w": round(median, 2),
            "mean_2w": round(statistics.mean(vals), 2),
            "suggest_mom": round(max(-cap_abs, min(cap_abs, median)), 2),
            "examples": ", ".join(r.name for r in rows[:3]),
        }
    return out


if __name__ == "__main__":
    rs = fetch_opinet()
    print(f"[오피넷] {len(rs)}일치 수집:")
    for r in rs:
        print(" ", r)
    print("변동:", gasoline_daily_change(rs))
    try:
        life = fetch_life_prices()
        print(f"[참가격] 생필품 {len(life)}개 수집")
        for bucket, info in life_price_bucket_mom(life).items():
            print(f"  {bucket}: {info}")
    except Exception as e:
        print("[참가격] 수집 실패:", e)
