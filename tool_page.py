"""
팀 관리툴 화면 (app.py 의 '관리툴' 모드에서 불러 씁니다)

읽는 데이터: 구글 스프레드시트 '특수팀_실적관리' 의 3개 탭
  - 점포마스터 : 점포명 | 담당자 | 운영형태 | 오픈일 | 지역 | 주소 | 입지
  - 월실적     : 연월 | 점포명 | 총매출액(원) | 영업일수 | 매출이익(원) | 영업이익(천원)
  - 예상목표   : 점포명 | 구분(예상일매출 / 예상월이익) | M | M+1 | ... | M+11   (단위: 천원)
"""
import re
import streamlit as st
import pandas as pd

PERF_SHEET_NAME = "특수팀_실적관리"
TAB_STORE = "점포마스터"
TAB_MONTH = "월실적"
TAB_TARGET = "예상목표"

# 구분 칸에 적는 이름 (이 글자가 포함되어 있으면 인식)
KIND_DAILY_KEYWORD = "일매출"
KIND_PROFIT_KEYWORD = "이익"


# ───────────────────────── 값 변환 도우미 ─────────────────────────
def _num(v):
    """'6,301' / '-552' / '' -> 숫자 또는 None"""
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ym(v):
    """'202611', '2026-11', '2026.11', '2026-11-18', 202611.0 -> '202611' (인식 못 하면 '')"""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    m = re.match(r"^(\d{4})\D*(\d{1,2})", s)
    if not m:
        return ""
    mon = int(m.group(2))
    if mon < 1 or mon > 12:
        return ""
    return f"{int(m.group(1)):04d}{mon:02d}"


def _month_diff(a, b):
    """a(YYYYMM) 에서 b(YYYYMM) 까지 몇 달 뒤인지"""
    return (int(b[:4]) - int(a[:4])) * 12 + (int(b[4:]) - int(a[4:]))


def ym_label(ym):
    return f"{ym[:4]}년 {int(ym[4:])}월"


def _pick(rec, prefix):
    """열 이름이 prefix 로 시작하는 첫 값을 반환 (예: '영업이익' -> '영업이익(천원)')"""
    for k, v in rec.items():
        if str(k).strip().startswith(prefix):
            return v
    return ""


def _records(values):
    """get_all_values() 결과 -> [dict, ...] (완전히 빈 줄은 제외)"""
    if not values or len(values) < 2:
        return []
    header = [str(h).strip() for h in values[0]]
    rows = []
    for raw in values[1:]:
        raw = list(raw) + [""] * (len(header) - len(raw))
        if not any(str(c).strip() for c in raw):
            continue
        rows.append(dict(zip(header, [str(c).strip() for c in raw])))
    return rows


def fnum(v, dec=0, suffix=""):
    if v is None:
        return "-"
    try:
        return f"{v:,.{dec}f}{suffix}"
    except (ValueError, TypeError):
        return "-"


