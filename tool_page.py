"""
SD 허브(팀 관리툴) 화면 - app.py 의 '관리툴' 모드에서 불러 씁니다.

읽는 데이터: 구글 스프레드시트 '특수팀_실적관리' 의 3개 탭
  - 점포마스터 : 점포명 | 담당자 | 운영형태 | 오픈일 | 지역 | 주소 | 입지
  - 월실적     : 연월 | 점포명 | 총매출액(원) | 영업일수 | 매출이익(원) | 영업이익(천원)
  - 예상목표   : 점포명 | 구분(예상일매출 / 예상월이익) | M | M+1 | ... | M+11   (단위: 천원)
"""
import re
import math
from html import escape
import streamlit as st
import pandas as pd

PERF_SHEET_NAME = "특수팀_실적관리"
TAB_STORE = "점포마스터"
TAB_MONTH = "월실적"
TAB_TARGET = "예상목표"

KIND_DAILY_KEYWORD = "일매출"
KIND_PROFIT_KEYWORD = "이익"

COLOR_GOOD = "#0b9a6a"
COLOR_WARN = "#f08c00"
COLOR_BAD = "#d0143c"
COLOR_NAVY = "#2c4a8a"
COLOR_GRAY = "#cbd3e3"


# ───────────────────────── 값 변환 도우미 ─────────────────────────
def _num(v):
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ym(v):
    """'202611', '2026-11', '2026.11', '2026-11-18' -> '202611' (인식 못 하면 '')"""
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
    return (int(b[:4]) - int(a[:4])) * 12 + (int(b[4:]) - int(a[4:]))


def ym_label(ym):
    return f"{ym[:4]}년 {int(ym[4:])}월"


def _pick(rec, prefix):
    for k, v in rec.items():
        if str(k).strip().startswith(prefix):
            return v
    return ""


def _records(values):
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
    """쉼표 넣은 숫자 문자열 (None 이면 '-')"""
    if v is None:
        return "-"
    try:
        return f"{v:,.{dec}f}{suffix}"
    except (ValueError, TypeError):
        return "-"


def pct_text(a, b, dec=1):
    return f"{a / b * 100:.{dec}f}%" if b else "-"


# ───────────────────────── 데이터 읽기 ─────────────────────────
@st.cache_data(ttl=300, show_spinner="실적 데이터를 불러오는 중...")
def load_perf(_get_gc):
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

    monthly = {}
    for rec in _records(month_v):
        ym = _ym(rec.get("연월", ""))
        name = rec.get("점포명", "").strip()
        sales = _num(_pick(rec, "총매출액"))
        if not ym or not name or sales is None:
            continue
        monthly.setdefault(name, {})[ym] = {   # 같은 연월·점포는 아래쪽 행이 우선
            "sales": sales,
            "days": _num(_pick(rec, "영업일수")) or 0,
            "pl": _num(_pick(rec, "매출이익")),
            "op": _num(_pick(rec, "영업이익")),
        }

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
    return {"error": None, "masters": masters, "master_by_name": master_by_name,
            "monthly": monthly, "targets": targets, "months": months}


# ───────────────────────── 지표 계산 ─────────────────────────
def calc_store(D, name, yms):
    """한 점포의 선택 기간 지표.
    일매출 = 총매출 ÷ 영업일수 / 예상일매출 = 월별 예상일매출의 영업일수 가중평균 / 달성률 = 일매출 ÷ 예상일매출"""
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

    tw, td = 0.0, 0.0       # Σ(예상일매출×영업일수), Σ(영업일수)
    if open_ym:
        for ym, r in recs:
            k = _month_diff(open_ym, ym)
            if k in tgt["daily"] and r["days"] > 0:
                tw += tgt["daily"][k] * r["days"]
                td += r["days"]

    daily = (sales / days / 1000.0) if days > 0 else None
    target = (tw / td) if td > 0 else None
    rate = (daily / target * 100.0) if (daily is not None and target) else None
    margin = (pl / sales * 100.0) if (pl is not None and sales > 0) else None
    return {"name": name, "has_data": bool(recs), "sales": sales, "days": days,
            "pl": pl, "op": op, "daily": daily, "target": target, "rate": rate,
            "margin": margin, "tw": tw, "td": td}


