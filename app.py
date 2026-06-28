"""
소비자물가 추정 엔진 — Streamlit UI (얇은 표현층).
근거: PRD v3.1. UI는 입력→전망(§5)→검증(§8)→리포트(§12)만 담당하고,
모든 계산은 engine/ 패키지가 수행한다(§13: UI는 마지막).

실행:  streamlit run app.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
import pandas as pd

from engine.db import connect, load_buckets, load_index, published_core_sum, insert_event, load_items
from engine.event_engine import load_events, headline_contrib, classify_importance
from engine.service_engine import compute_private_service, service_event_components
from engine.backtest import load_entries as load_bt, learning_feedback, backtest_from_forecast, r7_mae
from engine.uncertainty import bucket_sensitivity
from engine.forecast import run_direct, run_bottomup
from web_update import (
    PRICE_GO_URL,
    fetch_life_prices,
    fetch_opinet,
    gasoline_monthly_avg,
    life_price_bucket_mom,
)
from web_factor_scan import scan_factors, scan_drivers, STARS, contribution_pp, macro_risk_score

st.set_page_config(page_title="소비자물가 추정 엔진", layout="wide")
st.title("📊 소비자물가 추정 엔진 v0.1")
st.caption("PRD v3.1 기반 · 채권전략용 재현 가능한 CPI 전망 · 지수법 우선(§5.1) · 재현 게이트(검증6·7) 통과 시에만 발행")

try:
    con = connect()
except FileNotFoundError:
    # 클라우드 최초 실행 등 DB 없을 때 자동 초기화
    import subprocess
    with st.spinner("DB 초기화 중 (최초 실행)…"):
        subprocess.run([sys.executable, "data/seed.py"], check=True)
    con = connect()

buckets = load_buckets(con)
published = published_core_sum(con)
idx_df = pd.read_sql("SELECT * FROM monthly_index ORDER BY ym", con)

# ── 사이드바: KOSIS 동기화 + 입력월·지수 선택 ─────────────────
st.sidebar.header("데이터 (Tier 2)")
if st.sidebar.button("🔄 KOSIS 월별 지수 동기화"):
    try:
        import kosis_fetch
        with st.spinner("KOSIS 월별 지수를 동기화하는 중입니다..."):
            s, e = kosis_fetch._default_range()
            n = kosis_fetch.sync(s, e)
        level = "success" if n else "warning"
        message = (
            f"KOSIS {n}개월 동기화 완료 ({s}~{e})."
            if n
            else f"KOSIS에서 새로 반영할 월별 지수가 없습니다 ({s}~{e})."
        )
        st.session_state["kosis_sync_result"] = (level, message)
        st.rerun()
    except Exception as ex:
        st.session_state["kosis_sync_result"] = ("error", f"KOSIS 동기화 실패: {ex}")
        st.rerun()

sync_result = st.session_state.get("kosis_sync_result")
if sync_result:
    level, message = sync_result
    getattr(st.sidebar, level)(message)
st.sidebar.caption(f"품목 가중치(Tier2): {len(load_items(connect()))}개 적재 · "
                   "보도자료 2022 기준")

st.sidebar.header("입력 (§4.1)")
target_label = st.sidebar.text_input("전망 대상월 라벨", "2026년 6월")
months = idx_df["ym"].tolist()
prev_ym = st.sidebar.selectbox("직전월 지수", months, index=len(months) - 1)
base_ym = st.sidebar.selectbox("전년동월 지수(YoY 분모)", months, index=0)

prev_row = load_index(con, prev_ym)
base_row = load_index(con, base_ym)
prev = {"headline": prev_row["headline"], "core1": prev_row["core1"], "core2": prev_row["core2"]}
base = {"headline": base_row["headline"], "core1": base_row["core1"], "core2": base_row["core2"]}
confirmed_base = bool(base_row["confirmed"])

st.sidebar.markdown(f"**직전월** {prev_ym}: H {prev['headline']} / C① {prev['core1']} / C② {prev['core2']}")
st.sidebar.markdown(f"**전년동월** {base_ym}: H {base['headline']} / C① {base['core1']} / C② {base['core2']} "
                    f"({'확정' if confirmed_base else '미확정'})")

mode = st.sidebar.radio(
    "전망 모드",
    ["bottom-up (버킷 MoM → 재정규화)", "direct (근원 MoM 직접)"],
    key="forecast_mode",
)

# ── 본문 ──────────────────────────────────────────────────
if mode.startswith("direct"):
    st.subheader("MoM 가정 (직접)")
    c1, c2, c3 = st.columns(3)
    h_mom = c1.number_input("헤드라인 MoM (%)", value=0.15, step=0.05, format="%.2f")
    c1_mom = c2.number_input("근원① MoM (%)", value=0.20, step=0.05, format="%.2f")
    c2_mom = c3.number_input("근원② MoM (%)", value=0.25, step=0.05, format="%.2f")
    res = run_direct(target_label, prev, base,
                     mom={"headline": h_mom, "core1": c1_mom, "core2": c2_mom},
                     confirmed_base=confirmed_base)
else:
    st.subheader("버킷별 기본 MoM 가정 (§4.2 / §4.4)")
    st.caption("품목코드 미확보(Tier 1) — 10개 버킷 단위. 곡물/도시가스 분리는 Tier 2에서 정밀화. "
               "이벤트(§7)는 아래에서 자동 가산.")

    # 오피넷 라이브 유가 수집 (§4.4, 무키) → 석유류 MoM 자동 제안
    seokyu_default = 0.10
    oc1, oc2 = st.columns([1, 3])
    if oc1.button("🔄 오피넷 유가 새로고침"):
        try:
            st.session_state["opinet"] = fetch_opinet()
        except Exception as e:
            st.error(f"오피넷 수집 실패: {e}")
    opinet_data = st.session_state.get("opinet")
    if opinet_data:
        cur  = opinet_data.get("cur")
        prev = opinet_data.get("prev")
        if cur:
            src_label = f"{cur['n_weeks']}주 평균" if cur.get("n_weeks") else cur.get("source", "")
            oc2.success(
                f"오피넷 **{cur['ym']} 당월 평균** 보통휘발유 **{cur['avg']}원/L** "
                f"({src_label} · {cur['source']})"
            )
            # 전월 평균 — 자동수집 우선, 없으면 수동 입력
            if prev:
                oc2.info(f"전월({prev['ym']}) 평균: **{prev['avg']}원/L** (오피넷 월별CSV 자동수집)")
                base_oil = prev["avg"]
            else:
                base_oil = oc2.number_input(
                    "전월 오피넷 보통휘발유 평균(원/L)", value=2009.08, step=1.0,
                    help="전월 월별 평균가 — 오피넷 CSV 자동수집 실패 시 수동 입력")
            if base_oil:
                seokyu_default = round((cur["avg"] / base_oil - 1) * 100, 2)
                oc2.caption(
                    f"→ 석유류 MoM 자동 제안 **{seokyu_default:+.2f}%** "
                    f"(당월 {cur['avg']} / 전월 {base_oil})"
                )

    # 참가격 생필품 주간정보 → 생필품 관련 버킷 MoM 보조 제안
    life_signals = {}
    pc1, pc2 = st.columns([1, 3])
    if pc1.button("🔄 참가격 생필품 새로고침"):
        try:
            st.session_state["life_prices"] = fetch_life_prices()
        except Exception as e:
            st.error(f"참가격 수집 실패: {e}")
    life_recs = st.session_state.get("life_prices")
    if life_recs:
        life_signals = life_price_bucket_mom(life_recs)
        if life_signals:
            bucket_labels = {b.code: b.name for b in buckets}
            ldf = pd.DataFrame([
                {
                    "품목군": bucket_labels.get(code, code),
                    "샘플수": info["count"],
                    "2주 중앙값(%)": info["median_2w"],
                    "MoM 제안(%)": info["suggest_mom"],
                    "예시": info["examples"],
                }
                for code, info in life_signals.items()
            ])
            pc2.dataframe(ldf, hide_index=True, use_container_width=True)
            pc2.caption(
                f"참가격({PRICE_GO_URL}) 생필품 주간정보의 '금주 vs 2주전 대비' 신호입니다. "
                "할인 노이즈를 줄이기 위해 품목군별 중앙값을 쓰고 입력 제안은 ±3%로 제한합니다."
            )
        else:
            pc2.warning("참가격 상품은 수집됐지만 엔진 품목군으로 매핑된 표본이 부족합니다.")

    bmom = {}
    cols = st.columns(2)
    for i, b in enumerate(buckets):
        flags = ("①" if b.in_core1 else "") + ("②" if b.in_core2 else "")
        if b.code == "seokyu":
            # 오피넷 새 값이 오면 위젯이 재초기화되도록 key에 가격 반영
            skey = f"bm_seokyu_{seokyu_default}"
            bmom[b.code] = cols[i % 2].number_input(
                f"{b.name}  (w={b.weight}, 근원{flags or '제외'}) ⛽오피넷연동", value=seokyu_default,
                step=0.05, format="%.2f", key=skey)
        elif b.code in life_signals:
            life_default = life_signals[b.code]["suggest_mom"]
            lkey = f"bm_{b.code}_life_{life_default}"
            bmom[b.code] = cols[i % 2].number_input(
                f"{b.name}  (w={b.weight}, 근원{flags or '제외'}) 🧺참가격연동",
                value=life_default, step=0.05, format="%.2f", key=lkey)
        else:
            bmom[b.code] = cols[i % 2].number_input(
                f"{b.name}  (w={b.weight}, 근원{flags or '제외'})", value=0.10, step=0.05,
                format="%.2f", key=f"bm_{b.code}")

    use_events = st.checkbox("이벤트 계수 반영 (§7)", value=True)
    ev_target = st.text_input("이벤트 반영월 (YYYY-MM)", "2026-06",
                              help="이 월에 해당하는 event_coef 행 + 전월 이벤트의 되돌림이 적용됨")

    # ── 🎯 결정요인 라이브 트래킹 (⭐ 우선순위, §4.5) ──────────────────────
    bw_map = {b.code: b.weight for b in buckets}
    items_map = load_items(con)   # Tier2: 458품목 가중치·버킷 (있으면 품목단위 자동충격)
    with st.expander("🎯 결정요인 라이브 트래킹 (⭐ 우선순위) — 그 달 CPI 흔들 핵심요인만", expanded=True):
        st.caption("458품목 전부가 아니라 **CPI 결정요인**(석유류·공공요금/통신·농축수산(기상)·외식·환율·가공식품)을 "
                   "⭐중요도 순으로 라이브 추적합니다. 환율·국제유가는 CPI 품목이 아닌 **상류 거시지표**로 별도 표시. "
                   "근거: 통계청 동향+한은 점검회의 결정요인.")
        dr1, dr2 = st.columns([1, 2])
        only_recent_d = dr2.checkbox(f"시의성 필터({ev_target}±1월)", value=True, key="drv_recent")
        if dr1.button("🎯 결정요인 추적 실행"):
            with st.spinner("결정요인별 웹 추적 중(석유·공공·농축수산·외식·환율·가공)…"):
                try:
                    st.session_state["driver_sigs"] = scan_drivers(
                        items_map, recent_ym=ev_target if only_recent_d else None)
                except Exception as e:
                    st.error(f"결정요인 추적 실패: {e}")

        sigs = st.session_state.get("driver_sigs")
        driver_save_result = st.session_state.pop("driver_save_result", None)
        if driver_save_result:
            st.success(driver_save_result)
        if sigs:
            arrow = {"up": "▲상방", "down": "▼하방", "?": "·미상"}
            drv_rows = []
            for s in sigs:
                head = f"{STARS[s.importance]}  **{s.driver}**"
                st.markdown(head + ("  · 🌐상류 거시지표" if s.macro else ""))
                if s.macro_headline:
                    st.caption(f"📈 {s.macro_headline}　—　{s.macro_note}")
                if not s.cands and not s.macro:
                    st.caption("　(관련 최신 기사 없음)")
                for c in s.cands:
                    contrib = contribution_pp(c.item_weight, c.suggest_shock)
                    st.caption(f"　{arrow[c.direction]} **{c.item}**(w={c.item_weight}) "
                               f"충격 {c.suggest_shock:+.0f}% → 기여 {contrib:+.3f}%p　·　{c.title[:48]}")
                    drv_rows.append({
                        "저장": False, "드라이버": f"{STARS[s.importance]} {s.driver}",
                        "버킷": c.bucket, "품목": c.item, "이벤트(헤드라인)": c.title,
                        "방향": arrow[c.direction], "가중치": c.item_weight,
                        "shock_pct(%)": c.suggest_shock, "기여도(%p)": round(contrib, 3),
                        "반영월": ev_target, "되돌림": 0.0, "출처": c.source, "링크": c.link,
                    })
            if drv_rows:
                st.markdown("**→ event_coef 반영(체크 후 shock 보정)**")
                drv_edit = st.data_editor(
                    pd.DataFrame(drv_rows), hide_index=True, use_container_width=True,
                    disabled=["드라이버", "버킷", "품목", "이벤트(헤드라인)", "방향", "기여도(%p)", "출처", "링크"],
                    column_config={"링크": st.column_config.LinkColumn("링크", width="small")},
                    key="driver_editor")
                if st.button("✅ 선택 결정요인을 event_coef에 저장"):
                    saved = skipped = 0
                    for _, row in drv_edit.iterrows():
                        if not row["저장"]:
                            continue
                        ok = insert_event(
                            con, ym=row["반영월"], name=row["이벤트(헤드라인)"],
                            target_bucket=row["버킷"], target_weight=float(row["가중치"]),
                            shock_pct=float(row["shock_pct(%)"]), reversal_rate=float(row["되돌림"]),
                            importance=None,
                            direction="up" if "상방" in row["방향"] else ("down" if "하방" in row["방향"] else None),
                            reason=f"결정요인 라이브 추적({row['드라이버']}, {row['출처']}): {row['링크']}")
                        saved += ok
                        skipped += (not ok)
                    if saved or skipped:
                        st.session_state["driver_save_result"] = (
                            f"{saved}건 저장" + (f" · {skipped}건 중복 건너뜀" if skipped else "")
                        )
                        st.session_state.pop("driver_sigs", None)
                        st.rerun()
                    else:
                        st.warning("저장할 결정요인을 먼저 체크하세요.")

    # ── 🔍 보조: 품목군 전체 훑기 (광범위 스캔, §4.5) ──────────────────────
    with st.expander("🔍 보조: 품목군 전체 훑기 (광범위 스캔, §4.5)", expanded=False):
        st.caption(f"대상월 CPI 영향 요인을 웹에서 자동 수집해 후보로 제안합니다. "
                   f"Tier2 품목 가중치 {len(items_map)}개 로드 — 헤드라인에 품목이 잡히면 그 품목의 "
                   f"**정확한 가중치+충격값(%)을 자동 채움**, 안 잡히면 버킷가중치(확인 요). "
                   f"최종 저장 전 **사용자 확인**이 원칙입니다(§4.5).")
        wc1, wc2 = st.columns([1, 2])
        only_recent = wc2.checkbox(f"시의성 필터({ev_target}±1월 기사만)", value=True)
        if wc1.button("🔍 요인 웹 스캔 실행"):
            with st.spinner("구글뉴스에서 품목군별 요인 수집 중…"):
                try:
                    cands = scan_factors(recent_ym=ev_target if only_recent else None,
                                         items=items_map or None)
                    st.session_state["factor_cands"] = cands
                except Exception as e:
                    st.error(f"웹 스캔 실패: {e}")

        cands = st.session_state.get("factor_cands")
        factor_save_result = st.session_state.pop("factor_save_result", None)
        if factor_save_result:
            st.success(factor_save_result)
        if cands:
            arrow = {"up": "▲상방", "down": "▼하방", "?": "·미상"}
            rows = []
            for c in cands:
                tw = c.item_weight if c.weight_confirmed else bw_map.get(c.bucket, 0.0)
                imp = classify_importance(headline_contrib(tw, c.suggest_shock))
                rows.append({
                    "저장": False, "버킷": c.bucket,
                    "품목": c.item or f"({c.bucket_name} 전체)",
                    "가중치확정": "✅품목" if c.weight_confirmed else "⚠️버킷(확인)",
                    "이벤트(헤드라인)": c.title, "방향": arrow[c.direction],
                    "가중치": tw, "shock_pct(%)": c.suggest_shock,
                    "반영월": ev_target, "되돌림": 0.0, "중요도(추정)": imp,
                    "출처": c.source, "링크": c.link,
                })
            edited = st.data_editor(
                pd.DataFrame(rows), hide_index=True, use_container_width=True,
                disabled=["버킷", "품목", "가중치확정", "이벤트(헤드라인)", "방향",
                          "중요도(추정)", "출처", "링크"],
                column_config={"링크": st.column_config.LinkColumn("링크", width="small")},
                key="factor_editor")
            st.caption("**저장** 체크 후 가중치·shock_pct 보정 → 버튼으로 event_coef 반영. "
                       "⚠️품목 미매칭 행은 버킷 전체 가중치라 **반드시 해당 품목 가중치로 줄여** 보정하세요. "
                       "✅품목 행은 가중치·충격값이 자동(헤드라인 %)이나 최종 확인은 필요합니다.")
            if st.button("✅ 선택 항목을 event_coef에 저장"):
                saved, skipped = 0, 0
                for _, row in edited.iterrows():
                    if not row["저장"]:
                        continue
                    ok = insert_event(
                        con, ym=row["반영월"], name=row["이벤트(헤드라인)"],
                        target_bucket=row["버킷"], target_weight=float(row["가중치"]),
                        shock_pct=float(row["shock_pct(%)"]),
                        reversal_rate=float(row["되돌림"]),
                        importance=row["중요도(추정)"],
                        direction="up" if "상방" in row["방향"] else ("down" if "하방" in row["방향"] else None),
                        reason=f"웹 스캔 자동 탐지({row['출처']}). 사용자 확인 필요: {row['링크']}")
                    saved += ok
                    skipped += (not ok)
                if saved or skipped:
                    st.session_state["factor_save_result"] = (
                        f"{saved}건 저장 완료"
                        + (f" · {skipped}건은 중복(같은 월·이벤트명)으로 건너뜀" if skipped else "")
                    )
                    st.session_state.pop("factor_cands", None)
                    st.rerun()
                else:
                    st.warning("저장할 항목을 먼저 체크하세요.")

    events = load_events(con) if use_events else None

    # 개인서비스 이중계상 방지 (§5.4)
    service_result = None
    use_service = st.checkbox("개인서비스 §5.4 정밀 계산 (이중계상 방지)", value=False)
    if use_service:
        gaein_w = next(b.weight for b in buckets if b.code == "gaein")
        ev_for_svc = events or []
        e_adj, r_adj = service_event_components(ev_for_svc, ev_target, gaein_w) if use_events else (0.0, 0.0)
        sc1, sc2, sc3 = st.columns(3)
        seasonal = sc1.number_input("SeasonalPrior(순수 계절성 %)", value=0.30, step=0.05, format="%.2f")
        sticky = sc2.number_input("StickyService(하방경직 %)", value=0.05, step=0.05, format="%.2f")
        trend = sc3.number_input("기조추세 MoM(%)", value=0.20, step=0.05, format="%.2f")
        seasonal_corrected = st.checkbox("SeasonalPrior 이벤트 보정됨(규칙1)", value=True)
        service_result = compute_private_service(seasonal, e_adj, r_adj, sticky, trend,
                                                 seasonal_event_corrected=seasonal_corrected)
        c = service_result.components
        st.caption(f"개인서비스 MoM = 계절 {c.seasonal_prior:+.2f} + 당월이벤트 {c.event_adj:+.3f} "
                   f"+ 되돌림 {c.reversal_adj:+.3f} + 하방경직 {c.sticky_adj:+.2f} "
                   f"= **{service_result.private_service_mom:+.3f}%** (gaein 기본값 대체)")
        if service_result.sticky_offset:
            st.caption(f"· 되돌림·하방경직 상계량(규칙3): {service_result.sticky_offset:.3f}%p")
        for f in service_result.double_count_flags:
            st.warning(f)
        if service_result.overadjust_warning:
            st.warning(service_result.overadjust_warning)

    # 재정규화 Tier 선택 (§5.3 / §1.3): Tier2=458품목 공식 core정의(붙임1)로 정밀 재정규화
    use_tier2 = st.checkbox(
        f"Tier 2 정밀 재정규화 (458품목 공식 근원정의) — 품목 {len(items_map)}개", value=bool(items_map),
        help="근원②=농산물(곡물제외)·도시가스·석유류 제외, 근원①=식료품+에너지(전기료·도시가스·지역난방비) 제외. "
             "곡물/도시가스/상수도 분리는 버킷 단위로 불가 → 품목 단위만 정확.")

    # 시나리오 확률(§9.1) 입력: 헤드라인 백테스트 MAE + 평균오차(R-4 편향)
    bt_h = load_bt(con, "headline")
    mae_h = r7_mae(bt_h, "headline") if bt_h else None
    mean_err_h = (sum(e.error_pp for e in bt_h) / len(bt_h)) if bt_h else 0.0
    # 거시지표(환율·국제유가) 리스크 → 시나리오 상/하방 이동(§9.1). 결정요인 추적 결과 사용.
    sigs_for_risk = st.session_state.get("driver_sigs") or []
    macro_r, macro_detail = macro_risk_score(sigs_for_risk)
    res = run_bottomup(target_label, prev, base, buckets, bmom,
                       published={"core1": published.get("core1"), "core2": published.get("core2")},
                       confirmed_base=confirmed_base,
                       events=events, target_ym=ev_target if use_events else None,
                       service_result=service_result,
                       mae_pp=mae_h, mean_error_pp=mean_err_h,
                       items=items_map if (use_tier2 and items_map) else None,
                       macro_risk=macro_r)

    if res.event_rows:
        st.markdown("**이벤트 계수 분해 (§7 / §4.5)**")
        edf = pd.DataFrame([
            {"중요도": {"High": "🔴High", "Medium": "🟡Medium", "Low": "🟢Low"}[er.importance],
             "이벤트": er.name, "버킷": er.bucket, "종류": er.kind,
             "유효충격(%)": round(er.eff_shock, 2),
             "헤드라인 기여도(%p)": round(er.contrib_pp, 3), "사유": er.reason}
            for er in res.event_rows
        ])
        st.dataframe(edf, hide_index=True, use_container_width=True)
        for w in res.event_warnings:
            st.warning(w)

# ── 결과 ──────────────────────────────────────────────────
st.divider()
gate_color = "🟢" if res.gate_ok else "🔴"
st.subheader(f"전망 결과  {gate_color} 재현 게이트 {'통과' if res.gate_ok else '미통과 — 발행 금지'}  · 신뢰도 {res.grade}")

r = pd.DataFrame([
    {"지표": m.name, "가정 MoM(%)": round(m.mom_pct, 3),
     "예상 지수": round(m.proj_index, 2), "YoY(%)": round(m.yoy, 2)}
    for m in (res.headline, res.core1, res.core2)
])
st.dataframe(r, hide_index=True, use_container_width=True)

# ── 이번 달 Key Factor (품목군 기여도, §4.5 라) ────────────
if res.bucket_contrib:
    st.markdown("**🔑 이번 달 Key Factor — 품목군별 헤드라인 기여도(%p)**")
    kf = pd.DataFrame([
        {"순위": i + 1, "품목군": name, "가중치": w, "최종 MoM(%)": mom,
         "헤드라인 기여도(%p)": contrib,
         "방향": "▲ 상방" if contrib > 0 else ("▼ 하방" if contrib < 0 else "—")}
        for i, (name, w, mom, contrib) in enumerate(res.bucket_contrib)
    ])
    st.dataframe(kf, hide_index=True, use_container_width=True)
    top = res.bucket_contrib[0]
    pos = [c for c in res.bucket_contrib if c[3] > 0][:2]
    neg = [c for c in res.bucket_contrib if c[3] < 0][:2]
    msg = f"최대 동인: **{top[0]}** ({top[3]:+.3f}%p). "
    if pos:
        msg += "상방 " + ", ".join(f"{n}({c:+.3f})" for n, _w, _m, c in pos) + ". "
    if neg:
        msg += "하방 " + ", ".join(f"{n}({c:+.3f})" for n, _w, _m, c in neg) + "."
    st.caption(msg)

# ── 불확실성·시나리오 확률 (§9 / §9.1) ─────────────────────
if res.scenarios:
    s = res.scenarios
    sc = st.columns(4)
    sc[0].metric("Base", f"{s['base']}%")
    sc[1].metric("Upside ↑", f"{s['upside']}%")
    sc[2].metric("Downside ↓", f"{s['downside']}%")
    if res.pred_interval:
        sc[3].metric("헤드라인 예측구간", f"{res.pred_interval[0]:.2f}~{res.pred_interval[1]:.2f}%")
    st.caption("시나리오 확률(§9.1) = 백테스트 오차분포(정규근사) + 품목군 리스크 점수 혼합 · "
               "과소추정 편향이 있으면 상방으로 스큐 · 합 100%")
    # 거시지표(환율·국제유가) 리스크 연동 표시 (bottom-up 모드에서만 정의됨)
    _md = locals().get("macro_detail")
    if _md:
        dirs = {1: "▲상방", -1: "▼하방"}
        parts = " · ".join(f"{drv}: {dirs.get(sg, '·')}" for drv, sg, _hl in _md)
        st.caption(f"🌐 거시지표 리스크 연동(§9.1): **{macro_r:+.2f}** ({parts}) "
                   f"→ 시나리오 {'상방' if macro_r > 0 else ('하방' if macro_r < 0 else '중립')} 이동.")
    elif locals().get("sigs_for_risk") == []:
        st.caption("🌐 거시지표 리스크: 「🎯 결정요인 추적 실행」하면 환율·국제유가 방향이 시나리오에 자동 반영됩니다.")
    try:
        sens = bucket_sensitivity(buckets, {b.code: b.weight for b in buckets})
        st.caption("품목군 오차 민감도(§9) 상위: " + " · ".join(f"{n}" for n, _ in sens))
    except Exception:
        pass

cL, cR = st.columns(2)
with cL:
    st.markdown("**정합성 검증 (§8)**")
    cdf = pd.DataFrame([
        {"검증": c.no, "항목": c.name,
         "값": round(c.value, 3) if c.value is not None else "—",
         "게이트": ("—" if c.passed is None else ("✅" if c.passed else "❌")),
         "비고": c.note}
        for c in res.checks
    ])
    st.dataframe(cdf, hide_index=True, use_container_width=True)
    if res.renorm:
        tier = next(iter(res.renorm.values())).tier
        st.caption(f"재정규화 진단(§1.3) — **Tier {tier}** "
                   f"({'458품목 공식 근원정의' if tier == 2 else '9버킷 all-or-nothing'}):")
        for core, rn in res.renorm.items():
            if rn.residual_vs_published is not None:
                label = "품목포함합" if rn.tier == 2 else "버킷단순합"
                st.caption(f"· {core}: {label} {rn.included_raw_sum:.1f} vs 공표 "
                           f"{rn.published_sum:.1f} → 잔차 {rn.residual_vs_published:+.1f}"
                           + ("  (곡물·도시가스·상수도 분리 반영)" if rn.tier == 2 and core == "core2" else ""))

with cR:
    st.markdown("**리포트 출력 (§12)**")
    st.code(res.report, language="markdown")

# ── 백테스트 학습 루프 (§10) ───────────────────────────────
st.divider()
with st.expander("📉 백테스트 학습 루프 (§10) — R-1~R-7", expanded=False):
    entries = load_bt(con)
    if entries:
        st.markdown("**R-1 오차 분해 (저장된 발표 후 실측)**")
        bdf = pd.DataFrame([
            {"월": e.ym, "지표": {"headline": "헤드라인", "core1": "근원①", "core2": "근원②"}[e.metric],
             "전망 YoY": e.forecast_yoy, "실제 YoY": e.actual_yoy,
             "오차(%p)": e.error_pp, "원인(R-2)": e.cause}
            for e in entries
        ])
        st.dataframe(bdf, hide_index=True, use_container_width=True)
        st.markdown("**R-4/R-7 학습 피드백 (다음 추정 되먹임)**")
        for line in learning_feedback(entries):
            st.markdown(f"- {line}")
    else:
        st.info("저장된 백테스트 없음.")

    # 라이브 백테스트: 현재 전망 vs 실제(DB에 실제 지수가 있으면)
    st.markdown("---")
    st.markdown("**라이브 백테스트** — 현재 전망 vs 실제 발표 지수")
    actual_ym = st.selectbox("실제 발표월 선택", months, index=len(months) - 1, key="bt_actual")
    arow = load_index(con, actual_ym)
    if arow and st.button("이 월로 라이브 백테스트 실행"):
        actual = {"headline": arow["headline"], "core1": arow["core1"], "core2": arow["core2"]}
        prev_idx = {"headline": prev["headline"], "core1": prev["core1"], "core2": prev["core2"]}
        bt = backtest_from_forecast(res, actual, prev_idx)
        st.session_state["live_backtest_result"] = {
            "rows": [
                {"지표": e.metric, "전망 YoY": round(e.forecast_yoy, 2),
                 "실제 YoY": round(bt["actual_yoy"][e.metric], 2), "오차(%p)": round(e.error_pp, 3),
                 "원인(R-2)": bt["attributions"][e.metric].dominant}
                for e in bt["entries"]
            ],
            "ratios": {m: bt["core_mom_ratio"].get(m) for m in ("core1", "core2")},
        }

    live_bt = st.session_state.get("live_backtest_result")
    if live_bt:
        ldf = pd.DataFrame(live_bt["rows"])
        st.dataframe(ldf, hide_index=True, use_container_width=True)
        for m in ("core1", "core2"):
            ratio = live_bt["ratios"].get(m)
            if ratio:
                flag = " 🔴 서비스·근원재 과소추정" if ratio >= 3 else ""
                st.caption(f"R-3 {m}: 실제/전망 근원 MoM {ratio:.1f}배{flag}")

con.close()