# ───────────────────────── 데이터 읽기 ─────────────────────────
@st.cache_data(ttl=300, show_spinner="실적 데이터를 불러오는 중...")
def load_perf(_get_gc):
    """3개 탭을 읽어 정리한 dict 를 반환. 문제가 있으면 {'error': '...'}"""
    try:
        gc = _get_gc()
        doc = gc.open(PERF_SHEET_NAME)
    except Exception as e:
        return {"error": f"'{PERF_SHEET_NAME}' 시트를 열 수 없습니다. 서비스 계정에 공유했는지 확인해 주세요. ({e})"}

    def read_tab(title):
        try:
            return doc.worksheet(title).get_all_values()
        except Exception:
            return None

    store_v = read_tab(TAB_STORE)
    month_v = read_tab(TAB_MONTH)
    target_v = read_tab(TAB_TARGET)
    missing = [t for t, v in ((TAB_STORE, store_v), (TAB_MONTH, month_v), (TAB_TARGET, target_v)) if v is None]
    if missing:
        return {"error": "시트에 다음 탭이 없습니다: " + ", ".join(missing)}

    # 점포마스터
    masters = []
    master_by_name = {}
    for rec in _records(store_v):
        name = rec.get("점포명", "").strip()
        if not name:
            continue
        m = {
            "name": name,
            "manager": rec.get("담당자", "").strip(),
            "type": rec.get("운영형태", "").strip(),
            "open_date": rec.get("오픈일", "").strip(),
            "open_ym": _ym(rec.get("오픈일", "")),
            "region": rec.get("지역", "").strip(),
            "address": rec.get("주소", "").strip(),
            "place": rec.get("입지", "").strip(),
        }
        masters.append(m)
        master_by_name[name] = m

    # 월실적 (같은 연월·점포가 여러 번 있으면 아래쪽 행이 우선)
    monthly = {}
    for rec in _records(month_v):
        ym = _ym(rec.get("연월", ""))
        name = rec.get("점포명", "").strip()
        sales = _num(_pick(rec, "총매출액"))
        if not ym or not name or sales is None:
            continue
        monthly.setdefault(name, {})[ym] = {
            "sales": sales,
            "days": _num(_pick(rec, "영업일수")) or 0,
            "pl": _num(_pick(rec, "매출이익")),
            "op": _num(_pick(rec, "영업이익")),
        }

    # 예상목표 (M, M+1, ... 열)
    targets = {}
    if target_v and len(target_v) >= 2:
        header = [str(h).strip() for h in target_v[0]]
        try:
            i_name = header.index("점포명")
            i_kind = header.index("구분")
        except ValueError:
            return {"error": "'예상목표' 탭 1행에 '점포명', '구분' 열이 있어야 합니다."}
        k_cols = {}
        for i, h in enumerate(header):
            mm = re.match(r"^M\s*(?:\+\s*(\d+))?$", h, re.IGNORECASE)
            if mm:
                k_cols[i] = int(mm.group(1)) if mm.group(1) else 0
        for raw in target_v[1:]:
            raw = list(raw) + [""] * (len(header) - len(raw))
            name = str(raw[i_name]).strip()
            kind = str(raw[i_kind]).replace(" ", "")
            if not name or not kind:
                continue
            if KIND_DAILY_KEYWORD in kind:
                slot = "daily"
            elif KIND_PROFIT_KEYWORD in kind:
                slot = "profit"
            else:
                continue
            d = targets.setdefault(name, {"daily": {}, "profit": {}})
            for i, k in k_cols.items():
                v = _num(raw[i])
                if v is not None:
                    d[slot][k] = v

    months = sorted({ym for per in monthly.values() for ym in per})
    return {
        "error": None,
        "masters": masters,
        "master_by_name": master_by_name,
        "monthly": monthly,
        "targets": targets,
        "months": months,
    }


# ───────────────────────── 지표 계산 ─────────────────────────
def calc_store(D, name, yms):
    """한 점포의 선택 기간 지표"""
    m = D["master_by_name"].get(name, {})
    open_ym = m.get("open_ym", "")
    per = D["monthly"].get(name, {})
    tgt = D["targets"].get(name, {"daily": {}, "profit": {}})
    recs = [(ym, per[ym]) for ym in yms if ym in per]

    sales = sum(r["sales"] for _, r in recs)
    days = sum(r["days"] for _, r in recs)
    pls = [r["pl"] for _, r in recs if r["pl"] is not None]
    ops = [r["op"] for _, r in recs if r["op"] is not None]
    pl = sum(pls) if pls else None
    op = sum(ops) if ops else None

    tlist = []
    if open_ym:
        for ym, _ in recs:
            k = _month_diff(open_ym, ym)
            if k in tgt["daily"]:
                tlist.append(tgt["daily"][k])

    daily = (sales / days / 1000.0) if days > 0 else None           # 천원
    target = (sum(tlist) / len(tlist)) if tlist else None             # 천원 (월별 예상의 평균)
    rate = (daily / target * 100.0) if (daily is not None and target) else None
    margin = (pl / sales * 100.0) if (pl is not None and sales > 0) else None
    return {
        "name": name, "has_data": bool(recs), "sales": sales, "days": days,
        "pl": pl, "op": op, "daily": daily, "target": target, "rate": rate,
        "margin": margin, "tlist": tlist,
    }