def calc_group(D, names, yms):
    end = yms[-1] if yms else ""
    start = yms[0] if yms else ""
    rows = [calc_store(D, n, yms) for n in names]
    data_rows = [r for r in rows if r["has_data"]]

    opened, new_in_period = 0, 0
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
    daily = (sales / days / 1000.0) if days > 0 else None
    tw = sum(r["tw"] for r in data_rows)
    td = sum(r["td"] for r in data_rows)
    target = (tw / td) if td > 0 else None
    rate = (daily / target * 100.0) if (daily is not None and target) else None

    pl_rows = [r for r in data_rows if r["pl"] is not None]
    pl_sales = sum(r["sales"] for r in pl_rows)
    pl_sum = sum(r["pl"] for r in pl_rows) if pl_rows else None
    margin = (pl_sum / pl_sales * 100.0) if (pl_rows and pl_sales > 0) else None

    op_rows = [r for r in data_rows if r["op"] is not None]
    op_sum = sum(r["op"] for r in op_rows) if op_rows else None
    black = len([r for r in op_rows if r["op"] > 0])

    rate_rows = [r for r in data_rows if r["rate"] is not None]
    achieved = len([r for r in rate_rows if r["rate"] >= 100.0])

    return {"opened": opened, "new_in_period": new_in_period, "data_n": len(data_rows),
            "daily": daily, "target": target, "rate": rate, "margin": margin,
            "op_sum": op_sum, "op_n": len(op_rows), "black": black,
            "rate_n": len(rate_rows), "achieved": achieved}


# ───────────────────────── 그래프(도넛) / 카드 HTML ─────────────────────────
TOOL_CSS = """
<style>
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 4px 0 10px; }
.kpi { background: #ffffff; border: 1px solid #d5dcea; border-radius: 12px; padding: 12px 14px;
       box-shadow: 0 1px 4px rgba(31,58,115,.06); display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.kpi .kpi-body { min-width: 0; }
.kpi .kpi-l { font-size: 12px; color: #6b7a99; font-weight: 600; }
.kpi .kpi-v { font-size: 22px; font-weight: 800; color: #1f3a73; margin-top: 2px; letter-spacing: -0.5px; white-space: nowrap; }
.kpi .kpi-v.neg { color: #d0143c; }
.kpi .kpi-v small { font-size: 13px; font-weight: 600; color: #6b7a99; margin-left: 2px; }
.kpi .kpi-s { font-size: 11.5px; color: #8591ab; margin-top: 2px; line-height: 1.4; }
.mgr-name { font-size: 17px; font-weight: 800; color: #1f3a73; margin: 0 0 2px; }
.mgr-sub { font-size: 12px; color: #8591ab; margin-bottom: 4px; }

.sc { display: flex; flex-direction: column; gap: 10px; }
.sc-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.sc-name { font-size: 16px; font-weight: 800; color: #1f3a73; line-height: 1.3; word-break: keep-all; }
.sc-tags { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; }
.sc-tag { font-size: 11px; background: #eaf0fb; color: #2c4a8a; border-radius: 6px; padding: 2px 7px; font-weight: 600; }
.sc-tag.place { background: #fff3e0; color: #b25e00; }
.sc-meta { font-size: 11.5px; color: #8591ab; margin-top: 4px; }
.sc-nums { display: flex; gap: 10px; border-top: 1px solid #edf0f6; padding-top: 8px; }
.sc-nums > div { flex: 1; }
.sc-nums .l { font-size: 11px; color: #8591ab; }
.sc-nums .v { font-size: 17px; font-weight: 800; color: #1f3a73; }
.sc-nums .v.neg { color: #d0143c; }
.sc-nums .v small { font-size: 11px; font-weight: 600; color: #8591ab; margin-left: 2px; }

.tbl-wrap { overflow-x: auto; margin: 6px 0 10px; }
table.tbl { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; }
table.tbl th { background: #f2f5fb; color: #4a5b80; font-weight: 700; padding: 8px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid #d5dcea; }
table.tbl td { padding: 7px 10px; text-align: right; border-bottom: 1px solid #edf0f6; white-space: nowrap; color: #1f2d4d; }
table.tbl th:first-child, table.tbl td:first-child { text-align: left; }
table.tbl td.neg { color: #d0143c; }
@media (max-width: 640px) { .kpi .kpi-v { font-size: 18px; } }
</style>
"""


