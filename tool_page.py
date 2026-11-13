"""
SD 허브(팀 관리툴) 화면 - app.py 의 '관리툴' 모드에서 불러 씁니다.

읽는 데이터: 구글 스프레드시트 '특수팀_실적관리' 의 3개 탭
  - 점포마스터 : 점포명 | 담당자 | 운영형태 | 오픈일 | 지역 | 주소 | 입지
  - 월실적     : 연월 | 점포명 | 총매출액(천원) | 영업일수 | 매출이익(천원) | 영업이익(천원)
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
TAB_GOAL = "목표"          # (선택) MBO 목표: 대상 | 지표 | 기간 | 목표값
TAB_MGR = "담당자"         # (선택) 담당자 표시 순서: 담당자 | 순서 | 사용여부(N 이면 화면에서 숨김)
TEAM_KEYS = ("팀", "팀전체", "전체", "특수개발팀", "특수팀")

KIND_DAILY_KEYWORD = "일매출"
KIND_PROFIT_KEYWORD = "이익"

ACHIEVE_PCT = 80.0         # 달성점으로 세는 기준(예상일매출 대비 달성률 %)

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

    # '목표' 탭(선택): 대상 | 지표 | 기간(연간/월) | 목표값  -> 없으면 그냥 건너뜀
    goals = {}
    grades = {}
    goal_v = read_tab(TAB_GOAL)
    for rec in _records(goal_v or []):
        who = rec.get("대상", "").replace(" ", "")
        metric = rec.get("지표", "").replace(" ", "")
        period = rec.get("기간", "").replace(" ", "") or "연간"
        val = _num(rec.get("목표값", ""))
        if who and metric and val is not None:
            goals[(who, metric, period)] = val
            gv = {}
            for lab in GRADE_COLS:       # 등급 기준 열 (선택): E+ | E | V+ | V | G+ | N+ | N | U+
                x = _num(rec.get(lab, ""))
                if x is not None:
                    gv[lab] = x
            if gv:
                grades[(who, metric, period)] = gv

    # '담당자' 탭(선택): 담당자 | 순서 | 사용여부 -> 없으면 가나다순
    mgr_order, mgr_hidden = {}, set()
    for rec in _records(read_tab(TAB_MGR) or []):
        n = rec.get("담당자", "").strip()
        if not n:
            continue
        o = _num(rec.get("순서", ""))
        if o is not None:
            mgr_order[n] = o
        if rec.get("사용여부", "").strip().upper() in ("N", "X", "NO", "FALSE", "0", "미사용", "아니오"):
            mgr_hidden.add(n)

    months = sorted({ym for per in monthly.values() for ym in per})
    return {"error": None, "masters": masters, "master_by_name": master_by_name,
            "monthly": monthly, "targets": targets, "months": months, "goals": goals, "grades": grades,
            "mgr_order": mgr_order, "mgr_hidden": mgr_hidden}


def ordered_managers(D):
    """화면에 보여줄 담당자 목록: '담당자' 탭의 순서 -> 탭에 없는 사람은 뒤에 가나다순, 사용여부 N 은 제외"""
    hidden = D.get("mgr_hidden", set())
    order = D.get("mgr_order", {})
    names = {m["manager"] for m in D["masters"] if m["manager"] and m["manager"] not in hidden}
    return sorted(names, key=lambda n: (n not in order, order.get(n, 0), n))


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

    daily = (sales / days) if days > 0 else None      # 시트 금액이 이미 천원 단위
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

    opened, new_in_period, opened_year = 0, 0, 0
    for n in names:
        oy = D["master_by_name"].get(n, {}).get("open_ym", "")
        if oy and end and oy <= end:
            opened += 1
            if oy[:4] == end[:4]:
                opened_year += 1
            if start and oy >= start:
                new_in_period += 1
    # 출점수 = 선택한 기간 안에 새로 오픈한 점포 수 (월별: 그 달만 / 누계: 시작월~종료월 합)
    opened = new_in_period

    sales = sum(r["sales"] for r in data_rows)
    days = sum(r["days"] for r in data_rows)
    daily = (sales / days) if days > 0 else None
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
    achieved = len([r for r in rate_rows if r["rate"] >= ACHIEVE_PCT])     # MBO 달성점 = 달성률 80% 이상

    return {"opened": opened, "new_in_period": new_in_period, "opened_year": opened_year, "data_n": len(data_rows),
            "daily": daily, "target": target, "rate": rate, "margin": margin,
            "op_sum": op_sum, "op_n": len(op_rows), "black": black,
            "rate_n": len(rate_rows), "achieved": achieved}


# ───────────────────────── 그래프(도넛) / 카드 HTML ─────────────────────────
TOOL_CSS = """
<style>
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 4px 0 10px; }
.kpi { --ac: #2c4a8a; background: linear-gradient(160deg, #ffffff 55%, #f3f6fc 100%); border: 1px solid #dde3ef;
       border-left: 5px solid var(--ac); border-radius: 14px; padding: 14px 16px 12px;
       box-shadow: 0 3px 10px rgba(31,58,115,.08); min-width: 0; }
