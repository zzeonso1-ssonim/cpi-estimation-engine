"""DB 접근 헬퍼 — cpi.db 로드."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from engine.core_engine import Bucket

DB_PATH = Path(__file__).parent.parent / "data" / "cpi.db"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} 없음 — 먼저 `python data/seed.py`로 DB를 생성하세요."
        )
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def load_buckets(con) -> list[Bucket]:
    rows = con.execute(
        "SELECT code,name,weight,in_core1,in_core2 FROM item_map WHERE level='bucket'"
    ).fetchall()
    return [Bucket(r["code"], r["name"], r["weight"], r["in_core1"], r["in_core2"])
            for r in rows]


def load_index(con, ym: str):
    return con.execute("SELECT * FROM monthly_index WHERE ym=?", (ym,)).fetchone()


def load_items(con) -> dict:
    """품목 단위(Tier2) 가중치·버킷 매핑. name -> {weight, bucket, c1, c2}."""
    rows = con.execute(
        "SELECT name,weight,parent_bucket,in_core1,in_core2 FROM item_map WHERE level='item'"
    ).fetchall()
    return {r["name"]: {"weight": r["weight"], "bucket": r["parent_bucket"],
                        "c1": r["in_core1"], "c2": r["in_core2"]} for r in rows}


def published_core_sum(con) -> dict:
    rows = con.execute("SELECT key,value FROM meta").fetchall()
    return {r["key"]: r["value"] for r in rows}


def load_app_state(con) -> dict:
    try:
        rows = con.execute("SELECT key, value FROM app_state").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def save_app_state(con, state: dict):
    con.execute(
        "CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    for k, v in state.items():
        con.execute(
            "INSERT OR REPLACE INTO app_state(key, value) VALUES (?, ?)", (k, str(v))
        )
    con.commit()


def insert_event(con, ym, name, target_bucket, target_weight, shock_pct,
                 lag_months=0.0, reversal_rate=0.0, importance=None,
                 direction=None, reason="") -> bool:
    """event_coef에 1행 추가(웹 요인 스캔 확정분 등). 같은 (ym,name) 중복은 건너뜀.
    반환: 실제로 삽입했으면 True, 중복이라 건너뛰면 False."""
    dup = con.execute("SELECT 1 FROM event_coef WHERE ym=? AND name=?",
                      (ym, name)).fetchone()
    if dup:
        return False
    con.execute(
        "INSERT INTO event_coef(ym,name,target_bucket,target_weight,shock_pct,"
        "lag_months,reversal_rate,importance,direction,reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ym, name, target_bucket, target_weight, shock_pct, lag_months,
         reversal_rate, importance, direction, reason),
    )
    con.commit()
    return True