def rate_color(rate):
    if rate is None:
        return COLOR_GRAY
    if rate >= 100:
        return COLOR_GOOD
    if rate >= 80:
        return COLOR_WARN
    return COLOR_BAD


def donut_svg(pct, color, size=76, stroke=9, dec=0):
    """가운데에 퍼센트가 들어가는 도넛. pct 가 None 이면 회색 고리 + '-'"""
    r = (size - stroke) / 2.0
    c = 2 * math.pi * r
    ratio = 0.0 if pct is None else max(0.0, min(pct, 100.0)) / 100.0
    text = "-" if pct is None else f"{pct:.{dec}f}%"
    col = COLOR_GRAY if pct is None else color
    fs = 14 if size >= 76 else 12
    cx = size / 2.0
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="flex:none">'
        f'<circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="#e6ebf4" stroke-width="{stroke}"/>'
        f'<circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="{col}" stroke-width="{stroke}" '
        f'stroke-linecap="butt" stroke-dasharray="{c * ratio:.2f} {c:.2f}" transform="rotate(-90 {cx} {cx})"/>'
        f'<text x="{cx}" y="{cx}" text-anchor="middle" dominant-baseline="central" '
        f'font-size="{fs}" font-weight="800" fill="#1f2d4d">{text}</text></svg>'
    )


def kpi_html(items):
    """items: dict(label, value, unit, sub, neg, donut)"""
    out = ['<div class="kpi-grid">']
    for it in items:
        cls = " neg" if it.get("neg") else ""
        unit = it.get("unit", "")
        u = f"<small>{unit}</small>" if unit and it["value"] != "-" else ""
        sub = f'<div class="kpi-s">{it["sub"]}</div>' if it.get("sub") else ""
        donut = it.get("donut", "")
        out.append(
            f'<div class="kpi"><div class="kpi-body"><div class="kpi-l">{it["label"]}</div>'
            f'<div class="kpi-v{cls}">{it["value"]}{u}</div>{sub}</div>{donut}</div>'
        )
    out.append("</div>")
    return "".join(out)


def group_cards(G, mode):
    cumulative = (mode == "누계")
    # 1) 출점수
    open_sub = "" if cumulative else f"당월 신규 {G['new_in_period']}점"
    # 5) 영업이익
    op_sub = ""
    if G["op_n"] < G["data_n"]:
        op_sub = f"입력 {G['op_n']}/{G['data_n']}점"
    # 6) 흑자점
    black_sub = f"구성비 {pct_text(G['black'], G['op_n'])}"
    if G["op_n"] < G["data_n"]:
        black_sub += f" · 입력 {G['op_n']}점 기준"
    black_ratio = (G["black"] / G["op_n"] * 100.0) if G["op_n"] else None

    items = [
        dict(label="출점수", value=fnum(G["opened"]), unit="점", sub=open_sub),
        dict(label="매익율", value=fnum(G["margin"], 1), unit="%"),
        dict(label="일매출", value=fnum(G["daily"]), unit="천원"),
        dict(label="예상매출 달성률", value=fnum(G["achieved"]), unit="점 달성",
             sub=f"구성비 {pct_text(G['achieved'], G['rate_n'])} · 예상 {fnum(G['target'], 0, '천원')}",
             donut=donut_svg(G["rate"], rate_color(G["rate"]), dec=1)),
        dict(label="영업이익 누계" if cumulative else "영업이익", value=fnum(G["op_sum"]), unit="천원",
             sub=op_sub, neg=(G["op_sum"] is not None and G["op_sum"] < 0)),
        dict(label="흑자점", value=fnum(G["black"]), unit="점", sub=black_sub,
             donut=donut_svg(black_ratio, COLOR_NAVY, dec=0)),
    ]
    return kpi_html(items)