def calc_group(D, names, yms):
    """점포 묶음(팀 전체 / 담당자) 지표"""
    end = yms[-1] if yms else ""
    start = yms[0] if yms else ""
    rows = [calc_store(D, n, yms) for n in names]
    data_rows = [r for r in rows if r["has_data"]]

    opened = 0
    new_in_period = 0
    for n in names:
        oy = D["master_by_name"].get(n, {}).get("open_ym", "")
        if oy and end and oy <= end:
            opened += 1
            if start and oy >= start:
                new_in_period += 1
    if opened == 0:
        opened = len(data_rows)

    sales = sum(r["sales"] for r in data_rows)
    days = sum(r["days"] for r in data_rows)
    all_t = [t for r in data_rows for t in r["tlist"]]
    daily = (sales / days / 1000.0) if days > 0 else None
    target = (sum(all_t) / len(all_t)) if all_t else None
    rate = (daily / target * 100.0) if (daily is not None and target) else None

    pl_rows = [r for r in data_rows if r["pl"] is not None]
    pl_sum = sum(r["pl"] for r in pl_rows) if pl_rows else None
    margin = (pl_sum / sum(r["sales"] for r in pl_rows) * 100.0) if (pl_rows and sum(r["sales"] for r in pl_rows) > 0) else None

    op_rows = [r for r in data_rows if r["op"] is not None]
    op_sum = sum(r["op"] for r in op_rows) if op_rows else None
    black = len([r for r in op_rows if r["op"] > 0])

    rate_rows = [r for r in data_rows if r["rate"] is not None]
    achieved = len([r for r in rate_rows if r["rate"] >= 100.0])

    return {
        "opened": opened, "new_in_period": new_in_period, "data_n": len(data_rows),
        "sales": sales, "days": days, "daily": daily, "target": target, "rate": rate,
        "pl_sum": pl_sum, "margin": margin,
        "op_sum": op_sum, "op_n": len(op_rows), "black": black,
        "rate_n": len(rate_rows), "achieved": achieved,
    }


# ───────────────────────── 화면 조각 ─────────────────────────
TOOL_CSS = """
<style>
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 4px 0 10px; }
.kpi { background: #ffffff; border: 1px solid #d5dcea; border-radius: 12px; padding: 12px 14px; box-shadow: 0 1px 4px rgba(31,58,115,.06); }
.kpi .kpi-l { font-size: 12px; color: #6b7a99; font-weight: 600; }
.kpi .kpi-v { font-size: 22px; font-weight: 800; color: #1f3a73; margin-top: 2px; letter-spacing: -0.5px; }
.kpi .kpi-v.neg { color: #d0143c; }
.kpi .kpi-v small { font-size: 13px; font-weight: 600; color: #6b7a99; margin-left: 2px; }
.kpi .kpi-s { font-size: 11.5px; color: #8591ab; margin-top: 2px; line-height: 1.4; }
.mgr-name { font-size: 17px; font-weight: 800; color: #1f3a73; margin: 0 0 2px; }
.mgr-sub { font-size: 12px; color: #8591ab; margin-bottom: 4px; }
@media (max-width: 640px) { .kpi .kpi-v { font-size: 18px; } }
</style>
"""


