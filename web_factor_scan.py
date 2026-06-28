"""
웹 요인 스캐너 (무키) — 대상월 CPI에 영향을 주는 요인을 웹에서 자동 탐지.
근거: PRD v3.1 §4.5(웹서치 체크리스트). "사람이 웹을 직접 찾아 seed.py에 입력"하던
반자동 흐름을, 구글뉴스 RSS(키 불필요)로 후보를 자동 제안하는 흐름으로 전환한다.

흐름:
  1. 품목군별 검색질의로 구글뉴스 RSS 수집(무키)
  2. 헤드라인 → 품목군(버킷) 키워드 매칭 + 방향(인상/인하) 추정
  3. 후보 이벤트 리스트 반환 → UI가 체크리스트로 표시, 사용자가 확정해 event_coef에 저장

주의: 자동 탐지는 '후보 제안'일 뿐 충격값(shock_pct)·가중치는 사용자가 확인·보정해야 한다
      (§4.5의 본질은 사람이 검토하는 체크리스트). RSS 구조 변경 시 파싱이 깨질 수 있음.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

# ── 품목군별 검색질의 + 키워드 (버킷 코드 기준) ────────────────────────────
#   query : 구글뉴스 검색어(해당 품목군 동향을 잡는 대표 질의)
#   keys  : 헤드라인 재확인용 키워드(질의 누수 방지)
BUCKET_QUERIES: dict[str, dict] = {
    "seokyu":    {"query": "휘발유 경유 기름값 유가",
                  "keys": ["휘발유", "경유", "기름값", "유가", "주유소", "정유", "석유", "리터"]},
    "egw":       {"query": "도시가스 전기요금 가스요금 인상",
                  "keys": ["도시가스", "전기요금", "가스요금", "수도요금", "난방비", "공공요금"]},
    "nonggsan":  {"query": "농산물 채소 과일 가격",
                  "keys": ["농산물", "채소", "과일", "배추", "무", "쌀", "곡물", "사과", "양파", "대파"]},
    "chuksusan": {"query": "축산물 수산물 한우 가격",
                  "keys": ["축산", "수산", "한우", "돼지", "계란", "달걀", "생선", "고등어", "오징어"]},
    "gagong":    {"query": "가공식품 가격 인상",
                  "keys": ["가공식품", "라면", "빵", "과자", "식용유", "우유", "커피", "즉석"]},
    "gonggong":  {"query": "공공요금 통신요금 버스요금 인상",
                  "keys": ["통신요금", "통신비", "버스요금", "지하철", "택시요금", "의료", "보험료", "전철"]},
    "gaein":     {"query": "외식 물가 개인서비스 요금",
                  "keys": ["외식", "학원비", "여행", "숙박", "미용", "보험", "수리비", "음식점"]},
    "jipse":     {"query": "전세 월세 집세 임대료",
                  "keys": ["전세", "월세", "집세", "임대료", "주거비"]},
    "gongeop":   {"query": "공산품 담배 의류 가격 인상",
                  "keys": ["공산품", "담배", "의류", "생활용품", "가전"]},
}

# 방향 키워드(§4.5 — 상방/하방). 앞쪽일수록 우선.
_UP = ["인상", "상승", "급등", "오름", "올라", "올린", "인상안", "치솟", "뛰", "상한가", "최고가"]
_DOWN = ["인하", "하락", "급락", "내림", "내려", "내린", "떨어", "하향", "동결 해제 인하", "최저가"]

# 버킷별 변동성 계수(§4.5/§5.4 도메인) — 중요 품목 워치리스트 우선순위에 사용.
#   영향도 = 품목가중치 × 변동성. 석유류·농산물은 변동 큼, 서비스는 끈적(낮음)이나 가중치 큼.
BUCKET_VOLATILITY = {
    "seokyu": 1.0, "nonggsan": 1.0, "chuksusan": 0.8, "egw": 0.7, "gagong": 0.5,
    "gonggong": 0.55, "gaein": 0.4, "gongeop": 0.3, "jipse": 0.3,
}

# 한글 버킷명(표시용) — seed.py와 동일
BUCKET_NAMES = {
    "seokyu": "석유류", "egw": "전기·가스·수도", "nonggsan": "농산물(곡물 포함)",
    "chuksusan": "축산물·수산물", "gagong": "가공식품", "gonggong": "공공서비스",
    "gaein": "개인서비스", "jipse": "집세", "gongeop": "기타 공업제품",
}

_TAG = re.compile(r"<[^>]+>")


@dataclass
class Candidate:
    bucket: str            # 버킷 코드
    bucket_name: str
    title: str             # 헤드라인
    direction: str         # up/down/?
    suggest_shock: float   # 제안 충격(%) — 헤드라인 %파싱 or 방향 placeholder
    link: str
    pub: str               # 발행일(RSS pubDate)
    source: str            # 언론사
    item: str = ""         # 매칭된 개별 품목명(Tier2). 없으면 ""
    item_weight: float = 0.0   # 그 품목의 CPI 가중치(천분비). item 있을 때만
    weight_confirmed: bool = False  # True=품목가중치 확정, False=버킷가중치(사용자 확인요)


def _fetch_rss(query: str, timeout: int = 12) -> str:
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_items(xml_text: str) -> list[dict]:
    """RSS XML → [{title, link, pub, source}]."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if title:
            out.append({"title": _TAG.sub("", title), "link": link, "pub": pub, "source": source})
    return out


