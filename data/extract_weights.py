"""
가중치 보도자료 PDF → 458품목 2022 가중치 CSV 추출 (재현용, 1회성).
근거: 통계청 '2022년 기준 소비자물가지수 가중치 개편 결과' 보도자료 붙임2(지출목적별 품목).

붙임2 품목 리스트는 PDF p12~p17(0-index 11~16), 2단 레이아웃으로
  '품목명 ’20가중치 ’22가중치 ’22-’20' 형태. 458개·합 1000.0으로 검증.

실행:  python data/extract_weights.py "<보도자료.pdf 경로>"
출력:  data/item_weights_2022.csv  (item, w2020, w2022)
"""
from __future__ import annotations
import csv
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent / "item_weights_2022.csv"
# 품목명 + 3개 소수(20년·22년·차이). 한 줄 최대 2개(좌/우단). 분류헤더는 소수3개 미충족→자동 제외.
ROW_RE = re.compile(r"([가-힣A-Za-z][가-힣A-Za-z0-9·\-()]*)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(-?\d+\.\d+)")
ITEM_PAGES = range(11, 17)  # p12~p17


def extract(pdf_path: Path) -> list[tuple[str, float, float]]:
    import pdfplumber
    items = []
    with pdfplumber.open(pdf_path) as pdf:
        for pi in ITEM_PAGES:
            text = pdf.pages[pi].extract_text() or ""
            for line in text.split("\n"):
                for m in ROW_RE.finditer(line):
                    nm, w20, w22, _ = m.groups()
                    items.append((nm, float(w20), float(w22)))
    return items


def main(pdf_path: str):
    items = extract(Path(pdf_path))
    total = round(sum(w for _, _, w in items), 1)
    print(f"추출 품목 {len(items)}개 · 2022 가중치 합 {total}")
    assert len(items) == 458, f"품목 수 458 아님: {len(items)}"
    assert abs(total - 1000.0) < 0.05, f"가중치 합 1000 아님: {total}"
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "w2020", "w2022"])
        w.writerows(items)
    print(f"저장: {OUT}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python data/extract_weights.py <보도자료.pdf 경로>")
        sys.exit(1)
    main(sys.argv[1])