.kpi .kpi-l { display: flex; align-items: center; gap: 7px; font-size: 14px; color: #55648a; font-weight: 700; }
.kpi .kpi-l svg { flex: none; }
.kpi .kpi-v { font-size: 27px; font-weight: 800; color: #1f3a73; margin-top: 6px; letter-spacing: -0.5px; white-space: nowrap; line-height: 1.2; }
.kpi .kpi-v.neg { color: #d0143c; }
.kpi .kpi-v small { font-size: 14px; font-weight: 600; color: #6b7a99; margin-left: 3px; }
.kpi .kpi-s { font-size: 12px; color: #7d8aa8; margin-top: 5px; line-height: 1.4; }
.kpi { display: flex; flex-direction: column; }
.kpi .kpi-bot { margin-top: auto; padding-top: 10px; }
.kpi.nobot { justify-content: center; }
.kpi .kpi-d { font-size: 12.5px; font-weight: 600; color: #7d8aa8; }
.kpi .kpi-d b { font-weight: 800; }
.kpi .kpi-d.up { color: #0b9a6a; }
.kpi .kpi-d.dn { color: #d0143c; }
.kpi .kpi-d.d0 { color: #9aa5bd; font-weight: 500; }
.kpi .kpi-goal { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 2px 6px; font-size: 12.5px; color: #55648a; font-weight: 600; margin-bottom: 4px; }
.kpi .kpi-goal b { font-weight: 800; }
.kpi .kpi-goal .gr { display: inline-flex; align-items: center; gap: 6px; }
.kpi .gchip { display: inline-block; min-width: 24px; text-align: center; font-size: 12px; font-weight: 800; color: #fff; border-radius: 99px; padding: 1px 8px; line-height: 1.4; }
.kpi .kpi-gp { position: relative; display: flex; height: 7px; background: #e6ebf4; border-radius: 99px; overflow: hidden; margin-bottom: 6px; }
.kpi .kpi-gp i { display: block; flex: none; height: 100%; }
.kpi .kpi-gp u { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: #ffffff; text-decoration: none; }
.kpi .kpi-ex { font-size: 11.5px; font-weight: 600; color: #7d8aa8; margin-bottom: 4px; line-height: 1.35; }
.kpi .kpi-ex b.up { color: #0b9a6a; font-weight: 800; }
.kpi .kpi-ex b.dn { color: #f08c00; font-weight: 800; }
.kpi .kpi-tl { position: relative; height: 16px; margin-bottom: 3px; }
.kpi .kpi-tl span { position: absolute; top: 0; transform: translateX(-50%); font-size: 11.5px; font-weight: 800; color: #7d8aa8; line-height: 16px; white-space: nowrap; }
.kpi .kpi-tl.r { text-align: right; }
.kpi .kpi-tl.r span { position: static; transform: none; margin-left: 10px; display: inline-block; }
.kpi .kpi-pair { display: flex; align-items: flex-end; gap: 16px; }
.kpi .kpi-pair .kpi-sep { width: 1px; align-self: stretch; background: #dde3ef; margin: 4px 0; }
.kpi .kpi-g2 { margin-bottom: 8px; }
.kpi .kpi-g2 .trk { position: relative; height: 8px; border-radius: 99px;
    background: linear-gradient(90deg, #fbdde2 0 66.67%, #ffe8c2 66.67% 83.33%, #cfeee1 83.33% 100%); }
.kpi .kpi-g2 .fl { position: absolute; left: 0; top: 0; height: 100%; border-radius: 99px; background: var(--ac); }
.kpi .kpi-g2 .tk { position: absolute; top: -3px; bottom: -3px; width: 2px; background: #55648a; border-radius: 2px; }
.kpi .kpi-g2 .lb { position: relative; height: 14px; margin-top: 5px; }
.kpi .kpi-g2 .lb span { position: absolute; transform: translateX(-50%); font-size: 10.5px; color: #7d8aa8; font-weight: 600; }
.st-key-periodbar { background: #ffffff; border: 1px solid #d5dcea; border-radius: 16px; padding: 0 !important; gap: 0 !important;
    overflow: hidden; box-shadow: 0 4px 14px rgba(31,58,115,.09); margin-bottom: 8px; }
.pb-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;
    padding: 12px 20px; background: linear-gradient(90deg, #eef3fc, #f8faff); border-bottom: 1px solid #e0e6f2; }
.pb-title { display: flex; align-items: center; gap: 9px; font-size: 16px; font-weight: 800; color: #1f3a73; }
.pb-title .ic { width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(145deg, #1f3a73, #3d64b0); }
.pb-chip { font-size: 13px; font-weight: 700; color: #2c4a8a; background: #ffffff; border: 1px solid #c9d4ea;
    border-radius: 999px; padding: 4px 14px; }
.pb-chip b { color: #0b9a6a; margin-right: 6px; }
/* 조회 기간 흰 영역: 높이를 84px 로 고정하고, 그 안에서 한 줄(40px)을 세로 가운데에 놓음 (padding 에 의존하지 않음) */
.st-key-periodbody { display: flex !important; flex-direction: column !important; justify-content: center !important;
    height: 84px !important; min-height: 84px !important; padding: 16px 20px 0 59px !important; box-sizing: border-box !important; }
.st-key-periodbody [data-testid="stHorizontalBlock"] { align-items: flex-start !important; gap: 0.8rem;
    height: 40px !important; min-height: 40px !important; }
.st-key-periodbody [data-testid="stRadio"], .st-key-periodbody [data-testid="stSelectbox"], .st-key-periodbody [data-testid="stButton"] {
    height: 40px !important; min-height: 40px !important; display: flex !important; flex-direction: column; justify-content: center; }
.pb-lab { font-size: 14px; font-weight: 700; color: #55648a; text-align: left; white-space: nowrap; line-height: 40px; height: 40px; margin: 0; }
.st-key-periodbody [data-testid="stElementContainer"], .st-key-periodbody [data-testid="element-container"] { margin: 0 !important; }
/* 알약 선택(라디오를 알약처럼 꾸밈): 조회 기간 / 담당자 필터 공통. 선택된 항목 = 네이비 바탕 + 흰 글씨 */
.st-key-periodbody [data-testid="stWidgetLabel"], .st-key-mgr_filter [data-testid="stWidgetLabel"] { display: none !important; }
.st-key-periodbody [data-testid="stMarkdownContainer"] p, .st-key-mgr_filter [data-testid="stMarkdownContainer"] { margin: 0; }
/* 라디오 알약 (이전에 마음에 들어 하셨던 모양): 연한 파란 트랙 안에 항목들이 가로로 놓임 */
.st-key-periodbody [role="radiogroup"], .st-key-mgr_filter [role="radiogroup"] {
    display: flex !important; flex-direction: row !important; flex-wrap: nowrap; gap: 6px !important; align-items: center;
    background: #eef2fa; padding: 5px; border-radius: 14px; width: fit-content; }
.st-key-periodbody [role="radiogroup"] > label, .st-key-mgr_filter [role="radiogroup"] > label {
    margin: 0 !important; padding: 0 20px !important; border-radius: 10px; cursor: pointer;
    display: flex !important; align-items: center; justify-content: center; background: transparent; }
.st-key-periodbody [role="radiogroup"] > label { height: 30px; }
.st-key-mgr_filter [role="radiogroup"] > label { height: 38px; padding: 0 22px !important; }
.st-key-periodbody [role="radiogroup"] > label p, .st-key-mgr_filter [role="radiogroup"] > label p {
    font-size: 15px !important; font-weight: 700 !important; color: #55648a; margin: 0 !important; white-space: nowrap; line-height: 1.2; }
.st-key-mgr_filter [role="radiogroup"] > label p { font-size: 16px !important; }
.st-key-periodbody [role="radiogroup"] > label:hover, .st-key-mgr_filter [role="radiogroup"] > label:hover { background: #dfe7f6; }
/* 조회 기간 줄: 내용 크기대로 왼쪽부터 붙여 배치, 세로 가운데 */
.st-key-periodbody [data-testid="stHorizontalBlock"] { gap: 10px !important; justify-content: flex-start !important; }
.st-key-periodbody [data-testid="stColumn"], .st-key-periodbody [data-testid="column"] {
    flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-periodbody [data-testid="stColumn"]:last-child, .st-key-periodbody [data-testid="column"]:last-child { margin-left: auto; }
.st-key-periodbody [data-testid="stSelectbox"] { width: 170px; }
.st-key-periodbody [data-baseweb="select"] > div { min-height: 40px !important; height: 40px !important; }
.st-key-periodbody div.stButton > button { min-height: 40px !important; height: 40px; padding: 0 18px; }
.pb-lab.gap { margin-left: 26px; }
.pb-tilde { font-size: 16px; font-weight: 700; color: #55648a; line-height: 40px; height: 40px; margin: 0; }
/* 탭 메뉴: 눈에 띄는 알약 형태 (클래스 이름에 의존하지 않도록 속성으로 지정) */
[data-baseweb="tab-list"], [role="tablist"], [data-testid="stTabs"] > div:first-child { gap: 8px !important; background: #eef2fa !important; padding: 6px !important; border-radius: 14px !important; }
button[data-baseweb="tab"], button[role="tab"], [data-testid="stTab"] { height: 48px !important; padding: 0 26px !important; border-radius: 10px !important; background: transparent !important; }
button[role="tab"] p, button[role="tab"] div, button[role="tab"] span, [data-testid="stTab"] p { font-size: 17px !important; font-weight: 700 !important; color: #55648a; }
button[role="tab"]:hover, [data-testid="stTab"]:hover { background: #dfe7f6 !important; }
button[role="tab"][aria-selected="true"], [data-testid="stTab"][aria-selected="true"] { background: #2c4a8a !important; box-shadow: 0 3px 8px rgba(31,58,115,.28); }
button[role="tab"][aria-selected="true"] p, button[role="tab"][aria-selected="true"] div,
button[role="tab"][aria-selected="true"] span, [data-testid="stTab"][aria-selected="true"] p { color: #ffffff !important; }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none !important; }
.st-key-mgr_filter { margin-top: 18px; }
.st-key-mgr_filter [data-testid="stHorizontalBlock"] {
    align-items: flex-start !important; justify-content: flex-start !important; gap: 16px !important; }
.st-key-mgr_filter [data-testid="stColumn"], .st-key-mgr_filter [data-testid="column"] {
    flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
/* 점포 카드 줄: 칸 3개를 똑같은 너비로 (위 담당자 필터용 '내용 크기대로' 규칙이 카드 칸에 번지지 않게 함) */
div[class*="st-key-mgrbox_"] [data-testid="stHorizontalBlock"] { align-items: stretch; gap: 1rem; }
.sc-top { min-height: 100px; }
/* 점포 상세의 '← 점포 목록으로' 버튼: 오픈점 보기 버튼과 같은 알약 디자인 */
div[class*="st-key-backbtn_"] button { height: 40px; min-height: 40px; border: none !important; border-radius: 10px !important;
    background: #eef2fa !important; padding: 0 20px; }
div[class*="st-key-backbtn_"] button p { font-weight: 700 !important; font-size: 15px !important; color: #2c4a8a; white-space: nowrap; margin: 0; }
div[class*="st-key-backbtn_"] button:hover { background: #2c4a8a !important; }
div[class*="st-key-backbtn_"] button:hover p { color: #ffffff !important; }
/* 요약 화면: 지표별 비교 그래프 카드 2x2 */
.sm-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 8px 0 14px; }
@media (max-width: 900px) { .sm-grid { grid-template-columns: minmax(0, 1fr); } }
.sm-v small:empty { display: none; }
.sm-card { background: #ffffff; border: 1px solid #d5dcea; border-radius: 14px; padding: 16px 20px 10px; min-width: 0; }
.sm-t { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 17px; font-weight: 800; color: #1f3a73; }
.sm-tl { display: flex; align-items: center; gap: 8px; }
.sm-chip { font-size: 13px; font-weight: 800; color: #1f2d4d; background: #eef2fa; border-radius: 999px; padding: 4px 12px; white-space: nowrap; }
.sm-n { font-size: 13px; color: #6b7a99; margin: 4px 0 20px; }
.sm-row { display: flex; align-items: center; gap: 12px; height: 42px; }
.sm-nm { width: 60px; flex: none; font-size: 15px; font-weight: 700; color: #2a3a5e; }
.sm-tr { position: relative; flex: 1; height: 16px; }
.sm-bar { position: absolute; left: 0; right: 0; top: 0; bottom: 0; border-radius: 99px; overflow: hidden; }
.sm-fl { position: absolute; left: 0; top: 0; height: 100%; display: block; }
.sm-avg { position: absolute; top: -5px; bottom: -5px; width: 3px; margin-left: -1.5px; border-radius: 2px; background: #1f2d4d; box-shadow: 0 0 0 1px rgba(255,255,255,.9); }
.sm-avl { position: absolute; top: -17px; transform: translateX(-50%); font-size: 12px; font-weight: 800; color: #1f2d4d; white-space: nowrap; line-height: 1; }
/* 점포 카드의 오픈 순번 배지 */
.sc-no { display: flex; align-items: center; justify-content: center; min-width: 32px; height: 32px; padding: 0 6px; box-sizing: border-box;
    border-radius: 10px; background: #2c4a8a; color: #fff; font-size: 15px; font-weight: 800; flex: none; }
.sm-v { width: 140px; flex: none; text-align: right; font-size: 17px; font-weight: 800; color: #1f2d4d; line-height: 1.2; white-space: nowrap; }
.sm-v small { display: block; font-size: 12px; font-weight: 500; color: #6b7a99; min-height: 13px; }
.sm-v.top { color: #0b9a6a; }
div[class*="st-key-mgrbtn_"] button:disabled { opacity: .45; }
div[class*="st-key-mgrbtn_"] button:disabled:hover { background: #eef2fa !important; }
div[class*="st-key-mgrbtn_"] button:disabled:hover p { color: #2c4a8a !important; }
.st-key-mgr_filter .sec-h { margin: 0; white-space: nowrap; height: 48px; }
/* 담당자별 줄: 제목 / 필터 / 오픈점 보기 모두 48px 높이 상자 안에서 세로 가운데 */
.st-key-mgr_filter [data-testid="stHorizontalBlock"] { height: 48px !important; align-items: center !important; }
.st-key-mgr_filter [data-testid="stColumn"], .st-key-mgr_filter [data-testid="column"] { height: 48px !important; }
.st-key-mgr_filter [data-testid="stRadio"], .st-key-mgr_filter [data-testid="stButton"] {
    height: 48px !important; min-height: 48px !important; display: flex !important; flex-direction: column; justify-content: center; }
.st-key-mgr_filter div[class*="st-key-mgrbtn_"] { height: 48px; align-items: center; }
/* 라디오 글자 크기: 안쪽 어떤 요소든 같은 크기로 (오픈점 보기 버튼과 같은 16px) */
.st-key-mgr_filter [data-testid="stRadio"] label, .st-key-mgr_filter [data-testid="stRadio"] label * {
    font-size: 16px !important; font-weight: 700 !important; line-height: 1.2 !important; }
.st-key-periodbody [data-testid="stRadio"] label, .st-key-periodbody [data-testid="stRadio"] label * {
    font-size: 15px !important; font-weight: 700 !important; line-height: 1.2 !important; }
.mgr-row { min-height: 44px; padding-top: 0; margin-bottom: 6px; }
.st-key-mgr_filter [data-testid="stVerticalBlock"] { gap: 0 !important; }
.mgr-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; padding-top: 4px; }
.mgr-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.mgr-chip { font-size: 13px; font-weight: 600; color: #2c4a8a; background: #eaf0fb; border-radius: 999px; padding: 4px 12px; }
div[class*="st-key-mgrbtn_"] { display: flex; }
div[class*="st-key-mgrbtn_"] button { min-width: 0; height: 38px; min-height: 38px; border: none !important; border-radius: 10px !important;
    background: #eef2fa !important; padding: 0 20px; }
div[class*="st-key-mgrbtn_"] button p { font-weight: 700 !important; font-size: 16px !important; color: #2c4a8a; white-space: nowrap; margin: 0; }
div[class*="st-key-mgrbtn_"] button:hover { background: #2c4a8a !important; }
div[class*="st-key-mgrbtn_"] button:hover p { color: #ffffff !important; }
.sec-h { display: flex; align-items: center; gap: 10px; font-size: 22px; font-weight: 800; color: #1a2b4f; margin: 22px 0 16px; }
.sc-top { justify-content: space-evenly !important; align-items: center; gap: 12px; }
.sc-info { flex: 0 1 auto !important; min-width: 0; }
.sc-lead { display: flex; align-items: center; gap: 10px; min-width: 0; }
.sc-nums.three > div { text-align: center; }
.sc-nums.three .v { justify-content: center; }
.sec-ic { width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
          background: linear-gradient(145deg, #1f3a73, #3d64b0); flex: none; }
div[class*="st-key-mgrbox_"] { border-bottom: 1px solid #e3e8f2; padding-bottom: 14px; }
.chart-card.empty { border-style: dashed; background: #fafbfd; box-shadow: none; }
div[data-testid="stVerticalBlock"] { gap: 0.7rem; }
.mgr-name { font-size: 22px; font-weight: 800; color: #1f3a73; margin: 0 0 2px; }
.mgr-sub { font-size: 12px; color: #8591ab; margin-bottom: 4px; }
.sec-title { font-size: 15px; font-weight: 800; color: #1f3a73; margin: 18px 0 6px; padding-top: 14px; border-top: 2px solid #e3e8f2; }
.chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(620px, 1fr)); gap: 12px; align-items: stretch; }
.chart-grid .chart-card { margin: 0; }

.sc { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
.sc-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.sc-info { flex: 1; min-width: 0; }
.sc-namerow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
.sc-namerow .sc-tag { font-size: 12px; padding: 3px 9px; }
.sc-nums.three .v { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 3px; }
.sc-nums.three .v small { margin-left: 0; }
.store-head { background: #f6f8fc; border: 1px solid #d5dcea; border-radius: 14px; padding: 16px 20px; margin: 4px 0 6px; }
.store-head .sh-row { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.store-head .sh-name { font-size: 26px; font-weight: 800; color: #1f3a73; line-height: 1.3; }
.store-head .sh-meta { font-size: 15px; font-weight: 700; color: #3a4a6b; margin-top: 8px; }
.store-head .sh-addr { font-size: 14px; color: #5a6a8a; margin-top: 10px; padding-top: 10px; border-top: 1px dashed #d5dcea; }
.chart-grid { margin-bottom: 26px; }
.chart-grid .chart-card.empty { align-self: start; }
.sc-name { font-size: 20px; font-weight: 800; color: #1f3a73; line-height: 1.3; word-break: keep-all; }
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

.sc-donut { display: flex; flex-direction: column; align-items: center; flex: none; }
.sc-donut .cap { font-size: 11px; color: #6b7a99; font-weight: 700; margin-top: 1px; }
.sc-nums.three > div { min-width: 0; }
.sc-nums.three { gap: 8px; align-items: stretch; }
.sc-nums.three .l { font-size: 13px; font-weight: 700; color: #55648a; }
.sc-nums.three .v { font-size: 22px; line-height: 1.25; white-space: nowrap; color: #1f2d4d; }
.sc-nums.three .v small { font-size: 12px; color: #6b7a99; }
/* 영업이익은 크기는 같고 색만 다르게 (흑자 초록 / 적자 빨강) */
.sc-nums.three .op .v.pos { color: #0b9a6a; }
.sc-nums.three .op .v.neg { color: #d0143c; }
.chart-card { background: #fff; border: 1px solid #d5dcea; border-radius: 12px; padding: 12px 14px 8px; margin: 8px 0 12px;
              box-shadow: 0 1px 4px rgba(31,58,115,.06); }
.chart-card .chart-title { font-size: 14px; font-weight: 800; color: #1f3a73; margin-bottom: 2px; }
.chart-card .chart-note { font-size: 11.5px; color: #8591ab; margin-bottom: 4px; }
.chart-card > svg { width: 100%; max-width: 900px; height: auto; display: block; margin: 0 auto; }
.chart-card .chart-title svg, .sec-title svg, .sh-addr svg { display: inline-block; vertical-align: -3px; }
.legend-note { font-size: 11.5px; color: #8591ab; margin: 2px 0 8px; }
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
    fs = 16 if size >= 84 else (14 if size >= 76 else 12)
    cx = size / 2.0
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="flex:none">'
        f'<circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="#e6ebf4" stroke-width="{stroke}"/>'
        f'<circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="{col}" stroke-width="{stroke}" '
        f'stroke-linecap="butt" stroke-dasharray="{c * ratio:.2f} {c:.2f}" transform="rotate(-90 {cx} {cx})"/>'
        f'<text x="{cx}" y="{cx}" text-anchor="middle" dominant-baseline="central" '
        f'font-size="{fs}" font-weight="800" fill="#1f2d4d">{text}</text></svg>'
    )


COLOR_PURPLE = "#7b5cc4"
COLOR_BLUE = "#1e88e5"


def mini_icon(kind, color, size=18):
    """카드 라벨 옆 작은 벡터 아이콘"""
    if kind == "store":
        b = f'<path d="M3 10.5 12 3l9 7.5V21H3z" fill="{color}"/><rect x="9.5" y="13" width="5" height="8" fill="#fff"/>'
    elif kind == "pct":  # (미사용 가능)
        b = (f'<circle cx="12" cy="12" r="10" fill="{color}"/>'
             '<path d="M8 16 16 8" stroke="#fff" stroke-width="2" stroke-linecap="round"/>'
             '<circle cx="8.5" cy="8.5" r="1.8" fill="#fff"/><circle cx="15.5" cy="15.5" r="1.8" fill="#fff"/>')
    elif kind == "coin":
        b = (f'<circle cx="12" cy="12" r="10" fill="{color}"/>'
             '<path d="M8 8l4 9 4-9M7 11.5h10M7 14h10" stroke="#fff" stroke-width="1.7" fill="none" stroke-linecap="round"/>')
    elif kind == "target":
        b = (f'<circle cx="12" cy="12" r="10" fill="{color}"/><circle cx="12" cy="12" r="6" fill="#fff"/>'
             f'<circle cx="12" cy="12" r="2.6" fill="{color}"/>')
    elif kind == "bars":
        b = (f'<rect x="3" y="12" width="4.5" height="9" rx="1.2" fill="{color}"/>'
             f'<rect x="9.7" y="7" width="4.5" height="14" rx="1.2" fill="{color}"/>'
             f'<rect x="16.5" y="3" width="4.5" height="18" rx="1.2" fill="{color}"/>')
    elif kind == "check":
        b = (f'<circle cx="12" cy="12" r="10" fill="{color}"/>'
             '<path d="M7 12.5l3.2 3.2L17 9" stroke="#fff" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    elif kind == "team":
        b = (f'<circle cx="9" cy="8" r="3.6" fill="{color}"/><path d="M2 20c0-4 3-6.5 7-6.5s7 2.5 7 6.5z" fill="{color}"/>'
             f'<circle cx="17" cy="9" r="3" fill="{color}" fill-opacity=".7"/><path d="M16 14c4 0 6.5 2 6.5 6H18c0-2.5-.8-4.5-2-6z" fill="{color}" fill-opacity=".7"/>')
    elif kind == "trophy":
        b = (f'<path d="M7 3h10v6a5 5 0 0 1-10 0z" fill="{color}"/>'
             f'<path d="M7 5H3.5c0 3 1.3 4.8 3.8 5.2M17 5h3.5c0 3-1.3 4.8-3.8 5.2" stroke="{color}" stroke-width="1.6" fill="none"/>'
             f'<rect x="10.9" y="13.5" width="2.2" height="4" fill="{color}"/><rect x="7.5" y="17.5" width="9" height="3.2" rx="1.2" fill="{color}"/>')
    elif kind == "badge":
        ink = "#2a4685"
        b = (f'<rect x="4" y="3" width="16" height="19" rx="2.5" fill="{color}"/>'
             f'<rect x="9" y="5" width="6" height="2" rx="1" fill="{ink}"/>'
             f'<circle cx="12" cy="11.8" r="2.7" fill="{ink}"/><path d="M7.4 19.2c0-3 2.1-4.4 4.6-4.4s4.6 1.4 4.6 4.4z" fill="{ink}"/>')
    elif kind == "cal":
        b = (f'<rect x="3" y="5" width="18" height="16" rx="3" fill="{color}"/>'
             '<rect x="3" y="5" width="18" height="5" rx="2" fill="#000" fill-opacity=".18"/>'
             f'<rect x="7" y="2.5" width="2.2" height="5" rx="1.1" fill="{color}"/><rect x="14.8" y="2.5" width="2.2" height="5" rx="1.1" fill="{color}"/>'
             '<rect x="6.5" y="12.5" width="3" height="3" rx=".8" fill="#2a4685"/><rect x="11" y="12.5" width="3" height="3" rx=".8" fill="#2a4685"/>'
             '<rect x="15.5" y="12.5" width="2.5" height="3" rx=".8" fill="#2a4685" fill-opacity=".6"/>')
    elif kind == "pin":
        b = (f'<path d="M12 22s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z" fill="{color}"/>'
             '<circle cx="12" cy="10" r="2.8" fill="#fff"/>')
    else:   # person
        b = f'<circle cx="12" cy="8" r="4.4" fill="{color}"/><path d="M3.5 21c0-5 3.8-8 8.5-8s8.5 3 8.5 8z" fill="{color}"/>'
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" '
            f'style="vertical-align:middle">{b}</svg>')


def sec_heading(kind, text):
    return f'<div class="sec-h"><span class="sec-ic">{mini_icon(kind, "#ffffff", 20)}</span>{text}</div>'


def kpi_html(items):
    """items: dict(label, value, unit, sub, neg, accent, icon, gauge)"""
    out = ['<div class="kpi-grid">']
    # 한 줄 안에 아래쪽 줄(게이지/전월대비/목표)이 있는 카드가 하나라도 있으면 모든 카드를 위쪽 정렬(글자 높이 통일)
    any_bot = any(it.get("gauge") is not None or it.get("delta") for it in items)
    for it in items:
        cls = " neg" if it.get("neg") else ""
        unit = it.get("unit", "")
        u = f"<small>{unit}</small>" if unit and it["value"] != "-" else ""
        sub = f'<div class="kpi-s">{it["sub"]}</div>' if it.get("sub") else ""
        ac = it.get("accent", COLOR_NAVY)
        ic = mini_icon(it["icon"], ac) if it.get("icon") else ""
        g = it.get("gauge")
        gauge = ""
        if g is not None:
            w = max(0.0, min(g, 120.0)) / 120.0 * 100.0
            gauge = (f'<div class="kpi-g2"><div class="trk"><i class="fl" style="width:{w:.1f}%"></i>'
                     '<b class="tk" style="left:66.67%"></b><b class="tk" style="left:83.33%"></b></div>'
                     '<div class="lb"><span style="left:66.67%">80%</span><span style="left:83.33%">100%</span></div></div>')
        delta = it.get("delta") or ""
        bot = f'<div class="kpi-bot">{gauge}{delta}</div>' if (gauge or delta) else ""
        nb = "" if (bot or any_bot) else " nobot"
        if it.get("value2") is not None:
            v2 = it["value2"]
            unit2 = it.get("unit2", "")
            u2 = f"<small>{unit2}</small>" if unit2 and v2 != "-" else ""
            vals = (f'<div class="kpi-pair"><div class="kpi-v{cls}">{it["value"]}{u}</div>'
                    f'<span class="kpi-sep"></span><div class="kpi-v">{v2}{u2}</div></div>')
        else:
            vals = f'<div class="kpi-v{cls}">{it["value"]}{u}</div>'
        out.append(
            f'<div class="kpi{nb}" style="--ac:{ac}"><div class="kpi-l">{ic}{it["label"]}</div>'
            f'{vals}{sub}{bot}</div>'
        )
    out.append("</div>")
    return "".join(out)


def _op_accent(v):
    if v is None:
        return COLOR_GRAY
    return COLOR_GOOD if v >= 0 else COLOR_BAD


def _prev_ym(ym):
    y, m = int(ym[:4]), int(ym[4:]) - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}{m:02d}"


def delta_html(cur, prev, kind, name=""):
    """'전월 대비 ▲ 1.2%p' 한 줄. kind: pp(%p) / pt(점) / amt(천원)"""
    if cur is None:
        return ""
    if prev is None:
        return ""        # 전월 자료가 없으면 줄 자체를 빼서 카드가 비어 보이지 않게 함
    d = cur - prev
    if kind == "pp":
        thr, txt = 0.05, f"{abs(d):,.1f}%p"
    elif kind == "pt":
        thr, txt = 0.5, f"{abs(d):,.0f}점"
    else:
        thr, txt = 0.5, f"{abs(d):,.0f}천원"
    pre = f"{name} " if name else ""
    if abs(d) < thr:
        return f'<div class="kpi-d d0">{pre}전월과 동일</div>'
    cls, ar = ("up", "▲") if d > 0 else ("dn", "▼")
    return f'<div class="kpi-d {cls}">{pre}전월 대비 <b>{ar} {txt}</b></div>'


def _n(v, unit=""):
    """목표 줄용 숫자: 천원은 정수, 나머지는 소수가 있을 때만 1자리 (예: 9.5점)"""
    if v is None:
        return "-"
    if unit == "천원" or abs(v - round(v)) < 1e-9:
        s = f"{v:,.0f}"
    else:
        s = f"{v:,.1f}"
    return s + unit


# 등급 사다리 (낮음 -> 높음). '+등급' = 이 사다리에서 한 칸 위
GRADE_LADDER = ["U", "U+", "N", "N+", "G", "G+", "V", "V+", "E", "E+"]
GRADE_COLS = ["E+", "E", "V+", "V", "G+", "N+", "N", "U+"]        # 시트에서 읽는 등급 기준 열 (G = 목표값, U = 그 아래)
DAILY_GRADE_PCT = {"E+": 115.0, "E": 110.0, "V+": 107.5, "V": 105.0, "G+": 102.5,
                   "N+": 97.5, "N": 95.0, "U+": 90.0}              # 일매출: 목표값 대비 % 기본 기준


def grade_color(g):
    if g in ("E+", "E", "V+", "V"):
        return COLOR_GOOD
    if g in ("G+", "G"):
        return "#2f6fd0"
    if g in ("N+", "N"):
        return COLOR_WARN
    return COLOR_BAD


def _nn(v, unit=""):
    """단위 글자 없이 숫자만 (라벨용)"""
    s = _n(v, unit)
    return s[:-len(unit)] if unit and s.endswith(unit) else s


def goal_html(cur, goal, unit, tag="목표", grade=None, note="", top=None, top_label="", name=""):
    """목표가 있을 때만 쓰는 '목표 12점 중 5점 · 42%' + 진행 막대.
    막대 끝 = 최상위 등급 기준(top) 또는 실적(더 큰 쪽). 눈금(흰 선) = 목표(G)와 최상위 등급(E+ 등).
    목표를 넘은 부분은 초록. 아래 한 줄에 '+N 초과 · 안내'. grade 가 있으면 등급 칩"""
    if goal is None or goal <= 0 or cur is None:
        return ""
    pct = cur / goal * 100.0
    col = COLOR_GOOD if pct >= 100 else COLOR_NAVY
    chip = ""
    if grade:
        chip = f'<span class="gchip" style="background:{grade_color(grade)}" title="누계 기준 현재 등급">{grade}</span>'
    pct_title = "G(목표) 대비 달성률"
    has_top = top is not None and top > goal
    scale = max(cur, goal, top if has_top else goal)
    gpos = goal / scale * 100.0
    navy_w = min(cur, goal) / scale * 100.0
    green_w = max(cur - goal, 0.0) / scale * 100.0
    bar = f'<i style="width:{navy_w:.1f}%;background:{COLOR_NAVY}"></i>'
    if green_w > 0:
        bar += f'<i style="width:{green_w:.1f}%;background:{COLOR_GOOD}"></i>'
    ticks, labels = "", ""
    if gpos < 99.5:
        ticks += f'<u style="left:{gpos:.1f}%"></u>'
    if has_top:
        tpos = top / scale * 100.0
        if tpos < 99.5:
            ticks += f'<u style="left:{tpos:.1f}%"></u>'
        gcol, tcol = grade_color("G"), grade_color(top_label)
        gtxt = f"G({_nn(goal, unit)})"
        ttxt = f"{top_label}({_nn(top, unit)})"
        # 라벨은 항상 눈금 아래 가운데. 두 눈금이 가까워 겹칠 것 같으면 서로 반대쪽으로 같은 만큼만 벌림
        gl_ = min(max(gpos, 9.0), 91.0)
        tl_ = min(max(tpos, 9.0), 91.0)
        need = 20.0
        if tl_ - gl_ < need:
            push = (need - (tl_ - gl_)) / 2.0
            gl_ -= push
            tl_ += push
            if tl_ > 91.0:
                gl_ -= tl_ - 91.0
                tl_ = 91.0
            if gl_ < 9.0:
                tl_ += 9.0 - gl_
                gl_ = 9.0
        labels = (f'<div class="kpi-tl"><span style="left:{gl_:.1f}%;color:{gcol}">{gtxt}</span>'
                  f'<span style="left:{tl_:.1f}%;color:{tcol}">{ttxt}</span></div>')
    if not has_top:
        # 등급 기준이 없으면 G(목표) 라벨만 눈금(또는 막대 끝) 왼쪽에
        gcol = grade_color("G")
        labels = (f'<div class="kpi-tl"><span style="left:{min(gpos, 100.0):.1f}%;'
                  f'transform:translateX(-100%);color:{gcol}">G({_nn(goal, unit)})</span></div>')
    parts = []
    if cur > goal:
        parts.append(f'<b class="up">+{_n(cur - goal, unit)} 초과</b>')
    elif cur < goal:
        parts.append(f'<b class="dn">{_n(goal - cur, unit)} 미달</b>')
    if note:
        parts.append(note)
    extra = f'<div class="kpi-ex">{" · ".join(parts)}</div>' if parts else ""
    left = f'{name or tag} <b>{_n(cur, unit)}</b>'
    return (f'<div class="kpi-goal"><span>{left}</span>'
            f'<span class="gr" title="{pct_title}">{chip}<b style="color:{col}">{pct:.0f}%</b></span></div>'
            f'<div class="kpi-gp">{bar}{ticks}</div>{labels}{extra}')


def _who_key(who):
    return who.replace(" ", "")


def find_goal(D, who, metric, period):
    """who: '팀' 또는 담당자 이름. 없으면 None"""
    goals = D.get("goals", {}) if D else {}
    if who == "팀":
        for k in TEAM_KEYS:
            if (k, metric, period) in goals:
                return goals[(k, metric, period)]
        return None
    return goals.get((_who_key(who), metric, period))


def find_grades(D, who, metric, period):
    """시트 '목표' 탭의 등급 기준 열 값 (dict: 등급 -> 기준값). 없으면 빈 dict"""
    grades = D.get("grades", {}) if D else {}
    if who == "팀":
        for k in TEAM_KEYS:
            if (k, metric, period) in grades:
                return grades[(k, metric, period)]
        return {}
    return grades.get((_who_key(who), metric, period), {})


def grade_thresholds(D, who, metric, goal):
    """등급별 기준값 dict (G = 목표값 포함). 등급 기준이 하나도 없으면 빈 dict"""
    if goal is None or goal <= 0:
        return {}
    th = dict(find_grades(D, who, metric, "연간"))
    if metric == "일매출":
        for lab, pc in DAILY_GRADE_PCT.items():
            if lab not in th:
                th[lab] = goal * pc / 100.0
    if not th:
        return {}
    th["G"] = goal
    return th


def grade_top(D, who, metric, goal):
    """(최상위 등급 기준값, 등급 이름). 없으면 (None, '')"""
    th = grade_thresholds(D, who, metric, goal)
    for lab in reversed(GRADE_LADDER):
        if lab in th and lab != "G":
            return th[lab], lab
    return None, ""


def grade_info(D, who, metric, cur, goal, opened_year=None):
    """(현재 등급, 안내 문구). 등급 기준이 하나도 없으면 (None, '').
    개인의 달성점/흑자점은 올해 출점수가 출점 목표 이상이면 한 칸 위(+등급)로 올림"""
    if cur is None or goal is None or goal <= 0:
        return None, ""
    th = grade_thresholds(D, who, metric, goal)
    if not th:
        return None, ""
    label = "U"
    for lab in reversed(GRADE_LADDER):
        if lab in th and cur >= th[lab] - 1e-9:
            label = lab
            break
    note = ""
    is_team = _who_key(who) in TEAM_KEYS or who == "팀"
    if (not is_team) and metric in ("달성점", "흑자점") and opened_year is not None:
        og = find_goal(D, who, "출점수", "연간")
        if og is not None:
            if opened_year >= og - 1e-9:
                label = GRADE_LADDER[min(GRADE_LADDER.index(label) + 1, len(GRADE_LADDER) - 1)]
                note = "출점 달성 +등급"
            else:
                note = f"출점 달성 시 +등급 ({_n(opened_year)}/{_n(og)}점)"
    return label, note


def group_cards(G, mode, P=None, D=None, who="팀"):
    """P: 전월 계산 결과(월별 보기에서만 전달), D/who: 목표 조회용"""
    cumulative = (mode == "누계")
    use_d = P is not None
    p_ok = use_d and P["data_n"] > 0
    pv = (lambda k: P[k] if p_ok else None)
    # 1) 출점수 (+ 목표가 있으면 달성 막대)
    open_sub = ""
    open_bot = ""
    goal_year = find_goal(D, who, "출점수", "연간") if D else None
    goal_mon = find_goal(D, who, "출점수", "월") if (D and not cumulative) else None
    goal_part = ""
    if goal_year is not None:
        og, onote = grade_info(D, who, "출점수", G["opened_year"], goal_year)
        otop, otl = grade_top(D, who, "출점수", goal_year)
        goal_part = goal_html(G["opened_year"], goal_year, "점", "올해 목표", og, onote, otop, otl, "출점수")
    elif goal_mon is not None:
        goal_part = goal_html(G["new_in_period"], goal_mon, "점", "월 목표", name="출점수")
    open_bot = goal_part + open_bot
    # 영업이익 / 흑자점의 작은 글자
    if G["op_n"] == 0:
        op_sub = "영업이익 미입력"
    else:
        part = ""
        if G["op_n"] < G["data_n"]:
            part = f" · 입력 {G['op_n']}/{G['data_n']}점"
        op_sub = f"흑자 {G['black']}점 · {G['op_n']}점 중 구성비 {pct_text(G['black'], G['op_n'])}{part}"

    dl = (lambda cur, prev, kind, name="": delta_html(cur, prev, kind, name)) if use_d else (lambda *a, **k: "")
    black_cur = G["black"] if G["op_n"] > 0 else None
    black_prev = P["black"] if (p_ok and P["op_n"] > 0) else None

    def gl(metric, cur, unit, cum_only=False, tag="목표"):
        """'목표' 탭에 해당 지표 목표가 있으면 목표 줄, 없으면 빈 문자열 (연간 우선, 없으면 월)
        cum_only=True 이면 누계 보기에서만 표시 (연간 목표를 한 달 실적과 비교하면 의미가 어긋나므로)"""
        if not D or cur is None:
            return ""
        if cum_only and not cumulative:
            return ""
        g = find_goal(D, who, metric, "연간")
        if g is not None:
            gr, gnote = grade_info(D, who, metric, cur, g, G["opened_year"])
            top, tl = grade_top(D, who, metric, g)
            return goal_html(cur, g, unit, tag, gr, gnote, top, tl, metric)
        g = find_goal(D, who, metric, "월")
        return goal_html(cur, g, unit, tag, name=metric) if g is not None else ""

    items = [
        dict(label="출점수 / 매익율", value=fnum(G["opened"]), unit="점", sub=open_sub, accent=COLOR_NAVY,
             icon="store", value2=fnum(G["margin"], 1), unit2="%",
             delta=open_bot + dl(G["margin"], pv("margin"), "pp", "매익율")),
        dict(label="일매출", value=fnum(G["daily"]), unit="천원", accent=COLOR_BLUE, icon="coin",
             delta=gl("일매출", G["daily"], "천원", True, "일매출 목표") + dl(G["daily"], pv("daily"), "amt", "일매출")),
        dict(label="예상매출 달성률", value=fnum(G["rate"], 1), unit="%",
             sub=f"{G['rate_n']}점 중 {G['achieved']}점 달성 · 구성비 {pct_text(G['achieved'], G['rate_n'])}",
             neg=(G["rate"] is not None and G["rate"] < ACHIEVE_PCT),
            accent=rate_color(G["rate"]), icon="target",
            delta=gl("달성점", G["achieved"], "점", True, "달성점 목표") + dl(G["rate"], pv("rate"), "pp", "달성률")),
        dict(label="영업이익 누계" if cumulative else "영업이익", value=fnum(G["op_sum"]), unit="천원",
             sub=op_sub, neg=(G["op_sum"] is not None and G["op_sum"] < 0),
             accent=_op_accent(G["op_sum"]), icon="bars",
             delta=gl("흑자점", black_cur, "점", True, "흑자점 목표") + dl(G["op_sum"], pv("op_sum"), "amt", "영업이익")
             + dl(black_cur, black_prev, "pt", "흑자점")),
    ]
    return kpi_html(items)


# ───────────────────────── 기간 선택 ─────────────────────────
def icon_button(label, icon, **kw):
    """머티리얼 아이콘 버튼. 스트림릿 버전이 icon 을 지원하지 않으면 아이콘 없이 표시"""
    try:
        return st.button(label, icon=icon, **kw)
    except TypeError:
        return st.button(label, **kw)


def refresh_button(key):
    if icon_button("새로고침", ":material/refresh:", key=key):
        load_perf.clear()
        st.rerun()


def pill_select(options, key, on_change=None):
    """알약 모양 단일 선택. 버튼 여러 개를 컨테이너 이름(_on/_off)으로 꾸밈 -> 어떤 스트림릿 버전에서도 동일"""
    cur = st.session_state.get(key)
    if cur not in options:
        cur = options[0]
        st.session_state[key] = cur

    def _set(opt):
        st.session_state[key] = opt
        if on_change:
            on_change()

    with st.container(key=f"pill_{key}"):
        cols = st.columns(len(options))
        for i, (c, opt) in enumerate(zip(cols, options)):
            with c:
                with st.container(key=f"pl_{key}_{i}_{'on' if opt == cur else 'off'}"):
                    st.button(opt, key=f"{key}_btn_{i}", on_click=_set, args=(opt,))
    return st.session_state[key]


def pick_period(months):
    """조회 기간 바: 카드 하나 안에 [제목] [보기 방식] [월 선택] [월 선택] [여백] [새로고침]"""
    if "tool_pmode" not in st.session_state:
        st.session_state["tool_pmode"] = "누계"      # 처음 접속하면 '올해 1월 ~ 최신월 누계'로 시작
    with st.container(key="periodbar"):
        head = st.empty()
        with st.container(key="periodbody"):
            # 라벨을 위가 아니라 입력칸 '왼쪽'에 두고 전부 세로 가운데 정렬 -> 겹침/높이 어긋남이 생기지 않음
            l1, c1, l2, c2, l3, c3, c5 = st.columns([1, 1, 1, 1, 1, 1, 1])
            lab = lambda t, g="": st.markdown(f'<div class="pb-lab{g}">{t}</div>', unsafe_allow_html=True)
            with l1:
                lab("보기 방식")
            with c1:
                mode = st.radio("보기 방식", ["월별", "누계"], horizontal=True, key="tool_pmode",
                                label_visibility="collapsed")
            with c5:
                refresh_button("tool_refresh")
            if mode == "월별":
                with l2:
                    lab("기준 월", " gap")
                with c2:
                    m = st.selectbox("기준 월", months[::-1], format_func=ym_label, key="tool_pm",
                                     label_visibility="collapsed")
                yms, text = [m], ym_label(m)
            else:
                last_year = months[-1][:4]
                first_of_year = [x for x in months if x[:4] == last_year][0]
                with l2:
                    lab("조회 구간", " gap")
                with c2:
                    s = st.selectbox("시작월", months, index=months.index(first_of_year),
                                     format_func=ym_label, key="tool_ps", label_visibility="collapsed")
                with l3:
                    st.markdown('<div class="pb-tilde">~</div>', unsafe_allow_html=True)
                with c3:
                    e = st.selectbox("종료월", months, index=len(months) - 1, format_func=ym_label,
                                     key="tool_pe", label_visibility="collapsed")
                if s > e:
                    s, e = e, s
                yms = [x for x in months if s <= x <= e]
                text = f"{ym_label(s)} ~ {ym_label(e)}"
        head.markdown(
            f'<div class="pb-head"><div class="pb-title"><span class="ic">{mini_icon("cal", "#ffffff", 18)}</span>조회 기간</div>'
            f'<div class="pb-chip"><b>{mode}</b>{text}</div></div>', unsafe_allow_html=True)
    return mode, yms, text


# ───────────────────────── 점포 카드 / 상세 ─────────────────────────
SORT_OPTIONS = ["오픈일 빠른순","달성률 높은순", "일매출 높은순", "영업이익 높은순"]


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
    # 오픈일 빠른순 (1월 -> 11월). 오픈일이 비어 있는 점포는 맨 뒤
    return sorted(calcs, key=lambda c: (open_key(c) == "", open_key(c)))


def store_card_html(D, c, yms, no=None):
    m = D["master_by_name"].get(c["name"], {})
    no_html = f'<span class="sc-no">{no}</span>' if no else ""
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
    if c["op"] is None:
        op_cls = ""
    else:
        op_cls = " neg" if c["op"] < 0 else " pos"
    # 위: [오픈 순번] [이름·태그·담당자] [달성률 도넛] / 아래: 일매출 · 매익율 · 영업이익(강조)
    return (
        '<div class="sc">'
        f'<div class="sc-top"><div class="sc-lead">{no_html}<div class="sc-info">'
        f'<div class="sc-namerow"><span class="sc-name">{escape(c["name"])}</span>{"".join(tags)}</div>'
        f'<div class="sc-meta">{" · ".join(meta)}</div></div></div>'
        f'<div class="sc-donut">{donut_svg(c["rate"], rate_color(c["rate"]), size=76, stroke=9, dec=0)}'
        '<div class="cap">예상매출 달성률</div></div></div>'
        '<div class="sc-nums three">'
        f'<div><div class="l">일매출</div><div class="v">{fnum(c["daily"])}<small>천원</small></div></div>'
        f'<div><div class="l">매익율</div><div class="v">{fnum(c["margin"], 1)}<small>%</small></div></div>'
        f'<div class="op"><div class="l">영업이익</div><div class="v{op_cls}">{fnum(c["op"])}<small>천원</small></div></div>'
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
    tags = []
    if m.get("place"):
        tags.append(f'<span class="sc-tag place">{escape(m["place"])}</span>')
    if m.get("type"):
        tags.append(f'<span class="sc-tag">{escape(m["type"])}</span>')
    info = [escape(x) for x in (m.get("manager"), (m.get("open_date") and f"오픈 {m['open_date']}"), m.get("region")) if x]
    addr = (f'<div class="sh-addr">{mini_icon("pin", COLOR_BAD, 16)} {escape(m["address"])}</div>'
            if m.get("address") else "")
    st.markdown(
        '<div class="store-head"><div class="sh-row">'
        f'<div class="sh-name">{escape(name)}</div>{"".join(tags)}</div>'
        f'<div class="sh-meta">{" · ".join(info)}</div>{addr}</div>',
        unsafe_allow_html=True,
    )

    c = calc_store(D, name, yms)
    per = D["monthly"].get(name, {})
    tgt = D["targets"].get(name, {"daily": {}, "profit": {}})
    open_ym = m.get("open_ym", "")

    # 선택 기간 요약 카드 (월별 보기일 때만 전월 대비)
    if len(yms) == 1:
        pc = calc_store(D, name, [_prev_ym(yms[0])])
        pk = (lambda k: pc[k] if pc["has_data"] else None)
        dl = lambda cur, prev, kind: delta_html(cur, prev, kind)
    else:
        pk = (lambda k: None)
        dl = lambda *a: ""
    items = [
        dict(label="일매출", value=fnum(c["daily"]), unit="천원", sub=period_text, accent=COLOR_BLUE, icon="coin",
             delta=dl(c["daily"], pk("daily"), "amt")),
        dict(label="예상매출 달성률", value=fnum(c["rate"], 1), unit="%",
             sub="예상 " + fnum(c["target"], 0, "천원"),
             neg=(c["rate"] is not None and c["rate"] < 100),
             accent=rate_color(c["rate"]), icon="target", gauge=c["rate"],
             delta=dl(c["rate"], pk("rate"), "pp")),
        dict(label="매익율", value=fnum(c["margin"], 1), unit="%", accent=COLOR_PURPLE, icon="pct",
             delta=dl(c["margin"], pk("margin"), "pp")),
        dict(label="영업이익", value=fnum(c["op"]), unit="천원",
             sub="" if c["op"] is not None else "미입력",
             neg=(c["op"] is not None and c["op"] < 0), accent=_op_accent(c["op"]), icon="bars",
             delta=dl(c["op"], pk("op"), "amt")),
    ]
    st.markdown(kpi_html(items), unsafe_allow_html=True)

    if not per:
        st.info("이 점포의 월 실적이 아직 없습니다.")
        return

    # 월별 표 행 + 영업이익 누계 (전체 기간)
    rows = []
    op_total, op_cnt = 0.0, 0
    for ym in sorted(per):
        r = per[ym]
        k = _month_diff(open_ym, ym) if open_ym else None
        t_daily = tgt["daily"].get(k) if k is not None else None
        t_profit = tgt["profit"].get(k) if k is not None else None
        daily = (r["sales"] / r["days"]) if r["days"] > 0 else None
        rate = (daily / t_daily * 100.0) if (daily is not None and t_daily) else None
        margin = (r["pl"] / r["sales"] * 100.0) if (r["pl"] is not None and r["sales"] > 0) else None
        if r["op"] is not None:
            op_total += r["op"]
            op_cnt += 1
        rows.append([
            ym_label(ym), fnum(r["sales"]), fnum(r["days"]), fnum(daily), fnum(t_daily),
            fnum(rate, 1), fnum(margin, 1), fnum(r["op"]), fnum(t_profit),
        ])

    # 보여줄 연도 (실적이 두 해 이상이면 선택)
    years = sorted({ym[:4] for ym in per})
    if len(years) > 1:
        year = st.selectbox("그래프 연도", years, index=len(years) - 1, key=f"tool_year_{name}")
    else:
        year = years[0]
    pts = year_points(per, tgt, open_ym, year)
    last_ym = max(per)

    # 그래프 영역: 넓은 화면은 좌우(매출 | 영업이익), 좁은 화면은 위아래 (CSS grid 가 자동 전환)
    sales_card = (
        f'<div class="chart-card"><div class="chart-title">{mini_icon("bars", COLOR_NAVY, 17)} 매출 흐름 ({year}년, 일매출 천원)</div>'
        '<div class="chart-note">막대 = 일매출 · 주황 = 예상일매출 · 아래 표 = 월별 예상일매출 / 달성률 / 매익율 '
        '(막대에 마우스를 올리면 자세히 보입니다)</div>'
        + sales_chart_svg(pts, open_ym, last_ym) + '</div>'
    )
    if op_cnt == 0:
        ops_card = (f'<div class="chart-card empty"><div class="chart-title">{mini_icon("coin", COLOR_GOOD, 17)} 영업이익 흐름 (천원)</div>'
                    '<div class="chart-note">영업이익이 아직 입력되지 않았습니다. 입력하면 월별 흐름이 여기에 표시됩니다.</div></div>')
    else:
        ops_card = (
            f'<div class="chart-card"><div class="chart-title">{mini_icon("coin", COLOR_GOOD, 17)} 영업이익 흐름 ({year}년, 천원)'
            f' <span style="font-weight:600;color:#6b7a99;font-size:12px;">· 입력된 달 누계 {fnum(op_total)}천원</span></div>'
            '<div class="chart-note">초록 = 흑자 · 빨강 = 적자</div>'
            + ops_chart_svg(pts, open_ym, last_ym) + '</div>'
        )
    st.markdown(f'<div class="sec-title">{mini_icon("bars", COLOR_NAVY, 18)} 월별 흐름</div>'
                f'<div class="chart-grid">{sales_card}{ops_card}</div>', unsafe_allow_html=True)

    with st.expander("월별 상세 표 보기"):
        st.markdown(monthly_table_html(rows), unsafe_allow_html=True)


# ───────────────────────── 직접 그리는 그래프(SVG) ─────────────────────────
def _mlabel(ym):
    return f"{ym[2:4]}.{int(ym[4:]):02d}"


def year_points(per, tgt, open_ym, year):
    """해당 연도 1~12월 칸 만들기. state: pre(오픈 전) / data(실적 있음) / none(오픈 후인데 실적 없음)"""
    pts = []
    for mo in range(1, 13):
        ym = f"{year}{mo:02d}"
        p = {"ym": ym, "mo": mo, "state": "none", "daily": None, "target": None,
             "rate": None, "margin": None, "op": None, "sales": None}
        if open_ym and ym < open_ym:
            p["state"] = "pre"
        r = per.get(ym)
        k = _month_diff(open_ym, ym) if open_ym else None
        if k is not None and k >= 0:
            p["target"] = tgt["daily"].get(k)
        if r:
            p["state"] = "data"
            p["sales"] = r["sales"]
            p["daily"] = (r["sales"] / r["days"]) if r["days"] > 0 else None
            p["rate"] = (p["daily"] / p["target"] * 100.0) if (p["daily"] is not None and p["target"]) else None
            p["margin"] = (r["pl"] / r["sales"] * 100.0) if (r["pl"] is not None and r["sales"] > 0) else None
            p["op"] = r["op"]
        pts.append(p)
    return pts


CH_W = 760          # 그래프 기준 폭 (12칸 고정)
CH_X0 = 78          # 왼쪽 행 이름 영역
CH_X1 = CH_W - 6


def sales_chart_svg(pts, open_ym="", last_ym=""):
    """1~12월 고정 칸. 위: 매익율 / 막대: 일매출 + 예상일매출 선 / 아래: 월·예상일매출·달성률"""
    W = CH_W
    x0, x1 = CH_X0, CH_X1
    slot = (x1 - x0) / 12.0
    bw = 28.0
    y_top, y_base = 32, 130
    vals = [p["daily"] for p in pts if p["daily"] is not None] + [p["target"] for p in pts if p["target"] is not None]
    vmax = max(vals) if vals else 1.0
    if vmax <= 0:
        vmax = 1.0
    scale = (y_base - y_top) / vmax
    y_m = y_base + 18       # 월
    y_t = y_base + 38       # 예상일매출
    y_r = y_base + 58       # 달성률
    y_g = y_base + 78       # 매익율
    y_o = y_base + 96       # 오픈 표시
    H = y_base + 102

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="inherit">']
    # 범례
    out.append(f'<rect x="{W - 190}" y="3" width="10" height="10" fill="{COLOR_NAVY}" rx="2"/>'
               f'<text x="{W - 175}" y="13" font-size="12" fill="#6b7a99">일매출</text>'
               f'<line x1="{W - 120}" y1="8" x2="{W - 104}" y2="8" stroke="{COLOR_WARN}" stroke-width="3"/>'
               f'<text x="{W - 98}" y="13" font-size="12" fill="#6b7a99">예상일매출</text>')
    # 행 이름
    out.append(f'<text x="2" y="{y_g}" font-size="12" font-weight="700" fill="#6b7a99">매익율</text>')
    out.append(f'<text x="2" y="{y_m}" font-size="12" font-weight="700" fill="#6b7a99">월</text>')
    out.append(f'<text x="2" y="{y_t}" font-size="12" font-weight="700" fill="{COLOR_WARN}">예상일매출</text>')
    out.append(f'<text x="2" y="{y_r}" font-size="12" font-weight="700" fill="#6b7a99">달성률</text>')
    out.append(f'<line x1="{x0 - 4}" y1="{y_base}" x2="{x1}" y2="{y_base}" stroke="#d5dcea" stroke-width="1"/>')

    line_pts = []
    top_labels = []
    for i, p in enumerate(pts):
        cx = x0 + slot * (i + 0.5)
        has = p["state"] == "data"
        faint = p["state"] != "data" and not (last_ym and p["ym"] <= last_ym and p["state"] == "none")
        lab_col = "#c3cbdc" if p["state"] == "pre" or (not has and last_ym and p["ym"] > last_ym) else "#6b7a99"
        out.append(f'<text x="{cx:.1f}" y="{y_m}" text-anchor="middle" font-size="13" font-weight="{"700" if has else "400"}" '
                   f'fill="{lab_col}">{p["mo"]}월</text>')
        if has:
            mt = "-" if p["margin"] is None else f'{p["margin"]:.1f}%'
            out.append(f'<text x="{cx:.1f}" y="{y_g}" text-anchor="middle" font-size="12" font-weight="700" fill="#1f3a73">{mt}</text>')
            if p["daily"] is not None:
                h = max(p["daily"] * scale, 1.0)
                out.append(f'<rect x="{cx - bw / 2:.1f}" y="{y_base - h:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                           f'fill="{COLOR_NAVY}" rx="3"/>')
                # 막대 위 숫자: 예상일매출 선 위에 그려지도록 따로 모았다가 마지막에 추가 (흰 테두리로 선과 겹쳐도 읽힘)
                top_labels.append(f'<text x="{cx:.1f}" y="{y_base - h - 6:.1f}" text-anchor="middle" font-size="12" '
                                  f'font-weight="700" fill="#1f2d4d" stroke="#ffffff" stroke-width="3.5" stroke-linejoin="round" '
                                  f'paint-order="stroke" pointer-events="none">{p["daily"]:,.0f}</text>')
            rt = "-" if p["rate"] is None else f'{p["rate"]:.0f}%'
            out.append(f'<text x="{cx:.1f}" y="{y_r}" text-anchor="middle" font-size="12" font-weight="800" '
                       f'fill="{rate_color(p["rate"]) if p["rate"] is not None else "#8591ab"}">{rt}</text>')
        # 예상일매출 (실적이 있는 달에만 표시)
        if has and p["target"] is not None:
            ty = y_base - p["target"] * scale
            out.append(f'<line x1="{cx - bw / 2 - 4:.1f}" y1="{ty:.1f}" x2="{cx + bw / 2 + 4:.1f}" y2="{ty:.1f}" '
                       f'stroke="{COLOR_WARN}" stroke-width="2" stroke-linecap="round" stroke-opacity=".85"/>')
            line_pts.append(f"{cx:.1f},{ty:.1f}")
            out.append(f'<text x="{cx:.1f}" y="{y_t}" text-anchor="middle" font-size="12" font-weight="700" '
                       f'fill="{COLOR_WARN}">{p["target"]:,.0f}</text>')
        # 오픈 표시
        if open_ym and p["ym"] == open_ym:
            out.append(f'<text x="{cx:.1f}" y="{y_o}" text-anchor="middle" font-size="11" font-weight="700" '
                       f'fill="{COLOR_GOOD}">▲오픈</text>')
        # 마우스 올렸을 때 말풍선
        if has:
            tip = (f'{p["ym"][:4]}년 {p["mo"]}월 · 일매출 {fnum(p["daily"])}천원 / 예상 {fnum(p["target"])}천원 / '
                   f'달성률 {fnum(p["rate"], 1)}% / 매익율 {fnum(p["margin"], 1)}%')
            out.append(f'<rect x="{cx - slot / 2:.1f}" y="0" width="{slot:.1f}" height="{H}" fill="transparent">'
                       f'<title>{tip}</title></rect>')
    if len(line_pts) >= 2:
        out.append(f'<polyline points="{" ".join(line_pts)}" fill="none" stroke="{COLOR_WARN}" '
                   f'stroke-width="1.2" stroke-dasharray="4 3"/>')
    out.extend(top_labels)      # 숫자는 선 위에 (맨 마지막)
    out.append("</svg>")
    return "".join(out)


def ops_chart_svg(pts, open_ym="", last_ym=""):
    """1~12월 고정 칸 영업이익 막대 (흑자 초록 / 적자 빨강, 입력 전은 '미입력')"""
    W, H = CH_W, 232        # 매출 그래프와 같은 높이 (좌우로 놓였을 때 월 위치가 맞도록)
    x0, x1 = CH_X0, CH_X1
    n = len(pts)
    slot = (x1 - x0) / n
    bw = 28.0
    y_top, y_bot = 32, 118
    y_month = 148
    vals = [p["op"] for p in pts if p["op"] is not None]
    vmax = max([0.0] + vals)
    vmin = min([0.0] + vals)
    span = (vmax - vmin) or 1.0

    def Y(v):
        return y_top + (vmax - v) / span * (y_bot - y_top)

    zy = Y(0.0)
    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="inherit">']
    out.append(f'<line x1="{x0}" y1="{zy:.1f}" x2="{x1}" y2="{zy:.1f}" stroke="#b8c2d8" stroke-width="1"/>')
    out.append(f'<text x="2" y="{zy + 4:.1f}" font-size="12" font-weight="700" fill="#6b7a99">영업이익</text>')
    for i, p in enumerate(pts):
        cx = x0 + slot * (i + 0.5)
        v = p["op"]
        has = p["state"] == "data"
        if has and v is None:
            out.append(f'<text x="{cx:.1f}" y="{zy - 5:.1f}" text-anchor="middle" font-size="11" fill="#a0aac0">미입력</text>')
        elif v is not None:
            yv = Y(v)
            top, h = (yv, zy - yv) if v >= 0 else (zy, yv - zy)
            h = max(h, 1.0)
            col = COLOR_GOOD if v >= 0 else COLOR_BAD
            out.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{col}" rx="3">'
                       f'<title>{p["ym"][:4]}년 {p["mo"]}월 영업이익 {v:,.0f}천원</title></rect>')
            ty = top - 5 if v >= 0 else top + h + 13
            out.append(f'<text x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle" font-size="12" font-weight="700" '
                       f'fill="{col}">{v:,.0f}</text>')
        lab_col = "#c3cbdc" if p["state"] == "pre" or (not has and last_ym and p["ym"] > last_ym) else "#6b7a99"
        out.append(f'<text x="{cx:.1f}" y="{y_month}" text-anchor="middle" font-size="13" font-weight="{"700" if has else "400"}" '
                   f'fill="{lab_col}">{p["mo"]}월</text>')
        if open_ym and p["ym"] == open_ym:
            out.append(f'<text x="{cx:.1f}" y="{y_month + 18}" text-anchor="middle" font-size="11" font-weight="700" '
                       f'fill="{COLOR_GOOD}">▲오픈</text>')
    out.append("</svg>")
    return "".join(out)


def render_store_cards(D, names, yms, period_text, ctx, sort_by="오픈일 빠른순"):
    """점포 카드 목록. 카드의 '상세 보기'를 누르면 상세 화면으로 전환 (ctx 별로 따로 기억)"""
    state_key = f"tool_store_{ctx}"
    sel = st.session_state.get(state_key)
    if sel and sel in D["master_by_name"]:
        def _back():
            st.session_state[state_key] = None
        with st.container(key=f"backbtn_{ctx}"):
            icon_button("점포 목록으로", ":material/arrow_back:", key=f"tool_back_{ctx}", on_click=_back)
        render_store_detail(D, sel, yms, period_text)
        return

    if not names:
        st.info("조건에 맞는 점포가 없습니다.")
        return
    calcs = sort_calcs(D, [calc_store(D, n, yms) for n in names], sort_by)
    # 오픈 순번: 정렬 방식과 상관없이 '오픈일 빠른 순서'대로 1, 2, 3 ... (몇 번째로 오픈한 점포인지)
    by_open = sort_calcs(D, calcs, "오픈일 빠른순")
    open_no = {c["name"]: i + 1 for i, c in enumerate(by_open)}
    st.markdown(
        '<div class="legend-note">원 그래프 = 예상매출 달성률 (일매출 ÷ 예상일매출) · '
        '<span style="color:#0b9a6a;font-weight:700;">초록 100% 이상</span> · '
        '<span style="color:#f08c00;font-weight:700;">주황 80~100%</span> · '
        '<span style="color:#d0143c;font-weight:700;">빨강 80% 미만</span></div>',
        unsafe_allow_html=True,
    )
    PER_ROW = 3
    for i in range(0, len(calcs), PER_ROW):
        cols = st.columns(PER_ROW)
        for j, c in enumerate(calcs[i:i + PER_ROW]):
            with cols[j]:
                with st.container(border=True):
                    st.markdown(store_card_html(D, c, yms, open_no.get(c["name"])), unsafe_allow_html=True)

                    def _open(n=c["name"]):
                        st.session_state[state_key] = n
                    st.button("상세 보기", key=f"tool_sc_{ctx}_{c['name']}", on_click=_open,
                              use_container_width=True)


# ───────────────────────── 탭 화면 ─────────────────────────
def _sm_card(icon, color, title, note, items, avg_frac, avg_label="평균", chip=""):
    """요약 그래프 카드 1장. items: dict(name, key, frac, main, sub, color, rest)
    평균선은 막대 위아래로 삐져나오게 그리고, 첫 줄 위에 라벨을 붙임. 값이 없는 줄에는 선을 그리지 않음"""
    keys = [it["key"] for it in items if it["key"] is not None]
    top = max(keys) if (len(keys) > 1 and max(keys) > min(keys) and max(keys) > 0) else None
    chip_html = f'<span class="sm-chip">{chip}</span>' if chip else ""
    out = [f'<div class="sm-card"><div class="sm-t"><span class="sm-tl">{mini_icon(icon, color, 20)}{title}</span>{chip_html}</div>'
           f'<div class="sm-n">{note}</div>']
    for idx, it in enumerate(items):
        fl = ""
        if it["frac"] is not None:
            fl = f'<i class="sm-fl" style="width:{max(0.0, min(it["frac"], 1.0)) * 100:.1f}%;background:{it["color"]}"></i>'
        mk = ""
        if avg_frac is not None:
            pos = f'{max(0.0, min(avg_frac, 1.0)) * 100:.1f}%'
            if it["frac"] is not None:
                mk += f'<b class="sm-avg" style="left:{pos}"></b>'
            if idx == 0:
                mk += f'<span class="sm-avl" style="left:{pos}">{avg_label}</span>'
        cls = " top" if (top is not None and it["key"] == top) else ""
        out.append(
            f'<div class="sm-row"><div class="sm-nm">{escape(it["name"])}</div>'
            f'<div class="sm-tr"><div class="sm-bar" style="background:{it.get("rest", "#eef2fa")}">{fl}</div>{mk}</div>'
            f'<div class="sm-v{cls}">{it["main"]}<small>{it["sub"]}</small></div></div>')
    out.append("</div>")
    return "".join(out)


def summary_html(D, managers, yms, all_names):
    """요약: 담당자별 출점수 / 일매출 / 예상매출 달성점 구성비 / 흑자점 구성비 비교 그래프 4장"""
    G = []
    for mgr in managers:
        names = [m["name"] for m in D["masters"] if m["manager"] == mgr]
        G.append((mgr, calc_group(D, names, yms)))
    T = calc_group(D, all_names, yms)

    # 1) 출점수
    vals = [g["opened"] for _, g in G]
    mx = max(vals) if max(vals) > 0 else 1
    avg = sum(vals) / len(vals)
    items = [dict(name=n, key=g["opened"], frac=g["opened"] / mx, main=f'{g["opened"]}점', sub="",
                  color=COLOR_NAVY) for n, g in G]
    c1 = _sm_card("store", COLOR_NAVY, "출점수", f"선택 기간 신규 오픈 · 합계 {T['opened']}점",
                  items, avg / mx, "평균", chip=f"평균 {avg:.1f}점")

    # 2) 일매출
    dv = [g["daily"] for _, g in G if g["daily"] is not None]
    tdaily = T["daily"]
    mx = max(dv + ([tdaily] if tdaily else []) + [1])
    items = []
    for n, g in G:
        d = g["daily"]
        if d is None:
            items.append(dict(name=n, key=None, frac=None, main="-", sub="실적 없음", color=COLOR_BLUE))
        else:
            col = COLOR_BLUE if (tdaily is None or d >= tdaily) else "#9dc3ee"
            items.append(dict(name=n, key=d, frac=d / mx, main=fnum(d), sub="천원", color=col))
    c2 = _sm_card("coin", COLOR_BLUE, "일매출",
                  "점포당 일매출 · 연한 막대 = 평균 미만",
                  items, (tdaily / mx) if tdaily else None, "평균",
                  chip=(f"평균 {fnum(tdaily)}천원" if tdaily else ""))

    # 3) 예상매출 달성점 구성비 (모수 = 예상목표가 있어 달성률을 구할 수 있는 점포)
    def ratio_items(num_k, den_k, color, rest, sub_extra):
        its = []
        for n, g in G:
            den, num = g[den_k], g[num_k]
            if den > 0:
                r = num / den
                its.append(dict(name=n, key=r, frac=r, main=f"{r * 100:.1f}%",
                                sub=f"{num}/{den}점" + sub_extra(g), color=color, rest=rest))
            else:
                its.append(dict(name=n, key=None, frac=None, main="-", sub="대상 없음", color=color, rest=rest))
        tr = (T[num_k] / T[den_k]) if T[den_k] > 0 else None
        return its, tr

    its, tr = ratio_items("achieved", "rate_n", COLOR_GOOD, "#e6ebf4", lambda g: "")
    c3 = _sm_card("target", COLOR_GOOD, "예상매출 달성점 구성비",
                  "초록 = 달성(80%↑) · 회색 = 미달 · 예상목표 있는 점포 기준",
                  its, tr, "평균", chip=(f"평균 {tr * 100:.1f}%" if tr is not None else ""))

    # 4) 흑자점 구성비 (모수 = 영업이익이 입력된 점포)
    its, tr = ratio_items("black", "op_n", COLOR_GOOD, "#f7c6d0", lambda g: f" · 입력 {g['op_n']}/{g['data_n']}")
    c4 = _sm_card("check", COLOR_GOOD, "흑자점 구성비",
                  "초록 = 흑자 · 분홍 = 적자 · 영업이익 입력 점포 기준",
                  its, tr, "평균", chip=(f"평균 {tr * 100:.1f}%" if tr is not None else ""))

    return f'<div class="sm-grid">{c1}{c2}{c3}{c4}</div>'


def view_performance(D, yms, mode, period_text):
    all_names = [m["name"] for m in D["masters"]]

    def prev_of(names):
        # 월별 보기에서만 '전월 대비'를 계산 (누계 보기는 None)
        if mode == "월별" and len(yms) == 1:
            return calc_group(D, names, [_prev_ym(yms[0])])
        return None

    st.markdown(sec_heading("trophy", f"팀 전체 · {period_text}"), unsafe_allow_html=True)
    st.markdown(group_cards(calc_group(D, all_names, yms), mode, prev_of(all_names), D, "팀"),
                unsafe_allow_html=True)

    managers = ordered_managers(D)
    if not managers:
        st.markdown(sec_heading("badge", "담당자별"), unsafe_allow_html=True)
        st.info("점포마스터에 담당자가 입력되어 있지 않습니다.")
        return

    # 제목 + 담당자 알약 필터 (전체 / 이름...)
    options = ["요약"] + managers
    if st.session_state.get("tool_mgr_pick") not in options:
        st.session_state["tool_mgr_pick"] = "요약"

    def _reset_detail():
        for k in list(st.session_state.keys()):
            if str(k).startswith("tool_store_a"):
                st.session_state[k] = None

    def _toggle():
        st.session_state["tool_show_open"] = not st.session_state.get("tool_show_open", True)
        _reset_detail()

    with st.container(key="mgr_filter"):
        h1, h2, h3 = st.columns([1, 1, 1])
        with h1:
            st.markdown(sec_heading("badge", "담당자별"), unsafe_allow_html=True)
        with h2:
            pick = st.radio("담당자", options, horizontal=True, key="tool_mgr_pick",
                            label_visibility="collapsed", on_change=_reset_detail)
        with h3:
            # 오픈점 보기 버튼: 항상 담당자 필터 바로 오른쪽 (전체/담당자 선택과 무관하게 같은 자리)
            showing = st.session_state.get("tool_show_open", True)    # 기본 = 열림 (직접 닫으면 그 상태 유지)
            with st.container(key="mgrbtn_filter"):
                st.button("오픈점 닫기 ▲" if showing else "오픈점 보기 ▶", key="tool_open_toggle", on_click=_toggle,
                          disabled=((pick or "요약") == "요약"))
    pick = pick or "요약"
    show_open = st.session_state.get("tool_show_open", True)

    if pick == "요약":
        st.markdown(summary_html(D, managers, yms, all_names), unsafe_allow_html=True)
        return

    def _mgr_box(mgr, mi):
        names = [m["name"] for m in D["masters"] if m["manager"] == mgr]
        chips = "".join(f'<span class="mgr-chip">{escape(n)}</span>' for n in names)
        with st.container(key=f"mgrbox_{mi}"):
            # 이름 줄 + 카드를 한 덩어리로 그려서 간격을 내 CSS(mgr-row 의 margin-bottom)만으로 정함
            st.markdown(f'<div class="mgr-row"><div class="mgr-name">{escape(mgr)}</div>'
                        f'<div class="mgr-chips">{chips}</div></div>'
                        + group_cards(calc_group(D, names, yms), mode, prev_of(names), D, mgr),
                        unsafe_allow_html=True)
            if show_open:
                render_store_cards(D, names, yms, period_text, ctx=f"a{mi}")

    for mi, mgr in enumerate(managers):
        if pick == mgr:
            _mgr_box(mgr, mi)


def view_stores(D, yms, period_text):
    st.markdown(sec_heading("store", f"점포 현황 · {period_text}"), unsafe_allow_html=True)
    # 상세 화면일 때는 필터를 숨기고 상세만 보여줌
    if st.session_state.get("tool_store_b") in D["master_by_name"]:
        render_store_cards(D, [], yms, period_text, ctx="b")
        return

    managers = ordered_managers(D)
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

    D = load_perf(get_gc)
    problem = None
    if D.get("error"):
        problem = ("error", D["error"])
    elif not D["masters"]:
        problem = ("info", "점포마스터 탭에 점포가 아직 없습니다.")
    elif not D["months"]:
        problem = ("info", "월실적 탭에 실적 데이터가 아직 없습니다.")
    if problem:
        (st.error if problem[0] == "error" else st.info)(problem[1])
        e1, _e2 = st.columns([1, 5])
        with e1:
            if icon_button("새로고침", ":material/refresh:", key="tool_refresh_err", use_container_width=True):
                load_perf.clear()
                st.rerun()
        return

    mode, yms, period_text = pick_period(D["months"])

    tab_perf, tab_store, tab_weekly = st.tabs([":material/bar_chart: 실적 현황",
                                               ":material/storefront: 점포 현황",
                                               ":material/edit_note: 주간 활동 보고"])
    with tab_perf:
        view_performance(D, yms, mode, period_text)
    with tab_store:
        view_stores(D, yms, period_text)
    with tab_weekly:
        st.info("주간 활동 보고는 지금은 '휴게소 데이터 허브'의 '주간 활동 보고' 탭을 그대로 사용합니다. "
                "실적 현황·점포 현황을 마친 뒤 이어서 만들겠습니다.")