_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def detect_direction(title: str) -> tuple[str, float]:
    """헤드라인 → (방향, 제안충격%).
    헤드라인에 'NN%'가 있으면 그 값을 방향부호와 결합해 충격값으로 제안(자동 충격값),
    없으면 부호만 있는 placeholder(±1.0)."""
    direction = "?"
    for kw in _UP:
        if kw in title:
            direction = "up"
            break
    else:
        for kw in _DOWN:
            if kw in title:
                direction = "down"
                break
    if direction == "?":
        return "?", 0.0
    sign = 1.0 if direction == "up" else -1.0
    m = _PCT_RE.search(title)
    if m:
        val = float(m.group(1))
        if val <= 100:  # 비현실적 대형 %는 무시(placeholder로)
            return direction, round(sign * val, 1)
    return direction, sign * 1.0


def match_item(title: str, items: dict, bucket: str | None = None) -> tuple[str, dict] | None:
    """헤드라인에 등장하는 개별 품목명을 찾아 (품목명, 정보) 반환.
    가드레일:
      · 매칭 품목의 소속 버킷이 검색 버킷(bucket)과 같을 때만 인정
        → '양산신문'의 '신문'(공업제품)이 석유류 검색에 끼는 오매칭 차단.
      · 2글자 이상만, 여러 개면 가장 긴(구체적) 품목명 우선."""
    hits = [(nm, info) for nm, info in items.items()
            if len(nm) >= 2 and nm in title and (bucket is None or info["bucket"] == bucket)]
    if not hits:
        return None
    # 긴(구체적) 품목 우선, 동률이면 제목 앞쪽(주어 위치) 우선
    hits.sort(key=lambda x: (-len(x[0]), title.index(x[0])))
    return hits[0]


def _strip_source(title: str, source: str) -> str:
    """제목 끝의 ' - 언론사' 및 알려진 언론사명을 제거(품목 오매칭 방지)."""
    t = title
    if source and source in t:
        t = t.replace(source, " ")
    # 흔한 언론사 접미사 패턴 ' - XXX' 제거
    return re.sub(r"\s*-\s*[^\-]{1,12}$", "", t)


def _matches_bucket(title: str, keys: list[str]) -> bool:
    return any(k in title for k in keys)


