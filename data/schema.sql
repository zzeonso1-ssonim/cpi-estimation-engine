-- 소비자물가 추정 엔진 — 데이터베이스 스키마
-- 근거: PRD v3.1 §4.3 「실행용 데이터베이스 스키마」 (5개 테이블)
-- SQLite. 모든 가중치는 천분비(합=1000) 기준.

-- ───────────────────────────────────────────────────────────
-- 1) 품목코드 매핑 — 정의 레이어(§4.2)
--    Tier 1은 '버킷' 단위(10개 품목군), Tier 2에서 458 품목코드로 확장.
--    level = 'bucket' | 'item' 으로 두 단계를 한 테이블에 수용.
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS item_map (
    code            TEXT PRIMARY KEY,     -- 버킷키 또는 통계청 품목코드
    name            TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('bucket', 'item')),
    weight          REAL NOT NULL,        -- 원가중치(천분비)
    in_core1        INTEGER NOT NULL CHECK (in_core1 IN (0, 1)),  -- 근원① 식료품및에너지제외 포함
    in_core2        INTEGER NOT NULL CHECK (in_core2 IN (0, 1)),  -- 근원② 농산물및석유류제외 포함
    parent_bucket   TEXT,                 -- level='item'일 때 소속 버킷코드 (Tier 2)
    note            TEXT
);

-- ───────────────────────────────────────────────────────────
-- 2) 월별 지수 — 지수법 계산의 원천(§4.1, §5.1)
--    confirmed: 1=확정(KOSTAT 공표), 0=추정(미확정) — §1.5 출처·확정 플래그
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS monthly_index (
    ym          TEXT PRIMARY KEY,         -- 'YYYY-MM'
    headline    REAL,                     -- 총지수
    core1       REAL,                     -- 식료품및에너지제외
    core2       REAL,                     -- 농산물및석유류제외
    confirmed   INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    source      TEXT
);

-- ───────────────────────────────────────────────────────────
-- 3) 품목군 MoM 가정 — 전망 입력 관리(§4.3, §4.4)
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bucket_mom (
    ym              TEXT NOT NULL,
    bucket_code     TEXT NOT NULL REFERENCES item_map(code),
    base_mom        REAL NOT NULL DEFAULT 0,   -- 기본 MoM(%)
    event_adj       REAL NOT NULL DEFAULT 0,   -- 이벤트 조정(%)
    final_mom       REAL,                      -- 최종 MoM(%) = base + event (NULL이면 base+event 자동)
    confidence      TEXT,                      -- A/B/C (§4.4 신뢰도)
    PRIMARY KEY (ym, bucket_code)
);

-- ───────────────────────────────────────────────────────────
-- 4) 이벤트 계수 — 정성 판단의 수치화(§7, §4.5)
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_coef (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ym              TEXT NOT NULL,             -- CPI 반영월(시차 적용 후) 'YYYY-MM'
    name            TEXT NOT NULL,             -- 이벤트명
    target_bucket   TEXT REFERENCES item_map(code),  -- 대상 품목군(버킷)
    target_weight   REAL NOT NULL DEFAULT 0,   -- 영향 받는 품목 가중치(천분비, 버킷의 일부)
    shock_pct       REAL NOT NULL DEFAULT 0,   -- 당월 충격률(해당 품목 MoM %)
    lag_months      REAL NOT NULL DEFAULT 0,   -- 반영 시차(개월)
    reversal_rate   REAL NOT NULL DEFAULT 0,   -- 되돌림률(0~1)
    importance      TEXT,                      -- High/Medium/Low (§4.5 다)
    direction       TEXT,                      -- up/down
    reason          TEXT NOT NULL,             -- 수동 조정 사유(필수)
    error_contrib   REAL                       -- 발표 후 오차기여도(백테스트에서 채움)
);

-- ───────────────────────────────────────────────────────────
-- 5) 백테스트 — 학습 루프 실행(§10)
-- ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ym              TEXT NOT NULL,
    metric          TEXT NOT NULL,             -- headline/core1/core2
    forecast_yoy    REAL,
    actual_yoy      REAL,
    error_pp        REAL,                      -- 오차(%p) = forecast - actual
    cause           TEXT,                      -- MoM오판/기저/이벤트 (R-2)
    corrected       INTEGER DEFAULT 0
);