# ───────────────────────── 기간 선택 ─────────────────────────
def pick_period(months):
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


# ───────────────────────── 점포 카드 / 상세 ─────────────────────────
SORT_OPTIONS = ["오픈일 최신순", "달성률 높은순", "일매출 높은순", "영업이익 높은순"]


def sort_calcs(D, calcs, sort_by):
    def open_key(c):
        return D["master_by_name"].get(c["name"], {}).get("open_date", "")

    def num_key(field):
        # 값이 없는 점포는 맨 뒤로
        return lambda c: (c[field] is None, -(c[field] if c[field] is not None else 0))

    if sort_by == "달성률 높은순":
        return sorted(calcs, key=num_key("rate"))
    if sort_by == "일매출 높은순":
        return sorted(calcs, key=num_key("daily"))
    if sort_by == "영업이익 높은순":
        return sorted(calcs, key=num_key("op"))
    return sorted(calcs, key=open_key, reverse=True)


def store_card_html(D, c, yms):
    m = D["master_by_name"].get(c["name"], {})
    tags = []
    if m.get("place"):
        tags.append(f'<span class="sc-tag place">{escape(m["place"])}</span>')
    if m.get("type"):
        tags.append(f'<span class="sc-tag">{escape(m["type"])}</span>')
    meta = []
    if m.get("manager"):
        meta.append(escape(m["manager"]))
    if m.get("open_ym") and yms:
        n = _month_diff(m["open_ym"], yms[-1]) + 1
        if n >= 1:
            meta.append(f"오픈 {n}개월차")
    op_neg = " neg" if (c["op"] is not None and c["op"] < 0) else ""
    return (
        '<div class="sc">'
        '<div class="sc-top"><div>'
        f'<div class="sc-name">{escape(c["name"])}</div>'
        f'<div class="sc-tags">{"".join(tags)}</div>'
        f'<div class="sc-meta">{" · ".join(meta)}</div></div>'
        f'{donut_svg(c["rate"], rate_color(c["rate"]), size=76, dec=0)}</div>'
        '<div class="sc-nums">'
        f'<div><div class="l">일매출</div><div class="v">{fnum(c["daily"])}<small>천원</small></div></div>'
        f'<div><div class="l">영업이익</div><div class="v{op_neg}">{fnum(c["op"])}<small>천원</small></div></div>'
        '</div></div>'
    )