def _pub_ym(pub: str) -> str | None:
    """RSS pubDate(예: 'Wed, 24 Jun 2026 ...') → 'YYYY-MM'. 실패 시 None."""
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", pub)
    if not m:
        return None
    mon = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
           "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
    mm = mon.get(m.group(2))
    return f"{m.group(3)}-{mm}" if mm else None


def scan_factors(buckets: list[str] | None = None, max_per_bucket: int = 4,
                 recent_ym: str | None = None, items: dict | None = None) -> list[Candidate]:
    """
    품목군별 구글뉴스 RSS를 돌려 CPI 영향 요인 후보를 수집.
      buckets     : 검색할 버킷 코드(None이면 전체)
      max_per_bucket : 버킷당 후보 상한
      recent_ym   : 'YYYY-MM' 주면 그 달±직전월 기사만(시의성 필터). None이면 필터 없음.
      items       : db.load_items() 결과. 주면 헤드라인의 개별 품목을 매칭해
                    그 품목의 정확한 가중치·버킷을 채움(Tier2 자동 충격값).
    """
    targets = buckets or list(BUCKET_QUERIES.keys())
    keep_ym = None
    if recent_ym:
        keep_ym = {recent_ym}
        y, mth = map(int, recent_ym.split("-"))
        pm = (y - 1, 12) if mth == 1 else (y, mth - 1)
        keep_ym.add(f"{pm[0]:04d}-{pm[1]:02d}")

    results: list[Candidate] = []
    for code in targets:
        spec = BUCKET_QUERIES.get(code)
        if not spec:
            continue
        try:
            news = _parse_items(_fetch_rss(spec["query"]))
        except Exception:
            continue
        seen: set[str] = set()
        n = 0
        for it in news:
            if n >= max_per_bucket:
                break
            title = it["title"]
            if title in seen or not _matches_bucket(title, spec["keys"]):
                continue
            if keep_ym is not None:
                iy = _pub_ym(it["pub"])
                if iy is not None and iy not in keep_ym:
                    continue
            seen.add(title)
            direction, shock = detect_direction(title)
            cand = Candidate(
                bucket=code, bucket_name=BUCKET_NAMES.get(code, code), title=title,
                direction=direction, suggest_shock=shock, link=it["link"],
                pub=it["pub"], source=it["source"])
            # Tier2: 헤드라인에 개별 품목이 있으면 정확한 가중치·버킷으로 보강
            if items:
                hit = match_item(_strip_source(title, it["source"]), items, bucket=code)
                if hit:
                    nm, info = hit
                    cand.item = nm
                    cand.item_weight = info["weight"]
                    cand.weight_confirmed = True
                    cand.bucket = info["bucket"]
                    cand.bucket_name = BUCKET_NAMES.get(info["bucket"], info["bucket"])
            results.append(cand)
            n += 1
    # 방향 미상은 뒤로, 방향 있는 것 우선
    results.sort(key=lambda c: (c.direction == "?", c.bucket))
    return results