def kpi_html(items):
    """items: [(라벨, 값, 단위, 부가설명, 빨강여부), ...]"""
    out = ['<div class="kpi-grid">']
    for label, value, unit, sub, neg in items:
        cls = " neg" if neg else ""
        u = f"<small>{unit}</small>" if unit and value != "-" else ""
        s = f'<div class="kpi-s">{sub}</div>' if sub else ""
        out.append(f'<div class="kpi"><div class="kpi-l">{label}</div><div class="kpi-v{cls}">{value}{u}</div>{s}</div>')
    out.append("</div>")
    return "".join(out)


def group_cards(G, cumulative):
    pct = lambda a, b: (f"{a / b * 100:.1f}%" if b else "-")
    op_label = "영업이익 누계" if cumulative else "영업이익"
    items = [
        ("오픈 점포", fnum(G["opened"]), "곳",
         f"기간 내 신규 {G['new_in_period']}곳 · 실적 있는 점포 {G['data_n']}곳", False),
        ("일매출 (점포 평균)", fnum(G["daily"]), "천원",
         "예상 " + fnum(G["target"], 0, "천원") if G["target"] is not None else "예상 -", False),
        ("예상매출 달성률", fnum(G["rate"], 1), "%",
         f"달성점 {G['achieved']}/{G['rate_n']}곳", (G["rate"] is not None and G["rate"] < 100)),
        (op_label, fnum(G["op_sum"]), "천원",
         f"영업이익 입력 {G['op_n']}곳", (G["op_sum"] is not None and G["op_sum"] < 0)),
        ("매출이익률", fnum(G["margin"], 1), "%",
         "매출이익 " + fnum(G["pl_sum"] / 1000.0 if G["pl_sum"] is not None else None, 0, "천원"), False),
        ("흑자점", fnum(G["black"]), "곳",
         f"구성비 {pct(G['black'], G['op_n'])} (영업이익 입력 {G['op_n']}곳 중)", False),
        ("예상매출 달성점", fnum(G["achieved"]), "곳",
         f"구성비 {pct(G['achieved'], G['rate_n'])} ({G['rate_n']}곳 중)", False),
    ]
    return kpi_html(items)


def pick_period(months):
    """기간 선택 (월별 / 누계). (모드, 연월 리스트, 설명글) 반환"""
    c1, c2, c3 = st.columns([1.1, 1.4, 1.4])
    with c1:
        mode = st.radio("보기 방식", ["월별", "누계"], horizontal=True, key="tool_pmode")
    if mode == "월별":
        with c2:
            m = st.selectbox("기준 월", months[::-1], format_func=ym_label, key="tool_pm")
        return mode, [m], ym_label(m)
    last_year = months[-1][:4]
    first_of_year = [x for x in months if x[:4] == last_year][0]
    with c2:
        s = st.selectbox("시작월", months, index=months.index(first_of_year), format_func=ym_label, key="tool_ps")
    with c3:
        e = st.selectbox("종료월", months, index=len(months) - 1, format_func=ym_label, key="tool_pe")
    if s > e:
        s, e = e, s
    yms = [x for x in months if s <= x <= e]
    return mode, yms, f"{ym_label(s)} ~ {ym_label(e)}"