def monthly_table_html(rows):
    head = ["연월", "총매출(천원)", "영업일수", "일매출(천원)", "예상일매출(천원)", "달성률(%)", "매익율(%)",
            "영업이익(천원)", "예상월이익(천원)"]
    out = ['<div class="tbl-wrap"><table class="tbl"><thead><tr>']
    out += [f"<th>{h}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for i, v in enumerate(r):
            neg = ""
            if i in (7,) and v not in ("-", "") and v.startswith("-"):
                neg = ' class="neg"'
            out.append(f"<td{neg}>{v}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_store_detail(D, name, yms, period_text):
    m = D["master_by_name"].get(name, {})
    st.markdown(f"##### 🏪 {name}")
    tags = []
    if m.get("place"):
        tags.append(f'<span class="sc-tag place">{escape(m["place"])}</span>')
    if m.get("type"):
        tags.append(f'<span class="sc-tag">{escape(m["type"])}</span>')
    info = [escape(x) for x in (m.get("manager"), (m.get("open_date") and f"오픈 {m['open_date']}"), m.get("region")) if x]
    st.markdown(f'<div class="sc-tags">{"".join(tags)}</div><div class="sc-meta">{" · ".join(info)}</div>',
                unsafe_allow_html=True)
    if m.get("address"):
        st.caption("📍 " + m["address"])

    c = calc_store(D, name, yms)
    items = [
        dict(label="일매출", value=fnum(c["daily"]), unit="천원", sub=period_text),
        dict(label="예상매출 달성률", value=fnum(c["rate"], 1), unit="%",
             sub="예상 " + fnum(c["target"], 0, "천원"),
             donut=donut_svg(c["rate"], rate_color(c["rate"]), dec=0)),
        dict(label="매익율", value=fnum(c["margin"], 1), unit="%"),
        dict(label="영업이익", value=fnum(c["op"]), unit="천원",
             neg=(c["op"] is not None and c["op"] < 0)),
    ]
    st.markdown(kpi_html(items), unsafe_allow_html=True)

    per = D["monthly"].get(name, {})
    if not per:
        st.info("이 점포의 월 실적이 아직 없습니다.")
        return
    tgt = D["targets"].get(name, {"daily": {}, "profit": {}})
    open_ym = m.get("open_ym", "")
    rows, chart, op_chart = [], [], []
    for ym in sorted(per):
        r = per[ym]
        k = _month_diff(open_ym, ym) if open_ym else None
        t_daily = tgt["daily"].get(k) if k is not None else None
        t_profit = tgt["profit"].get(k) if k is not None else None
        daily = (r["sales"] / r["days"] / 1000.0) if r["days"] > 0 else None
        rate = (daily / t_daily * 100.0) if (daily is not None and t_daily) else None
        margin = (r["pl"] / r["sales"] * 100.0) if (r["pl"] is not None and r["sales"] > 0) else None
        rows.append([
            ym_label(ym), fnum(r["sales"] / 1000.0), fnum(r["days"]), fnum(daily), fnum(t_daily),
            fnum(rate, 1), fnum(margin, 1), fnum(r["op"]), fnum(t_profit),
        ])
        chart.append({"연월": ym_label(ym), "일매출": daily, "예상일매출": t_daily})
        if r["op"] is not None or t_profit is not None:
            op_chart.append({"연월": ym_label(ym), "영업이익": r["op"], "예상월이익": t_profit})
    st.markdown(monthly_table_html(rows), unsafe_allow_html=True)

    cdf = pd.DataFrame(chart).set_index("연월")
    if len(cdf) >= 2:
        st.caption("일매출 vs 예상일매출 (천원)")
        st.line_chart(cdf, height=240)
    if op_chart:
        st.caption("영업이익 vs 예상월이익 (천원)")
        st.bar_chart(pd.DataFrame(op_chart).set_index("연월"), height=240)


def render_store_cards(D, names, yms, period_text, ctx, sort_by="오픈일 최신순"):
    """점포 카드 목록. 카드의 '상세 보기'를 누르면 상세 화면으로 전환 (ctx 별로 따로 기억)"""
    state_key = f"tool_store_{ctx}"
    sel = st.session_state.get(state_key)
    if sel and sel in D["master_by_name"]:
        def _back():
            st.session_state[state_key] = None
        st.button("← 점포 목록으로", key=f"tool_back_{ctx}", on_click=_back)
        render_store_detail(D, sel, yms, period_text)
        return

    if not names:
        st.info("조건에 맞는 점포가 없습니다.")
        return
    calcs = sort_calcs(D, [calc_store(D, n, yms) for n in names], sort_by)
    PER_ROW = 3
    for i in range(0, len(calcs), PER_ROW):
        cols = st.columns(PER_ROW)
        for j, c in enumerate(calcs[i:i + PER_ROW]):
            with cols[j]:
                with st.container(border=True):
                    st.markdown(store_card_html(D, c, yms), unsafe_allow_html=True)

                    def _open(n=c["name"]):
                        st.session_state[state_key] = n
                    st.button("상세 보기", key=f"tool_sc_{ctx}_{c['name']}", on_click=_open,
                              use_container_width=True)


# ───────────────────────── 탭 화면 ─────────────────────────
def view_performance(D, yms, mode, period_text):
    all_names = [m["name"] for m in D["masters"]]

    st.markdown(f"#### 🏆 팀 전체 · {period_text}")
    st.markdown(group_cards(calc_group(D, all_names, yms), mode), unsafe_allow_html=True)

    st.markdown("#### 👤 담당자별")
    sel_mgr = st.session_state.get("tool_mgr")
    managers = sorted({m["manager"] for m in D["masters"] if m["manager"]})
    if not managers:
        st.info("점포마스터에 담당자가 입력되어 있지 않습니다.")
        return

    if sel_mgr and sel_mgr in managers:
        def _back():
            st.session_state["tool_mgr"] = None
            st.session_state["tool_store_a"] = None
        st.button("← 담당자 목록으로", key="tool_mgr_back", on_click=_back)
        names = [m["name"] for m in D["masters"] if m["manager"] == sel_mgr]
        st.markdown(f'<div class="mgr-name">{escape(sel_mgr)}</div>'
                    f'<div class="mgr-sub">{period_text} · 담당 {len(names)}점</div>', unsafe_allow_html=True)
        st.markdown(group_cards(calc_group(D, names, yms), mode), unsafe_allow_html=True)
        render_store_cards(D, names, yms, period_text, ctx="a")
        return

    for mgr in managers:
        names = [m["name"] for m in D["masters"] if m["manager"] == mgr]
        with st.container(border=True):
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f'<div class="mgr-name">{escape(mgr)}</div><div class="mgr-sub">담당 {len(names)}점</div>',
                            unsafe_allow_html=True)
            with c2:
                def _open(mg=mgr):
                    st.session_state["tool_mgr"] = mg
                st.button("점포 보기 ▶", key=f"tool_mgr_{mgr}", on_click=_open, use_container_width=True)
            st.markdown(group_cards(calc_group(D, names, yms), mode), unsafe_allow_html=True)


def view_stores(D, yms, period_text):
    st.markdown(f"#### 🏪 점포 현황 · {period_text}")
    # 상세 화면일 때는 필터를 숨기고 상세만 보여줌
    if st.session_state.get("tool_store_b") in D["master_by_name"]:
        render_store_cards(D, [], yms, period_text, ctx="b")
        return

    managers = sorted({m["manager"] for m in D["masters"] if m["manager"]})
    types = sorted({m["type"] for m in D["masters"] if m["type"]})
    places = sorted({m["place"] for m in D["masters"] if m["place"]})
    c1, c2, c3 = st.columns(3)
    with c1:
        f_mgr = st.selectbox("담당자", ["전체"] + managers, key="tool_f_mgr")
    with c2:
        f_type = st.selectbox("운영형태", ["전체"] + types, key="tool_f_type")
    with c3:
        f_place = st.selectbox("입지", ["전체"] + places, key="tool_f_place")
    c4, c5 = st.columns([1, 2])
    with c4:
        sort_by = st.selectbox("정렬", SORT_OPTIONS, key="tool_f_sort")
    with c5:
        q = st.text_input("점포명 검색", key="tool_f_q", placeholder="점포명 일부를 입력")

    names = [m["name"] for m in D["masters"]
             if (f_mgr == "전체" or m["manager"] == f_mgr)
             and (f_type == "전체" or m["type"] == f_type)
             and (f_place == "전체" or m["place"] == f_place)
             and (not q.strip() or q.strip() in m["name"])]
    st.caption(f"{len(names)}점")
    render_store_cards(D, names, yms, period_text, ctx="b", sort_by=sort_by)


# ───────────────────────── 진입점 ─────────────────────────
def render_tool(get_gc):
    st.markdown(TOOL_CSS, unsafe_allow_html=True)

    top1, top2 = st.columns([6, 1])
    with top2:
        if st.button("↻ 새로고침", key="tool_refresh", use_container_width=True):
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
                "실적 현황·점포 현황을 마친 뒤 이어서 만들겠습니다.")