# ── CPI 결정요인 우선순위 프레임워크 (사용자 제공: 통계청 동향+한은 점검회의) ──────
#   importance: 3=⭐⭐⭐ 결정적 / 2=⭐⭐ 주요 / 1=⭐ 보조
#   queries   : 드라이버별 구글뉴스 질의(여러 개 → 합쳐 수집)
#   items     : 이 드라이버에 매핑되는 CPI 품목(헤드라인 매칭·기여도 산출용)
#   macro     : True면 CPI 품목이 아닌 상류 거시지표(환율·국제유가) — 가중치 없이 맥락신호로 추적
DRIVERS = [
    {"rank": 1, "key": "oil", "name": "석유류 / 국제유가", "importance": 3, "macro": False,
     "queries": ["휘발유 경유 기름값 유가", "두바이유 국제유가 WTI"],
     "items": ["휘발유", "경유", "등유", "자동차용LPG"],
     "macro_signal": {"name": "국제유가(두바이유)", "query": "두바이유 국제유가 브렌트유",
                      "note": "석유류 선행지표 — 휘발유·경유로 시차 반영"}},
    {"rank": 2, "key": "policy", "name": "공공요금·통신비(정책)", "importance": 3, "macro": False,
     "queries": ["통신비 통신요금 인하 정책", "전기요금 가스요금 공공요금 인상 동결",
                 "버스요금 지하철요금 인상", "택시요금 대중교통요금", "상수도요금 난방비 공공요금"],
     "items": ["휴대전화료", "인터넷이용료", "전기료", "도시가스", "지역난방비",
               "상수도료", "하수도료", "시내버스료", "시외버스료", "도시철도료",
               "택시료", "열차료"]},
    {"rank": 3, "key": "fresh", "name": "농축수산물(기상)", "importance": 2, "macro": False,
     "queries": ["농산물 채소 과일 가격 폭염 한파", "한우 돼지고기 계란 가격", "수산물 고등어 가격"],
     "items": ["배추", "무", "양파", "파", "마늘", "상추", "시금치", "토마토", "감자", "오이",
               "사과", "배", "귤", "수박", "참외", "딸기", "복숭아", "포도",
               "국산쇠고기", "수입쇠고기", "돼지고기", "닭고기", "달걀",
               "고등어", "갈치", "오징어", "명태", "새우"]},
    {"rank": 4, "key": "service", "name": "개인서비스 / 외식(근원)", "importance": 2, "macro": False,
     "queries": ["외식 물가 개인서비스 요금", "삼겹살 치킨 김밥 외식비"],
     "items": ["삼겹살(외식)", "치킨", "김밥", "커피(외식)", "구내식당식사비",
               "미용료", "보험서비스료", "중학생학원비"]},
    {"rank": 5, "key": "fx", "name": "환율(USD/KRW)", "importance": 2, "macro": True,
     "queries": ["원달러 환율 원화 약세"],
     "items": [],
     "macro_signal": {"name": "원/달러 환율", "query": "원달러 환율 전망",
                      "note": "수입물가 경로 — 석유류·수입 가공식품 상방 압력"}},
    {"rank": 6, "key": "processed", "name": "가공식품", "importance": 2, "macro": False,
     "queries": ["가공식품 가격 인상", "라면 빵 우유 가격"],
     "items": ["라면", "빵", "우유", "커피", "식용유", "즉석식품", "김치", "고춧가루"]},
    # rank7 기저효과: 웹 추적 대상 아님(엔진 §5.2 기저효과 검산으로 처리) → DRIVERS 제외
]
STARS = {3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}

# 품목 ↔ 뉴스 표현 별칭 — KOSIS 공식 품목명은 'OO료'지만 기사는 'OO요금/비'로 씀.
#   이 별칭이 헤드라인에 있으면 해당 KOSIS 품목으로 매칭(공공요금·통신·대중교통 드라이버 핵심 보강).
ITEM_ALIASES: dict[str, list[str]] = {
    "휴대전화료": ["통신비", "통신요금", "휴대폰요금", "휴대폰 요금", "휴대전화요금",
                "이동통신요금", "이동통신 요금", "통신 요금", "5G 요금", "요금제"],
    "인터넷이용료": ["인터넷요금", "인터넷 요금", "초고속인터넷"],
    "유선전화료": ["유선전화요금", "집전화요금"],
    "전기료": ["전기요금", "전기 요금", "한전 요금"],
    "도시가스": ["가스요금", "도시가스요금", "가스 요금", "도시가스 요금"],
    "지역난방비": ["난방비", "지역난방요금", "지역난방 요금"],
    "상수도료": ["수도요금", "상수도요금", "수돗물요금", "수도 요금"],
    "하수도료": ["하수도요금"],
    "시내버스료": ["버스요금", "시내버스요금", "버스 요금", "시내버스 요금", "버스비"],
    "시외버스료": ["시외버스요금", "고속버스요금", "시외버스 요금"],
    "도시철도료": ["지하철요금", "전철요금", "도시철도요금", "지하철 요금", "지하철 운임"],
    "택시료": ["택시요금", "택시 요금", "택시비"],
    "열차료": ["기차요금", "KTX요금", "철도요금", "열차 요금"],
    "외래진료비": ["외래진료비", "병원 진료비", "진료비"],
    "입원진료비": ["입원비", "입원 진료비"],
}