def store_table_df(D, names, yms):
    rows = []
    for n in names:
        c = calc_store(D, n, yms)
        m = D["master_by_name"].get(n, {})
        rows.append({
            "점포명": n,
            "담당자": m.get("manager", ""),
            "운영형태": m.get("type", ""),
            "오픈일": m.get("open_date", ""),
            "일매출(천원)": round(c["daily"]) if c["daily"] is not None else None,
            "예상일매출(천원)": round(c["target"]) if c["target"] is not None else None,
            "달성률(%)": round(c["rate"], 1) if c["rate"] is not None else None,
            "매익률(%)": round(c["margin"], 1) if c["margin"] is not None else None,
            "영업이익(천원)": round(c["op"]) if c["op"] is not None else None,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("오픈일", ascending=False, kind="stable").reset_index(drop=True)
    return df


def show_store_table(df, key):
    """표를 보여주고, 한 줄을 누르면 그 점포명을 반환"""
    if df.empty:
        st.info("표시할 점포가 없습니다.")
        return None
    cfg = {
        "일매출(천원)": st.column_config.NumberColumn(format="%d"),
        "예상일매출(천원)": st.column_config.NumberColumn(format="%d"),
        "달성률(%)": st.column_config.NumberColumn(format="%.1f"),
        "매익률(%)": st.column_config.NumberColumn(format="%.1f"),
        "영업이익(천원)": st.column_config.NumberColumn(format="%d"),
    }
    ev = st.dataframe(
        df, use_container_width=True, hide_index=True, column_config=cfg,
        on_select="rerun", selection_mode="single-row", key=key,
    )
    st.caption("점포 줄을 누르면 아래에 상세가 나옵니다.")
    try:
        rows = ev.selection.rows
    except Exception:
        rows = []
    if rows:
        return df.iloc[rows[0]]["점포명"]
    return None


def render_store_detail(D, name, yms, period_text):
    m = D["master_by_name"].get(name, {})
    st.markdown(f"##### 🏪 {name}")
    info = [x for x in (m.get("manager"), m.get("type"), (m.get("open_date") and f"오픈 {m['open_date']}"),
                        m.get("region"), m.get("place")) if x]
    if info:
        st.caption(" · ".join(info))
    if m.get("address"):
        st.caption("📍 " + m["address"])

    c = calc_store(D, name, yms)
    items = [
        ("일매출", fnum(c["daily"]), "천원", period_text, False),
        ("예상일매출", fnum(c["target"]), "천원", "월별 예상의 평균", False),
        ("예상매출 달성률", fnum(c["rate"], 1), "%", "", (c["rate"] is not None and c["rate"] < 100)),
        ("매출이익률", fnum(c["margin"], 1), "%", "", False),
        ("영업이익", fnum(c["op"]), "천원", "", (c["op"] is not None and c["op"] < 0)),
    ]
    st.markdown(kpi_html(items), unsafe_allow_html=True)

    per = D["monthly"].get(name, {})
    if not per:
        st.info("이 점포의 월 실적이 아직 없습니다.")
        return
    tgt = D["targets"].get(name, {"daily": {}, "profit": {}})
    open_ym = m.get("open_ym", "")
    rows, chart = [], []
    for ym in sorted(per):
        r = per[ym]
        k = _month_diff(open_ym, ym) if open_ym else None
        t_daily = tgt["daily"].get(k) if k is not None else None
        t_profit = tgt["profit"].get(k) if k is not None else None
        daily = (r["sales"] / r["days"] / 1000.0) if r["days"] > 0 else None
        rate = (daily / t_daily * 100.0) if (daily is not None and t_daily) else None
        margin = (r["pl"] / r["sales"] * 100.0) if (r["pl"] is not None and r["sales"] > 0) else None
        rows.append({
            "연월": ym_label(ym),
            "총매출(천원)": round(r["sales"] / 1000.0),
            "영업일수": int(r["days"]),
            "일매출(천원)": round(daily) if daily is not None else None,
            "예상일매출(천원)": round(t_daily) if t_daily is not None else None,
            "달성률(%)": round(rate, 1) if rate is not None else None,
            "매익률(%)": round(margin, 1) if margin is not None else None,
            "영업이익(천원)": round(r["op"]) if r["op"] is not None else None,
            "예상월이익(천원)": round(t_profit) if t_profit is not None else None,
        })
        chart.append({"연월": ym_label(ym), "일매출": daily, "예상일매출": t_daily})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    cdf = pd.DataFrame(chart).set_index("연월")
    if len(cdf) >= 2:
        st.line_chart(cdf, height=240)


# ───────────────────────── 탭 화면 ─────────────────────────
def view_performance(D, yms, mode, period_text):
    cumulative = (mode == "누계")
    all_names = [m["name"] for m in D["masters"]]

    st.markdown(f"#### 🏆 팀 전체 · {period_text}")
    st.markdown(group_cards(calc_group(D, all_names, yms), cumulative), unsafe_allow_html=True)

    st.markdown("#### 👤 담당자별")
    sel_mgr = st.session_state.get("tool_mgr")
    managers = sorted({m["manager"] for m in D["masters"] if m["manager"]})
    if not managers:
        st.info("점포마스터에 담당자가 입력되어 있지 않습니다.")
        return

    if sel_mgr and sel_mgr in managers:
        def _back():
            st.session_state["tool_mgr"] = None
        st.button("← 담당자 목록으로", key="tool_mgr_back", on_click=_back)
        names = [m["name"] for m in D["masters"] if m["manager"] == sel_mgr]
        st.markdown(f'<div class="mgr-name">{sel_mgr}</div><div class="mgr-sub">{period_text} · 담당 점포 {len(names)}곳</div>',
                    unsafe_allow_html=True)
        st.markdown(group_cards(calc_group(D, names, yms), cumulative), unsafe_allow_html=True)
        picked = show_store_table(store_table_df(D, names, yms), key="tool_tbl_mgr")
        if picked:
            render_store_detail(D, picked, yms, period_text)
        return

    for mgr in managers:
        names = [m["name"] for m in D["masters"] if m["manager"] == mgr]
        with st.container(border=True):
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f'<div class="mgr-name">{mgr}</div><div class="mgr-sub">담당 점포 {len(names)}곳</div>',
                            unsafe_allow_html=True)
            with c2:
                def _open(mg=mgr):
                    st.session_state["tool_mgr"] = mg
                st.button("점포 보기 ▶", key=f"tool_mgr_{mgr}", on_click=_open, use_container_width=True)
            st.markdown(group_cards(calc_group(D, names, yms), cumulative), unsafe_allow_html=True)


def view_stores(D, yms, period_text):
    st.markdown(f"#### 🏪 점포 현황 · {period_text}")
    managers = sorted({m["manager"] for m in D["masters"] if m["manager"]})
    types = sorted({m["type"] for m in D["masters"] if m["type"]})
    c1, c2 = st.columns(2)
    with c1:
        f_mgr = st.selectbox("담당자", ["전체"] + managers, key="tool_f_mgr")
    with c2:
        f_type = st.selectbox("운영형태", ["전체"] + types, key="tool_f_type")
    names = [m["name"] for m in D["masters"]
             if (f_mgr == "전체" or m["manager"] == f_mgr) and (f_type == "전체" or m["type"] == f_type)]
    picked = show_store_table(store_table_df(D, names, yms), key="tool_tbl_all")
    if picked:
        render_store_detail(D, picked, yms, period_text)


# ───────────────────────── 진입점 ─────────────────────────
def render_tool(get_gc):
    st.markdown(TOOL_CSS, unsafe_allow_html=True)

    top1, top2 = st.columns([6, 1])
    with top2:
        if st.button("🔄 새로고침", key="tool_refresh", use_container_width=True):
            load_perf.clear()
            st.rerun()

    D = load_perf(get_gc)
    if D.get("error"):
        st.error(D["error"])
        return
    if not D["masters"]:
        st.info("점포마스터 탭에 점포가 아직 없습니다.")
        return
    if not D["months"]:
        st.info("월실적 탭에 실적 데이터가 아직 없습니다.")
        return

    mode, yms, period_text = pick_period(D["months"])

    tab_perf, tab_store, tab_weekly = st.tabs(["📊 실적 현황", "🏪 점포 현황", "📝 주간 활동 보고"])
    with tab_perf:
        view_performance(D, yms, mode, period_text)
    with tab_store:
        view_stores(D, yms, period_text)
    with tab_weekly:
        st.info("주간 활동 보고는 지금은 '휴게소 데이터 허브'의 '주간 활동 보고' 탭을 그대로 사용합니다. "
                "관리툴에서 어떻게 보여줄지는 다음 단계에서 정하겠습니다.")