def item_in_headline(item: str, headline: str) -> bool:
    """헤드라인에 KOSIS 품목명 또는 그 뉴스 별칭이 등장하면 True."""
    if len(item) >= 2 and item in headline:
        return True
    return any(a in headline for a in ITEM_ALIASES.get(item, ()))


@dataclass
class DriverSignal:
    rank: int
    driver: str            # 드라이버명
    importance: int        # 3/2/1
    macro: bool            # 거시지표 여부
    cands: list            # 이 드라이버의 Candidate들(품목 매칭, 기여도순)
    key: str = ""          # 드라이버 코드(oil/policy/fresh/service/fx/processed)
    macro_note: str = ""   # 거시지표 맥락 메모(환율·국제유가)
    macro_headline: str = ""   # 거시지표 최신 헤드라인
    macro_risk: int = 0    # 거시지표 인플레 리스크 부호(+1 상방/-1 하방/0 중립)


# 거시지표 인플레 리스크 방향 키워드 (헤드라인 → 상방/하방)
_FX_UP = ["약세", "상승", "급등", "오름", "올라", "뛰", "상향", "고환율", "위협", "불안", "돌파"]
_FX_DOWN = ["강세", "하락", "급락", "내림", "내려", "안정", "하향", "진정"]
_OIL_UP = ["상승", "급등", "오름", "올라", "뛰", "치솟", "강세", "반등", "상향", "최고가", "돌파"]
_OIL_DOWN = ["하락", "급락", "내림", "내려", "하회", "안정", "회귀", "약세", "진정", "최저"]


def _macro_direction(key: str, headline: str) -> int:
    """거시 헤드라인 → 인플레 리스크 부호. 환율 상승/약세·유가 상승 = 상방(+1).
    상·하방 키워드가 함께 있으면 **헤드라인에 먼저 나오는(주제어)** 쪽이 이긴다
    (예: '유가 70달러 하회…재반등 전망' → '하회'가 앞 → 하방)."""
    if not headline:
        return 0
    up, down = (_FX_UP, _FX_DOWN) if key == "fx" else (_OIL_UP, _OIL_DOWN)
    up_pos = min((headline.find(k) for k in up if k in headline), default=-1)
    down_pos = min((headline.find(k) for k in down if k in headline), default=-1)
    if up_pos == -1 and down_pos == -1:
        return 0
    if down_pos == -1:
        return 1
    if up_pos == -1:
        return -1
    return 1 if up_pos < down_pos else -1


def macro_risk_score(sigs: list) -> tuple[float, list]:
    """거시 드라이버(환율·국제유가) 최신 헤드라인 방향 → 인플레 리스크점수[-1,1].
    importance 가중평균(유가 ⭐⭐⭐·환율 ⭐⭐). 반환: (점수, [(드라이버, 부호, 헤드라인)] 설명).
    → forecast 리스크점수에 가산되어 시나리오 상/하방 확률을 이동(§9.1)."""
    num = den = 0.0
    detail = []
    for s in sigs:
        if not s.macro_headline:
            continue
        sign = _macro_direction(s.key, s.macro_headline)
        if sign == 0:
            continue
        num += sign * s.importance
        den += s.importance
        detail.append((s.driver, sign, s.macro_headline))
    if den == 0:
        return 0.0, detail
    return max(-1.0, min(1.0, num / den)), detail


def scan_drivers(items: dict, recent_ym: str | None = None,
                 max_per_driver: int = 6, max_per_item: int = 2) -> list[DriverSignal]:
    """CPI 결정요인 우선순위(DRIVERS)대로 라이브 웹추적.
    드라이버별로 질의 → 헤드라인 수집 → 매핑품목 매칭·방향·%충격 → 기여도(%p)순 후보.
    거시 드라이버(환율·국제유가)는 맥락 헤드라인만 추적(CPI 가중치 없음).
      max_per_item: 한 품목(예: 통신비)이 슬롯을 독차지하지 않도록 품목당 후보 상한.
    반환: 드라이버 importance(⭐) 내림차순 DriverSignal 리스트."""
    keep_ym = _recent_window(recent_ym)
    item_index = {nm: info for nm, info in items.items()}
    out: list[DriverSignal] = []
    for d in DRIVERS:
        cands: list[Candidate] = []
        seen: set[str] = set()
        item_count: dict[str, int] = {}
        allow = set(d["items"])
        for q in d["queries"]:
            if len(cands) >= max_per_driver:
                break
            try:
                news = _parse_items(_fetch_rss(q))
            except Exception:
                continue
            for it in news:
                if len(cands) >= max_per_driver:
                    break
                title = it["title"]
                if title in seen:
                    continue
                if keep_ym is not None:
                    iy = _pub_ym(it["pub"])
                    if iy is not None and iy not in keep_ym:
                        continue
                # 이 드라이버의 매핑품목이 헤드라인에 있는지(공식명 또는 뉴스 별칭)
                clean = _strip_source(title, it["source"])
                hit = next((nm for nm in allow if item_in_headline(nm, clean)), None)
                if not hit or item_count.get(hit, 0) >= max_per_item:
                    continue
                seen.add(title)
                item_count[hit] = item_count.get(hit, 0) + 1
                info = item_index[hit]
                direction, shock = detect_direction(title)
                cands.append(Candidate(
                    bucket=info["bucket"], bucket_name=BUCKET_NAMES.get(info["bucket"], info["bucket"]),
                    title=title, direction=direction, suggest_shock=shock,
                    link=it["link"], pub=it["pub"], source=it["source"],
                    item=hit, item_weight=info["weight"], weight_confirmed=True))
        cands.sort(key=lambda c: -abs(contribution_pp(c.item_weight, c.suggest_shock)))
        sig = DriverSignal(rank=d["rank"], driver=d["name"], importance=d["importance"],
                           macro=d["macro"], cands=cands, key=d["key"])
        # 거시지표 헤드라인(환율·국제유가)
        ms = d.get("macro_signal")
        if ms:
            sig.macro_note = ms["note"]
            try:
                mnews = _parse_items(_fetch_rss(ms["query"]))
                if mnews:
                    sig.macro_headline = f"{mnews[0]['title']} ({mnews[0]['source']})"
                    sig.macro_risk = _macro_direction(sig.key, sig.macro_headline)
            except Exception:
                pass
        out.append(sig)
    out.sort(key=lambda s: (-s.importance, s.rank))
    return out


def _recent_window(recent_ym: str | None) -> set | None:
    if not recent_ym:
        return None
    keep = {recent_ym}
    y, mth = map(int, recent_ym.split("-"))
    pm = (y - 1, 12) if mth == 1 else (y, mth - 1)
    keep.add(f"{pm[0]:04d}-{pm[1]:02d}")
    return keep


def high_impact_watchlist(items: dict, top_n: int = 18,
                          min_weight: float = 0.3) -> list[tuple[str, dict, float]]:
    """458품목 중 CPI 영향 큰 '중요 품목' 워치리스트.
    영향도 = 품목가중치 × 버킷변동성. 반환: [(품목명, info, 영향도)] 영향도 내림차순 top_n.
    → 전부 추적 불가하므로 '영향 클' 품목만 골라 라이브 추적(기획 의도)."""
    scored = []
    for nm, info in items.items():
        if info["weight"] < min_weight:
            continue
        vol = BUCKET_VOLATILITY.get(info["bucket"], 0.4)
        scored.append((nm, info, round(info["weight"] * vol, 2)))
    scored.sort(key=lambda x: -x[2])
    return scored[:top_n]


def contribution_pp(weight: float, shock: float) -> float:
    """헤드라인 기여도(%p) = 가중치/1000 × 충격(%)  (§4.5 라). 영향도 순위용."""
    return weight / 1000 * shock


def scan_watchlist(items: dict, top_n: int = 18, recent_ym: str | None = None,
                   max_age_days: int = 60) -> list[Candidate]:
    """중요 품목 워치리스트를 품목별로 라이브 웹추적.
    각 품목명으로 구글뉴스 질의 → 최신 헤드라인의 방향·%충격 → Candidate(품목 가중치 확정).
    결과는 |헤드라인 기여도(%p)| 내림차순(영향 큰 순)으로 정렬."""
    watch = high_impact_watchlist(items, top_n)
    keep_ym = None
    if recent_ym:
        keep_ym = {recent_ym}
        y, mth = map(int, recent_ym.split("-"))
        pm = (y - 1, 12) if mth == 1 else (y, mth - 1)
        keep_ym.add(f"{pm[0]:04d}-{pm[1]:02d}")

    out: list[Candidate] = []
    for nm, info, _score in watch:
        try:
            news = _parse_items(_fetch_rss(f"{nm} 가격 요금"))
        except Exception:
            continue
        picked = None
        for it in news:
            title = it["title"]
            if nm not in _strip_source(title, it["source"]):
                continue
            if keep_ym is not None:
                iy = _pub_ym(it["pub"])
                if iy is not None and iy not in keep_ym:
                    continue
            picked = it
            break
        if not picked:  # 최근 관련 기사 없음 → 추적은 하되 충격 0(변화 없음)
            out.append(Candidate(
                bucket=info["bucket"], bucket_name=BUCKET_NAMES.get(info["bucket"], info["bucket"]),
                title=f"(최근 {nm} 관련 기사 없음)", direction="?", suggest_shock=0.0,
                link="", pub="", source="", item=nm, item_weight=info["weight"],
                weight_confirmed=True))
            continue
        direction, shock = detect_direction(picked["title"])
        out.append(Candidate(
            bucket=info["bucket"], bucket_name=BUCKET_NAMES.get(info["bucket"], info["bucket"]),
            title=picked["title"], direction=direction, suggest_shock=shock,
            link=picked["link"], pub=picked["pub"], source=picked["source"],
            item=nm, item_weight=info["weight"], weight_confirmed=True))
    # 영향 큰 순(|기여도 %p|) 정렬
    out.sort(key=lambda c: -abs(contribution_pp(c.item_weight, c.suggest_shock)))
    return out


if __name__ == "__main__":
    import sys
    ym = sys.argv[1] if len(sys.argv) > 1 else None
    items = None
    try:
        from engine.db import connect, load_items
        items = load_items(connect())
        print(f"[Tier2] 품목 가중치 {len(items)}개 로드 — 품목 매칭 활성화")
    except Exception as e:
        print(f"[Tier2] 품목 가중치 미로드({e}) — 버킷 단위로만 스캔")
    cands = scan_factors(recent_ym=ym, items=items)
    print(f"[웹 요인 스캔] 후보 {len(cands)}건" + (f" (시의성 {ym}±1월)" if ym else ""))
    for c in cands:
        arrow = {"up": "▲", "down": "▼", "?": "·"}[c.direction]
        tag = (f"  ⟶ 품목 '{c.item}' w={c.item_weight} 충격{c.suggest_shock:+.1f}%"
               if c.item else f"  (버킷가중치·확인요 충격{c.suggest_shock:+.1f}%)")
        print(f"  {arrow} [{c.bucket_name}] {c.title[:42]}{tag}")
