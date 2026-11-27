"""
SD 허브(팀 관리툴) 화면 - app.py 의 '관리툴' 모드에서 불러 씁니다.

읽는 데이터: 구글 스프레드시트 '특수팀_실적관리' 의 3개 탭
  - 점포마스터 : 점포명 | 담당자 | 운영형태 | 오픈일 | 지역 | 주소 | 입지
  - 월실적     : 연월 | 점포명 | 총매출액(천원) | 영업일수 | 매출이익(천원) | 영업이익(천원)
  - 예상목표   : 점포명 | 구분(예상일매출 / 예상월이익) | M | M+1 | ... | M+11   (단위: 천원)
"""
import io
import re
import math
import json
import zipfile
import uuid
import hashlib
from html import escape
from datetime import datetime, timezone, timedelta
import datetime as _dt
import streamlit as st
import pandas as pd
import gspread
import streamlit.components.v1 as components

PERF_SHEET_NAME = "특수팀_실적관리"
TAB_STORE = "점포마스터"
TAB_MONTH = "월실적"
TAB_TARGET = "예상목표"
TAB_GOAL = "목표"          # (선택) MBO 목표: 대상 | 지표 | 기간 | 목표값
TAB_MGR = "담당자"         # (선택) 담당자 표시 순서: 담당자 | 순서 | 사용여부(N 이면 화면에서 숨김)
TAB_CORP = "전사특수점"    # (선택, 지도용) 전사 특수점 현황: 위도/경도/업종/일매출 등
TAB_DAILY_CUM = "일일_누계원시"      # (선택, 일일보고용) 사내 일일(신) 당월 누계 원시 그대로 붙여넣는 탭
TAB_DAILY_RECENT = "최근점_일별원시"  # (선택, 일일보고용) 최근 오픈점 일자별 매출 원시 그대로 붙여넣는 탭
MAP_EXPORT_TAB = "지도_추출"     # 지도 화면에서 '필터링된 목록 내보내기' 눌렀을 때 쓸 탭(없으면 새로 만듦)
MAP_EXPORT_HEADERS = ["구분", "점포명", "브랜드", "업종대분류", "업종세부", "주소", "위도", "경도", "일매출", "추출일시"]
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


# ───────────────────────── 데이터 읽기 및 일일 원시 파서 ─────────────────────────
def _pct_clean(v):
    """'26.8%', '26.8', '0.268' 등 퍼센트 입력값을 0~100 기준 float(예: 26.8)로 정규화합니다."""
    raw_s = str(v or "").strip()
    if not raw_s:
        return None
    has_pct = "%" in raw_s
    cleaned = raw_s.replace(",", "").replace("%", "").strip()
    try:
        f = float(cleaned)
        if f != f:
            return None
    except ValueError:
        return None
    if not has_pct and 0 < abs(f) <= 1.0:
        return round(f * 100.0, 2)
    return f


def _norm_store_code(code):
    """'VNX78' -> 'NX78', 'mn78' -> 'MN78' 형태로 점포코드를 정규화합니다."""
    s = str(code or "").strip().upper()
    if len(s) == 5 and s.startswith("V") and re.match(r"^V[A-Z]{2}\d{2}$", s):
        return s[1:]
    return s


def _norm_store_name_key(name):
    return re.sub(r"\s+", "", str(name or "").strip()).lower()


def _build_master_lookup(masters):
    """점포코드·정규화 점포명·'역' 접미사 허용 매칭용 빠른 탐색 사전을 만듭니다."""
    by_code = {}
    by_norm = {}
    by_station_stem = {}
    stem_ambiguous = set()
    for m in masters:
        nm = m["name"]
        nk = _norm_store_name_key(nm)
        if nk:
            by_norm[nk] = nm
            st_key = nk[:-1] if nk.endswith("역") and len(nk) > 2 else nk
            if st_key in by_station_stem and by_station_stem[st_key] != nm:
                stem_ambiguous.add(st_key)
            else:
                by_station_stem[st_key] = nm
        cd = _norm_store_code(m.get("code", ""))
        if cd:
            by_code[cd] = nm
    for amb in stem_ambiguous:
        by_station_stem.pop(amb, None)
    return by_code, by_norm, by_station_stem


def _match_master_store(raw_name, raw_code, lookup):
    by_code, by_norm, by_station_stem = lookup
    cd = _norm_store_code(raw_code)
    if cd and cd in by_code:
        return by_code[cd]
    nk = _norm_store_name_key(raw_name)
    if not nk:
        return None
    if nk in by_norm:
        return by_norm[nk]
    st_key = nk[:-1] if nk.endswith("역") and len(nk) > 2 else nk
    return by_station_stem.get(st_key)


def parse_daily_cum_raw(values, masters, fallback_ym=""):
    """'일일_누계원시' 탭에 붙여넣은 사내 '일일(신)' 추출본에서 우리 팀 점포의 당월 누적 매출·영업일수·매출이익을 추출합니다.
    - 상단 제목/빈 줄이 있어도 '매출액'·'영업일수'가 있는 헤더 줄을 자동 탐색합니다.
    - 점포명 열의 헤더가 병합되어 비어 있는 BI 양식(C열 코드, E열 점포명 등)도 행의 속성 칸들을 스캔해 정확히 매칭합니다.
    - 원 단위 숫자는 자동으로 천원 단위(÷ 1,000)로 변환합니다."""
    if not values or len(values) < 2 or not masters:
        return {"has_raw": False, "ym": fallback_ym, "records": {}, "matched_count": 0}

    # 상단 1~2행에 연월(예: 202611, 2026-11)이 적혀 있으면 우선 사용
    detected_ym = ""
    for r in values[:3]:
        for cell in r[:8]:
            s = str(cell or "").strip()
            if re.match(r"^20[2-4]\d[-./년\s]*(0?[1-9]|1[0-2])\s*월?$", s):
                cand = _ym(s)
                if cand:
                    detected_ym = cand
                    break
        if detected_ym:
            break
    target_ym = detected_ym or fallback_ym

    hdr_idx = -1
    i_sales, i_days, i_pl = -1, -1, -1
    for idx, row in enumerate(values[:20]):
        norm_cells = [re.sub(r"\s+", "", str(c or "")) for c in row]
        s_cols = [i for i, c in enumerate(norm_cells) if c in ("매출액", "총매출액", "누계매출액") or c.startswith("매출액(")]
        d_cols = [i for i, c in enumerate(norm_cells) if "영업일수" in c]
        if s_cols and d_cols:
            hdr_idx = idx
            i_sales = s_cols[0]
            i_days = d_cols[0]
            p_cols = [i for i, c in enumerate(norm_cells) if "매출이익" in c]
            i_pl = p_cols[0] if p_cols else -1
            break

    if hdr_idx < 0:
        return {"has_raw": False, "ym": target_ym, "records": {}, "matched_count": 0}

    lookup = _build_master_lookup(masters)
    raw_matched = {}
    sample_dailies = []

    for row in values[hdr_idx + 1:]:
        if len(row) <= max(i_sales, i_days):
            continue
        sales_v = _num(row[i_sales])
        days_v = _num(row[i_days])
        if sales_v is None or days_v is None or days_v <= 0:
            continue
        pl_v = _num(row[i_pl]) if (0 <= i_pl < len(row)) else None

        # 매출액 앞쪽 속성 열들에서 점포코드 후보와 점포명 후보를 탐색해 점포마스터와 매칭
        matched_name = None
        codes_in_row = []
        texts_in_row = []
        scan_end = min(i_sales, len(row))
        for cell in row[:scan_end]:
            t_str = str(cell or "").strip()
            if not t_str:
                continue
            if re.match(r"^V?[A-Z]{2}\d{2,3}$", t_str.upper()):
                codes_in_row.append(t_str)
            else:
                texts_in_row.append(t_str)

        for cd in codes_in_row:
            matched_name = _match_master_store("", cd, lookup)
            if matched_name:
                break
        if not matched_name:
            for tx in texts_in_row:
                matched_name = _match_master_store(tx, "", lookup)
                if matched_name:
                    break

        if matched_name:
            raw_matched[matched_name] = {"sales": sales_v, "days": days_v, "pl": pl_v}
            sample_dailies.append(abs(sales_v) / days_v)

    if not raw_matched:
        return {"has_raw": False, "ym": target_ym, "records": {}, "matched_count": 0}

    # 원 단위 자동 판별: 일매출 중앙값이 50,000 이상이면 원 단위로 보고 1,000으로 나눔
    sample_dailies.sort()
    median_daily = sample_dailies[len(sample_dailies) // 2]
    scale = 1000.0 if median_daily >= 50000.0 else 1.0

    scaled = {}
    max_days = 0
    for nm, d in raw_matched.items():
        max_days = max(max_days, int(round(d["days"])))
        scaled[nm] = {
            "sales": d["sales"] / scale,
            "days": d["days"],
            "pl": (d["pl"] / scale) if d["pl"] is not None else None,
        }
    return {
        "has_raw": True,
        "ym": target_ym,
        "records": scaled,
        "matched_count": len(scaled),
        "max_days": max_days,
    }


def _parse_iso_or_kr_date(s, default_year=None):
    txt = str(s or "").strip()
    if not txt:
        return None
    m = re.match(r"^(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?$", txt)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m2 = re.match(r"^(\d{1,2})\s*월\s*(\d{1,2})\s*일", txt)
    if m2:
        yr = default_year or datetime.now(KST).year
        try:
            return _dt.date(int(yr), int(m2.group(1)), int(m2.group(2)))
        except ValueError:
            return None
    return None


def parse_recent_daily_raw(values, masters):
    """'최근점_일별원시' 탭에 붙여넣은 BI 일자별 매출 리포트(상단 리포트 필터 메타 포함)를 파싱합니다."""
    if not values or len(values) < 2:
        return {"has_raw": False, "stores": [], "dates": [], "last_date": None, "report_ym": ""}

    # 리포트 필터 줄에 기간(년월) = 202611 이 있으면 연도 힌트로 추출
    hint_ym = ""
    for r in values[:8]:
        joined = " ".join(str(c or "") for c in r[:5])
        m = re.search(r"기간\(년월\)[^0-9]*(20[2-4]\d(?:0[1-9]|1[0-2]))", joined)
        if m:
            hint_ym = m.group(1)
            break
    hint_year = int(hint_ym[:4]) if len(hint_ym) >= 4 else datetime.now(KST).year

    first_date_row = -1
    for idx, r in enumerate(values):
        if r and _parse_iso_or_kr_date(r[0], default_year=hint_year) is not None:
            first_date_row = idx
            break
    if first_date_row < 1:
        return {"has_raw": False, "stores": [], "dates": [], "last_date": None, "report_ym": hint_ym}

    # 날짜 시작 행 위쪽 최대 6줄에서 점포코드·점포명 헤더 추출 ('리포트', '데이터 행', '매출액<' 줄은 제외)
    hdr_start = max(0, first_date_row - 6)
    hdr_rows = []
    for r in values[hdr_start:first_date_row]:
        c0 = str(r[0] if r else "").strip()
        if any(skip_kw in c0 for skip_kw in ("리포트", "데이터 행")):
            continue
        if "기간" in c0 or "일자" in c0:
            continue
        hdr_rows.append(r)

    max_cols = max((len(r) for r in (hdr_rows + values[first_date_row:first_date_row + 5])), default=0)
    lookup = _build_master_lookup(masters)

    col_defs = []
    for c_idx in range(1, max_cols):
        tokens = []
        for hr in hdr_rows:
            if c_idx < len(hr):
                t_val = str(hr[c_idx] or "").strip()
                if t_val and "매출액" not in t_val and t_val not in tokens:
                    tokens.append(t_val)
        if not tokens:
            continue
        codes = [tk for tk in tokens if re.match(r"^V?[A-Z]{2}\d{2,3}$", tk.upper())]
        names = [tk for tk in tokens if tk not in codes]
        raw_code = codes[0] if codes else ""
        raw_name = names[-1] if names else (codes[-1] if codes else "")
        if not raw_name:
            continue
        matched = _match_master_store(raw_name, raw_code, lookup)
        display_name = matched or raw_name
        col_defs.append({
            "col_idx": c_idx,
            "name": display_name,
            "raw_name": raw_name,
            "code": _norm_store_code(raw_code),
            "in_master": bool(matched),
        })

    if not col_defs:
        return {"has_raw": False, "stores": [], "dates": [], "last_date": None, "report_ym": hint_ym}

    # 날짜별 값 수집 (같은 날짜가 여러 번 나오면 아래쪽 최신 값 우선)
    by_date = {}
    sample_vals = []
    for r in values[first_date_row:]:
        if not r:
            continue
        d_obj = _parse_iso_or_kr_date(r[0], default_year=hint_year)
        if d_obj is None:
            continue
        d_map = by_date.setdefault(d_obj, {})
        for cd in col_defs:
            ci = cd["col_idx"]
            v = _num(r[ci]) if ci < len(r) else None
            if v is not None:
                d_map[cd["name"]] = v
                if v > 0:
                    sample_vals.append(v)

    if not by_date:
        return {"has_raw": False, "stores": [], "dates": [], "last_date": None, "report_ym": hint_ym}

    sample_vals.sort()
    scale = 1000.0 if (sample_vals and sample_vals[len(sample_vals) // 2] >= 50000.0) else 1.0

    sorted_dates = sorted(by_date.keys())
    # 실제 매출이 입력된 가장 마지막 영업일
    dates_with_sales = [d for d in sorted_dates if any((v or 0) > 0 for v in by_date[d].values())]
    last_active_date = dates_with_sales[-1] if dates_with_sales else sorted_dates[-1]
    active_dates = [d for d in sorted_dates if d <= last_active_date]

    seen_names = set()
    stores_out = []
    for cd in col_defs:
        sname = cd["name"]
        if sname in seen_names:
            continue
        seen_names.add(sname)
        daily_map = {}
        for d_obj in active_dates:
            raw_v = by_date[d_obj].get(sname)
            daily_map[d_obj] = (raw_v / scale) if raw_v is not None else None
        stores_out.append({
            "name": sname,
            "raw_name": cd["raw_name"],
            "code": cd["code"],
            "in_master": cd["in_master"],
            "daily_map": daily_map,
        })

    rep_ym = last_active_date.strftime("%Y%m") if last_active_date else hint_ym
    return {
        "has_raw": True,
        "stores": stores_out,
        "dates": active_dates,
        "last_date": last_active_date,
        "report_ym": rep_ym,
    }


@st.cache_data(ttl=300, show_spinner="실적 데이터를 불러오는 중...")
def load_perf(_get_gc):
    try:
        gc = _get_gc()
        doc = gc.open(PERF_SHEET_NAME)
    except Exception as e:
        return {"error": f"'{PERF_SHEET_NAME}' 시트를 열 수 없습니다. 서비스 계정에 공유했는지 확인해 주세요. ({e})"}

    def read_tab(title, auto_create=False):
        try:
            return doc.worksheet(title).get_all_values()
        except gspread.exceptions.WorksheetNotFound:
            if auto_create:
                try:
                    doc.add_worksheet(title=title, rows=1500, cols=26)
                except Exception:
                    pass
            return None
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
        raw_prom = (rec.get("추진주체") or rec.get("구분") or "").strip()
        promoter = "R&R" if "R&R" in raw_prom.upper().replace(" ", "") else (raw_prom or "특수개발")
        exp_margin = _pct_clean(rec.get("예상매익률") or rec.get("예상매익율") or _pick(rec, "예상매익"))
        store_code = _norm_store_code(rec.get("점포코드") or rec.get("코드") or "")
        m = {
            "name": name,
            "code": store_code,
            "manager": rec.get("담당자", "").strip(),
            "type": rec.get("운영형태", "").strip(),
            "open_date": rec.get("오픈일", "").strip(),
            "open_ym": _ym(rec.get("오픈일", "")),
            "region": rec.get("지역", "").strip(),
            "address": rec.get("주소", "").strip(),
            "place": rec.get("입지", "").strip(),
            "promoter": promoter,
            "exp_margin": exp_margin,
            # 지도용(선택 칸): 아직 좌표/업종을 안 채웠으면 그냥 빈 값으로 둠 -> 지도에서는 핀 없이 목록에만 나옴
            "lat": _num(rec.get("위도", "")),
            "lng": _num(rec.get("경도", "")),
            "biz_major": rec.get("업종대분류", "").strip(),
            "biz_minor": rec.get("업종세부", "").strip(),
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

    # 일일보고용 원시 탭 2개(없으면 빈 시트로 자동 생성해 두어 바로 붙여넣기 가능하게 함)
    recent_raw_v = read_tab(TAB_DAILY_RECENT, auto_create=True)
    recent_daily = parse_recent_daily_raw(recent_raw_v or [], masters)

    yesterday_ym = (datetime.now(KST).date() - _dt.timedelta(days=1)).strftime("%Y%m")
    fallback_cum_ym = recent_daily.get("report_ym") or yesterday_ym
    cum_raw_v = read_tab(TAB_DAILY_CUM, auto_create=True)
    daily_cum_meta = parse_daily_cum_raw(cum_raw_v or [], masters, fallback_ym=fallback_cum_ym)

    if daily_cum_meta.get("has_raw") and daily_cum_meta.get("ym"):
        c_ym = daily_cum_meta["ym"]
        for s_name, c_rec in daily_cum_meta["records"].items():
            prev_entry = monthly.setdefault(s_name, {}).get(c_ym, {})
            monthly[s_name][c_ym] = {
                "sales": c_rec["sales"],
                "days": c_rec["days"],
                "pl": c_rec["pl"] if c_rec["pl"] is not None else prev_entry.get("pl"),
                "op": prev_entry.get("op"),  # 월 마감 본부손익(영업이익)이 이미 있으면 그대로 보존
            }

    months = sorted({ym for per in monthly.values() for ym in per})
    return {"error": None, "masters": masters, "master_by_name": master_by_name,
            "monthly": monthly, "targets": targets, "months": months, "goals": goals, "grades": grades,
            "mgr_order": mgr_order, "mgr_hidden": mgr_hidden,
            "daily_cum_meta": daily_cum_meta, "recent_daily": recent_daily}


@st.cache_data(ttl=300, show_spinner="전사 특수점 데이터를 불러오는 중...")
def load_corp_stores(_get_gc):
    """'전사특수점' 탭(선택): 지도 전용 데이터. 탭이 아직 없거나 비어 있으면 그냥 빈 목록을 돌려줍니다(오류 아님).
    기대 열(이름이 비슷하면 알아서 매칭): 점포명 | 브랜드 | 위도 | 경도 | 업종대분류 | 업종세부 | 일매출 | 폐점여부 | 오픈일 | 주소"""
    try:
        gc = _get_gc()
        doc = gc.open(PERF_SHEET_NAME)
        values = doc.worksheet(TAB_CORP).get_all_values()
    except Exception:
        return []

    rows = []
    for rec in _records(values):
        name = rec.get("점포명", "").strip()
        if not name:
            continue
        closed = rec.get("폐점여부", "").strip()
        if closed and closed[0] in ("Y", "y", "폐"):
            continue
        rows.append({
            "name": name,
            "brand": rec.get("브랜드", "").strip() or "GS25",
            "lat": _num(rec.get("위도", "")),
            "lng": _num(rec.get("경도", "")),
            "biz_major": rec.get("업종대분류", "").strip(),
            "biz_minor": rec.get("업종세부", "").strip(),
            "daily_sales": _num(rec.get("일매출", "")),
            "address": rec.get("주소", "").strip(),
            "open_date": rec.get("오픈일", "").strip(),
        })
    return rows


def ordered_managers(D):
    """화면에 보여줄 담당자 목록: '담당자' 탭의 순서 -> 탭에 없는 사람은 뒤에 가나다순, 사용여부 N 은 제외"""
    hidden = D.get("mgr_hidden", set())
    order = D.get("mgr_order", {})
    names = {m["manager"] for m in D["masters"] if m["manager"] and m["manager"] not in hidden}
    return sorted(names, key=lambda n: (n not in order, order.get(n, 0), n))


def ordered_managers_for(D, names):
    """ordered_managers 와 같지만, 주어진 점포명 목록(names)을 담당한 담당자만 추려서 돌려줍니다.
    (예: 선택한 연도에 오픈한 점포만 names 로 넘기면 -> 그 연도에 오픈 실적이 있는 담당자만 나옴)"""
    hidden = D.get("mgr_hidden", set())
    order = D.get("mgr_order", {})
    name_set = set(names)
    mgrs = {m["manager"] for m in D["masters"]
            if m["manager"] and m["manager"] not in hidden and m["name"] in name_set}
    return sorted(mgrs, key=lambda n: (n not in order, order.get(n, 0), n))


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


def calc_group(D, names, yms, open_yms=None):
    """yms: 매출/영업이익 등 실적 합산에 쓸 기간(기준연도). open_yms: 출점수 집계용 기간(조회 연도) -
    따로 안 주면 yms 와 같게 씁니다. 두 기간을 분리한 이유는 '조회 연도(어떤 점포가 보이는지)'와
    '기준연도(그 점포들의 매출을 어느 해 걸로 보여줄지)'가 서로 다를 수 있기 때문입니다."""
    open_yms = yms if open_yms is None else open_yms
    end = open_yms[-1] if open_yms else ""
    start = open_yms[0] if open_yms else ""
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
       box-shadow: 0 3px 10px rgba(31,58,115,.08); min-width: 0; position: relative; }
.kpi .kpi-spark { position: absolute; top: 30px; right: 14px; opacity: .92; }
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
/* 하위 메뉴(2단계: 내 보고 작성/팀 현황/팀 활동계획, 입찰관리, 자료실 등) - 큰 메뉴(1단계)와 톤을 구분하기 위해 알약을 버리고
   '밑줄 탭' 모양으로: 선택 = 밑줄 + 굵은 네이비 글자, 미선택 = 연한 회색 글자, 배경 없음 */
div[class*="st-key-lvl2tabs"] [data-baseweb="tab-list"],
div[class*="st-key-lvl2tabs"] [role="tablist"],
div[class*="st-key-lvl2tabs"] [data-testid="stTabs"] > div:first-child {
    background: transparent !important; padding: 0 !important; border-radius: 0 !important;
    gap: 22px !important; border-bottom: 2px solid #e3e8f2 !important; }
div[class*="st-key-lvl2tabs"] button[data-baseweb="tab"],
div[class*="st-key-lvl2tabs"] button[role="tab"],
div[class*="st-key-lvl2tabs"] [data-testid="stTab"] {
    height: 40px !important; padding: 0 2px !important; border-radius: 0 !important; background: transparent !important;
    box-shadow: none !important; border-bottom: 3px solid transparent !important; margin-bottom: -2px; }
div[class*="st-key-lvl2tabs"] button[role="tab"] p,
div[class*="st-key-lvl2tabs"] button[role="tab"] div,
div[class*="st-key-lvl2tabs"] button[role="tab"] span,
div[class*="st-key-lvl2tabs"] [data-testid="stTab"] p {
    font-size: 15.5px !important; font-weight: 600 !important; color: #8591ab !important; }
div[class*="st-key-lvl2tabs"] button[role="tab"]:hover,
div[class*="st-key-lvl2tabs"] [data-testid="stTab"]:hover { background: transparent !important; }
div[class*="st-key-lvl2tabs"] button[role="tab"]:hover p,
div[class*="st-key-lvl2tabs"] [data-testid="stTab"]:hover p { color: #55648a !important; }
div[class*="st-key-lvl2tabs"] button[role="tab"][aria-selected="true"],
div[class*="st-key-lvl2tabs"] [data-testid="stTab"][aria-selected="true"] {
    background: transparent !important; box-shadow: none !important; border-bottom: 3px solid #2c4a8a !important; }
div[class*="st-key-lvl2tabs"] button[role="tab"][aria-selected="true"] p,
div[class*="st-key-lvl2tabs"] button[role="tab"][aria-selected="true"] div,
div[class*="st-key-lvl2tabs"] button[role="tab"][aria-selected="true"] span,
div[class*="st-key-lvl2tabs"] [data-testid="stTab"][aria-selected="true"] p {
    color: #1f3a73 !important; font-weight: 800 !important; }
/* 하위 메뉴(3단계: 계약완료·오픈확정 물건/추진 물건, 팀원 화면 전용) - 2단계보다도 더 가볍게,
   탭이라기보단 같은 화면 안의 토글처럼: 배경·알약 없이 글자 + 선택된 것만 얇은 밑줄 */
.st-key-lvl3tabs [data-baseweb="tab-list"], .st-key-lvl3tabs [role="tablist"] {
    background: transparent !important; padding: 0 !important; border-radius: 0 !important; gap: 18px !important; }
.st-key-lvl3tabs button[data-baseweb="tab"], .st-key-lvl3tabs button[role="tab"] {
    height: 32px !important; padding: 0 2px !important; border-radius: 0 !important; background: transparent !important;
    border-bottom: 2px solid transparent !important; }
.st-key-lvl3tabs button[role="tab"] p, .st-key-lvl3tabs button[role="tab"] div, .st-key-lvl3tabs button[role="tab"] span {
    font-size: 13.5px !important; font-weight: 600 !important; color: #9aa5bd !important; }
.st-key-lvl3tabs button[role="tab"]:hover { background: transparent !important; }
.st-key-lvl3tabs button[role="tab"]:hover p { color: #55648a !important; }
.st-key-lvl3tabs button[role="tab"][aria-selected="true"] { background: transparent !important; box-shadow: none !important;
    border-bottom: 2px solid #55648a !important; }
.st-key-lvl3tabs button[role="tab"][aria-selected="true"] p, .st-key-lvl3tabs button[role="tab"][aria-selected="true"] div,
.st-key-lvl3tabs button[role="tab"][aria-selected="true"] span { color: #2c4a8a !important; font-weight: 800 !important; }
.st-key-mgr_filter { margin-top: 18px; }
.st-key-mgr_filter [data-testid="stHorizontalBlock"] {
    align-items: flex-start !important; justify-content: flex-start !important; gap: 16px !important; }
.st-key-mgr_filter [data-testid="stColumn"], .st-key-mgr_filter [data-testid="column"] {
    flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
/* 점포 카드 줄: 칸 3개를 똑같은 너비로 (위 담당자 필터용 '내용 크기대로' 규칙이 카드 칸에 번지지 않게 함) */
div[class*="st-key-mgrbox_"] [data-testid="stHorizontalBlock"] { align-items: stretch; gap: 1rem; }
.sc-top { min-height: 100px; }
/* 점포 상세의 '← 점포 목록으로' 버튼: 오픈점 보기 버튼과 같은 알약 디자인 */
div[class*="st-key-backbtn_"] button { height: 44px; min-height: 44px; border: none !important; border-radius: 10px !important;
    background: #2c4a8a !important; padding: 0 22px; box-shadow: 0 3px 10px rgba(31,58,115,.25); }
div[class*="st-key-backbtn_"] button p { font-weight: 800 !important; font-size: 16px !important; color: #ffffff !important; white-space: nowrap; margin: 0; }
div[class*="st-key-backbtn_"] button svg, div[class*="st-key-backbtn_"] button [data-testid="stIconMaterial"] { color: #ffffff !important; }
div[class*="st-key-backbtn_"] button:hover { background: #1e3a6f !important; }
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
/* 영업이익 / 달성률 TOP5·주의 랭킹 카드 (버튼 기반: 점포명 클릭 시 세션 상태만 바꿔서 같은 탭에서 바로 펼침) */
div[class*="st-key-rkcard_"] { background: #ffffff; border: 1px solid #d5dcea; border-radius: 14px;
    padding: 16px 20px 16px; margin: 8px 0 14px; height: 100%; }
/* 카드 안 TOP5 / 주의 필요 두 칸 사이 간격만 좀 좁게 */
div[class*="st-key-rkcard_"] [data-testid="stHorizontalBlock"] { gap: 14px !important; }
div[class*="st-key-rkcard_"] [data-testid="stColumn"] { min-width: 0 !important; }
/* 앱 전체 기본 세로 간격(0.7rem)이 랭킹 행 사이에도 그대로 적용돼서 버튼들이 전보다 훨씬 헐렁해 보였던 부분 보정:
   랭킹 카드 안쪽 세로 블록만 간격을 2px로 확 좁혀서 원래의 촘촘한 목록 느낌으로 되돌림 */
div[class*="st-key-rkcard_"] [data-testid="stVerticalBlock"] { gap: 2px !important; }
/* 랭킹으로 접기 버튼: 점포 목록으로 버튼과 같은 네이비 알약 스타일 */
div[class*="st-key-rkclose_"] button { height: 40px; min-height: 40px; border: none !important; border-radius: 10px !important;
    background: #2c4a8a !important; padding: 0 18px; box-shadow: 0 3px 10px rgba(31,58,115,.25); }
div[class*="st-key-rkclose_"] button p { font-weight: 800 !important; font-size: 15px !important; color: #ffffff !important; white-space: nowrap; margin: 0; }
div[class*="st-key-rkclose_"] button svg, div[class*="st-key-rkclose_"] button [data-testid="stIconMaterial"] { color: #ffffff !important; }
div[class*="st-key-rkclose_"] button:hover { background: #1e3a6f !important; }
.rk-t { display: flex; align-items: center; gap: 8px; font-size: 17px; font-weight: 800; color: #1f3a73; }
.rk-n { font-size: 12.5px; color: #8591ab; margin: 4px 0 10px; }
.rk-h { font-size: 12.5px; font-weight: 800; margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid #edf0f6; }
.rk-h.good { color: #0b9a6a; }
.rk-h.bad { color: #d0143c; }
.rk-sep { height: 1px; background: #edf0f6; margin: 14px 0 10px; }
.rk-empty { font-size: 12.5px; color: #aab4ca; padding: 6px 0; }
.rk-detail { margin-top: 14px; padding-top: 14px; border-top: 1px dashed #d5dcea; }
/* 랭킹 점포명 버튼: 일반 st.button을 아주 얇은 '행'처럼 보이게 꾸밈 */
div[class*="st-key-rk_"] { margin-bottom: 0; }
div[class*="st-key-rk_"] button { min-height: 27px; height: 27px; width: auto !important; border: none !important;
    background: transparent !important; padding: 0 6px; border-radius: 6px; justify-content: flex-start !important; }
div[class*="st-key-rk_"] button p { font-size: 13.5px !important; font-weight: 600 !important; color: #2c4a8a !important;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0; text-align: left; }
div[class*="st-key-rk_"] button:hover { background: #eef2fa !important; }
div[class*="st-key-rk_"] button:hover p { text-decoration: underline; text-decoration-color: #c7d2ea; }
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
.store-head .sh-addr { font-size: 14px; color: #2c4a8a; margin-top: 10px; padding-top: 10px; border-top: 1px dashed #d5dcea;
    display: inline-flex; align-items: center; gap: 2px; text-decoration: none; font-weight: 600; }
.store-head .sh-addr:hover { color: #1e88e5; text-decoration: underline; }
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
/* 주간 활동 보고 - 팀 현황 표 (가운데 정렬 + 색깔 헤더로, 실적현황 쪽 표와 통일) */
.wk-tbl-wrap { overflow-x: auto; margin: 6px 0 16px; border: 1px solid #d5dcea; border-radius: 12px; }
table.wk-tbl { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; }
table.wk-tbl th { background: linear-gradient(90deg, #eef3fc, #f8faff); color: #1f3a73; font-weight: 800;
    padding: 9px 10px; text-align: center; white-space: nowrap; border-bottom: 2px solid #c9d4ea; }
table.wk-tbl td { padding: 8px 10px; text-align: center; vertical-align: middle; border-bottom: 1px solid #edf0f6; color: #1f2d4d; }
table.wk-tbl tr:last-child td { border-bottom: none; }
table.wk-tbl tr:nth-child(even) td { background: #fafbfd; }
table.wk-tbl td.wk-prog { max-width: 360px; white-space: normal; text-align: left; line-height: 1.5; }
table.wk-tbl td.wk-day { font-weight: 700; color: #2c4a8a; background: #f3f6fc !important; }
table.wk-tbl td.wk-empty { color: #aab4ca; padding: 16px; }
.wk-badge { display: inline-block; font-size: 12px; font-weight: 800; border-radius: 999px; padding: 3px 11px; white-space: nowrap; }
.wk-badge.open { background: #e3f3ec; color: #0b9a6a; }
.wk-badge.done { background: #e8edfb; color: #2c4a8a; }
.wk-badge.prog { background: #fef1df; color: #b25e00; }
.wk-badge.new { background: #ffe3e3; color: #d0143c; }
/* 제출 현황 카드: 실적현황 카드(.kpi)와 같은 느낌이지만, '제출 현황' 제목 글자 크기 정도로 작게 */
.subm-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 6px 0 16px; }
.subm-card { --ac: #2c4a8a; background: #ffffff; border: 1px solid #dde3ef; border-left: 4px solid var(--ac);
    border-radius: 10px; padding: 9px 12px 8px; box-shadow: 0 2px 7px rgba(31,58,115,.07); min-width: 0; }
.subm-card .sb-l { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 700; color: #55648a; }
.subm-card .sb-l svg { flex: none; }
.subm-card .sb-v { font-size: 15px; font-weight: 800; margin-top: 3px; }
.subm-card .sb-v.ok { color: #0b9a6a; }
.subm-card .sb-v.no { color: #d0143c; }
.subm-card .sb-s { font-size: 11px; color: #8591ab; margin-top: 2px; }
/* 자료실 (구글 드라이브 연동) */
div[class*="st-key-lib_pills_"] [data-testid="stWidgetLabel"] { display: none !important; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] {
    display: flex !important; flex-direction: row !important; flex-wrap: wrap !important; gap: 6px !important;
    align-items: center; background: #eef2fa; padding: 5px; border-radius: 12px; width: fit-content; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label {
    margin: 0 !important; height: 34px; padding: 0 16px !important; border-radius: 9px; cursor: pointer;
    display: flex !important; align-items: center; justify-content: center; background: transparent; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label > div:first-child { display: none !important; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label p {
    font-size: 14px !important; font-weight: 700 !important; color: #55648a !important; margin: 0 !important; white-space: nowrap; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label:hover { background: #dfe7f6; }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label:has(input:checked) {
    background: #2c4a8a !important; box-shadow: 0 2px 6px rgba(31,58,115,.22); }
div[class*="st-key-lib_pills_"] [role="radiogroup"] > label:has(input:checked) p { color: #ffffff !important; }
.st-key-lib_bulkbar { background: linear-gradient(90deg, #eef3fc, #f8faff); border: 1px solid #d5dcea;
    border-radius: 12px; padding: 10px 16px !important; margin: 4px 0 8px; }
.st-key-lib_bulkbar [data-testid="stHorizontalBlock"] { align-items: center !important; }
div[class*="st-key-librow_"] { background: #ffffff; border: 1px solid #dfe6f3; border-radius: 12px;
    padding: 10px 16px !important; box-shadow: 0 2px 6px rgba(31,58,115,.04);
    transition: border-color .15s ease, box-shadow .15s ease; }
div[class*="st-key-librow_"]:hover { border-color: #b5c4e3; box-shadow: 0 4px 12px rgba(31,58,115,.09); }
div[class*="st-key-librow_"] [data-testid="stHorizontalBlock"] { align-items: center !important; }
.lib-item { display: flex; align-items: center; gap: 10px; min-width: 0; flex-wrap: wrap; }
.lib-ext { display: inline-flex; align-items: center; justify-content: center; min-width: 44px; height: 24px;
    padding: 0 8px; border-radius: 6px; font-size: 11px; font-weight: 800; letter-spacing: .3px; flex: none; }
.lib-ext.pdf { background: #fde8ec; color: #c9184a; }
.lib-ext.hwp { background: #e3f2fd; color: #0277bd; }
.lib-ext.xls { background: #e3f3ec; color: #0b9a6a; }
.lib-ext.ppt { background: #fff0e0; color: #d95f02; }
.lib-ext.doc { background: #e8edfb; color: #2c4a8a; }
.lib-ext.img { background: #f1e8fb; color: #6a35b5; }
.lib-ext.zip { background: #eaeef7; color: #1f3a73; }
.lib-ext.etc { background: #eef2fa; color: #55648a; }
.lib-title { font-size: 15px; font-weight: 700; color: #1f2d4d; word-break: break-all; line-height: 1.35; }
.lib-cat { font-size: 11.5px; font-weight: 700; color: #2c4a8a; background: #eaf0fb; border-radius: 999px;
    padding: 2px 10px; white-space: nowrap; flex: none; }
.lib-store { font-size: 12px; font-weight: 800; color: #ffffff; background: linear-gradient(135deg, #2c4a8a, #3a5fa8);
    border-radius: 999px; padding: 3px 11px; white-space: nowrap; flex: none; }
.lib-meta { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap;
    font-size: 12.5px; color: #6b7a99; font-weight: 600; }
.lib-date { color: #3a4a6b; font-weight: 700; white-space: nowrap; }
.lib-fresh { font-size: 11px; font-weight: 800; border-radius: 999px; padding: 2px 8px; white-space: nowrap; }
.lib-fresh.new { background: #e3f3ec; color: #0b9a6a; }
.lib-fresh.ok { background: #eef2fa; color: #2c4a8a; }
.lib-fresh.old { background: #fef1df; color: #b25e00; }
.lib-size { font-size: 12px; color: #8591ab; min-width: 58px; text-align: right; white-space: nowrap; }
.lib-sel-badge { display: inline-flex; align-items: center; height: 36px; padding: 0 14px; border-radius: 999px;
    background: #ffffff; border: 1px solid #c9d4ea; font-size: 13.5px; font-weight: 700; color: #2c4a8a; }
.lib-sel-badge b { color: #0b9a6a; margin: 0 3px; font-weight: 800; }
.lib-setup { background: #ffffff; border: 1px solid #d5dcea; border-left: 6px solid #2c4a8a; border-radius: 14px;
    padding: 20px 24px; box-shadow: 0 4px 14px rgba(31,58,115,.07); margin-top: 8px; }
.lib-setup h4 { margin: 0 0 8px; font-size: 17px; font-weight: 800; color: #1f3a73; }
.lib-setup p { margin: 4px 0 12px; font-size: 13.5px; color: #55648a; line-height: 1.55; }
.lib-tree { background: #f6f8fc; border: 1px solid #e1e7f3; border-radius: 10px; padding: 12px 16px;
    font-family: Consolas, monospace; font-size: 13px; color: #1f3a73; line-height: 1.6; white-space: pre; overflow-x: auto; }
/* 입찰 관리 배지 및 추출 바 (+ 폼 기본 제출 버튼 primaryFormSubmit 흰색 글씨 보장) */
div[data-testid="stFormSubmitButton"] button[kind*="primary"],
div[data-testid="stFormSubmitButton"] button[data-testid*="primary"] {
    background-color: #2c4a8a !important; border-color: #2c4a8a !important; color: #ffffff !important; }
div[data-testid="stFormSubmitButton"] button[kind*="primary"]:hover,
div[data-testid="stFormSubmitButton"] button[data-testid*="primary"]:hover {
    background-color: #1f3a73 !important; border-color: #1f3a73 !important; color: #ffffff !important; }
div[data-testid="stFormSubmitButton"] button[kind*="primary"] *,
div[data-testid="stFormSubmitButton"] button[data-testid*="primary"] * {
    color: #ffffff !important; fill: #ffffff !important; }
table.wk-tbl td.wk-num { white-space: nowrap; }
/* 최근 오픈점 일별 추이 미니 차트 카드 */
.rc-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 10px 0 18px; }
@media (max-width: 960px) { .rc-grid { grid-template-columns: minmax(0, 1fr); } }
.rc-card { background: #ffffff; border: 1px solid #d5dcea; border-radius: 14px; padding: 14px 16px 10px;
    box-shadow: 0 3px 10px rgba(31,58,115,.06); min-width: 0; }
.rc-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
.rc-title-wrap { min-width: 0; }
.rc-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 17px; font-weight: 800; color: #1f3a73; }
.rc-rr { display: inline-block; font-size: 10.5px; font-weight: 800; background: #fff3bf; color: #8a5a00;
    border: 1px solid #f3d26b; border-radius: 5px; padding: 1px 6px; line-height: 1.3; }
.rc-sub { font-size: 12px; color: #6b7a99; font-weight: 600; margin-top: 2px; }
.rc-kpis { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; flex: none; }
.rc-avg { text-align: right; margin-right: 4px; }
.rc-avg .v { font-size: 18px; font-weight: 800; color: #1f2d4d; line-height: 1.1; }
.rc-avg .v small { font-size: 11px; color: #7d8aa8; font-weight: 600; margin-left: 2px; }
.rc-avg .s { font-size: 11px; color: #8591ab; font-weight: 600; }
.rc-rate { font-size: 12.5px; font-weight: 800; border-radius: 999px; padding: 3px 10px; white-space: nowrap; }
.rc-rate.good { background: #e3f3ec; color: #0b9a6a; }
.rc-rate.warn { background: #fef1df; color: #b25e00; }
.rc-rate.bad { background: #fde8ec; color: #d0143c; }
.rc-rate.na { background: #eef2fa; color: #6b7a99; }
.rc-trend { font-size: 11px; font-weight: 800; border-radius: 6px; padding: 2px 7px; white-space: nowrap; }
.rc-trend.up { background: #ebfbee; color: #0b9a6a; }
.rc-trend.dn { background: #fff5f5; color: #d0143c; }
.rc-trend.flat { background: #f1f3f8; color: #6b7a99; }
.rc-card svg { width: 100%; height: auto; display: block; margin-top: 2px; }

.wk-badge.win { background: #e3f3ec; color: #0b9a6a; }
.wk-badge.lose { background: #fde8ec; color: #d0143c; }
.wk-badge.wait { background: #e8edfb; color: #2c4a8a; }
.wk-badge.void { background: #fef1df; color: #b25e00; }
.wk-badge.skip { background: #eef1f6; color: #7d8aa8; }
.st-key-bid_export_bar { background: linear-gradient(90deg, #eef3fc, #f8faff); border: 1px solid #d5dcea;
    border-radius: 12px; padding: 10px 16px !important; margin: 4px 0 10px; }
.st-key-bid_export_bar [data-testid="stHorizontalBlock"] { align-items: center !important; }
.bid-comp-chip { display: inline-block; font-size: 11px; font-weight: 700; color: #3a4a6b; background: #eef2fa;
    border-radius: 6px; padding: 1px 6px; margin: 1px 2px; white-space: nowrap; }
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
    elif kind == "folder":
        b = (f'<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.8l1.6 2H19.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z" fill="{color}"/>')
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
        spark = f'<div class="kpi-spark" title="월별 출점 흐름(0점인 달은 점으로만 표시)">{it["spark"]}</div>' \
            if it.get("spark") else ""
        out.append(
            f'<div class="kpi{nb}" style="--ac:{ac}">{spark}<div class="kpi-l">{ic}{it["label"]}</div>'
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


def _open_flow_svg(D, names, yms, w=150, h=42):
    """월별 신규 출점수 미니 막대그래프. 출점이 없는 달은 막대 대신 작은 회색 점만 찍어서
    '몇 개월째 출점이 없는지' 공백이 바로 눈에 보이게 함. yms 가 2개월 이상일 때만 의미가 있음"""
    if len(yms) < 2:
        return ""
    counts = []
    for ym in yms:
        c = sum(1 for n in names if D["master_by_name"].get(n, {}).get("open_ym") == ym)
        counts.append((ym, c))
    mx = max(c for _, c in counts)
    if mx == 0:
        return ""
    n = len(counts)
    bw = w / n
    parts = []
    for i, (ym, c) in enumerate(counts):
        cx = i * bw + bw / 2
        bw2 = min(bw * 0.62, 14)
        x = cx - bw2 / 2
        lbl = ym_label(ym)
        if c > 0:
            bh = max(3.0, c / mx * (h - 9))
            y = h - bh
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw2:.1f}" height="{bh:.1f}" rx="1.5" '
                         f'fill="{COLOR_NAVY}"><title>{lbl} · {c}점</title></rect>')
        else:
            parts.append(f'<circle cx="{cx:.1f}" cy="{h - 2:.1f}" r="1.6" fill="#c7cedd">'
                          f'<title>{lbl} · 출점 없음</title></circle>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="display:block">'
            + "".join(parts) + "</svg>")


def group_cards(G, mode, P=None, D=None, who="팀", flow_svg="", metric_label=""):
    """P: 전월 계산 결과(월별 보기에서만 전달), D/who: 목표 조회용
    flow_svg: 출점수 카드 우측 상단에 넣을 월별 출점 흐름 미니 그래프(선택, _open_flow_svg 로 생성)
    metric_label: 일매출 카드에 '어느 해 매출인지' 표시(예: '26년 매출')"""
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
             delta=open_bot + dl(G["margin"], pv("margin"), "pp", "매익율"),
             spark=flow_svg or None),
        dict(label="일매출", value=fnum(G["daily"]), unit="천원", sub=metric_label, accent=COLOR_BLUE, icon="coin",
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


def pick_period(D):
    """조회 연도 바: 담당자 실적은 보통 '그 해 1년치 누계'만 보기 때문에, 예전의 [보기 방식(월별/누계)] +
    [월~월 선택] 대신 연도 하나만 고르면 그 해 1월 ~ (올해면 최신월, 지난해면 12월까지) 누계를 바로 보여줍니다.
    월별로 쪼개 보는 화면(점포 상세의 월별 흐름 그래프 등)은 이 선택과 무관하게 그대로 동작합니다."""
    months = D["months"]
    years = sorted({m[:4] for m in months} |
                   {mm["open_ym"][:4] for mm in D["masters"] if mm.get("open_ym")}, reverse=True)
    if not years:
        years = [str(datetime.now().year)]
    if st.session_state.get("tool_year") not in years:
        st.session_state["tool_year"] = years[0]
    with st.container(key="periodbar"):
        head = st.empty()
        with st.container(key="periodbody"):
            l1, c1, c5 = st.columns([1, 1, 6])
            with l1:
                st.markdown('<div class="pb-lab">조회 연도</div>', unsafe_allow_html=True)
            with c1:
                year = st.selectbox("조회 연도", years, format_func=lambda y: f"{y}년",
                                    key="tool_year", label_visibility="collapsed")
            with c5:
                refresh_button("tool_refresh")
        yms = [m for m in months if m[:4] == year]
        text = (f"{year}년 누계 ({ym_label(yms[0])} ~ {ym_label(yms[-1])})" if yms
                else f"{year}년 누계 (실적 데이터 없음)")
        head.markdown(
            f'<div class="pb-head"><div class="pb-title"><span class="ic">{mini_icon("cal", "#ffffff", 18)}</span>조회 연도</div>'
            f'<div class="pb-chip">{text}</div></div>', unsafe_allow_html=True)
    return yms, text, year


def metric_period(D):
    """실적 수치(일매출/매익율/영업이익 등)에 쓸 '기준 연도'. 위의 조회 연도(오픈연도 기준 필터)와는
    별개로, 언제나 '오늘 날짜 기준 올해' 데이터를 보여줍니다 - 예전에 오픈한 점포라도 '지금 얼마를
    팔고 있는지'를 확인하려는 용도입니다. (아직 입력 안 된 달은 그냥 빈 칸으로 남습니다.)
    내년에 새 연도 오픈점이 생기면, 그 시점에 다시 요청해주시면 '예전 오픈점은 올해(완결된 해)에
    고정, 새 연도 오픈점만 그 해'로 나누는 방식으로 조정할 수 있습니다."""
    year = str(datetime.now(KST).year)
    yms = [m for m in D["months"] if m[:4] == year]
    short = f"{year[2:]}년 매출"
    if yms:
        full = f"{year}년 매출 ({ym_label(yms[0])} ~ {ym_label(yms[-1])})"
    else:
        full = f"{year}년 매출 (아직 입력된 실적 없음)"
    return yms, year, full, short


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
    if m.get("address"):
        map_url = f'https://map.kakao.com/?q={escape(m["address"])}'
        addr = (f'<a class="sh-addr" href="{map_url}" target="_blank" rel="noopener" '
                f'title="카카오맵에서 보기">{mini_icon("pin", COLOR_BAD, 16)} {escape(m["address"])}</a>')
    else:
        addr = ""
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
        with st.container(key=f"backbtn_{ctx}_b"):
            icon_button("점포 목록으로", ":material/arrow_back:", key=f"tool_back_{ctx}_b", on_click=_back)
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


def summary_html(D, managers, yms, open_yms, all_names):
    """요약: 담당자별 출점수 / 일매출 / 예상매출 달성점 구성비 / 흑자점 구성비 비교 그래프 4장.
    yms: 매출 등 실적 집계 기준연도 / open_yms: 출점수 집계용 조회 연도"""
    G = []
    for mgr in managers:
        names = [n for n in all_names if D["master_by_name"].get(n, {}).get("manager") == mgr]
        G.append((mgr, calc_group(D, names, yms, open_yms=open_yms)))
    T = calc_group(D, all_names, yms, open_yms=open_yms)

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


def _rank_items(D, names, yms, metric):
    """metric: 'op_avg'(월평균 영업이익) / 'rate'(예상매출 달성률).
    달성률은 선택 기간의 마지막 달에 새로 오픈한 점포(실적이 아직 한 달도 안 쌓인 곳)는 빼고 비교"""
    last = yms[-1] if yms else ""
    out = []
    for n in names:
        if metric == "rate":
            open_ym = D["master_by_name"].get(n, {}).get("open_ym", "")
            if open_ym and open_ym == last:
                continue
            c = calc_store(D, n, yms)
            if c["rate"] is not None:
                out.append((n, c["rate"]))
        else:
            per = D["monthly"].get(n, {})
            ops = [per[ym]["op"] for ym in yms if ym in per and per[ym]["op"] is not None]
            if ops:
                out.append((n, sum(ops) / len(ops)))
    return out


def _rank_split(items, metric, k=5):
    """단순 순위가 아니라 '실제로 좋은지/나쁜지' 기준으로 나눔.
    영업이익(op_avg): TOP = 흑자(0 이상)만, Worst = 적자(0 미만)만
    달성률(rate): TOP = 80% 이상만, Worst = 80% 미만만
    -> 전원이 흑자/달성이면 Worst 가 0개일 수 있고, 그게 정상"""
    if metric == "rate":
        good_pool = [it for it in items if it[1] >= ACHIEVE_PCT]
        bad_pool = [it for it in items if it[1] < ACHIEVE_PCT]
    else:
        good_pool = [it for it in items if it[1] >= 0]
        bad_pool = [it for it in items if it[1] < 0]
    good = sorted(good_pool, key=lambda x: -x[1])[:k]
    bad = sorted(bad_pool, key=lambda x: x[1])[:k]
    return good, bad


_RANK_DOT = {COLOR_GOOD: "🟢", COLOR_WARN: "🟠", COLOR_BAD: "🔴"}


def _rank_rows(items, unit, color_fn, dec, state_key, prefix):
    """랭킹 한 칸(TOP 또는 주의) 안의 점포 버튼들. 누르면 바로 아래 상세가 펼쳐짐(페이지 이동 없음)"""
    if not items:
        st.markdown('<div class="rk-empty">표시할 점포가 없습니다</div>', unsafe_allow_html=True)
        return
    for i, (n, v) in enumerate(items):
        dot = _RANK_DOT.get(color_fn(v), "⚪")
        label = f"{dot} {n}  ·  {fnum(v, dec)}{unit}"
        with st.container(key=f"{prefix}_{i}"):
            if st.button(label, key=f"{prefix}_btn_{i}"):
                st.session_state[state_key] = n
                st.rerun()


def _rank_metric_block(title, icon, note, good, bad, unit, color_fn, dec, state_key, prefix):
    st.markdown(f'<div class="rk-t">{mini_icon(icon, COLOR_NAVY, 20)}{title}</div>'
                f'<div class="rk-n">{note}</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f'<div class="rk-h good">TOP {len(good)}</div>', unsafe_allow_html=True)
        _rank_rows(good, unit, color_fn, dec, state_key, f"{prefix}_g")
    with c2:
        st.markdown(f'<div class="rk-h bad">WORST {len(bad)}</div>', unsafe_allow_html=True)
        _rank_rows(bad, unit, color_fn, dec, state_key, f"{prefix}_b")


def render_ranking(D, names, yms, period_text, state_key, qkey="team"):
    """영업이익 월평균 / 예상매출 달성률 TOP5 · Worst5 (팀 전체·담당자별 공통).
    두 지표 카드는 처음 레이아웃처럼 한 줄에 나란히 두고, 점포명 버튼을 누르면 바로 아래에 그 점포의
    상세가 펼쳐짐(세션 상태만 바꾸는 방식이라 로그인 화면으로 튕기지 않음)"""
    og, ob = _rank_split(_rank_items(D, names, yms, "op_avg"), "op_avg")
    rg, rb = _rank_split(_rank_items(D, names, yms, "rate"), "rate")
    m1, m2 = st.columns(2)
    with m1:
        with st.container(key=f"rkcard_{qkey}_op"):
            _rank_metric_block("월평균 영업이익", "coin", "영업이익이 입력된 달만으로 계산한 한 달 평균 · TOP=흑자점, Worst=적자점",
                                og, ob, "천원", lambda v: COLOR_GOOD if v >= 0 else COLOR_BAD, 0,
                                state_key, f"rk_{qkey}_op")
    with m2:
        with st.container(key=f"rkcard_{qkey}_rate"):
            _rank_metric_block("예상매출 달성률", "target", "당월 신규 오픈점은 제외 · TOP=80%↑, Worst=80%↓",
                                rg, rb, "%", rate_color, 1, state_key, f"rk_{qkey}_rate")

    sel = st.session_state.get(state_key)
    if sel and sel in names:
        with st.container():
            st.markdown('<div class="rk-detail"></div>', unsafe_allow_html=True)

            def _close():
                st.session_state[state_key] = None
            with st.container(key=f"rkclose_{qkey}"):
                icon_button("랭킹으로 접기 ▲", ":material/expand_less:", key=f"{state_key}_close", on_click=_close)
            render_store_detail(D, sel, yms, period_text)


def _store_target_for_ym(D, name, ym):
    """점포의 특정 연월(ym) 당월 예상일매출을 반환합니다(해당 차수 k가 없으면 M(0차수) 값으로 폴백)."""
    m = D["master_by_name"].get(name, {})
    open_ym = m.get("open_ym", "")
    tgt_map = D["targets"].get(name, {}).get("daily", {})
    if not tgt_map:
        return None
    if open_ym and ym:
        k = _month_diff(open_ym, ym)
        if k in tgt_map:
            return tgt_map[k]
        if k >= 0 and tgt_map:
            nearest_k = min(tgt_map.keys(), key=lambda x: abs(x - k))
            return tgt_map[nearest_k]
    return tgt_map.get(0)


def _enrich_recent_store(D, st_item, all_dates, report_ym):
    """최근 오픈점 1개의 일자별 시계열, 당월 평균, 예상일매출, 달성률, 최근 추세를 계산합니다."""
    name = st_item["name"]
    m = D["master_by_name"].get(name, {})
    open_date_str = m.get("open_date", "")
    open_dt = _parse_iso_or_kr_date(open_date_str)
    target = _store_target_for_ym(D, name, report_ym)
    daily_map = st_item["daily_map"]

    # 점포 오픈일 이전 빈 날짜는 제외해 오픈 첫날부터의 실제 추이가 선명하게 보이도록 구성
    start_dt = all_dates[0]
    if open_dt and open_dt > start_dt:
        start_dt = open_dt
    else:
        first_sale_dates = [d for d in all_dates if (daily_map.get(d) or 0) > 0]
        if not open_dt and first_sale_dates and (first_sale_dates[0] - start_dt).days >= 5:
            start_dt = first_sale_dates[0]

    pts = []
    valid_vals = []
    for d in all_dates:
        if d < start_dt:
            continue
        v = daily_map.get(d)
        pts.append({"date": d, "val": v, "is_weekend": d.weekday() >= 5})
        if v is not None and v > 0:
            valid_vals.append(v)

    avg_daily = (sum(valid_vals) / len(valid_vals)) if valid_vals else None
    rate = (avg_daily / target * 100.0) if (avg_daily is not None and target) else None

    # 최근 추세: 유효 영업일이 6일 이상일 때 전반부 평균 vs 최근 절반(최대 7일) 평균 비교
    trend_dir, trend_pct = "", None
    if len(valid_vals) >= 6:
        win = min(7, len(valid_vals) // 2)
        prev_avg = sum(valid_vals[:-win]) / len(valid_vals[:-win])
        rec_avg = sum(valid_vals[-win:]) / win
        if prev_avg > 0:
            diff_p = (rec_avg - prev_avg) / prev_avg * 100.0
            trend_pct = diff_p
            if diff_p >= 5.0:
                trend_dir = "up"
            elif diff_p <= -5.0:
                trend_dir = "dn"
            else:
                trend_dir = "flat"

    return {
        "name": name,
        "manager": m.get("manager", "") or "-",
        "type": m.get("type", "") or "-",
        "place": m.get("place", ""),
        "promoter": m.get("promoter", "특수개발"),
        "open_date": open_date_str or (start_dt.strftime("%Y-%m-%d") if start_dt else "-"),
        "target": target,
        "avg_daily": avg_daily,
        "rate": rate,
        "days_cnt": len(valid_vals),
        "pts": pts,
        "trend_dir": trend_dir,
        "trend_pct": trend_pct,
    }


def recent_daily_chart_svg(pts, target=None, compact=False):
    """최근 오픈점 일자별 막대 + 주황 예상일매출 기준선 + 주말 음영 띠 벡터(SVG) 그래프."""
    W = 520
    H = 146 if compact else 164
    x0, x1 = 10.0, W - 12.0
    y_top = 24.0
    y_base = H - 24.0
    n = max(len(pts), 1)
    slot = (x1 - x0) / n
    bw = max(4.5, min(22.0, slot * 0.66))

    vals = [p["val"] for p in pts if p["val"] is not None and p["val"] > 0]
    if target:
        vals.append(target)
    vmax = max(vals) * 1.14 if vals else 1.0
    if vmax <= 0:
        vmax = 1.0
    scale = (y_base - y_top) / vmax

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="Malgun Gothic, sans-serif">']

    # 1) 주말(토·일) 세로 연회색 음영 띠
    for i, p in enumerate(pts):
        if p["is_weekend"]:
            sx = x0 + slot * i
            out.append(f'<rect x="{sx:.1f}" y="{y_top - 6:.1f}" width="{slot:.1f}" height="{y_base - y_top + 6:.1f}" fill="#f1f4fb" rx="2"/>')

    # 바닥 기준선
    out.append(f'<line x1="{x0:.1f}" y1="{y_base:.1f}" x2="{x1:.1f}" y2="{y_base:.1f}" stroke="#cfd8ea" stroke-width="1.2"/>')

    # 표시할 막대 상단 숫자 위치 선정(겹침 방지)
    non_null_indices = [i for i, p in enumerate(pts) if p["val"] is not None and p["val"] > 0]
    max_idx = max(non_null_indices, key=lambda i: pts[i]["val"]) if non_null_indices else -1
    min_idx = min(non_null_indices, key=lambda i: pts[i]["val"]) if non_null_indices else -1
    last_idx = non_null_indices[-1] if non_null_indices else -1
    priority_set = {i for i in (max_idx, min_idx, last_idx) if i >= 0}

    # 2) 일자별 막대 및 날짜 라벨
    for i, p in enumerate(pts):
        cx = x0 + slot * (i + 0.5)
        v = p["val"]
        d = p["date"]
        show_date = (n <= 31) or (i == 0) or (i == n - 1) or (d.day in (1, 5, 10, 15, 20, 25))
        if show_date:
            d_txt = f"{d.month}/{d.day}" if (i == 0 or d.day == 1) and n <= 20 else str(d.day)
            d_col = "#d0143c" if p["is_weekend"] else "#6b7a99"
            d_fw = "700" if (p["is_weekend"] or i == last_idx) else "500"
            out.append(f'<text x="{cx:.1f}" y="{H - 7}" text-anchor="middle" font-size="9.5" font-weight="{d_fw}" fill="{d_col}">{d_txt}</text>')

        if v is not None and v > 0:
            bh = max(2.5, v * scale)
            by = y_base - bh
            achieved = (target is not None and v >= target)
            bar_col = "#2c4a8a" if achieved else "#9ab0d9"
            out.append(
                f'<rect x="{cx - bw / 2:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2.5" fill="{bar_col}">'
                f'<title>{d.strftime("%m/%d")}: {fnum(v)}천원</title></rect>'
            )

    # 3) 주황색 당월 예상일매출 기준선 (숫자보다 먼저 그려서 숫자를 가리지 않음, 카드 상단에 예상 금액이 있으므로 우측 알약 박스는 생략)
    if target is not None and target > 0:
        ty = max(y_top - 2.0, min(y_base - 2.0, y_base - target * scale))
        out.append(f'<line x1="{x0:.1f}" y1="{ty:.1f}" x2="{x1:.1f}" y2="{ty:.1f}" stroke="#f08c00" stroke-width="1.9" stroke-dasharray="5,3"/>')

    # 4) 값 라벨: 흰색 외곽선(halo)을 입혀 주황 점선과 겹쳐도 또렷하게 읽히도록 맨 위에 배치
    placed_xs = []
    cand_order = sorted(priority_set) + [i for i in non_null_indices if i not in priority_set]
    min_gap = 25.0 if not compact else 28.0
    halo = 'style="paint-order:stroke;stroke:#ffffff;stroke-width:3px;stroke-linejoin:round;"'
    for i in cand_order:
        if n > 12 and i not in priority_set and (i % 2 != 0):
            continue
        cx = x0 + slot * (i + 0.5)
        if any(abs(cx - px) < min_gap for px in placed_xs):
            continue
        placed_xs.append(cx)
        v = pts[i]["val"]
        by = y_base - max(2.5, v * scale)
        is_hi = (i == last_idx or i == max_idx)
        t_col = "#1f3a73" if is_hi else "#55648a"
        t_fw = "800" if is_hi else "700"
        out.append(f'<text x="{cx:.1f}" y="{max(11.0, by - 4.0):.1f}" text-anchor="middle" font-size="9.3" font-weight="{t_fw}" fill="{t_col}" {halo}>{fnum(v)}</text>')

    out.append("</svg>")
    return "".join(out)


def _recent_store_card_html(item, compact=False):
    rr_html = '<span class="rc-rr">R&amp;R</span>' if item["promoter"] == "R&R" else ""
    rate = item["rate"]
    if rate is None:
        r_cls, r_txt = "na", "예상 미입력"
    elif rate >= 100.0:
        r_cls, r_txt = "good", f"달성 {rate:.1f}%"
    elif rate >= 80.0:
        r_cls, r_txt = "warn", f"달성 {rate:.1f}%"
    else:
        r_cls, r_txt = "bad", f"달성 {rate:.1f}%"

    place_part = f" · {escape(item['place'])}" if item.get("place") else ""
    tgt_part = f"예상 {fnum(item['target'])}" if item["target"] is not None else "예상 -"
    svg = recent_daily_chart_svg(item["pts"], target=item["target"], compact=compact)
    return (
        f'<div class="rc-card"><div class="rc-head">'
        f'<div class="rc-title-wrap"><div class="rc-name">{escape(item["name"])}{rr_html}</div>'
        f'<div class="rc-sub">{escape(item["manager"])} · {escape(item["type"])}{place_part} · 오픈 {escape(item["open_date"])}</div></div>'
        f'<div class="rc-kpis"><div class="rc-avg"><div class="v">{fnum(item["avg_daily"])}<small>천원/일</small></div>'
        f'<div class="s">{tgt_part} · {item["days_cnt"]}일 평균</div></div>'
        f'<span class="rc-rate {r_cls}">{r_txt}</span></div></div>'
        f'{svg}</div>'
    )


def _render_recent_open_tab(D):
    """실적 현황 > 2) 최근 오픈점 일별 추이 탭 (전 사용자 열람 가능)."""
    rd = D.get("recent_daily") or {}
    if not rd.get("has_raw") or not rd.get("stores"):
        st.info(
            f"아직 `{PERF_SHEET_NAME}` 스프레드시트의 **`{TAB_DAILY_RECENT}`** 탭에 일자별 매출 원시가 없습니다.\n\n"
            "사내 BI에서 추출한 최근 오픈점 일자별 리포트를 가공 없이 그대로 복사해 `최근점_일별원시` 탭 `A1`에 붙여넣고 상단 **새로고침**을 누르면 "
            "점포별 일자 추이 그래프와 예상매출 달성 흐름이 자동으로 나타납니다."
        )
        return

    all_dates = rd["dates"]
    last_dt = rd["last_date"]
    rep_ym = rd["report_ym"]
    enriched = [_enrich_recent_store(D, s, all_dates, rep_ym) for s in rd["stores"]]

    unmatched_stores = [s["raw_name"] for s in rd["stores"] if not s.get("in_master")]
    if unmatched_stores:
        st.warning(
            f":material/warning: `최근점_일별원시` 점포 중 아직 `점포마스터` 탭에 등록되지 않은 점포가 있습니다: "
            f"**{', '.join(unmatched_stores)}** (`점포마스터`와 `예상목표` 탭에 추가하시면 담당자·타입·예상일매출 기준선이 자동 반영됩니다)"
        )

    st.caption(
        f":material/event: 기준 데이터: **{all_dates[0].strftime('%Y-%m-%d')} ~ {last_dt.strftime('%Y-%m-%d')}** "
        f"({len(enriched)}점)  |  진한 네이비 막대 = 당월 예상일매출 이상 · 연파랑 = 예상 미달 · 주황 점선 = 당월 예상일매출 · 회색띠/빨간 날짜 = 주말(토·일)"
    )

    mgrs = []
    for it in enriched:
        if it["manager"] and it["manager"] != "-" and it["manager"] not in mgrs:
            mgrs.append(it["manager"])
    fc1, fc2 = st.columns([1.4, 3.6])
    with fc1:
        f_mgr = st.selectbox("담당자 필터", ["전체"] + mgrs, key="rc_mgr_filter")
    with fc2:
        all_st_names = [it["name"] for it in enriched]
        picked_names = st.multiselect("표시 점포 선택 (빼고 싶은 점포만 X로 제외 가능)", all_st_names,
                                      default=all_st_names, key="rc_store_pick")

    shown = [
        it for it in enriched
        if (f_mgr == "전체" or it["manager"] == f_mgr) and (it["name"] in picked_names)
    ]
    if not shown:
        st.caption("선택된 점포가 없습니다.")
        return

    cards_html = '<div class="rc-grid">' + "".join(_recent_store_card_html(it) for it in shown) + "</div>"
    st.markdown(cards_html, unsafe_allow_html=True)

    with st.expander("📋 일자별 상세 매출 숫자 표 보기 (단위: 천원)"):
        tbl_cols = ["일자"] + [it["name"] for it in shown]
        t_rows = []
        by_nm = {s["name"]: s["daily_map"] for s in rd["stores"]}
        for d in all_dates:
            wk_kr = ("월", "화", "수", "목", "금", "토", "일")[d.weekday()]
            r_dict = {"일자": f"{d.month}월 {d.day}일 ({wk_kr})"}
            for it in shown:
                v = by_nm.get(it["name"], {}).get(d)
                r_dict[it["name"]] = fnum(v) if (v is not None and v > 0) else ""
            t_rows.append(r_dict)
        st.dataframe(pd.DataFrame(t_rows, columns=tbl_cols), use_container_width=True, hide_index=True)


def _all_report_managers(D, names):
    """일일보고 좌측 요약표용 담당자 목록: 해당 연도 오픈 실적이 있는 모든 담당자를 누락 없이 포함해 합계가 일치하도록 합니다."""
    order = D.get("mgr_order", {})
    name_set = set(names)
    mgrs = {m["manager"] for m in D["masters"] if m["manager"] and m["name"] in name_set}
    return sorted(mgrs, key=lambda n: (n not in order, order.get(n, 999), n))


def _default_month_open_lines(get_gc, D, report_dt):
    """당월 오픈(예정)점 기본 줄 목록 자동 구성 (점포마스터 당월 오픈점 + 주간보고 당월 오픈확정/계약완료 물건)."""
    ym_str = report_dt.strftime("%Y%m")
    ym_kr = f"{report_dt.year}년 {report_dt.month}월"
    lines = []
    seen = set()

    for m in sorted(D["masters"], key=lambda x: x.get("open_date", "")):
        if m.get("open_ym") == ym_str:
            dt_o = _parse_iso_or_kr_date(m.get("open_date", ""))
            d_tag = f"{dt_o.month}/{dt_o.day}" if dt_o else ym_kr
            prom = m.get("promoter", "특수개발")
            lines.append(f"{m['name']}({d_tag}) {prom}")
            seen.add(_norm_store_name_key(m["name"]))

    if get_gc is not None:
        try:
            w_recs = load_weekly_all(get_gc)
            cur_code, _ = wk_info()
            cand_weeks = sorted({r.get("주차코드", "").strip() for r in w_recs if wk_parse(r.get("주차코드", ""))},
                                key=lambda c: wk_parse(c), reverse=True)
            latest_wk = cand_weeks[0] if cand_weeks else cur_code
            for r in w_recs:
                if r.get("유형") == "추진물건" and r.get("주차코드", "").strip() == latest_wk and r.get("상태") in ("오픈확정", "계약완료"):
                    d = _parse_item_json(r.get("추진물건", ""))
                    if str(d.get("오픈예상월") or "").strip() == ym_kr:
                        nm = str(d.get("물건명") or "").strip()
                        nk = _norm_store_name_key(nm)
                        if nm and nk not in seen:
                            seen.add(nk)
                            prom = str(d.get("추진주체") or "특수개발").strip()
                            lines.append(f"{nm}({report_dt.month}/예정) {prom}")
        except Exception:
            pass
    return lines


def _daily_report_export_html(report_title_date, year_short, end_month_num, team_summary_rows, store_detail_rows, recent_items):
    """임원 일일보고용 2페이지 HTML (원클릭 PNG 클립보드 복사 · PNG 파일 저장 · A4 가로 PDF 인쇄 지원)."""
    # 1) 좌측 요약표 HTML
    sum_trs = []
    for sr in team_summary_rows:
        cls = ' class="tot"' if sr["is_total"] else ""
        r_col = ""
        if sr["rate"] is not None:
            r_col = ' class="bad"' if sr["rate"] < 80 else (' class="good"' if sr["rate"] >= 100 else "")
        sum_trs.append(
            f"<tr{cls}>"
            f"<td>{escape(sr['who'])}</td>"
            f"<td>{sr['total_n']}</td><td>{sr['sd_n']}</td><td>{sr['rr_n']}</td>"
            f"<td>{fnum(sr['daily'])}</td><td>{fnum(sr['target'])}</td>"
            f"<td{r_col}>{f'{sr['rate']:.1f}%' if sr['rate'] is not None else '-'}</td>"
            f"</tr>"
        )

    # 2) 하단 신규 오픈점 상세표 HTML (달성률 색상 기준을 요약표와 통일: 100%↑ 초록 / 80~99.9% 기본 / 80% 미만 빨강)
    det_trs = []
    for dr in store_detail_rows:
        rr_badge = '<span class="rr-b">R&amp;R</span>' if dr["promoter"] == "R&R" else ""
        r_val = dr["rate"]
        r_cls = ""
        if r_val is not None:
            if r_val < 80:
                r_cls = "bad"
            elif r_val >= 100:
                r_cls = "good"

        m_diff = dr["margin_diff"]
        md_cls = "neg" if (m_diff is not None and m_diff < 0) else ""
        md_txt = f"{m_diff:+.1f}%" if m_diff is not None else ""

        op_avg, op_sum = dr["op_avg"], dr["op_sum"]
        oa_cls = "op neg" if (op_avg is not None and op_avg < 0) else "op"
        os_cls = "op neg" if (op_sum is not None and op_sum < 0) else "op"

        det_trs.append(
            "<tr>"
            f"<td>{escape(dr['open_date'])}</td>"
            f"<td class=\"mgr\">{escape(dr['manager'])}{rr_badge}</td>"
            f"<td class=\"nm\">{escape(dr['name'])}</td>"
            f"<td>{escape(dr['type'])}</td>"
            f"<td>{escape(dr['place'])}</td>"
            f"<td class=\"num\">{fnum(dr['target']) if dr['target'] is not None else ''}</td>"
            f"<td class=\"num b\">{fnum(dr['daily']) if dr['daily'] is not None else ''}</td>"
            f"<td class=\"num {r_cls}\">{f'{r_val:.1f}%' if r_val is not None else ''}</td>"
            f"<td class=\"num\">{f'{dr['exp_margin']:.1f}%' if dr['exp_margin'] is not None else ''}</td>"
            f"<td class=\"num\">{f'{dr['act_margin']:.1f}%' if dr['act_margin'] is not None else ''}</td>"
            f"<td class=\"num {md_cls}\">{md_txt}</td>"
            f"<td class=\"num {oa_cls}\">{fnum(op_avg) if op_avg is not None else ''}</td>"
            f"<td class=\"num {os_cls}\">{fnum(op_sum) if op_sum is not None else ''}</td>"
            "</tr>"
        )

    # 3) 2페이지 최근 오픈점 미니 차트 그리드 HTML
    if recent_items:
        p2_cards = '<div class="rc-grid p2-grid">' + "".join(_recent_store_card_html(it, compact=True) for it in recent_items) + "</div>"
    else:
        p2_cards = '<div class="empty-p2">최근점_일별원시 탭에 데이터가 없어 표시할 최근 오픈점 차트가 없습니다.</div>'

    file_tag = datetime.now(KST).strftime("%Y%m%d")
    return f"""
    <html><head><meta charset="utf-8">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
    <style>
      @page {{ size: A4 landscape; margin: 7mm 8mm; }}
      * {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      body {{ font-family: 'Malgun Gothic','맑은 고딕',sans-serif; color: #1f2d4d; background: #f4f6fb; margin: 0; padding: 12px 14px; }}
      .toolbar {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
          background: #ffffff; border: 1px solid #d5dcea; border-radius: 12px; padding: 10px 14px; margin-bottom: 12px;
          box-shadow: 0 2px 8px rgba(31,58,115,.07); }}
      .tb-btns {{ display: flex; flex-wrap: wrap; gap: 6px; }}
      .toolbar button {{ height: 34px; padding: 0 13px; border-radius: 8px; border: 1px solid #2c4a8a;
          background: #ffffff; color: #1f3a73; font-weight: 800; font-size: 12.5px; cursor: pointer; }}
      .toolbar button.pri {{ background: #2c4a8a; color: #ffffff; }}
      .toolbar button:hover {{ background: #1f3a73; color: #ffffff; }}
      #capStatus {{ font-size: 12px; font-weight: 700; color: #0b9a6a; }}
      .sheet-page {{ background: #ffffff; border: 1px solid #d7deee; border-radius: 12px; padding: 16px 18px;
          margin-bottom: 16px; box-shadow: 0 3px 12px rgba(31,58,115,.07); }}
      .p-head {{ display: flex; align-items: baseline; justify-content: space-between; border-bottom: 2.5px solid #1f3a73;
          padding-bottom: 7px; margin-bottom: 12px; }}
      .p-title {{ font-size: 18.5px; font-weight: 900; color: #1f3a73; letter-spacing: -0.3px; }}
      .p-meta {{ font-size: 12px; font-weight: 700; color: #55648a; }}
      .p1-top {{ margin-bottom: 16px; }}
      .p1-bottom {{ width: 100%; }}
      .sec-cap {{ display: flex; align-items: baseline; justify-content: space-between; font-size: 14px; font-weight: 900;
          color: #1f3a73; margin: 0 0 6px; }}
      .sec-cap small {{ font-size: 11px; font-weight: 600; color: #7d8aa8; }}
      table.dr {{ width: 100%; border-collapse: collapse; font-size: 11.4px; border: 1px solid #c9d4ea; }}
      table.dr.sum-tbl {{ font-size: 12px; }}
      table.dr th {{ background: #e9effa; color: #1f3a73; font-weight: 800; padding: 5px 6px; border: 1px solid #c9d4ea;
          text-align: center; white-space: nowrap; line-height: 1.3; }}
      table.dr th.op-h {{ background: #f7ecc6; color: #6e4e00; }}
      table.dr td {{ padding: 4px 6px; border: 1px solid #e1e7f3; text-align: center; white-space: nowrap; color: #1f2d4d; }}
      table.dr.sum-tbl td {{ padding: 5px 8px; }}
      table.dr tbody tr:nth-child(even) td {{ background: #fafbfe; }}
      table.dr tr.tot td {{ background: #eef3fc !important; font-weight: 900; color: #1f3a73; border-bottom: 2px solid #9ab0d9; font-size: 12.6px; }}
      table.dr td.nm {{ text-align: left; font-weight: 700; padding-left: 8px; }}
      table.dr td.mgr {{ font-weight: 700; }}
      table.dr td.num {{ text-align: right; padding-right: 7px; font-variant-numeric: tabular-nums; }}
      table.dr td.b {{ font-weight: 800; }}
      table.dr td.good {{ color: #0b9a6a; font-weight: 800; }}
      table.dr td.warn {{ color: #c96a00; font-weight: 800; }}
      table.dr td.bad {{ color: #d0143c; font-weight: 800; }}
      table.dr td.neg {{ color: #d0143c; font-weight: 700; }}
      table.dr td.op {{ background: #fffbe6 !important; font-weight: 700; }}
      table.dr td.op.neg {{ color: #d0143c; }}
      .rr-b {{ display: inline-block; margin-left: 4px; padding: 1px 5px; border-radius: 4px; background: #ffe066;
          color: #664400; font-size: 9.5px; font-weight: 900; vertical-align: 1px; }}
      /* 2페이지 미니 차트 카드 */
      .rc-grid.p2-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
      .rc-card {{ background: #ffffff; border: 1px solid #d5dcea; border-radius: 10px; padding: 9px 12px 6px; break-inside: avoid; page-break-inside: avoid; }}
      .rc-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 3px; }}
      .rc-name {{ display: flex; align-items: center; gap: 5px; font-size: 13.5px; font-weight: 900; color: #1f3a73; }}
      .rc-rr {{ display: inline-block; font-size: 9px; font-weight: 800; background: #fff3bf; color: #8a5a00; border: 1px solid #f3d26b; border-radius: 4px; padding: 0 5px; }}
      .rc-sub {{ font-size: 10px; color: #6b7a99; font-weight: 600; margin-top: 1px; }}
      .rc-kpis {{ display: flex; align-items: center; gap: 5px; flex: none; }}
      .rc-avg {{ text-align: right; margin-right: 2px; }}
      .rc-avg .v {{ font-size: 14px; font-weight: 900; color: #1f2d4d; line-height: 1.1; }}
      .rc-avg .v small {{ font-size: 9.5px; color: #7d8aa8; font-weight: 600; margin-left: 1px; }}
      .rc-avg .s {{ font-size: 9.5px; color: #8591ab; font-weight: 600; }}
      .rc-rate {{ font-size: 10.5px; font-weight: 800; border-radius: 999px; padding: 2px 8px; }}
      .rc-rate.good {{ background: #e3f3ec; color: #0b9a6a; }}
      .rc-rate.warn {{ background: #fef1df; color: #b25e00; }}
      .rc-rate.bad {{ background: #fde8ec; color: #d0143c; }}
      .rc-rate.na {{ background: #eef2fa; color: #6b7a99; }}
      .rc-trend {{ font-size: 9.5px; font-weight: 800; border-radius: 5px; padding: 2px 6px; }}
      .rc-trend.up {{ background: #ebfbee; color: #0b9a6a; }}
      .rc-trend.dn {{ background: #fff5f5; color: #d0143c; }}
      .rc-trend.flat {{ background: #f1f3f8; color: #6b7a99; }}
      .rc-card svg {{ width: 100%; height: auto; display: block; }}
      .empty-p2 {{ padding: 30px; text-align: center; color: #8591ab; font-size: 13px; }}
      @media print {{
        body {{ background: #ffffff; padding: 0; }}
        .no-print {{ display: none !important; }}
        .sheet-page {{ border: none; box-shadow: none; padding: 0; margin: 0; }}
        #page2 {{ page-break-before: always; break-before: page; }}
      }}
    </style></head><body>
      <div class="toolbar no-print">
        <div class="tb-btns">
          <button class="pri" onclick="capturePage('page1', 'copy', '일일보고_1_오픈점현황_{file_tag}.png')">📋 1페이지 이미지 복사</button>
          <button onclick="capturePage('page1', 'save', '일일보고_1_오픈점현황_{file_tag}.png')">💾 1페이지 PNG 저장</button>
          <button class="pri" onclick="capturePage('page2', 'copy', '일일보고_2_최근오픈점추이_{file_tag}.png')">📋 2페이지 이미지 복사</button>
          <button onclick="capturePage('page2', 'save', '일일보고_2_최근오픈점추이_{file_tag}.png')">💾 2페이지 PNG 저장</button>
          <button onclick="window.print()">🖨️ 전체 PDF 인쇄</button>
        </div>
        <div id="capStatus">버튼을 누르면 여백이 정리된 고해상도 이미지로 바로 복사·저장됩니다.</div>
      </div>

      <div id="page1" class="sheet-page">
        <div class="p-head">
          <div class="p-title">특수개발 오픈점 일일 현황 ({escape(report_title_date)})</div>
          <div class="p-meta">단위: 천원, %</div>
        </div>
        <div class="p1-top">
          <div class="sec-cap"><span>■ 특수개발 오픈점 현황</span><small>'{escape(year_short)}년 1월~{end_month_num}월(누계) · {escape(report_title_date)}</small></div>
          <table class="dr sum-tbl">
            <thead>
              <tr>
                <th rowspan="2">구분</th>
                <th colspan="3">출점수</th>
                <th rowspan="2">일매출</th>
                <th rowspan="2">예상매출</th>
                <th rowspan="2">달성률</th>
              </tr>
              <tr><th>계</th><th>특수개발</th><th>R&amp;R</th></tr>
            </thead>
            <tbody>{"".join(sum_trs)}</tbody>
          </table>
        </div>
        <div class="p1-bottom">
          <div class="sec-cap"><span>■ 신규 오픈점 현황 ({len(store_detail_rows)}점)</span><small>개점일 순 · 노란 칸 = 월 마감 본부손익 (정산 전 신규점 공란)</small></div>
          <table class="dr">
            <thead>
              <tr>
                <th rowspan="2">개점일</th>
                <th rowspan="2">담당자</th>
                <th rowspan="2">점포명</th>
                <th rowspan="2">타입</th>
                <th rowspan="2">입지</th>
                <th colspan="3">예상매출 달성률</th>
                <th colspan="3">예상 매익률 차이</th>
                <th colspan="2" class="op-h">본부손익</th>
              </tr>
              <tr>
                <th>예상</th><th>실적</th><th>달성률</th>
                <th>예상</th><th>실적</th><th>차이</th>
                <th class="op-h">월 평균</th><th class="op-h">누계</th>
              </tr>
            </thead>
            <tbody>{"".join(det_trs)}</tbody>
          </table>
        </div>
      </div>

      <div id="page2" class="sheet-page">
        <div class="p-head">
          <div class="p-title">■ 최근 오픈점 일자별 매출 현황 ({escape(report_title_date)})</div>
          <div class="p-meta">네이비 = 예상 달성일 · 연파랑 = 예상 미달일 · 주황 점선 = 당월 예상일매출 · 회색띠/적색일 = 주말(토·일) · 단위: 천원</div>
        </div>
        {p2_cards}
      </div>

      <script>
      async function capturePage(elId, mode, filename) {{
        var st = document.getElementById('capStatus');
        if (typeof html2canvas === 'undefined') {{
          st.style.color = '#d0143c';
          st.textContent = '사내망에서 이미지 변환 스크립트가 차단되었습니다. 우측 PDF 인쇄 버튼을 이용해 주세요.';
          return;
        }}
        st.style.color = '#2c4a8a';
        st.textContent = '고해상도 이미지를 생성 중입니다...';
        try {{
          var el = document.getElementById(elId);
          var canvas = await html2canvas(el, {{ scale: 2, backgroundColor: '#ffffff', useCORS: true }});
          if (mode === 'copy' && navigator.clipboard && window.ClipboardItem) {{
            canvas.toBlob(async function(blob) {{
              try {{
                await navigator.clipboard.write([new ClipboardItem({{ 'image/png': blob }})]);
                st.style.color = '#0b9a6a';
                st.textContent = '클립보드에 복사되었습니다! 메신저/메일 창에서 바로 Ctrl+V 로 붙여넣으세요.';
              }} catch (err) {{
                var a = document.createElement('a');
                a.href = canvas.toDataURL('image/png');
                a.download = filename;
                a.click();
                st.style.color = '#0b9a6a';
                st.textContent = '브라우저 보안 정책으로 클립보드 대신 PNG 파일로 바로 다운로드했습니다.';
              }}
            }}, 'image/png');
          }} else {{
            var a = document.createElement('a');
            a.href = canvas.toDataURL('image/png');
            a.download = filename;
            a.click();
            st.style.color = '#0b9a6a';
            st.textContent = filename + ' 파일이 저장되었습니다.';
          }}
        }} catch (e) {{
          st.style.color = '#d0143c';
          st.textContent = '이미지 생성 실패: ' + e;
        }}
      }}
      </script>
    </body></html>
    """


def _render_daily_report_tab(get_gc, D, year, myms):
    """실적 현황 > 3) 일일보고 출력 탭 (문구 자동 완성 + 1·2페이지 이미지 복사/저장 및 PDF 인쇄)."""
    cum_meta = D.get("daily_cum_meta") or {}
    rd = D.get("recent_daily") or {}

    default_dt = rd.get("last_date") or (datetime.now(KST).date() - _dt.timedelta(days=1))
    all_names = [m["name"] for m in D["masters"] if m.get("open_ym", "")[:4] == year]
    if not all_names:
        st.info(f"{year}년 오픈 점포 데이터가 없습니다.")
        return

    # 원시 연동 상태 배너
    cum_status = (
        f"당월 누계원시(`{TAB_DAILY_CUM}`): **{ym_label(cum_meta['ym'])} · {cum_meta['matched_count']}점 반영됨**"
        if cum_meta.get("has_raw") else f"당월 누계원시(`{TAB_DAILY_CUM}`): 미입력 (`월실적` 탭 기준으로 계산 중)"
    )
    rec_status = (
        f"최근점 일별원시(`{TAB_DAILY_RECENT}`): **{len(rd.get('stores', []))}점 · {rd['last_date'].strftime('%m/%d')}까지 반영됨**"
        if rd.get("has_raw") and rd.get("last_date") else f"최근점 일별원시(`{TAB_DAILY_RECENT}`): 미입력"
    )
    st.caption(f":material/sync: {cum_status}  |  {rec_status}")
    unmatched_in_rpt = [s["raw_name"] for s in rd.get("stores", []) if not s.get("in_master")]
    if unmatched_in_rpt:
        st.warning(
            f":material/warning: `최근점_일별원시` 점포 중 아직 `점포마스터` 탭에 등록되지 않은 점포가 있습니다: "
            f"**{', '.join(unmatched_in_rpt)}** (`점포마스터`와 `예상목표` 탭에 추가하시면 1페이지 출점수 합계 및 2페이지 예상일매출 기준선에 자동 반영됩니다)"
        )

    open_full = [f"{year}{mo:02d}" for mo in range(1, 13)]
    team_g = calc_group(D, all_names, myms, open_yms=open_full)

    # ── 1) 메신저/메일 보고 문구 생성기 ──
    st.markdown("##### :material/chat_paste_go: 1) 일일보고 본문 문구 (자동 완성)")
    mc1, mc2 = st.columns([1.3, 2.7])
    with mc1:
        rep_dt = st.date_input("보고 기준일 (누계 기준)", value=default_dt, key="dr_report_date")
        def_open_lines = _default_month_open_lines(get_gc, D, rep_dt)
        open_txt_key = f"dr_open_lines_{rep_dt.strftime('%Y%m')}"
        if open_txt_key not in st.session_state:
            st.session_state[open_txt_key] = "\n".join(def_open_lines)
        sched_raw = st.text_area(
            f"{rep_dt.month}월 오픈(예정)점 목록 (한 줄에 1점씩 자유 수정 가능)",
            key=open_txt_key, height=115,
            placeholder="예:\n담양매화마루(11/19) 특수개발\n영도해양박물관(11/29) R&R",
        )
    sched_lines = [ln.strip() for ln in str(sched_raw or "").splitlines() if ln.strip()]
    msg_lines = [
        f"{rep_dt.month}월 {rep_dt.day}일 누계",
        "오픈점 매출 보고 드립니다.",
        "",
        f"오픈점 {len(all_names)}점,",
        f"누계 일매출은 {fnum(team_g['daily'])}천원으로",
        f"예상매출 比 {fnum(team_g['rate'], 1)}% 달성중입니다.",
    ]
    if sched_lines:
        msg_lines += [
            "",
            f"당월 오픈 예정점은 {len(sched_lines)}점 입니다.",
            "",
            *sched_lines,
        ]
    full_msg = "\n".join(msg_lines)

    with mc2:
        st.text_area("완성된 보고 문구 미리보기", value=full_msg, height=205, key="dr_msg_preview")
        escaped_js_msg = json.dumps(full_msg, ensure_ascii=False)
        components.html(
            f"""
            <button id="cpBtn" style="height:36px;padding:0 18px;border:none;border-radius:9px;background:#2c4a8a;
              color:#fff;font-weight:800;font-size:13.5px;cursor:pointer;font-family:'Malgun Gothic',sans-serif;">
              📋 보고 문구 클립보드 복사
            </button>
            <span id="cpMsg" style="margin-left:10px;font-size:12.5px;font-weight:700;color:#0b9a6a;font-family:'Malgun Gothic',sans-serif;"></span>
            <script>
            document.getElementById('cpBtn').onclick = async function() {{
              var txt = {escaped_js_msg};
              try {{
                await navigator.clipboard.writeText(txt);
                document.getElementById('cpMsg').textContent = '복사 완료! 메신저/메일에 Ctrl+V 로 붙여넣으세요.';
              }} catch (e) {{
                var ta = document.createElement('textarea');
                ta.value = txt; document.body.appendChild(ta); ta.select();
                document.execCommand('copy'); document.body.removeChild(ta);
                document.getElementById('cpMsg').textContent = '복사 완료!';
              }}
            }};
            </script>
            """,
            height=46,
        )

    st.divider()

    # ── 2) 1페이지(팀 요약 + 전 점포 현황) & 2페이지(최근 오픈점 그래프) 시트 생성 ──
    st.markdown("##### :material/print: 2) 일일보고 리포트 시트 (이미지 바로 복사·저장 / PDF 인쇄)")
    st.caption(
        "좌측 **특수개발/R&R 구분**과 우측 **예상 매익률 차이**를 채우시려면 `점포마스터` 탭에 `추진주체`(`특수개발`/`R&R`), "
        "`예상매익률` 열을 추가해 값을 넣어주시면 자동으로 계산됩니다."
    )

    # 좌측 요약표 데이터 계산
    def _summary_row(label, store_names, is_total=False):
        g = calc_group(D, store_names, myms, open_yms=open_full)
        rr_n = sum(1 for n in store_names if D["master_by_name"].get(n, {}).get("promoter") == "R&R")
        total_n = len(store_names)
        return {
            "who": label,
            "is_total": is_total,
            "total_n": total_n,
            "sd_n": total_n - rr_n,
            "rr_n": rr_n,
            "daily": g["daily"],
            "target": g["target"],
            "rate": g["rate"],
        }

    mgr_list = _all_report_managers(D, all_names)
    summary_rows = [_summary_row("특수개발팀", all_names, is_total=True)]
    for mgr in mgr_list:
        m_names = [n for n in all_names if D["master_by_name"].get(n, {}).get("manager") == mgr]
        summary_rows.append(_summary_row(mgr, m_names, is_total=False))

    # 우측 전 점포 상세표 데이터 계산 (개점일 오름차순)
    sorted_masters = sorted(
        [D["master_by_name"][n] for n in all_names if n in D["master_by_name"]],
        key=lambda m: (m.get("open_date", ""), m["name"]),
    )
    detail_rows = []
    for m in sorted_masters:
        nm = m["name"]
        c = calc_store(D, nm, myms)
        per = D["monthly"].get(nm, {})
        op_vals = [per[ym]["op"] for ym in myms if ym in per and per[ym]["op"] is not None]
        op_sum = sum(op_vals) if op_vals else None
        op_avg = (op_sum / len(op_vals)) if op_vals else None
        exp_m = m.get("exp_margin")
        act_m = c["margin"]
        m_diff = (act_m - exp_m) if (act_m is not None and exp_m is not None) else None
        detail_rows.append({
            "open_date": m.get("open_date", ""),
            "manager": m.get("manager", ""),
            "promoter": m.get("promoter", "특수개발"),
            "name": nm,
            "type": m.get("type", ""),
            "place": m.get("place", ""),
            "target": c["target"],
            "daily": c["daily"],
            "rate": c["rate"],
            "exp_margin": exp_m,
            "act_margin": act_m,
            "margin_diff": m_diff,
            "op_avg": op_avg,
            "op_sum": op_sum,
        })

    recent_items = []
    if rd.get("has_raw") and rd.get("stores"):
        recent_items = [_enrich_recent_store(D, s, rd["dates"], rd["report_ym"]) for s in rd["stores"]]

    end_m = int(myms[-1][4:]) if myms else rep_dt.month
    report_html = _daily_report_export_html(
        f"{rep_dt.month}월 {rep_dt.day}일 누계",
        year[2:],
        end_m,
        summary_rows,
        detail_rows,
        recent_items,
    )
    components.html(report_html, height=920, scrolling=True)


def _render_perf_overview(D, yms, period_text, year, myms, mfull, mshort):
    """yms/period_text/year: 조회 연도(어떤 점포가 보이는지, 오픈연도 기준) / myms/mfull/mshort: 그 점포들의
    매출·영업이익·매익율을 실제로 어느 해 걸로 보여줄지(기준연도 = 오늘 날짜 기준 올해, metric_period 로 계산)"""
    mode = "누계"      # 이제 연도 선택만 있고 항상 누계로 봄 (전월 대비 비교는 더 이상 쓰지 않음)
    # 담당자 실적은 '그 연도에 오픈한 점포'만 대상으로 봄(오픈연도 기준) -> 예전에 오픈해서 지금도
    # 매출을 내는 점포는 여기서는 빼고, 점포 자체 실적은 '점포 현황' 탭에서 계속 전체 기간을 볼 수 있음
    all_names = [m["name"] for m in D["masters"] if m.get("open_ym", "")[:4] == year]
    # 출점수 집계용 기간: 실적 입력 여부와 상관없이 '선택 연도 1~12월' 전체 (실적이 아직 없는 연도도 출점수가 잡히도록)
    open_full = [f"{year}{mo:02d}" for mo in range(1, 13)]

    def prev_of(names):
        return None

    st.markdown(sec_heading("trophy", f"{year}년 오픈점 팀 전체 · {mfull}"), unsafe_allow_html=True)
    if not all_names:
        st.info(f"{year}년에 오픈한 점포가 없습니다. 연도를 바꿔서 확인해 주세요.")
        return
    st.markdown(group_cards(calc_group(D, all_names, myms, open_yms=open_full), mode, prev_of(all_names), D, "팀",
                             _open_flow_svg(D, all_names, yms), mshort),
                unsafe_allow_html=True)
    render_ranking(D, all_names, myms, mfull, "tool_rank_open_team", "team")

    managers = ordered_managers_for(D, all_names)
    if not managers:
        st.markdown(sec_heading("badge", "담당자별"), unsafe_allow_html=True)
        st.info(f"{year}년에 오픈한 점포를 담당한 담당자가 없습니다.")
        return

    # 제목 + 담당자 알약 필터 (전체 / 이름...)
    options = ["요약"] + managers
    if st.session_state.get("tool_mgr_pick") not in options:
        st.session_state["tool_mgr_pick"] = "요약"

    def _reset_detail():
        for k in list(st.session_state.keys()):
            if str(k).startswith("tool_store_a") or str(k).startswith("tool_rank_open_mgr"):
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
        st.markdown(summary_html(D, managers, myms, open_full, all_names), unsafe_allow_html=True)
        return

    def _mgr_box(mgr, mi):
        names = [n for n in all_names if D["master_by_name"].get(n, {}).get("manager") == mgr]
        chips = "".join(f'<span class="mgr-chip">{escape(n)}</span>' for n in names)
        with st.container(key=f"mgrbox_{mi}"):
            # 이름 줄 + 카드를 한 덩어리로 그려서 간격을 내 CSS(mgr-row 의 margin-bottom)만으로 정함
            st.markdown(f'<div class="mgr-row"><div class="mgr-name">{escape(mgr)}</div>'
                        f'<div class="mgr-chips">{chips}</div></div>'
                        + group_cards(calc_group(D, names, myms, open_yms=open_full), mode, prev_of(names), D, mgr,
                                       _open_flow_svg(D, names, yms), mshort),
                        unsafe_allow_html=True)
            render_ranking(D, names, myms, mfull, f"tool_rank_open_mgr_{mi}", f"mgr{mi}")
            if show_open:
                render_store_cards(D, names, myms, mfull, ctx=f"a{mi}")

    for mi, mgr in enumerate(managers):
        if pick == mgr:
            _mgr_box(mgr, mi)


def view_performance(D, yms, period_text, year, myms, mfull, mshort, get_gc=None):
    rd = D.get("recent_daily") or {}
    rec_cnt = len(rd.get("stores", [])) if rd.get("has_raw") else 0
    rec_tab_lbl = f":material/trending_up: 최근 오픈점 일별 추이 ({rec_cnt})" if rec_cnt else ":material/trending_up: 최근 오픈점 일별 추이"

    with st.container(key="lvl2tabs_perf"):
        t_annual, t_recent, t_daily_rpt = st.tabs([
            ":material/leaderboard: 연간 · 누계 실적",
            rec_tab_lbl,
            ":material/newspaper: 일일보고 출력",
        ])
    with t_annual:
        _render_perf_overview(D, yms, period_text, year, myms, mfull, mshort)
    with t_recent:
        _render_recent_open_tab(D)
    with t_daily_rpt:
        _render_daily_report_tab(get_gc, D, year, myms)


def view_stores(D, yms, period_text):
    st.markdown(sec_heading("store", f"점포 현황 · {period_text}"), unsafe_allow_html=True)
    # 상세 화면일 때는 필터를 숨기고 상세만 보여줌
    if st.session_state.get("tool_store_b") in D["master_by_name"]:
        render_store_cards(D, [], yms, period_text, ctx="b")
        return

    managers = ordered_managers(D)
    types = sorted({m["type"] for m in D["masters"] if m["type"]})
    places = sorted({m["place"] for m in D["masters"] if m["place"]})
    years = sorted({m["open_ym"][:4] for m in D["masters"] if m["open_ym"]}, reverse=True)
    c0, c1, c2, c3 = st.columns(4)
    with c0:
        f_year = st.selectbox("오픈연도", ["전체"] + years, key="tool_f_year")
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
             if (f_year == "전체" or m["open_ym"][:4] == f_year)
             and (f_mgr == "전체" or m["manager"] == f_mgr)
             and (f_type == "전체" or m["type"] == f_type)
             and (f_place == "전체" or m["place"] == f_place)
             and (not q.strip() or q.strip() in m["name"])]
    st.caption(f"{len(names)}점")
    render_store_cards(D, names, yms, period_text, ctx="b", sort_by=sort_by)


# ───────────────────────── 주간 활동 보고 ─────────────────────────
# 저장 위치: 구글시트 '휴게소_통합_데이터' 안에 탭을 2개로 나눠서 씁니다.
#   - '주간_활동보고' 탭: '휴게소 방문' 전용(휴게소 데이터허브가 읽는 탭과 완전히 같음) -> 방문 보고는 여전히
#     그 즉시 휴게소 데이터허브 지도 핀의 방문 이력에 그대로 나타납니다.
#   - '특수팀_주간보고' 탭(새로 추가): 추진물건 / 일정 / 특이사항 / 제출완료 등 SD팀 전용 데이터.
#     이 탭은 코드가 처음 실행될 때 없으면 자동으로 만들어 줍니다(수동으로 만들 필요 없음).
# 예전에는 이 둘을 같은 탭에 같이 썼었는데, 그러면 휴게소 데이터허브 쪽이 SD팀 전용 행(JSON 등)까지
# 그대로 읽어서 화면에 섞여 나오는 문제가 있었습니다. 탭을 분리해서 서로 완전히 독립적으로 동작하게 했습니다.
KST = timezone(timedelta(hours=9))
WEEKLY_DOC_NAME = "휴게소_통합_데이터"
WEEKLY_TAB = "주간_활동보고"          # 휴게소 방문 전용(휴게소 데이터허브와 공유)
WEEKLY_TEAM_TAB = "특수팀_주간보고"    # SD팀 전용(추진물건/일정/특이사항/제출완료) - 새로 분리한 탭
WEEKLY_HEADERS = ["주차코드", "주차표시", "사번", "이름", "방문휴게소", "활동내용", "추진물건", "특이사항", "작성시각",
                  "아이템ID", "유형", "상태", "최초등록주차", "수정시각", "수정자"]
ITEM_STATUS = ["추진물건", "오픈확정", "계약완료"]
SCHEDULE_DAYS = ["월", "화", "수", "목", "금"]
SCHEDULE_HALVES = ["오전", "오후"]
SCHEDULE_SLOTS = [f"{d}{h}" for d in SCHEDULE_DAYS for h in SCHEDULE_HALVES]

# 추진물건 상세 입력 칸(예전 엑셀 양식과 동일). '계약 여부'는 기존의 '상태' 칸을 그대로 쓰고(= 계약여부),
# 나머지 칸은 시트에 새 열을 추가하지 않고 '추진물건' 칸 하나에 JSON으로 묶어서 저장합니다.
# '공동담당자'는 추진물건 탭 전용 다중선택 칸이며(계약완료·오픈확정 시에는 각자 실적 분배 후 따로 입력), JSON 리스트로 함께 저장됩니다.
ITEM_BASE_COLS = ["상태", "오픈예상월", "물건명", "부문", "지역", "운영형태", "상권대분류", "세부상권",
                  "예상매출(천원)", "예상손익(천원)", "추진주체", "진행경과"]
ITEM_COLS = ["상태", "오픈예상월", "물건명", "공동담당자", "부문", "지역", "운영형태", "상권대분류", "세부상권",
             "예상매출(천원)", "예상손익(천원)", "추진주체", "진행경과"]
ITEM_NUM_COLS = ("예상매출(천원)", "예상손익(천원)")
# 화면(표)에 보여줄 순서: '경과(주)'는 저장값이 아니라 자동 계산 칸이라 맨 뒤 '진행경과' 앞에 끼워 넣습니다.
# 계약완료·오픈확정 탭에는 공동담당자 칸을 두지 않고, 추진 물건 탭에만 물건명 바로 다음에 공동담당자 칸을 둡니다.
DONE_EDITOR_COLS = ITEM_BASE_COLS[:-1] + ["경과(주)"] + [ITEM_BASE_COLS[-1]]
PROG_EDITOR_COLS = ITEM_COLS[:-1] + ["경과(주)"] + [ITEM_COLS[-1]]
EDITOR_COLS = DONE_EDITOR_COLS
# 예전에 저장된 데이터는 칸 이름이 달랐던 적이 있어서(상권세부상권/1년차 예상일매출(천원)/1년차 예상월손익(천원)),
# 그 이름으로 저장된 JSON을 읽을 때도 새 이름으로 그대로 보이도록 변환해 줍니다.
_ITEM_OLD_TO_NEW = {"상권세부상권": "세부상권", "1년차 예상일매출(천원)": "예상매출(천원)",
                    "1년차 예상월손익(천원)": "예상손익(천원)"}
SELECT_LIST_TAB = "선택목록"      # 부문/운영형태/상권대분류/추진주체 드롭다운 값을 관리하는 탭(직접 편집 가능)


@st.cache_data(ttl=300, show_spinner=False)
def load_select_lists(_get_gc):
    """'선택목록' 탭에서 드롭다운 값을 읽어옵니다. 탭이 없거나 비어 있으면 빈 목록을 돌려주고,
    그 경우 해당 칸은 드롭다운 대신 직접 입력 칸으로 자동으로 바뀝니다."""
    cols = ("부문", "운영형태", "상권대분류", "추진주체")
    out = {c: [] for c in cols}
    try:
        gc = _get_gc()
        values = gc.open(WEEKLY_DOC_NAME).worksheet(SELECT_LIST_TAB).get_all_values()
    except Exception:
        return out
    if not values:
        return out
    header = [str(h).strip() for h in values[0]]
    idx = {c: header.index(c) for c in cols if c in header}
    for c, i in idx.items():
        seen = []
        for row in values[1:]:
            if i < len(row) and str(row[i]).strip() and str(row[i]).strip() not in seen:
                seen.append(str(row[i]).strip())
        out[c] = seen
    return out


def _future_month_labels(n=24):
    """오늘이 속한 달부터 n개월치 '2026년 11월' 형식 목록(오픈예상월 드롭다운용)"""
    today = datetime.now(KST)
    y, m = today.year, today.month
    labels = []
    for i in range(n):
        mm = m + i
        yy = y + (mm - 1) // 12
        mo = (mm - 1) % 12 + 1
        labels.append(f"{yy}년 {mo}월")
    return labels


def _month_sort_key(label):
    """'2026년 11월' -> (2026, 11) 정렬키. 비어 있거나 못 읽으면 맨 뒤로 보냄"""
    m = re.match(r"^\s*(\d{4})년\s*(\d{1,2})월", str(label or ""))
    if not m:
        return (9999, 99)
    return (int(m.group(1)), int(m.group(2)))


def _norm_co_managers(val, exclude_name=""):
    """공동담당자 값을 깔끔한 문자열 리스트(공백·중복·본인 이름 제거)로 정규화합니다."""
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        parts = [p.strip() for p in re.split(r"[,·/]", s) if p.strip()]
    else:
        try:
            if isinstance(val, float) and val != val:
                return []
            parts = [str(x).strip() for x in val if x is not None and str(x).strip() and str(x).strip() != "<NA>"]
        except TypeError:
            return []
    ex = str(exclude_name or "").strip()
    out = []
    for p in parts:
        if p and p != ex and p not in out:
            out.append(p)
    return out


def _list_str_series(values):
    """빈 DataFrame에서도 MultiselectColumn 이 편집 가능하도록 PyArrow list<string> 타입 Series를 만듭니다."""
    cleaned = [_norm_co_managers(v) for v in values]
    try:
        import pyarrow as pa
        return pd.Series(cleaned, dtype=pd.ArrowDtype(pa.list_(pa.string())))
    except Exception:
        return pd.Series(cleaned, dtype="object")


def _parse_item_json(raw):
    """'추진물건' 칸 내용을 구조화된 dict로. (도입 전) 예전 데이터는 한 줄 텍스트였으므로,
    JSON으로 못 읽으면 그 글자 전체를 '물건명'으로 봅니다 -> 기존 데이터도 그대로 보입니다."""
    raw = str(raw or "").strip()
    if not raw:
        return {"물건명": "", "공동담당자": []}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            for old, new in _ITEM_OLD_TO_NEW.items():
                if old in d and new not in d:
                    d[new] = d.pop(old)
            d.setdefault("물건명", "")
            d["공동담당자"] = _norm_co_managers(d.get("공동담당자"))
            return d
    except Exception:
        pass
    return {"물건명": raw, "공동담당자": []}


def _item_num(v):
    """숫자로 바꿉니다. 빈 값은 물론, 표 편집기를 거치며 생기는 NaN(판다스의 '숫자 없음' 값)도
    여기서 같이 걸러내 None으로 돌려줘야, 저장되는 JSON/화면에 'nan' 글자가 남지 않습니다."""
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f:   # NaN (NaN은 자기 자신과도 같지 않다는 성질로 판별)
            return None
        return f
    except Exception:
        return None


def _fmt_item_num(v):
    try:
        if v is None or v == "":
            return ""
        f = float(v)
        if f != f:   # NaN
            return ""
        return f"{f:,.0f}"
    except Exception:
        return str(v)


def _dropdown_or_text(label, opts, width="small"):
    """선택목록에 값이 있으면 드롭다운(맨 앞에 빈 선택지 추가), 없으면(아직 안 채워졌으면) 직접 입력 칸.
    빈 선택지를 넣어두는 이유: 비어 있는 칸이 옵션 목록에 없는 값으로 보여서 'None' 글자로 깨지는 문제를 막기 위함"""
    if opts:
        options = [""] + [o for o in opts if o]
        return st.column_config.SelectboxColumn(label, options=options, width=width)
    return st.column_config.TextColumn(label, width=width)


def _build_seed_df(rows, fw_map, cur_code, include_co=False):
    """rows(ITEM_COLS 모양의 dict 리스트)를 data_editor 에 안정적으로 넘길 DataFrame으로 만듭니다.
    include_co=True 이면 추진 물건용(공동담당자 다중선택 칸 포함), False 이면 계약완료·오픈확정용입니다.
    이 함수로 만든 결과는 '처음 만들 때'와 '상태가 바뀌어 다른 탭으로 옮길 때'만 새로 호출하고, 그 사이에는
    같은 객체를 data_editor 에 계속 그대로 넘겨야 합니다(매번 새로 만들면 편집 중간에 표가 리셋되는 원인이 됩니다)."""
    ed_cols = PROG_EDITOR_COLS if include_co else DONE_EDITOR_COLS
    src_cols = ITEM_COLS if include_co else ITEM_BASE_COLS
    data = []
    for r in rows:
        row = {c: r.get(c, [] if c == "공동담당자" else "") for c in src_cols}
        if include_co:
            row["공동담당자"] = _norm_co_managers(row.get("공동담당자"))
        name = str(row.get("물건명", "")).strip()
        row["경과(주)"] = _weeks_between(fw_map.get(name, cur_code), cur_code) if name else None
        data.append(row)
    df = pd.DataFrame(data, columns=ed_cols) if data else pd.DataFrame(columns=ed_cols)
    for c in ed_cols:
        if c == "공동담당자":
            df[c] = _list_str_series(df[c].tolist() if len(df) else [])
        elif c in ITEM_NUM_COLS or c == "경과(주)":
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = df[c].apply(lambda v: "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v))
    return df.reset_index(drop=True)


def _item_editor(editor_key, own_seed_key, other_seed_key, allowed_statuses, status_options, default_status,
                  select_lists, month_opts, fw_map, cur_code, include_co=False, co_options=(), owner_name=""):
    """표 하나(탭 하나)를 그립니다. data_editor 에 넘기는 값은 세션에 저장해 둔 것을 매번 '그대로' 넘겨서
    (새로 만들어서 넘기면 한 칸 편집할 때마다 표가 리셋되는 원인이 됩니다) 편집 중 리셋을 막고,
    상태가 이 탭에 안 맞게 바뀐 줄만 감지해서 다른 탭 쪽으로 옮깁니다."""
    co_warn_msg = st.session_state.pop(f"{editor_key}_co_warn", None)
    if co_warn_msg:
        st.warning(co_warn_msg)

    seed_df = st.session_state[own_seed_key]
    ed_cols = PROG_EDITOR_COLS if include_co else DONE_EDITOR_COLS
    cfg = {
        "상태": st.column_config.SelectboxColumn("상태", options=status_options, required=True, width="small"),
        "오픈예상월": st.column_config.SelectboxColumn("오픈예상월", options=month_opts, width="small"),
        "물건명": st.column_config.TextColumn("물건명", required=True, width="medium"),
        "부문": _dropdown_or_text("부문", select_lists.get("부문")),
        "지역": st.column_config.TextColumn("지역", width="small"),
        "운영형태": _dropdown_or_text("운영형태", select_lists.get("운영형태")),
        "상권대분류": _dropdown_or_text("상권대분류", select_lists.get("상권대분류")),
        "세부상권": st.column_config.TextColumn("세부상권", width="small"),
        "예상매출(천원)": st.column_config.NumberColumn("예상매출(천원)", format="%d", width="small"),
        "예상손익(천원)": st.column_config.NumberColumn("예상손익(천원)", format="%d", width="small"),
        "추진주체": _dropdown_or_text("추진주체", select_lists.get("추진주체")),
        "경과(주)": st.column_config.NumberColumn("경과(주)", disabled=True, width="small",
                                                 help="최초 등록 주차부터 몇 주째 이어지는지 (자동 계산)"),
        "진행경과": st.column_config.TextColumn("진행경과", width="large"),
    }
    if include_co:
        cfg["공동담당자"] = st.column_config.MultiselectColumn(
            "공동담당자", options=list(co_options), default=[], width="medium",
            help="함께 추진 중인 담당자를 선택하세요 (2명 이상 선택 가능, 비워두면 단독 추진)",
        )
    edited = st.data_editor(seed_df, column_config=cfg, num_rows="dynamic", use_container_width=True,
                            hide_index=True, key=editor_key, column_order=ed_cols)

    keep_rows, moved_rows, blocked_co_names = [], [], []
    for rec in edited.to_dict("records"):
        name = str(rec.get("물건명") or "").strip()
        status = rec.get("상태") if rec.get("상태") in ITEM_STATUS else default_status
        rec["물건명"] = name
        rec["상태"] = status
        rec["공동담당자"] = _norm_co_managers(rec.get("공동담당자"), owner_name) if include_co else []
        if status in allowed_statuses or not name:
            keep_rows.append(rec)
        elif include_co and rec["공동담당자"] and status in ("오픈확정", "계약완료"):
            # 공동 추진 물건은 계약완료·오픈확정 시 담당자별로 실적을 나누어 각자 새로 입력하도록 상태 전환을 차단합니다.
            rec["상태"] = "추진물건"
            keep_rows.append(rec)
            blocked_co_names.append(name)
        else:
            moved_rows.append(rec)

    if blocked_co_names:
        st.session_state[own_seed_key] = _build_seed_df(
            [{c: r.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS} for r in keep_rows],
            fw_map, cur_code, include_co=include_co)
        joined = ", ".join(blocked_co_names)
        msg = (f":material/info: '{joined}' 물건은 공동 추진 물건입니다. 계약완료·오픈확정 단계에서는 "
               "실적을 반영할 담당자별로 '계약완료·오픈확정 물건' 탭에 각자 새로 입력해 주세요. "
               "(기존 공동 추진 물건은 주 담당자가 체크박스로 삭제 후 제출하면 정리됩니다)")
        st.session_state[f"{editor_key}_co_warn"] = msg
        st.toast("공동 물건은 각자 계약완료·오픈확정 탭에 새로 입력해 주세요.", icon=":material/info:")
        st.rerun()

    if moved_rows:
        # 상태가 이 탭과 안 맞게 바뀐 줄 -> 다른 탭 쪽에 더해 주고, 이 탭에서는 빼서 각각 다시 만듭니다.
        other_include_co = not include_co
        other_df = st.session_state.get(other_seed_key)
        other_rows = other_df.to_dict("records") if other_df is not None else []
        for r in moved_rows:
            if not other_include_co:
                r["공동담당자"] = []
        other_rows = other_rows + [{c: r.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS} for r in moved_rows]
        st.session_state[other_seed_key] = _build_seed_df(other_rows, fw_map, cur_code, include_co=other_include_co)
        st.session_state[own_seed_key] = _build_seed_df(
            [{c: r.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS} for r in keep_rows],
            fw_map, cur_code, include_co=include_co)
        moved_names = ", ".join(r["물건명"] for r in moved_rows if r["물건명"])
        if moved_names:
            st.toast(f"'{moved_names}' 상태가 바뀌어 다른 탭으로 옮겼습니다.", icon=":material/package_2:")
        st.rerun()

    out = []
    for rec in keep_rows:
        for c in ITEM_NUM_COLS:
            rec[c] = _item_num(rec.get(c))
        rec["공동담당자"] = _norm_co_managers(rec.get("공동담당자"), owner_name) if include_co else []
        out.append({c: rec.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS})
    return out


# 로그인 가능한 사람 목록(= 현재 실제 팀원) : '점포마스터' 탭의 담당자는 그 점포를 오픈할 당시 담당자라서
# 팀원이 바뀌어도 갱신되지 않습니다. 그래서 팀 현황에 보여줄 담당자 목록은 로그인 시트에서 따로 읽습니다.
LOGIN_SHEET_KEY = "1zD20Om2mHH7M9ppcialvqB1SxA9IyVq2aCLM9IWoo8c"


@st.cache_data(ttl=300, show_spinner=False)
def load_login_records(_get_gc):
    """'사용자 명단' 시트를 그대로 읽어옵니다. (사번/생년월일/이름 외에 팀현황표시/관리자여부 같은
    선택 열이 있으면 같이 읽히고, 없으면 그냥 빠집니다)"""
    try:
        gc = _get_gc()
        doc = gc.open_by_key(LOGIN_SHEET_KEY)
        values = doc.worksheet("사용자 명단").get_all_values()
    except Exception:
        return []
    return _records(values)


def load_login_members(_get_gc):
    """로그인 가능한 전체 이름 목록 (시트에 적힌 순서 그대로, 중복 제거)"""
    names = []
    for r in load_login_records(_get_gc):
        n = r.get("이름", "").strip()
        if n and n not in names:
            names.append(n)
    return names


def load_team_display_members(_get_gc):
    """'팀현황표시' 열이 Y인 사람만 (팀 현황 화면에 기본으로 보여줄 담당자).
    시트에 이 열이 아직 없거나 Y로 표시된 사람이 한 명도 없으면 빈 목록을 돌려주고,
    이 경우 부르는 쪽에서 전체 인원을 기본값으로 쓰도록 되어 있습니다."""
    names = []
    for r in load_login_records(_get_gc):
        n = r.get("이름", "").strip()
        if n and str(r.get("팀현황표시", "")).strip().upper() == "Y" and n not in names:
            names.append(n)
    return names


def wk_info(target_date=None):
    """주어진 날짜(기본: 오늘)가 속한 주의 (주차코드, 주차표시). 월요일 시작, '그 달 1일이 든 주 = 1주차'"""
    if target_date is None:
        target_date = datetime.now(KST).date()
    monday = target_date - _dt.timedelta(days=target_date.weekday())
    sunday = monday + _dt.timedelta(days=6)
    year, month = monday.year, monday.month
    first_day = _dt.date(year, month, 1)
    first_monday = first_day - _dt.timedelta(days=first_day.weekday())
    week_num = ((monday - first_monday).days // 7) + 1
    code = f"{year}-{month:02d}-{week_num}"
    label = f"{year}년 {month}월 {week_num}주차 ({monday.strftime('%m/%d')}~{sunday.strftime('%m/%d')})"
    return code, label


def wk_parse(code):
    try:
        y, m, w = str(code).strip().split("-")
        return int(y), int(m), int(w)
    except Exception:
        return None


def wk_monday(code):
    p = wk_parse(code)
    if not p:
        return None
    y, m, w = p
    first_day = _dt.date(y, m, 1)
    first_monday = first_day - _dt.timedelta(days=first_day.weekday())
    return first_monday + _dt.timedelta(weeks=w - 1)


def wk_prev(code):
    mon = wk_monday(code)
    if mon is None:
        return ""
    return wk_info(mon - _dt.timedelta(days=7))[0]


def wk_next(code):
    mon = wk_monday(code)
    if mon is None:
        return ""
    return wk_info(mon + _dt.timedelta(days=7))[0]


def wk_label_from_code(code):
    """실제 보고가 없는 주차(이전주/다음주 버튼으로만 이동한 주차)도 라벨을 만들어 보여주기 위한 보조 함수.
    wk_info()와 같은 모양('2026년 11월 4주차 (11/16~11/22)')을 코드만으로 다시 계산합니다."""
    p = wk_parse(code)
    mon = wk_monday(code)
    if not p or not mon:
        return code
    y, mth, w = p
    sun = mon + _dt.timedelta(days=6)
    return f"{y}년 {mth}월 {w}주차 ({mon.strftime('%m/%d')}~{sun.strftime('%m/%d')})"


def wk_short_label(label):
    # "2026년 11월 4주차 (11/16~11/22)" -> "2026년 11월 4주차"  (연/월을 남겨야 연도 넘어갈 때도 정돈되어 보임)
    s = str(label)
    return s.split(" (", 1)[0] if " (" in s else s


def _weeks_between(first_week, cur_code):
    """first_week 에 처음 등록된 물건이 cur_code 주차까지 몇 주째 이어지는지 (최소 1주)"""
    m1, m2 = wk_monday(first_week), wk_monday(cur_code)
    if not m1 or not m2:
        return 1
    return max(0, (m2 - m1).days // 7) + 1


def _ensure_ws(doc, tab_name, create=True):
    """해당 이름의 탭을 열어서 돌려줍니다. 탭이 없으면(create=True일 때) 새로 만들고 머리글을 채워 줍니다."""
    try:
        ws = doc.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        ws = doc.add_worksheet(title=tab_name, rows=3000, cols=len(WEEKLY_HEADERS))
        ws.append_row(WEEKLY_HEADERS)
        return ws
    header = ws.row_values(1)
    missing = [h for h in WEEKLY_HEADERS if h not in header]
    if missing:
        new_header = header + missing
        if ws.col_count < len(new_header):
            ws.add_cols(len(new_header) - ws.col_count)
        ws.update("A1", [new_header])      # 기존 칸은 그대로, 새 칸만 뒤에 추가
    return ws


def _weekly_ws(get_gc, create=True):
    """휴게소 방문 전용 탭('주간_활동보고', 휴게소 데이터허브와 공유)"""
    gc = get_gc()
    return _ensure_ws(gc.open(WEEKLY_DOC_NAME), WEEKLY_TAB, create)


def export_map_rows(get_gc, rows):
    """지도 화면에서 필터링된 목록을 '지도_추출' 탭에 통째로 덮어 씁니다(매번 최신 필터 결과로 교체).
    rows: [{"구분":.., "name":.., "brand":.., "biz_major":.., "biz_minor":.., "address":.., "lat":.., "lng":.., "daily_sales":..}, ...]"""
    gc = get_gc()
    doc = gc.open(PERF_SHEET_NAME)
    try:
        ws = doc.worksheet(MAP_EXPORT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = doc.add_worksheet(title=MAP_EXPORT_TAB, rows=max(len(rows) + 10, 100), cols=len(MAP_EXPORT_HEADERS))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = [[
        r.get("구분", ""), r.get("name", ""), r.get("brand", ""), r.get("biz_major", ""), r.get("biz_minor", ""),
        r.get("address", ""), r.get("lat") if r.get("lat") is not None else "", r.get("lng") if r.get("lng") is not None else "",
        r.get("daily_sales") if r.get("daily_sales") is not None else "", now,
    ] for r in rows]
    ws.clear()
    ws.update("A1", [MAP_EXPORT_HEADERS] + body)
    return len(body)


def _weekly_team_ws(get_gc, create=True):
    """SD팀 전용 탭('특수팀_주간보고': 추진물건/일정/특이사항/제출완료). 없으면 자동으로 새로 만듭니다."""
    gc = get_gc()
    return _ensure_ws(gc.open(WEEKLY_DOC_NAME), WEEKLY_TEAM_TAB, create)


def _infer_weekly_type(r):
    """'유형' 칸이 비어 있으면(= 휴게소 데이터허브의 예전 방식으로 입력된 행) 내용으로 유형을 추정해 채워줍니다."""
    if r.get("유형"):
        return r
    if str(r.get("방문휴게소", "")).strip():
        r["유형"] = "방문"
    elif str(r.get("추진물건", "")).strip():
        r["유형"] = "추진물건"
        r["상태"] = r.get("상태") or "추진물건"
        r["최초등록주차"] = r.get("최초등록주차") or r.get("주차코드", "")
        r["아이템ID"] = r.get("아이템ID") or hashlib.md5(
            f"{r.get('사번','')}|{r.get('주차코드','')}|{r.get('추진물건','')}".encode("utf-8")
        ).hexdigest()[:10]
    elif str(r.get("특이사항", "")).strip():
        r["유형"] = "특이사항"
    return r


@st.cache_data(ttl=60, show_spinner=False)
def load_weekly_all(_get_gc):
    """SD팀 전용 탭('특수팀_주간보고')만 읽습니다. 휴게소 방문 데이터는 여기 섞이지 않습니다."""
    ws = _weekly_team_ws(_get_gc, create=True)
    if ws is None:
        return []
    return [_infer_weekly_type(r) for r in _records(ws.get_all_values())]


def weekly_clear_cache():
    load_weekly_all.clear()


def _weekly_now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# '사번'/'이름' 글자가 시트 헤더와 코드에서 눈으로는 똑같아 보여도(보이지 않는 문자 차이 등으로)
# 매칭에 실패하는 경우를 막기 위한 안전장치: 예전부터 app.py가 위치로 써온 고정 자리
# (사번=3번째 칸, 이름=4번째 칸)를 글자 매칭이 실패했을 때 대신 사용합니다.
_LEGACY_POS = {"사번": 2, "이름": 3}


def _header_index(header, name):
    """헤더에서 name 칸의 위치(0부터)를 찾습니다. 글자로 못 찾으면 고정 위치로 한 번 더 시도합니다."""
    if name in header:
        return header.index(name)
    pos = _LEGACY_POS.get(name)
    if pos is not None and len(header) > pos:
        return pos
    return -1


def _weekly_row(header, **kw):
    row = [""] * len(header)
    for name, val in kw.items():
        i = _header_index(header, name)
        if 0 <= i < len(row):
            row[i] = val
    return row


def weekly_append_visit(get_gc, week_code, week_label, uid, uname, visit_name, activity, note):
    ws = _weekly_ws(get_gc)
    header = ws.row_values(1)
    now = _weekly_now()
    ws.append_row(_weekly_row(header, 주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                              방문휴게소=visit_name, 활동내용=activity, 특이사항=note,
                              작성시각=now, 유형="방문", 수정시각=now))
    weekly_clear_cache()


def _weekly_upsert(get_gc, match_fn, row_vals_map):
    """match_fn(header, row) -> bool 인 첫 행을 찾아 덮어쓰고, 없으면 새 행을 추가 (SD팀 전용 탭에만 씀)"""
    ws = _weekly_team_ws(get_gc)
    values = ws.get_all_values()
    header = values[0] if values else WEEKLY_HEADERS
    row = _weekly_row(header, **row_vals_map)
    for rn in range(2, len(values) + 1):
        if match_fn(header, values[rn - 1]):
            ws.update(f"A{rn}", [row])
            weekly_clear_cache()
            return
    ws.append_row(row)
    weekly_clear_cache()


def _cell(header, row, name):
    i = _header_index(header, name)
    return row[i].strip() if 0 <= i < len(row) else ""


def weekly_upsert_item(get_gc, week_code, week_label, uid, uname, item_id, status, content, first_week, editor_name=""):
    now = _weekly_now()

    def match(header, row):
        return (_cell(header, row, "주차코드") == week_code and _cell(header, row, "아이템ID") == item_id and item_id)

    _weekly_upsert(get_gc, match, dict(
        주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname, 추진물건=content,
        작성시각=now, 아이템ID=item_id, 유형="추진물건", 상태=status, 최초등록주차=first_week,
        수정시각=now, 수정자=editor_name,
    ))


def weekly_save_user_items(get_gc, week_code, week_label, uid, uname, items_payload):
    """한 사용자의 해당 주차 물건 목록을 시트 1회 조회로 일괄 반영합니다.
    - 새로 추가·수정된 물건은 업서트하고(관리자가 수정했던 행을 그대로 둔 경우 수정자 기록 보존),
    - 이번 주에 이미 제출했다가 표에서 지운 물건은 시트에서도 함께 삭제해 유령 행(특히 삭제한 공동물건 잔류)을 막습니다."""
    ws = _weekly_team_ws(get_gc)
    values = ws.get_all_values()
    header = values[0] if values else WEEKLY_HEADERS
    now = _weekly_now()

    existing_by_id = {}
    for rn in range(2, len(values) + 1):
        row = values[rn - 1]
        if (_cell(header, row, "주차코드") == week_code
                and _cell(header, row, "사번") == uid
                and _cell(header, row, "유형") == "추진물건"):
            iid = _cell(header, row, "아이템ID")
            if iid:
                existing_by_id[iid] = (rn, row)

    keep_ids = set()
    updates = []
    appends = []
    for it in items_payload:
        iid = it["item_id"]
        keep_ids.add(iid)
        status = it["status"]
        content = it["content"]
        first_week = it["first_week"]
        if iid in existing_by_id:
            rn, old_row = existing_by_id[iid]
            old_content = _cell(header, old_row, "추진물건")
            old_status = _cell(header, old_row, "상태")
            old_editor = _cell(header, old_row, "수정자")
            old_mtime = _cell(header, old_row, "수정시각") or now
            old_ctime = _cell(header, old_row, "작성시각") or now
            # 관리자가 고쳐둔 내용 그대로 다시 제출한 경우 수정자 표시를 유지합니다.
            unchanged = (old_content == content and old_status == status)
            editor_val = old_editor if unchanged else ""
            mtime_val = old_mtime if unchanged else now
            new_row = _weekly_row(
                header, 주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                추진물건=content, 작성시각=old_ctime, 아이템ID=iid, 유형="추진물건",
                상태=status, 최초등록주차=first_week, 수정시각=mtime_val, 수정자=editor_val,
            )
            updates.append({"range": f"A{rn}", "values": [new_row]})
        else:
            new_row = _weekly_row(
                header, 주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                추진물건=content, 작성시각=now, 아이템ID=iid, 유형="추진물건",
                상태=status, 최초등록주차=first_week, 수정시각=now, 수정자="",
            )
            appends.append(new_row)

    if updates:
        ws.batch_update(updates)
    if appends:
        ws.append_rows(appends)

    del_rns = [rn for iid, (rn, _) in existing_by_id.items() if iid not in keep_ids]
    for rn in sorted(del_rns, reverse=True):
        ws.delete_rows(rn)

    weekly_clear_cache()


def weekly_admin_update_items(get_gc, week_code, changes, admin_name=""):
    """관리자가 팀 현황 수정 모드에서 고친 물건들을 원본 시트에 일괄 덮어쓰고 수정자·수정시각을 남깁니다.
    changes: list of dict(uid, orig_item_id, new_item_id, content)
    물건명 오타를 고친 경우 아이템ID도 새 물건명 기준으로 함께 갱신해 이후 담당자 재제출 시 중복 행 생성을 막습니다."""
    if not changes:
        return 0
    ws = _weekly_team_ws(get_gc)
    values = ws.get_all_values()
    if not values:
        return 0
    header = values[0]
    now = _weekly_now()
    editor = str(admin_name or "").strip() or "관리자"

    # 같은 주차 내 이미 존재하는 아이템ID 집합(이름 변경 시 충돌 방지용)
    week_ids = {
        _cell(header, values[rn - 1], "아이템ID")
        for rn in range(2, len(values) + 1)
        if _cell(header, values[rn - 1], "주차코드") == week_code
    }

    updates = []
    for ch in changes:
        target_uid = str(ch["uid"]).strip()
        orig_id = str(ch["orig_item_id"]).strip()
        new_id = str(ch["new_item_id"]).strip() or orig_id
        new_content = ch["content"]
        for rn in range(2, len(values) + 1):
            row = values[rn - 1]
            if (_cell(header, row, "주차코드") == week_code
                    and _cell(header, row, "사번") == target_uid
                    and _cell(header, row, "아이템ID") == orig_id
                    and _cell(header, row, "유형") == "추진물건"):
                final_id = new_id if (new_id == orig_id or new_id not in week_ids) else orig_id
                week_ids.add(final_id)
                updated_row = list(row) + [""] * max(0, len(header) - len(row))
                for col_name, col_val in (
                    ("추진물건", new_content),
                    ("아이템ID", final_id),
                    ("수정시각", now),
                    ("수정자", editor),
                ):
                    ci = _header_index(header, col_name)
                    if 0 <= ci < len(updated_row):
                        updated_row[ci] = col_val
                updates.append({"range": f"A{rn}", "values": [updated_row[:len(header)]]})
                break

    if updates:
        ws.batch_update(updates)
        weekly_clear_cache()
    return len(updates)


def weekly_append_note(get_gc, week_code, week_label, uid, uname, note_text):
    """같은 주차·같은 사번의 '특이사항'은 새 줄로 쌓지 않고 덮어씁니다(추진물건/일정과 같은 업서트 방식).
    예전에는 제출할 때마다 한 줄씩 추가돼서, 같은 내용을 여러 번 제출하면 특이사항이 그 횟수만큼 중복 표시됐습니다."""
    now = _weekly_now()

    def match(header, row):
        return (_cell(header, row, "주차코드") == week_code and _cell(header, row, "사번") == uid
                and _cell(header, row, "유형") == "특이사항")

    _weekly_upsert(get_gc, match, dict(주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                                       특이사항=note_text, 작성시각=now, 유형="특이사항", 수정시각=now))


def weekly_dedupe_notes(get_gc):
    """업서트 방식을 적용하기 전에 쌓였던 '특이사항' 중복 행을 한 번만 정리합니다.
    같은 (주차코드, 사번) 조합의 '특이사항' 행이 여러 개면, 가장 나중에(아래쪽에) 적힌 1개만 남기고
    나머지는 지웁니다. 행을 지우면 번호가 밀리므로, 꼭 아래쪽(큰 행 번호)부터 지워야 안전합니다."""
    ws = _weekly_team_ws(get_gc)
    values = ws.get_all_values()
    if not values:
        return 0
    header = values[0]
    seen = {}
    dup_rows = []
    for rn in range(2, len(values) + 1):
        row = values[rn - 1]
        if _cell(header, row, "유형") != "특이사항":
            continue
        key = (_cell(header, row, "주차코드"), _cell(header, row, "사번"))
        if key in seen:
            dup_rows.append(seen[key])  # 이전에 봤던(더 위쪽) 같은 키의 행을 중복으로 지움 -> 가장 아래 것만 남음
        seen[key] = rn
    for rn in sorted(dup_rows, reverse=True):
        ws.delete_rows(rn)
    if dup_rows:
        weekly_clear_cache()
    return len(dup_rows)


def weekly_upsert_schedule(get_gc, week_code, week_label, uid, uname, sched):
    now = _weekly_now()
    content = json.dumps(sched, ensure_ascii=False)

    def match(header, row):
        return (_cell(header, row, "주차코드") == week_code and _cell(header, row, "사번") == uid
                and _cell(header, row, "유형") == "일정")

    _weekly_upsert(get_gc, match, dict(주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                                       추진물건=content, 작성시각=now, 유형="일정", 수정시각=now))


def weekly_mark_submitted(get_gc, week_code, week_label, uid, uname):
    now = _weekly_now()

    def match(header, row):
        return (_cell(header, row, "주차코드") == week_code and _cell(header, row, "사번") == uid
                and _cell(header, row, "유형") == "제출완료")

    _weekly_upsert(get_gc, match, dict(주차코드=week_code, 주차표시=week_label, 사번=uid, 이름=uname,
                                       작성시각=now, 유형="제출완료", 수정시각=now))
    return now


def weekly_latest_items_before(records, uid, week_code):
    """해당 사번의 '추진물건' 중, week_code 보다 이전 주차에서 가장 최근 주차에 들어있던 항목들
    (= 이번 주로 그대로 이어져야 할 활성 항목)"""
    cand = [r for r in records if r.get("유형") == "추진물건" and r.get("사번", "").strip() == uid]
    cur_key = wk_parse(week_code) or (9999, 99, 99)
    weeks = sorted({r.get("주차코드", "").strip() for r in cand
                    if wk_parse(r.get("주차코드", "")) and wk_parse(r.get("주차코드", "")) < cur_key},
                   key=lambda c: wk_parse(c))
    if not weeks:
        return []
    last_week = weeks[-1]
    return [r for r in cand if r.get("주차코드", "").strip() == last_week]


def weekly_items_of_week(records, uid, week_code):
    return [r for r in records if r.get("유형") == "추진물건" and r.get("사번", "").strip() == uid
            and r.get("주차코드", "").strip() == week_code]


def weekly_co_items_for_user(records, uid, uname, week_code):
    """다른 담당자(주 담당)가 작성한 추진물건 중 나를 공동담당자로 지정한 활성 추진물건 목록을 돌려줍니다.
    주 담당이 이번 주 보고를 이미 제출했으면 이번 주 데이터 기준, 아직 제출 전이면 직전 주차 이월 데이터를 기준으로 합니다."""
    me = str(uname or "").strip()
    if not me:
        return []
    other_uids = sorted({r.get("사번", "").strip() for r in records
                         if r.get("유형") == "추진물건" and r.get("사번", "").strip()
                         and r.get("사번", "").strip() != uid})
    out = []
    for ouid in other_uids:
        cur_items = weekly_items_of_week(records, ouid, week_code)
        if cur_items or weekly_submitted_at(records, ouid, week_code):
            src = cur_items
        else:
            src = weekly_latest_items_before(records, ouid, week_code)
        for r in src:
            if r.get("상태") != "추진물건":
                continue
            d = _parse_item_json(r.get("추진물건", ""))
            co_list = _norm_co_managers(d.get("공동담당자"), r.get("이름", ""))
            if me in co_list:
                out.append(r)
    return out


def weekly_note_of_week(records, uid, week_code):
    rows = [r for r in records if r.get("유형") == "특이사항" and r.get("사번", "").strip() == uid
            and r.get("주차코드", "").strip() == week_code]
    return rows[-1]["특이사항"] if rows else ""


def weekly_schedule_of_week(records, uid, week_code):
    rows = [r for r in records if r.get("유형") == "일정" and r.get("사번", "").strip() == uid
            and r.get("주차코드", "").strip() == week_code]
    if not rows:
        return {}
    try:
        return json.loads(rows[-1].get("추진물건", "") or "{}")
    except Exception:
        return {}


def weekly_submitted_at(records, uid, week_code):
    rows = [r for r in records if r.get("유형") == "제출완료" and r.get("사번", "").strip() == uid
            and r.get("주차코드", "").strip() == week_code]
    return rows[-1]["작성시각"] if rows else ""


@st.cache_data(ttl=300, show_spinner=False)
def load_rest_names(_get_gc):
    """휴게소 데이터허브의 메인 시트(시트1)에서 휴게소명 목록만 읽어옴 (방문 보고 드롭다운용)"""
    try:
        gc = _get_gc()
        doc = gc.open(WEEKLY_DOC_NAME)
        values = doc.sheet1.get_all_values()
    except Exception:
        return []
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    if "휴게소명" not in header:
        return []
    i = header.index("휴게소명")
    names = []
    for row in values[1:]:
        if i < len(row) and str(row[i]).strip():
            names.append(str(row[i]).strip())
    return names


def _weekly_status_banner(submitted_at):
    if submitted_at:
        st.success(f":material/check_circle: 이번 주 보고를 제출했습니다 ({submitted_at}). 다시 제출하면 시각이 갱신됩니다.")
    else:
        st.warning("이번 주 보고를 아직 제출하지 않았습니다.")


def _render_weekly_mine(get_gc, records, uid, uname, cur_code, cur_label,
                        D=None, login_members=None, team_display_members=None):
    st.markdown(f"**작성 주차: {cur_label}**")
    submitted_at = weekly_submitted_at(records, uid, cur_code)
    _weekly_status_banner(submitted_at)

    done_seed_key = f"wk_seed_done_{cur_code}_{uid}"
    prog_seed_key = f"wk_seed_prog_{cur_code}_{uid}"
    fw_key = f"wk_fw_{cur_code}_{uid}"
    seed_sig_key = f"wk_seed_sig_{cur_code}_{uid}"

    cur_week_items = weekly_items_of_week(records, uid, cur_code)
    # 관리자가 팀 현황에서 내 물건을 수정한 경우, 수정시각 서명이 바뀌면 편집 표에도 최신 내용을 자동 반영합니다.
    sheet_sig = "|".join(
        f"{r.get('아이템ID','')}:{r.get('수정시각','')}:{r.get('수정자','')}" for r in cur_week_items
    )
    admin_edited_items = [
        r for r in cur_week_items
        if r.get("수정자", "").strip() and r.get("수정자", "").strip() != str(uname or "").strip()
    ]
    if admin_edited_items and st.session_state.get(seed_sig_key) not in (None, sheet_sig):
        st.session_state.pop(done_seed_key, None)
        st.session_state.pop(prog_seed_key, None)

    if done_seed_key not in st.session_state or prog_seed_key not in st.session_state:
        seed = cur_week_items or weekly_latest_items_before(records, uid, cur_code)
        rows, fw_map = [], {}
        for r in seed:
            d = _parse_item_json(r.get("추진물건", ""))
            row = {c: d.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS}
            row["상태"] = r.get("상태") if r.get("상태") in ITEM_STATUS else "추진물건"
            row["물건명"] = str(d.get("물건명", "")).strip()
            row["공동담당자"] = _norm_co_managers(d.get("공동담당자"), uname) if row["상태"] == "추진물건" else []
            rows.append(row)
            if row["물건명"]:
                fw_map[row["물건명"]] = r.get("최초등록주차") or r.get("주차코드") or cur_code
        st.session_state[fw_key] = fw_map
        st.session_state[seed_sig_key] = sheet_sig
        done_rows = [r for r in rows if r["상태"] in ("오픈확정", "계약완료")]
        prog_rows = [r for r in rows if r["상태"] == "추진물건"]
        st.session_state[done_seed_key] = _build_seed_df(done_rows, fw_map, cur_code, include_co=False)
        st.session_state[prog_seed_key] = _build_seed_df(prog_rows, fw_map, cur_code, include_co=True)

    if admin_edited_items:
        edited_desc = ", ".join(
            f"**{_parse_item_json(r.get('추진물건', '')).get('물건명') or '-'}** "
            f"(수정: {r.get('수정자')} · {r.get('수정시각', '')})"
            for r in admin_edited_items
        )
        st.info(f":material/edit_square: 관리자가 수정한 항목이 반영되어 있습니다: {edited_desc}")

    st.markdown("##### :material/edit_note: 주간업무보고")
    st.caption("지난주 목록이 그대로 이어집니다. 표를 엑셀처럼 바로 고쳐 쓰고, 빈 줄(가장 아래)에 새 물건을 추가할 수 있습니다. "
               "각 줄 맨 앞의 체크박스는 그 줄을 지울 때 선택하는 용도입니다(선택하면 위에 지우기 버튼이 나타납니다). "
               "단독 추진 물건은 상태를 '오픈확정'이나 '계약완료'로 바꾸면 자동으로 옮겨지며, "
               "공동 추진 물건은 계약 완료 시 실적 반영 담당자별로 '계약완료·오픈확정 물건' 탭에 각자 새로 입력해 주세요.")
    fw_map = st.session_state.get(fw_key, {})
    select_lists = load_select_lists(get_gc)
    month_opts = [""] + _future_month_labels()

    mgr_hidden = D.get("mgr_hidden", set()) if isinstance(D, dict) else set()
    base_members = team_display_members or login_members or []
    co_options = [m for m in base_members if m and m != uname and m not in mgr_hidden]
    prog_seed_df = st.session_state.get(prog_seed_key)
    if prog_seed_df is not None and "공동담당자" in prog_seed_df.columns:
        for v in prog_seed_df["공동담당자"].tolist():
            for cm in _norm_co_managers(v, uname):
                if cm not in co_options:
                    co_options.append(cm)

    with st.container(key="lvl3tabs"):
        t_done, t_prog = st.tabs([":material/emoji_events: 계약완료·오픈확정 물건", ":material/package_2: 추진 물건"])
    with t_done:
        done_rows = _item_editor(f"wk_ed_done_{cur_code}_{uid}", done_seed_key, prog_seed_key,
                                 ("오픈확정", "계약완료"), ["오픈확정", "계약완료"], "오픈확정",
                                 select_lists, month_opts, fw_map, cur_code, include_co=False, owner_name=uname)
    with t_prog:
        prog_rows = _item_editor(f"wk_ed_prog_{cur_code}_{uid}", prog_seed_key, done_seed_key,
                                 ("추진물건",), ITEM_STATUS, "추진물건",
                                 select_lists, month_opts, fw_map, cur_code,
                                 include_co=True, co_options=co_options, owner_name=uname)
        co_items = weekly_co_items_for_user(records, uid, uname, cur_code)
        if co_items:
            st.markdown("###### :material/group: 공동 추진 물건 (읽기 전용 · 주 담당자 관리)")
            st.caption("다른 담당자(주 담당)가 나를 공동담당자로 지정한 물건입니다. 팀 현황과 인쇄본에 내 이름이 함께 표시되며, "
                       "내용 수정·삭제는 주 담당자 화면에서만 가능합니다. 계약 완료 시에는 실적 분배 기준에 맞춰 각자 '계약완료·오픈확정 물건' 탭에 새로 입력해 주세요.")
            co_views = []
            for r in co_items:
                v = _item_view_with_weeks(r, cur_code)
                v["구분"] = "공동"
                v["주담당"] = r.get("이름", "")
                co_views.append(v)
            co_cols = ["구분", "주담당", "이름", "오픈예상월", "물건명", "부문", "지역", "운영형태",
                       "상권대분류", "세부상권", "예상매출(천원)", "예상손익(천원)", "추진주체", "진행주수", "진행경과"]
            st.markdown(_wk_table_html(co_views, co_cols, status_col="구분", wrap_cols=("진행경과",)),
                        unsafe_allow_html=True)
            my_norm_names = {re.sub(r"\s+", "", str(r.get("물건명", ""))) for r in prog_rows if str(r.get("물건명", "")).strip()}
            dup_co = [v["물건명"] for v in co_views if re.sub(r"\s+", "", str(v.get("물건명", ""))) in my_norm_names]
            if dup_co:
                st.warning(f":material/warning: 공동 추진 물건과 같은 이름의 물건({', '.join(dup_co)})이 위 내 추진 물건 표에도 있습니다. "
                           "중복 표시되지 않도록 한쪽만 남겨 주세요.")
    draft = done_rows + prog_rows
    for r in draft:
        name = str(r.get("물건명", "")).strip()
        if name and name not in fw_map:
            fw_map[name] = cur_code
    st.session_state[fw_key] = fw_map

    st.markdown("---")
    st.markdown("##### :material/sticky_note_2: 특이사항")
    note_key = f"wk_note_{cur_code}_{uid}"
    if note_key not in st.session_state:
        st.session_state[note_key] = weekly_note_of_week(records, uid, cur_code)
    st.text_area("특이사항", key=note_key, placeholder="공유할 이슈·참고사항을 자유롭게 적어주세요.",
                 label_visibility="collapsed", height=100)

    st.markdown("---")
    st.markdown("##### :material/calendar_month: 이번 주 일정 (월~금 오전/오후)")
    sched_key = f"wk_sched_{cur_code}_{uid}"
    if sched_key not in st.session_state:
        st.session_state[sched_key] = weekly_schedule_of_week(records, uid, cur_code)
    sched = st.session_state[sched_key]
    prev_sched = weekly_schedule_of_week(records, uid, wk_prev(cur_code))
    for d in SCHEDULE_DAYS:
        c1, c2 = st.columns(2)
        for half, col in zip(SCHEDULE_HALVES, (c1, c2)):
            slot = f"{d}{half}"
            with col:
                val = st.text_input(f"{d}요일 {half}", value=sched.get(slot, ""), key=f"{sched_key}_{slot}",
                                    placeholder=f"{d} {half} 일정")
                sched[slot] = val
                if val and prev_sched.get(slot, "").strip() == val.strip():
                    st.caption(":material/history: 지난주와 동일")

    st.markdown("---")
    submit_clicked = st.button("이번 주 보고 제출하기", type="primary", icon=":material/send:",
                                key=f"wk_submit_{cur_code}_{uid}")
    submit_feedback = st.empty()
    if submit_clicked:
        items_payload = []
        for it in draft:
            name = str(it.get("물건명", "")).strip()
            if not name:
                continue
            status = it.get("상태", "추진물건")
            first_week = fw_map.get(name, cur_code)
            item_id = hashlib.md5(f"{uid}|{name}".encode("utf-8")).hexdigest()[:10]
            payload_dict = {c: it.get(c, [] if c == "공동담당자" else "") for c in ITEM_COLS if c != "상태"}
            payload_dict["공동담당자"] = _norm_co_managers(payload_dict.get("공동담당자"), uname) if status == "추진물건" else []
            content_json = json.dumps(payload_dict, ensure_ascii=False)
            items_payload.append(dict(item_id=item_id, status=status, content=content_json, first_week=first_week))
        weekly_save_user_items(get_gc, cur_code, cur_label, uid, uname, items_payload)
        st.session_state.pop(seed_sig_key, None)
        note_val = st.session_state.get(note_key, "").strip()
        if note_val:
            weekly_append_note(get_gc, cur_code, cur_label, uid, uname, note_val)
        if any(v.strip() for v in sched.values()):
            weekly_upsert_schedule(get_gc, cur_code, cur_label, uid, uname, sched)
        now = weekly_mark_submitted(get_gc, cur_code, cur_label, uid, uname)
        st.session_state[f"wk_just_submitted_{cur_code}_{uid}"] = now
        submit_feedback.success(f":material/check_circle: 제출되었습니다! ({now})")
        st.toast(f"이번 주 보고 제출 완료! ({now})", icon=":material/check_circle:")
        st.rerun()
    else:
        just_at = st.session_state.pop(f"wk_just_submitted_{cur_code}_{uid}", None)
        if just_at:
            submit_feedback.success(f":material/check_circle: 제출되었습니다! ({just_at})")
        else:
            # 버튼을 누르지 않은 평소 상태에도 바로 옆에서 제출 여부를 바로 확인할 수 있게 동일 배너를 한 번 더 보여줍니다.
            with submit_feedback.container():
                _weekly_status_banner(submitted_at)

    st.markdown("---")
    st.markdown("##### :material/location_on: 휴게소 방문 보고")
    st.caption("방문할 때마다 바로 제출해 주세요. 같은 시트에 저장되므로, 휴게소 데이터허브 지도의 방문 이력에도 그대로 나타납니다.")
    rest_names = load_rest_names(get_gc)
    with st.form(f"wk_visit_form_{uid}", clear_on_submit=True):
        v_name = st.selectbox("방문 휴게소", [""] + rest_names)
        v_act = st.text_area("활동 내용")
        v_note = st.text_area("특이사항(방문 관련)")
        v_sent = st.form_submit_button("방문 보고 제출")
    if v_sent:
        if not v_name:
            st.warning("방문 휴게소를 선택해 주세요.")
        elif not v_act.strip():
            st.warning("활동 내용을 입력해 주세요.")
        else:
            weekly_append_visit(get_gc, cur_code, cur_label, uid, uname, v_name, v_act.strip(), v_note.strip())
            st.success(f"'{v_name}' 방문 보고가 제출되었습니다.")
            st.rerun()


def _item_view(r):
    """시트 한 행(r)의 '추진물건' JSON을 풀어서, 팀 현황 표/인쇄용 보기에 쓸 완전한 한 줄 dict로 만듭니다.
    칸 순서는 담당자 입력 표(ITEM_COLS)와 동일하게 오픈예상월 -> 물건명 -> ... -> 최초등록주차 -> 진행경과 순으로 맞춥니다.
    (진행경과는 자유 서술 칸이라 담당자 입력 표와 똑같이 맨 마지막 칸으로 둡니다)"""
    # d.get(key, "") 는 JSON에 그 칸이 'null'로 저장돼 있으면(= 값이 있는데 None) 기본값 ""로 안 바뀌고
    # None을 그대로 돌려줘서 화면/인쇄에 "None" 글자가 찍히는 원인이 됩니다. 그래서 `or ""`로 한 번 더 걸러줍니다.
    d = _parse_item_json(r.get("추진물건", ""))
    owner = str(r.get("이름", "") or "").strip()
    co_list = _norm_co_managers(d.get("공동담당자"), owner) if r.get("상태") == "추진물건" else []
    display_name = " · ".join([owner] + co_list) if (owner and co_list) else (owner or " · ".join(co_list))
    return {
        "상태": r.get("상태", ""),
        "이름": display_name,
        "오픈예상월": d.get("오픈예상월") or "",
        "물건명": d.get("물건명") or "",
        "부문": d.get("부문") or "",
        "지역": d.get("지역") or "",
        "운영형태": d.get("운영형태") or "",
        "상권대분류": d.get("상권대분류") or "",
        "세부상권": d.get("세부상권") or "",
        "예상매출(천원)": _fmt_item_num(d.get("예상매출(천원)")),
        "예상손익(천원)": _fmt_item_num(d.get("예상손익(천원)")),
        "추진주체": d.get("추진주체") or "",
        "최초등록주차": r.get("최초등록주차", ""),
        "진행경과": d.get("진행경과") or "",
    }


def _item_view_with_weeks(r, sel):
    """_item_view 결과 앞에 '신규'(이번에 조회 중인 주차에 처음 등록됐으면 NEW) 배지용 칸을 추가하고,
    '진행주수'(경과주)를 최초등록주차 다음, 진행경과 앞에 끼워 넣은 버전 (추진 물건 표용)"""
    view = _item_view(r)
    weeks = _weeks_between(r.get("최초등록주차") or sel, sel)
    is_new = (r.get("최초등록주차") or "").strip() == str(sel).strip()
    row = {"신규": "NEW" if is_new else ""}
    for k, v in view.items():
        if k == "진행경과":
            continue
        row[k] = v
    row["진행주수"] = weeks
    row["진행경과"] = view["진행경과"]
    return row


# 칸 머리글을 두 줄로 줄바꿈해서 가로 간격을 좁히기 위한 표기(예: '오픈예상월' -> '오픈' 줄바꿈 '예상월').
# '예상매출(천원)'처럼 이미 자연스럽게 줄바꿈되는 칸은 그대로 두고, 줄바꿈 위치가 어색하게 끼일 수 있는
# 칸만 여기서 직접 지정합니다.
HEADER_2LINE = {"오픈예상월": "오픈<br>예상월", "운영형태": "운영<br>형태", "상권대분류": "상권<br>대분류",
                "최초등록주차": "최초<br>등록주차", "진행주수": "경과<br>(주)"}


def _th_label(c):
    return HEADER_2LINE.get(c, escape(str(c)))


_STATUS_BADGE = {"오픈확정": ("open", "오픈확정"), "계약완료": ("done", "계약완료"), "추진물건": ("prog", "추진물건"), "공동": ("done", "공동")}


def _status_badge_html(status):
    cls, label = _STATUS_BADGE.get(status, ("prog", status or "-"))
    return f'<span class="wk-badge {cls}">{escape(label)}</span>'


def _submit_status_html(items):
    """제출 현황 카드. 실적현황 카드(kpi_html)와 같은 색/아이콘 언어를 쓰지만, 글자 크기는 '제출 현황' 제목 정도로 작게.
    items: dict(name, at)  - at 이 있으면 제출완료, 없으면 미제출"""
    out = ['<div class="subm-grid">']
    for it in items:
        ok = bool(it.get("at"))
        ac = COLOR_GOOD if ok else COLOR_BAD
        ic = mini_icon("check" if ok else "person", ac, 16)
        v_cls = "ok" if ok else "no"
        v_text = "제출완료" if ok else "미제출"
        sub = escape(it["at"]) if ok else "아직 제출하지 않았습니다"
        out.append(
            f'<div class="subm-card" style="--ac:{ac}"><div class="sb-l">{ic}{escape(it["name"])}</div>'
            f'<div class="sb-v {v_cls}">{v_text}</div><div class="sb-s">{sub}</div></div>'
        )
    out.append("</div>")
    return "".join(out)


def _wk_table_html(rows, cols, status_col=None, wrap_cols=()):
    """팀 현황용 표를 가운데 정렬 + 색깔 헤더 HTML로 그립니다(실적현황 쪽 표와 디자인 통일).
    st.dataframe(캔버스 렌더링)는 CSS가 안 먹어서 가운데 정렬이 안 됐던 것을, 이 함수는 보통의 HTML
    <table>로 그려서 가운데 정렬이 실제로 적용되게 합니다.
    status_col: 그 칸을 배지(둥근 알약) 모양으로 그립니다. wrap_cols: 글자가 길어지면 줄바꿈되도록(진행경과 등) 왼쪽 정렬로 넓게 그립니다."""
    out = ['<div class="wk-tbl-wrap"><table class="wk-tbl"><thead><tr>']
    out += [f"<th>{_th_label(c)}</th>" for c in cols]
    out.append("</tr></thead><tbody>")
    if not rows:
        out.append(f'<tr><td class="wk-empty" colspan="{len(cols)}">해당 사항 없음</td></tr>')
    else:
        for r in rows:
            out.append("<tr>")
            for c in cols:
                v = r.get(c, "")
                if c == status_col:
                    out.append(f"<td>{_status_badge_html(v)}</td>")
                elif c == "신규":
                    out.append(f'<td>{"<span class=\'wk-badge new\'>NEW</span>" if v else ""}</td>')
                elif c in wrap_cols:
                    out.append(f'<td class="wk-prog">{escape(str(v)) if v else "-"}</td>')
                else:
                    out.append(f"<td>{escape(str(v)) if v not in (None, '') else '-'}</td>")
            out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _wk_schedule_table_html(days, halves, managers, sched_by_mgr, prev_sched_by_name):
    """담당자별 주간 일정 표. 요일 칸은 오전/오후 두 줄을 rowspan으로 병합해서 '월요일'이 두 번 반복되지 않게 합니다."""
    out = ['<div class="wk-tbl-wrap"><table class="wk-tbl"><thead><tr><th>요일</th><th>구분</th>']
    out += [f"<th>{escape(m)}</th>" for m in managers]
    out.append("</tr></thead><tbody>")
    for d in days:
        for hi, half in enumerate(halves):
            slot = f"{d}{half}"
            out.append("<tr>")
            if hi == 0:
                out.append(f'<td class="wk-day" rowspan="{len(halves)}">{escape(d)}요일</td>')
            out.append(f"<td>{escape(half)}</td>")
            for mgr in managers:
                v = sched_by_mgr.get(mgr, {}).get(slot, "").strip()
                prev_v = prev_sched_by_name.get(mgr, {}).get(slot, "").strip()
                if v and prev_v == v:
                    cell = f'{escape(v)} <span title="지난주와 동일">:material/history:</span>'
                else:
                    cell = escape(v) if v else "-"
                out.append(f"<td>{cell}</td>")
            out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _print_badge_html(status):
    """인쇄용 상태 배지. 화면(팀 현황)의 .wk-badge 와 같은 색 언어를 쓰되, 인쇄 HTML은 별도 문서라
    TOOL_CSS를 못 읽으므로 같은 모양의 .rpt-badge 클래스를 인쇄 HTML 자체 <style>에 따로 둡니다."""
    cls, label = _STATUS_BADGE.get(status, ("prog", status or "-"))
    return f'<span class="rpt-badge {cls}">{escape(label)}</span>'


PRINT_NOTE_MAXLEN = 70     # 인쇄용 '진행경과' 글자수 제한(이 길이를 넘으면 … 로 줄여서, 칸이 좁아져 글자가 깨지는 걸 막음)


def _month_short(label):
    """'2026년 11월' -> '26.11' (인쇄본 칸을 아끼기 위한 축약 표기). 못 읽으면 원문 그대로 돌려줌"""
    m = re.match(r"^\s*(\d{4})년\s*(\d{1,2})월", str(label or ""))
    if not m:
        return label
    return f"{int(m.group(1)) % 100:02d}.{int(m.group(2)):02d}"


def _rows_html(items, cols):
    """표 본문(tbody 안에 들어갈 <tr>들). 한 줄이 페이지 중간에서 잘리지 않도록 tr 에 'rpt-row' 클래스를 둡니다.
    '상태' 칸은 화면(팀 현황) 표와 똑같이 색깔 알약 배지로, '신규' 칸은 빨간 NEW 배지로 그립니다.
    '오픈예상월'은 인쇄에서만 '26.11'처럼 축약하고, '진행경과'는 너무 길면 글자수를 제한합니다."""
    if not items:
        return f'<tr class="rpt-row"><td colspan="{len(cols)}" style="text-align:center;color:#999;">해당 사항 없음</td></tr>'
    out = []
    for it in items:
        cells = []
        for c in cols:
            v = it.get(c, "")
            if c == "상태":
                cells.append(f"<td>{_print_badge_html(v)}</td>")
            elif c == "신규":
                cells.append(f'<td>{"<span class=\'rpt-badge new\'>NEW</span>" if v else ""}</td>')
            elif c == "오픈예상월":
                cells.append(f"<td>{escape(str(_month_short(v) or '-'))}</td>")
            elif c == "진행경과":
                txt = str(v)
                if len(txt) > PRINT_NOTE_MAXLEN:
                    txt = txt[:PRINT_NOTE_MAXLEN] + "…"
                cells.append(f'<td class="pg">{escape(txt)}</td>')
            else:
                cells.append(f"<td>{escape(str(v))}</td>")
        out.append('<tr class="rpt-row">' + "".join(cells) + "</tr>")
    return "".join(out)


# 인쇄 표 칸별 기본 폭(%). 글자가 원래 짧은 칸(부문/지역/오픈예상월 등)은 좁게 고정해 두고,
# 남는 공간을 전부 '진행경과'로 몰아줘서 긴 글이 있어도 칸이 찌그러지지 않게 합니다.
# 여기 없는 칸은 7%를 기본값으로 쓰고, '진행경과'는 나머지 전부를 가져갑니다.
PRINT_COL_PCT = {"신규": 5, "상태": 6.5, "이름": 6, "오픈예상월": 5.5, "물건명": 10, "부문": 5.5, "지역": 5.5,
                 "운영형태": 6.5, "상권대분류": 7.5, "예상매출(천원)": 7, "예상손익(천원)": 7,
                 "추진주체": 7, "최초등록주차": 7, "진행주수": 5}


def _colgroup_html(cols, pct_map=PRINT_COL_PCT, wide_col="진행경과"):
    """칸마다 미리 정한 폭(%)으로 <colgroup>을 만듭니다. 나머지 공간은 전부 wide_col(진행경과)에 몰아줘서,
    부문/지역처럼 원래 글자가 짧은 칸은 좁게, 진행경과는 넓게 고정되게 합니다."""
    fixed = {c: pct_map.get(c, 7) for c in cols if c != wide_col}
    used = sum(fixed.values())
    if wide_col in cols:
        fixed[wide_col] = max(100.0 - used, 15.0)
    total = sum(fixed.values()) or 100.0
    scale = 100.0 / total
    parts = [f'<col style="width:{fixed[c] * scale:.2f}%">' for c in cols]
    return "<colgroup>" + "".join(parts) + "</colgroup>"


def _month_names_html(items):
    """물건명 위주 칩 목록. 앞의 작은 점은 상태색(오픈확정=초록, 계약완료=파랑), 담당자는 작게 뒤에 붙임."""
    if not items:
        return '<div class="mo-empty">예정 물건 없음</div>'
    out = []
    for it in items:
        st_cls = "open" if it.get("상태") == "오픈확정" else "done"
        nm = escape(it.get("물건명", "") or "-")
        who = escape(it.get("이름", ""))
        who_html = f'<em>{who}</em>' if who else ""
        out.append(f'<div class="mo-chip"><i class="dot {st_cls}"></i><span>{nm}</span>{who_html}</div>')
    return "".join(out)


def _month_cards_html(section1, opened_year=None, confirmed_n=None):
    """'월별 오픈 예정 현황'을 카드 형태로 그립니다. 이 표는 임원 보고용이라, 물건이 없는 달이라고 해서
    그 카드를 점선·회색으로 죽이거나 그래프로 다른 달과 비교해서 '이 달은 비었다'를 시각적으로 티내지
    않습니다 - 모든 달 카드를 같은 테두리·배경·글자색으로 담담하게 통일해서 두고, 숫자와 물건 목록만
    조용히 보여줍니다('예정 물건 없음'도 다른 달 목록 자리에 같은 보조색으로 적습니다). '이번 달' 표시만
    그대로 두는데, 이건 좋고 나쁨이 아니라 지금이 어디인지 알려주는 시점 안내용입니다.
    달력처럼 '이번 달부터 4개월'을 항상 고정 카드로 보여주고, 그보다 먼 미래에 물건이 있으면 카드를
    이어서 더 붙입니다. 맨 끝에는(값이 있을 때만) '지금까지 누적 출점수'와 '앞으로 열릴 확정 물건 수
    (오픈확정·계약완료)'를 보여주는 진한 색 강조 카드를 하나 더 붙입니다."""
    by_month = {}
    for it in section1:
        key = it.get("오픈예상월") or "월 미정"
        by_month.setdefault(key, []).append(it)
    today = datetime.now(KST)
    y, m = today.year, today.month
    base_months = []
    for i in range(4):
        mm = m + i
        yy = y + (mm - 1) // 12
        mo = (mm - 1) % 12 + 1
        base_months.append(f"{yy}년 {mo}월")
    last_base_key = _month_sort_key(base_months[-1])
    extra_months = sorted(
        [k for k in by_month if k != "월 미정" and k not in base_months and _month_sort_key(k) > last_base_key],
        key=_month_sort_key)
    month_keys = base_months + extra_months
    if "월 미정" in by_month:
        month_keys.append("월 미정")

    cards = []
    for i, mk in enumerate(month_keys):
        items = by_month.get(mk, [])
        label = mk if mk == "월 미정" else _month_short(mk)
        n = len(items)
        is_cur = (i == 0 and mk != "월 미정")
        cls = " cur" if is_cur else ""
        tag = '<div class="mo-tag">이번달</div>' if is_cur else ""
        cards.append(
            f'<div class="mo-card{cls}">{tag}'
            f'<div class="mo-head"><div class="mo-m">{escape(label)}</div></div>'
            f'<div class="mo-body"><div class="mo-n">{n}<small>점</small></div>'
            f'<div class="mo-list">{_month_names_html(items)}</div></div></div>'
        )
    if opened_year is not None or confirmed_n is not None:
        cards.append(
            '<div class="mo-card sum">'
            '<div class="mo-m">현재까지</div>'
            f'<div class="mo-stat"><div class="mo-n">{opened_year if opened_year is not None else "-"}<small>점</small></div>'
            '<div class="mo-sub">누적 출점수</div></div>'
            '<div class="mo-sep"></div>'
            f'<div class="mo-stat"><div class="mo-n">{confirmed_n if confirmed_n is not None else "-"}<small>건</small></div>'
            '<div class="mo-sub">오픈확정·계약완료</div></div></div>'
        )
    legend = ('<div class="mo-legend"><span><i class="dot open"></i>오픈확정</span>'
              '<span><i class="dot done"></i>계약완료</span></div>')
    return f'<div class="mo-grid">{"".join(cards)}</div>{legend}'


def _weekly_print_html(week_label, section1, section2, notes, opened_year=None, confirmed_n=None):
    """회의자료용 인쇄 HTML(2페이지 구성). 브라우저 새 탭에서 열어 Ctrl+P → 'PDF로 저장'하면 정리된 PDF가 됩니다.
    (한글 폰트를 직접 심어야 하는 PDF 라이브러리 대신, 브라우저의 한글 글꼴을 그대로 쓰는 방식이라 더 안전합니다)
    - 1페이지: 월별 오픈 예정 현황(격자표) + 계약완료·오픈확정 물건 표
    - 2페이지: 추진 물건 표 (특이사항은 인쇄본에서 제외 - notes 인자는 호환을 위해 남겨 둠)
    표 한 줄이 페이지 경계에서 잘리지 않도록 '페이지 나눔 보호' 규칙을 같이 넣었습니다(아래 CSS @media print 부분).
    ※ 실제 인쇄 결과까지 직접 확인하지 못했습니다. 표가 아주 길면(줄이 매우 많으면) 그래도 중간에서
    페이지가 나뉠 수 있는데, 그 경우에도 표 '한 줄' 단위로는 끊기지 않도록만 보장합니다."""
    # 인쇄본은 A4 폭이 고정이라 칸이 많으면 '진행경과'가 찌그러져 글자가 깨집니다. 그래서 상대적으로
    # 덜 중요한 세부상권/추진주체는 두 표 모두에서 빼고, 최초등록주차는 이미 결론이 난 '오픈확정·계약완료'
    # 표에서만 빼서(화면과 동일한 기준) 그 공간을 진행경과 쪽에 더 몰아줍니다. '추진 물건' 표에는 이번 주
    # 새로 등록된 물건을 바로 알 수 있도록 '신규' 칸을 넣습니다.
    s1_cols = ["상태", "이름", "오픈예상월", "물건명", "부문", "지역", "운영형태", "상권대분류",
               "예상매출(천원)", "예상손익(천원)", "추진주체", "진행경과"]
    s2_cols = ["신규", "상태", "이름", "오픈예상월", "물건명", "부문", "지역", "운영형태", "상권대분류",
               "예상매출(천원)", "예상손익(천원)", "최초등록주차", "진행주수", "진행경과"]

    month_grid = _month_cards_html(section1, opened_year=opened_year, confirmed_n=confirmed_n)
    s1_head = "".join(f"<th>{_th_label(c)}</th>" for c in s1_cols)
    s2_head = "".join(f"<th>{_th_label(c)}</th>" for c in s2_cols)
    s1_colgroup = _colgroup_html(s1_cols)
    s2_colgroup = _colgroup_html(s2_cols)

    return f"""
    <html><head><meta charset="utf-8"><style>
    @page {{ size: A4 landscape; margin: 10mm 9mm; }}
    * {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; color-adjust: exact; }}
    body {{ font-family: 'Malgun Gothic','맑은 고딕',sans-serif; color:#1f2d4d; padding: 16px 18px; }}
    .no-print {{ text-align:right; margin-bottom:10px; }}
    .no-print button {{ height:38px; padding:0 18px; border:none; border-radius:9px; background:#2c4a8a; color:#fff;
        font-weight:800; font-size:14px; cursor:pointer; box-shadow:0 3px 9px rgba(31,58,115,.25); }}
    .no-print button:hover {{ background:#1e3a6f; }}
    .no-print .tip {{ font-size:11.5px; color:#8591ab; margin-top:5px; }}
    @media print {{ .no-print {{ display:none !important; }} }}
    h1 {{ font-size: 19px; color:#1f3a73; border-bottom: 3px solid #1f3a73; padding-bottom:7px; margin:0 0 6px; }}
    h2 {{ font-size:14.5px; color:#2c4a8a; margin:16px 0 6px; }}
    /* 화면(팀 현황)의 .wk-tbl-wrap 과 같은 느낌의 테두리+그림자. 인쇄 HTML은 별도 문서라
       화면 CSS를 못 읽으므로 여기서 동일한 모양을 직접 다시 정의합니다(이게 화면과 인쇄가 달라 보였던 핵심 차이). */
    .rpt-wrap {{ border:1px solid #d7deee; border-radius:12px; overflow:hidden; box-shadow:0 2px 10px rgba(31,58,115,.08); margin-bottom:4px; }}
    table.rpt {{ width:100%; border-collapse: collapse; font-size:11px; table-layout: fixed; }}
    table.rpt thead th {{ background: linear-gradient(90deg, #eef3fc, #f8faff); color:#1f3a73; font-weight:800;
         padding:6px 6px; text-align:center; border-bottom:2px solid #c9d4ea; overflow-wrap:break-word; }}
    table.rpt td {{ padding:5px 6px; border-bottom:1px solid #edf0f6; text-align:center; overflow-wrap:break-word; }}
    table.rpt td.pg {{ text-align:left; word-break:break-word; line-height:1.4; }}
    table.rpt tbody tr:nth-child(even) td {{ background:#fafbfd; }}
    table.rpt tbody tr:last-child td {{ border-bottom:none; }}
    .rpt-badge {{ display:inline-block; font-size:10.5px; font-weight:800; border-radius:999px; padding:2px 10px; white-space:nowrap; }}
    .rpt-badge.open {{ background:#e3f3ec; color:#0b9a6a; }}
    .rpt-badge.done {{ background:#e8edfb; color:#2c4a8a; }}
    .rpt-badge.prog {{ background:#fef1df; color:#b25e00; }}
    .rpt-badge.new {{ background:#ffe3e3; color:#d0143c; }}
    ul {{ font-size:13px; line-height:1.6; margin:4px 0; }}
    .page2 {{ margin-top: 10px; }}
    /* 월별 오픈 예정 현황: 월별 카드. 임원 보고용이라 물건이 없는 달도 다른 달과 똑같은 테두리·배경·
       글자색으로 두어서(점선/흐림/비교 그래프 없음) '이 달은 안 좋다'는 신호를 그림으로 주지 않습니다.
       '이번달' 표시만 시점 안내용으로 남겨 둡니다. */
    .mo-grid {{ display:flex; gap:9px; margin:14px 0 2px; align-items:stretch;
        -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
    .mo-card {{ flex:1; min-width:0; border:1px solid #d7deee; border-radius:12px; overflow:visible;
        text-align:center; box-shadow:0 3px 10px rgba(31,58,115,.09); background:#ffffff;
        position:relative; display:flex; flex-direction:column; break-inside:avoid; page-break-inside:avoid; }}
    .mo-card .mo-head {{ background:linear-gradient(135deg,#3a5fa8,#2c4a8a); border-radius:11px 11px 0 0;
        padding:8px 4px 7px; border-bottom:3px solid #1f3a73; }}
    .mo-card .mo-m {{ font-size:15px; font-weight:800; color:#ffffff; letter-spacing:.5px; }}
    .mo-card.cur {{ border:1.5px solid #1f3a73; box-shadow:0 0 0 3px rgba(44,74,138,.12), 0 4px 12px rgba(31,58,115,.16); }}
    .mo-card.cur .mo-head {{ background:linear-gradient(135deg,#2c4a8a,#1a3166); padding-top:11px; }}
    .mo-card .mo-tag {{ position:absolute; top:-9px; left:50%; transform:translateX(-50%); font-size:9.5px;
        font-weight:800; letter-spacing:.3px; color:#1f3a73; background:#ffffff; border:1.5px solid #1f3a73;
        border-radius:999px; padding:1px 9px; white-space:nowrap; }}
    .mo-card .mo-body {{ padding:9px 6px 10px; display:flex; flex-direction:column; flex:1;
        background:linear-gradient(180deg,#f5f8fd 0,#ffffff 46px); border-radius:0 0 11px 11px; }}
    .mo-card .mo-n {{ font-size:25px; font-weight:800; color:#1f3a73; line-height:1.1; margin-bottom:8px; }}
    .mo-card .mo-n small {{ font-size:11px; font-weight:700; color:#8591ab; margin-left:2px; }}
    .mo-card .mo-list {{ margin-top:0; border-top:1px dashed #d7deee; padding-top:7px; min-height:16px; }}
    .mo-chip {{ display:flex; align-items:center; gap:5px; background:#eef2fa; border-radius:7px; padding:3px 6px;
        margin-bottom:4px; text-align:left; font-size:10px; font-weight:700; color:#1f3a73; line-height:1.3; }}
    .mo-chip span {{ flex:1; min-width:0; overflow-wrap:anywhere; }}
    .mo-chip em {{ font-style:normal; font-size:8.5px; font-weight:600; color:#8591ab; white-space:nowrap; }}
    .dot {{ display:inline-block; width:7px; height:7px; border-radius:50%; flex:none; margin-right:3px; }}
    .dot.open {{ background:#0b9a6a; }}
    .dot.done {{ background:#2c4a8a; }}
    .mo-chip .dot {{ margin-right:0; }}
    .mo-card .mo-empty {{ color:#9aa5bd; font-weight:500; font-size:10px; padding:2px 0; }}
    .mo-legend {{ display:flex; justify-content:flex-end; gap:12px; font-size:9.5px; color:#6b7a99; margin:5px 4px 0; }}
    .mo-card.sum {{ background:linear-gradient(165deg,#1f3a73,#2c4a8a 60%,#3a5fa8); border:none; justify-content:center;
        padding:10px 6px; box-shadow:0 4px 12px rgba(31,58,115,.25); }}
    .mo-card.sum .mo-m {{ color:#cfdcf4; font-size:12px; margin-bottom:8px; }}
    .mo-card.sum .mo-stat {{ display:flex; flex-direction:column; align-items:center; margin:2px 0; }}
    .mo-card.sum .mo-n {{ color:#fff; margin-bottom:1px; }}
    .mo-card.sum .mo-n small {{ color:#cfdcf4; }}
    .mo-card.sum .mo-sub {{ font-size:10px; color:#cfdcf4; margin-bottom:4px; }}
    .mo-card.sum .mo-sep {{ width:70%; height:1px; background:rgba(255,255,255,.3); margin:6px auto; }}
    @media print {{
      body {{ padding:0 4mm; }}
      .page2 {{ page-break-before: always; break-before: page; margin-top:0; }}
      table.rpt {{ page-break-inside: auto; }}
      table.rpt thead {{ display: table-header-group; }}  /* 표가 페이지를 넘기면 다음 페이지에도 머리글이 다시 나옴 */
      tr.rpt-row {{ page-break-inside: avoid; break-inside: avoid; }}
      h2 {{ page-break-after: avoid; break-after: avoid; }}
    }}
    </style></head><body>
    <div class="no-print"><button onclick="window.print()">인쇄하기 / PDF로 저장</button>
      <div class="tip">버튼을 누르면 1페이지(월별 오픈 현황 + 계약완료·오픈확정 물건) / 2페이지(추진 물건)로 나뉜 인쇄 대화상자가 열립니다.</div></div>
    <h1>특수개발팀 주간 현황 · {escape(week_label)}</h1>
    <div class="page page1">
      <h2>1) 월별 오픈 예정 현황</h2>
      {month_grid}
      <h2>2) 계약완료 · 오픈확정 물건</h2>
      <div class="rpt-wrap"><table class="rpt">{s1_colgroup}<thead><tr>{s1_head}</tr></thead><tbody>{_rows_html(section1, s1_cols)}</tbody></table></div>
    </div>
    <div class="page page2">
      <h2>3) 추진 물건</h2>
      <div class="rpt-wrap"><table class="rpt">{s2_colgroup}<thead><tr>{s2_head}</tr></thead><tbody>{_rows_html(section2, s2_cols)}</tbody></table></div>
    </div>
    </body></html>
    """


def _canonical_item_dict(rec, include_co=False, owner_name=""):
    """원본과 편집본 비교 및 시트 JSON 직렬화를 위한 표준 dict 생성."""
    out = {}
    for c in ITEM_COLS:
        if c == "상태":
            continue
        if c == "공동담당자":
            out[c] = _norm_co_managers(rec.get(c), owner_name) if include_co else []
        elif c in ITEM_NUM_COLS:
            out[c] = _item_num(rec.get(c))
        else:
            v = rec.get(c, "")
            out[c] = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()
    return out


def _merge_opts(base_opts, extra_vals):
    out = list(base_opts or [])
    for v in extra_vals:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _render_admin_team_editor(get_gc, D, sel, section1, section2, all_candidates, admin_name=""):
    """관리자가 팀 현황에서 1) 오픈확정·계약완료 물건과 2) 추진 물건을 직접 고쳐 저장하는 편집 화면."""
    s1_key, m1_key = f"wk_adm_s1_{sel}", f"wk_adm_m1_{sel}"
    s2_key, m2_key = f"wk_adm_s2_{sel}", f"wk_adm_m2_{sel}"
    sig_key = f"wk_adm_sig_{sel}"

    cur_sig = "|".join(
        f"{r.get('아이템ID','')}:{r.get('수정시각','')}" for r in (section1 + section2)
    )
    s1_cols = ["상태", "이름", "오픈예상월", "물건명", "부문", "지역", "운영형태", "상권대분류",
               "세부상권", "예상매출(천원)", "예상손익(천원)", "추진주체", "진행경과"]
    s2_cols = ["상태", "이름", "공동담당자", "오픈예상월", "물건명", "부문", "지역", "운영형태", "상권대분류",
               "세부상권", "예상매출(천원)", "예상손익(천원)", "추진주체", "최초등록주차", "경과(주)", "진행경과"]

    if st.session_state.get(sig_key) != cur_sig or s1_key not in st.session_state or s2_key not in st.session_state:
        data1, meta1 = [], []
        for r in section1:
            d = _parse_item_json(r.get("추진물건", ""))
            owner = str(r.get("이름", "")).strip()
            canon = _canonical_item_dict(d, include_co=False, owner_name=owner)
            row = {"상태": r.get("상태", ""), "이름": owner, **canon}
            data1.append({c: row.get(c, "") for c in s1_cols})
            meta1.append(dict(uid=r.get("사번", "").strip(), owner=owner,
                              orig_item_id=r.get("아이템ID", "").strip(), orig_canon=canon))
        df1 = pd.DataFrame(data1, columns=s1_cols) if data1 else pd.DataFrame(columns=s1_cols)
        for c in s1_cols:
            if c in ITEM_NUM_COLS:
                df1[c] = pd.to_numeric(df1[c], errors="coerce")
            else:
                df1[c] = df1[c].apply(lambda v: "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v))

        data2, meta2 = [], []
        for r in section2:
            d = _parse_item_json(r.get("추진물건", ""))
            owner = str(r.get("이름", "")).strip()
            canon = _canonical_item_dict(d, include_co=True, owner_name=owner)
            row = {
                "상태": r.get("상태", ""),
                "이름": owner,
                "최초등록주차": r.get("최초등록주차", ""),
                "경과(주)": _weeks_between(r.get("최초등록주차") or sel, sel),
                **canon,
            }
            data2.append({c: row.get(c, [] if c == "공동담당자" else "") for c in s2_cols})
            meta2.append(dict(uid=r.get("사번", "").strip(), owner=owner,
                              orig_item_id=r.get("아이템ID", "").strip(), orig_canon=canon))
        df2 = pd.DataFrame(data2, columns=s2_cols) if data2 else pd.DataFrame(columns=s2_cols)
        for c in s2_cols:
            if c == "공동담당자":
                df2[c] = _list_str_series(df2[c].tolist() if len(df2) else [])
            elif c in ITEM_NUM_COLS or c == "경과(주)":
                df2[c] = pd.to_numeric(df2[c], errors="coerce")
            else:
                df2[c] = df2[c].apply(lambda v: "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v))

        st.session_state[s1_key] = df1.reset_index(drop=True)
        st.session_state[m1_key] = meta1
        st.session_state[s2_key] = df2.reset_index(drop=True)
        st.session_state[m2_key] = meta2
        st.session_state[sig_key] = cur_sig

    select_lists = load_select_lists(get_gc)
    parsed_all = [_parse_item_json(r.get("추진물건", "")) for r in (section1 + section2)]
    month_opts = _merge_opts([""] + _future_month_labels(), [d.get("오픈예상월", "") for d in parsed_all])

    def _adm_col_cfg(include_co=False, co_opts=()):
        cfg = {
            "상태": st.column_config.TextColumn("상태", disabled=True, width="small"),
            "이름": st.column_config.TextColumn("주담당" if include_co else "이름", disabled=True, width="small"),
            "오픈예상월": st.column_config.SelectboxColumn("오픈예상월", options=month_opts, width="small"),
            "물건명": st.column_config.TextColumn("물건명", required=True, width="medium"),
            "부문": _dropdown_or_text("부문", _merge_opts(select_lists.get("부문"), [d.get("부문", "") for d in parsed_all]) if select_lists.get("부문") else []),
            "지역": st.column_config.TextColumn("지역", width="small"),
            "운영형태": _dropdown_or_text("운영형태", _merge_opts(select_lists.get("운영형태"), [d.get("운영형태", "") for d in parsed_all]) if select_lists.get("운영형태") else []),
            "상권대분류": _dropdown_or_text("상권대분류", _merge_opts(select_lists.get("상권대분류"), [d.get("상권대분류", "") for d in parsed_all]) if select_lists.get("상권대분류") else []),
            "세부상권": st.column_config.TextColumn("세부상권", width="small"),
            "예상매출(천원)": st.column_config.NumberColumn("예상매출(천원)", format="%d", width="small"),
            "예상손익(천원)": st.column_config.NumberColumn("예상손익(천원)", format="%d", width="small"),
            "추진주체": _dropdown_or_text("추진주체", _merge_opts(select_lists.get("추진주체"), [d.get("추진주체", "") for d in parsed_all]) if select_lists.get("추진주체") else []),
            "최초등록주차": st.column_config.TextColumn("최초등록주차", disabled=True, width="small"),
            "경과(주)": st.column_config.NumberColumn("경과(주)", disabled=True, width="small"),
            "진행경과": st.column_config.TextColumn("진행경과", width="large"),
        }
        if include_co:
            cfg["공동담당자"] = st.column_config.MultiselectColumn(
                "공동담당자", options=list(co_opts), default=[], width="medium",
                help="함께 추진 중인 공동담당자 (주 담당자 본인은 저장 시 자동 제외됩니다)",
            )
        return cfg

    mgr_hidden = D.get("mgr_hidden", set()) if isinstance(D, dict) else set()
    co_opts = [m for m in all_candidates if m and m not in mgr_hidden]
    for d in parsed_all:
        for cm in _norm_co_managers(d.get("공동담당자")):
            if cm not in co_opts:
                co_opts.append(cm)

    st.markdown("##### 1) 오픈확정 · 계약완료 물건 (수정 중)")
    if len(st.session_state[s1_key]):
        ed1 = st.data_editor(
            st.session_state[s1_key], column_config=_adm_col_cfg(include_co=False),
            num_rows="fixed", use_container_width=True, hide_index=True,
            key=f"wk_adm_ed1_{sel}", column_order=s1_cols,
        )
    else:
        ed1 = st.session_state[s1_key]
        st.caption("해당 사항 없음")

    st.markdown("##### 2) 추진 물건 (수정 중)")
    if len(st.session_state[s2_key]):
        ed2 = st.data_editor(
            st.session_state[s2_key], column_config=_adm_col_cfg(include_co=True, co_opts=co_opts),
            num_rows="fixed", use_container_width=True, hide_index=True,
            key=f"wk_adm_ed2_{sel}", column_order=s2_cols,
        )
    else:
        ed2 = st.session_state[s2_key]
        st.caption("해당 사항 없음")

    bc1, bc2, _ = st.columns([2, 2, 6])
    with bc1:
        save_btn = st.button("변경사항 저장", type="primary", icon=":material/save:",
                             key=f"wk_adm_save_{sel}", use_container_width=True)
    with bc2:
        cancel_btn = st.button("취소 (보기 모드)", icon=":material/close:",
                               key=f"wk_adm_cancel_{sel}", use_container_width=True)

    if cancel_btn:
        for k in (s1_key, m1_key, s2_key, m2_key, sig_key):
            st.session_state.pop(k, None)
        st.session_state[f"_close_adm_edit_{sel}"] = True
        st.rerun()

    if save_btn:
        changes = []
        empty_err = False
        for recs, metas, inc_co in (
            (ed1.to_dict("records"), st.session_state[m1_key], False),
            (ed2.to_dict("records"), st.session_state[m2_key], True),
        ):
            for rec, meta in zip(recs, metas):
                new_canon = _canonical_item_dict(rec, include_co=inc_co, owner_name=meta["owner"])
                if not new_canon["물건명"]:
                    empty_err = True
                    break
                if new_canon != meta["orig_canon"]:
                    new_iid = hashlib.md5(f"{meta['uid']}|{new_canon['물건명']}".encode("utf-8")).hexdigest()[:10]
                    changes.append(dict(
                        uid=meta["uid"],
                        orig_item_id=meta["orig_item_id"],
                        new_item_id=new_iid,
                        content=json.dumps(new_canon, ensure_ascii=False),
                    ))
            if empty_err:
                break

        if empty_err:
            st.warning("물건명은 비워둘 수 없습니다. 물건명을 확인해 주세요.")
        elif not changes:
            st.info("변경된 내용이 없습니다.")
        else:
            n_up = weekly_admin_update_items(get_gc, sel, changes, admin_name=admin_name)
            for ch in changes:
                u = ch["uid"]
                for sk in (f"wk_seed_done_{sel}_{u}", f"wk_seed_prog_{sel}_{u}", f"wk_seed_sig_{sel}_{u}"):
                    st.session_state.pop(sk, None)
            for k in (s1_key, m1_key, s2_key, m2_key, sig_key):
                st.session_state.pop(k, None)
            st.session_state[f"_close_adm_edit_{sel}"] = True
            st.session_state[f"_adm_saved_msg_{sel}"] = f"{n_up}건의 수정사항을 원본 시트에 반영했습니다."
            st.rerun()


def _render_weekly_team(get_gc, D, login_members, team_display_members, records, cur_code, cur_label,
                        is_admin=False, admin_name=""):
    # 담당자 목록: 로그인 시트(현재 실제 팀원) 기준이 기본이지만, 테스트 계정 등
    # 로그인 시트에 아직 없는 이름이 실제로 보고를 제출한 경우에도 누락되지 않도록
    # 실제 보고 데이터에 등장한 이름들을 뒤에 합쳐 둡니다. (제출했는데 목록에 안 보이는 문제 방지)
    record_names = sorted({r.get("이름", "").strip() for r in records if r.get("이름", "").strip()})
    if login_members:
        all_candidates = list(login_members) + [n for n in record_names if n not in login_members]
    else:
        all_candidates = record_names
    # 표시할 담당자: 사용자 명단의 '팀현황표시' 열이 Y인 사람만 (없으면 전체). 화면에서 직접 켜고 끄는 선택 기능은
    # 빼고 시트 기준으로만 정하도록 했습니다 - 팀원 구성을 바꾸려면 '사용자 명단' 시트의 '팀현황표시' 열(Y/N)을 고쳐주세요.
    managers = [n for n in team_display_members if n in all_candidates] or all_candidates
    if not managers:
        st.info("표시할 담당자가 없습니다. '사용자 명단' 시트의 '팀현황표시' 열을 확인해 주세요.")
        return

    weeks = sorted({r.get("주차코드", "").strip() for r in records if wk_parse(r.get("주차코드", ""))},
                   key=lambda c: wk_parse(c), reverse=True)
    if cur_code not in weeks:
        weeks = [cur_code] + weeks

    label_of = {}
    for r in records:
        c = r.get("주차코드", "").strip()
        if c and c not in label_of:
            label_of[c] = r.get("주차표시", c)
    label_of.setdefault(cur_code, cur_label)

    sel = st.selectbox("조회 주차", weeks, format_func=lambda c: wk_short_label(label_of.get(c, c)), key="wk_team_week")
    st.caption(f":material/event: {label_of.get(sel, sel)}")

    st.markdown("##### :material/check_circle: 제출 현황")
    if managers:
        items = []
        for mgr in managers:
            at = next((r["작성시각"] for r in records if r.get("유형") == "제출완료"
                       and r.get("이름", "").strip() == mgr and r.get("주차코드", "").strip() == sel), "")
            items.append(dict(name=mgr, at=at))
        st.markdown(_submit_status_html(items), unsafe_allow_html=True)

    st.divider()

    section1 = [r for r in records if r.get("유형") == "추진물건" and r.get("주차코드", "").strip() == sel
                and r.get("상태") in ("오픈확정", "계약완료") and (not managers or r.get("이름", "").strip() in managers)]
    section1.sort(key=lambda r: _month_sort_key(_parse_item_json(r.get("추진물건", "")).get("오픈예상월")))
    rows1 = [_item_view(r) for r in section1]

    def _in_managers(r):
        if not managers:
            return True
        owner = r.get("이름", "").strip()
        if owner in managers:
            return True
        co = _norm_co_managers(_parse_item_json(r.get("추진물건", "")).get("공동담당자"), owner)
        return any(m in managers for m in co)

    section2 = [r for r in records if r.get("유형") == "추진물건" and r.get("주차코드", "").strip() == sel
                and r.get("상태") == "추진물건" and _in_managers(r)]
    section2.sort(key=lambda r: (
        _month_sort_key(_parse_item_json(r.get("추진물건", "")).get("오픈예상월")),
        -_weeks_between(r.get("최초등록주차") or sel, sel),
    ))
    rows2 = [_item_view_with_weeks(r, sel) for r in section2]

    edit_toggle_key = f"wk_adm_edit_mode_{sel}"
    if st.session_state.pop(f"_close_adm_edit_{sel}", False):
        st.session_state[edit_toggle_key] = False

    saved_msg = st.session_state.pop(f"_adm_saved_msg_{sel}", None)
    if saved_msg:
        st.success(f":material/check_circle: {saved_msg}")

    admin_editing = False
    if is_admin and (section1 or section2):
        tc1, tc2 = st.columns([3, 7])
        with tc1:
            admin_editing = st.toggle(":material/edit_square: 관리자 수정 모드", key=edit_toggle_key,
                                      help="담당자가 제출한 물건의 오타·매출·진행경과 등을 관리자가 직접 수정해 원본에 반영합니다.")
        with tc2:
            if admin_editing:
                st.caption("셀을 더블클릭해 내용을 고친 뒤 아래 **변경사항 저장** 버튼을 눌러주세요. (원본 덮어쓰기 + 수정자 기록)")

    if admin_editing:
        _render_admin_team_editor(get_gc, D, sel, section1, section2, all_candidates, admin_name=admin_name)
    else:
        for k in (f"wk_adm_s1_{sel}", f"wk_adm_m1_{sel}", f"wk_adm_s2_{sel}", f"wk_adm_m2_{sel}", f"wk_adm_sig_{sel}"):
            st.session_state.pop(k, None)

        st.markdown("##### 1) 오픈확정 · 계약완료 물건")
        if rows1:
            # 오픈확정·계약완료 물건은 이미 결론이 난 물건이라 '최초등록주차'가 큰 의미가 없어서 표에서 뺍니다
            # (추진 물건 표에는 진행 기간 관리를 위해 그대로 남겨 둡니다).
            s1_cols = [c for c in rows1[0].keys() if c != "최초등록주차"]
            st.markdown(_wk_table_html(rows1, s1_cols, status_col="상태", wrap_cols=("진행경과",)),
                        unsafe_allow_html=True)
        else:
            st.caption("해당 사항 없음")

        st.markdown("##### 2) 추진 물건")
        if rows2:
            s2_cols = list(rows2[0].keys())
            st.markdown(_wk_table_html(rows2, s2_cols, status_col="상태", wrap_cols=("진행경과",)),
                        unsafe_allow_html=True)
            stale = [r for r in rows2 if r["진행주수"] >= 4]
            if stale:
                st.warning(":material/warning: 4주 이상 그대로인 물건: " + ", ".join(f"{r['이름']}·{r['물건명']}" for r in stale))
        else:
            st.caption("해당 사항 없음")

    st.markdown("##### 3) 특이사항")
    notes = [r for r in records if r.get("유형") == "특이사항" and r.get("주차코드", "").strip() == sel
             and (not managers or r.get("이름", "").strip() in managers) and r.get("특이사항", "").strip()]
    if notes:
        for r in notes:
            st.markdown(f"- **{r['이름']}**: {r['특이사항']}")
    else:
        st.caption("해당 사항 없음")

    st.caption(":material/calendar_month: 담당자별 주간 일정은 옆의 '팀 활동계획' 탭에서 볼 수 있습니다.")

    if is_admin:   # 데이터를 지우는 정리 기능이라 관리자에게만 보입니다
        st.divider()
        with st.expander(":material/cleaning_services: (관리자) 특이사항 중복 행 정리"):
            st.caption("업서트 방식을 적용하기 전에 같은 주차·같은 담당자로 여러 번 제출해서 쌓인 '특이사항' 중복 행을 "
                       "한 번만 정리합니다. 같은 (주차, 담당자) 조합 중 가장 나중 것만 남기고 나머지를 지웁니다. "
                       "앞으로 제출되는 특이사항은 이미 중복 없이 덮어쓰기 방식으로 저장되므로, 이 정리는 과거에 쌓인 것만 지웁니다.")
            if st.button("중복 특이사항 정리 실행", icon=":material/cleaning_services:", key="wk_note_dedupe_btn"):
                n = weekly_dedupe_notes(get_gc)
                if n:
                    st.success(f":material/check_circle: 중복 {n}건을 정리했습니다.")
                else:
                    st.info("정리할 중복이 없습니다.")
                st.rerun()

    # '월별 오픈 예정 현황' 카드의 맨 끝 요약 카드용 숫자: 실적현황 탭과 숫자가 어긋나지 않도록
    # 별도로 입력받지 않고, 이미 있는 실적 데이터(D)와 이번 주 표에서 바로 계산합니다.
    opened_year = None
    if isinstance(D, dict) and D.get("masters"):
        cur_year = str(datetime.now(KST).year)
        opened_year = sum(1 for m in D["masters"] if str(m.get("open_ym", "")).startswith(cur_year))
    confirmed_n = len(rows1) if rows1 else 0

    st.divider()
    st.markdown("##### :material/print: 회의자료용 인쇄 보기")
    st.caption("아래를 열면 회의자료가 보이고, 그 안의 '인쇄하기 / PDF로 저장' 버튼을 누르면 "
               "1페이지(월별 오픈 현황 + 계약완료·오픈확정 물건) / 2페이지(추진 물건)로 나뉜 인쇄 대화상자가 열립니다. "
               "(브라우저 자체 인쇄 기능 대신 꼭 이 안의 버튼을 눌러주세요 - 그래야 디자인이 그대로 인쇄됩니다)")
    with st.expander("인쇄용 보기 열기"):
        html = _weekly_print_html(label_of.get(sel, sel), rows1, rows2, notes,
                                   opened_year=opened_year, confirmed_n=confirmed_n)
        components.html(html, height=750, scrolling=True)


def _render_weekly_plan(login_members, team_display_members, records, cur_code, cur_label):
    """'팀 활동계획' 탭: 담당자별 주간 일정만 따로 떼어서 보여줍니다(추진물건 표가 길어져도 일정은 바로 보임)."""
    record_names = sorted({r.get("이름", "").strip() for r in records if r.get("이름", "").strip()})
    if login_members:
        all_candidates = list(login_members) + [n for n in record_names if n not in login_members]
    else:
        all_candidates = record_names
    managers = [n for n in team_display_members if n in all_candidates] or all_candidates
    if not managers:
        st.info("표시할 담당자가 없습니다. '사용자 명단' 시트의 '팀현황표시' 열을 확인해 주세요.")
        return

    weeks = sorted({r.get("주차코드", "").strip() for r in records if wk_parse(r.get("주차코드", ""))},
                   key=lambda c: wk_parse(c), reverse=True)
    if cur_code not in weeks:
        weeks = [cur_code] + weeks
    label_of = {}
    for r in records:
        c = r.get("주차코드", "").strip()
        if c and c not in label_of:
            label_of[c] = r.get("주차표시", c)
    label_of.setdefault(cur_code, cur_label)

    # '이전 주 / 다음 주' 버튼은 (휴게소 데이터허브의 주차별 활동 대시보드와 같은 방식으로) 보고가 없는
    # 주차로도 자유롭게 이동할 수 있어야 하므로, 버튼으로 옮겨간 주차가 실제 보고 목록(weeks)에 없으면
    # 그 주차도 선택지에 임시로 끼워 넣어 줍니다(그래야 선택 칸이 '값이 목록에 없다'는 오류 없이 그대로 보여줌).
    nav_key = "wk_plan_week"
    if nav_key in st.session_state and st.session_state[nav_key] not in weeks:
        weeks = sorted(set(weeks) | {st.session_state[nav_key]},
                       key=lambda c: wk_parse(c) or (0, 0, 0), reverse=True)

    st.caption("조회 주차")
    c_prev, c_sel, c_next = st.columns([1, 4, 1])
    with c_sel:
        sel = st.selectbox("조회 주차", weeks,
                            format_func=lambda c: wk_short_label(label_of.get(c) or wk_label_from_code(c)),
                            key=nav_key, label_visibility="collapsed")

    # 이전주/다음주 버튼: st.session_state[nav_key] 를 'if st.button(...):' 안에서 직접 덮어쓰면,
    # 바로 위 selectbox(같은 key)가 이미 그려진 뒤라 스트림릿이 '위젯이 이미 그려진 뒤에는 그 세션 값을
    # 못 바꾼다'며 오류를 냅니다(데이터 유무와는 무관한 문제). on_click 콜백은 화면을 다시 그리기 '전에'
    # 실행되므로 이 문제가 생기지 않습니다 - 담당자 필터 알약(pill_select)에서 이미 쓰고 있는 방식과 동일.
    def _goto_week(code):
        st.session_state[nav_key] = code

    with c_prev:
        st.button("◀ 이전 주", key="wk_plan_prev", use_container_width=True,
                  on_click=_goto_week, args=(wk_prev(sel),))
    with c_next:
        st.button("다음 주 ▶", key="wk_plan_next", use_container_width=True,
                  on_click=_goto_week, args=(wk_next(sel),))
    st.caption(f":material/event: {label_of.get(sel) or wk_label_from_code(sel)}")

    prev_code = wk_prev(sel)
    prev_sched_by_name = {}
    for r in records:
        if r.get("유형") == "일정" and r.get("주차코드", "").strip() == prev_code:
            try:
                prev_sched_by_name[r.get("이름", "").strip()] = json.loads(r.get("추진물건", "") or "{}")
            except Exception:
                pass

    sched_by_mgr = {}
    for mgr in managers:
        sch = next((r for r in records if r.get("유형") == "일정" and r.get("주차코드", "").strip() == sel
                   and r.get("이름", "").strip() == mgr), None)
        try:
            sched_by_mgr[mgr] = json.loads(sch.get("추진물건", "") or "{}") if sch else {}
        except Exception:
            sched_by_mgr[mgr] = {}
    st.markdown("##### :material/calendar_month: 담당자별 주간 일정")
    st.markdown(_wk_schedule_table_html(SCHEDULE_DAYS, SCHEDULE_HALVES, managers, sched_by_mgr, prev_sched_by_name),
                unsafe_allow_html=True)
    st.caption(":material/history: = 지난주와 동일한 일정")


def render_weekly(get_gc, D, uid, uname, is_admin):
    cur_code, cur_label = wk_info()
    records = load_weekly_all(get_gc)

    # 팀 현황·팀 활동계획·인쇄는 모든 사용자에게 공개합니다. (관리자 전용으로 남기는 것: 중복 행 정리 등 데이터 정리 기능)
    login_members = load_login_members(get_gc)
    team_display_members = load_team_display_members(get_gc)
    with st.container(key="lvl2tabs"):
        t_mine, t_team, t_plan = st.tabs([":material/edit: 내 보고 작성", ":material/groups: 팀 현황",
                                          ":material/calendar_month: 팀 활동계획"])
    with t_mine:
        _render_weekly_mine(get_gc, records, uid, uname, cur_code, cur_label,
                            D=D, login_members=login_members, team_display_members=team_display_members)
    with t_team:
        _render_weekly_team(get_gc, D, login_members, team_display_members, records, cur_code, cur_label,
                            is_admin=is_admin, admin_name=uname)
    with t_plan:
        _render_weekly_plan(login_members, team_display_members, records, cur_code, cur_label)


# ───────────────────────── 준비 중인 메뉴 (자리만 먼저 만들어 둠) ─────────────────────────
def _coming_soon(icon, title, desc, bullets):
    st.markdown(sec_heading(icon, title), unsafe_allow_html=True)
    st.info(f":material/construction: {desc}")
    st.markdown("**앞으로 들어갈 기능(예정)**")
    for b in bullets:
        st.markdown(f"- {b}")


# 휴게소 지도(index.html/bid.html)와 같은 깃허브 페이지 저장소에 함께 올려 둘 SD 지도 전용 정적 페이지.
# (components.html 안에 카카오 SDK 스크립트를 바로 심으면 iframe 출처가 없어서(about:srcdoc) 카카오가
#  등록된 도메인으로 인식하지 못해 지도가 안 뜹니다. 그래서 bid.html 과 똑같이, 도메인이 있는 깃허브
#  페이지를 iframe 으로 불러오고 postMessage 로 점포 데이터만 전달합니다. 카카오 JS 키는 sd-map.html
#  안에 bid.html 과 동일한 값으로 고정돼 있어서(이미 이 도메인에 등록된 키) 여기서는 신경 쓸 필요가 없습니다.)
SD_MAP_PAGE_URL = "https://k958677wpal-oss.github.io/rest-area-map/sd-map.html"
SD_MAP_TARGET_ORIGIN = "https://k958677wpal-oss.github.io"


def _sd_map_component_html(points, height=560):
    """points: [{"name":, "brand":, "lat":, "lng":, "kind":, "biz":, "daily":}, ...]
    sd-map.html(깃허브 페이지) 을 iframe 으로 불러온 뒤, postMessage 로 점포 데이터만 전달합니다.
    카카오 JS 키는 bid.html 과 똑같이 sd-map.html 안에 이미 고정돼 있어서 여기서 따로 보낼 필요가 없습니다."""
    points_json = json.dumps(points, ensure_ascii=False)
    return f"""
    <iframe
        id="sdMapFrame"
        src="{SD_MAP_PAGE_URL}"
        style="width:100%; height:{height}px; border:none;"
        scrolling="no"
    ></iframe>
    <script>
      (function () {{
        var mapPoints = {points_json};
        var iframe = document.getElementById('sdMapFrame');
        function sendData() {{
          iframe.contentWindow.postMessage(
            {{ type: 'INIT_SD_MAP_DATA', points: mapPoints }},
            '{SD_MAP_TARGET_ORIGIN}'
          );
        }}
        iframe.onload = function () {{ sendData(); setTimeout(sendData, 1200); }};
      }})();
    </script>
    """


def view_store_map(get_gc, D):
    st.markdown(sec_heading("pin", "전국 특수점 지도"), unsafe_allow_html=True)

    team_rows = [{
        "구분": "우리팀 오픈점", "name": m["name"], "brand": "GS25",
        "biz_major": m.get("biz_major", ""), "biz_minor": m.get("biz_minor", ""),
        "address": m.get("address", ""), "lat": m.get("lat"), "lng": m.get("lng"),
        "daily_sales": None,
    } for m in D["masters"]]
    corp_rows = [{
        "구분": "전사 특수점", "name": r["name"], "brand": r.get("brand", "GS25"),
        "biz_major": r.get("biz_major", ""), "biz_minor": r.get("biz_minor", ""),
        "address": r.get("address", ""), "lat": r.get("lat"), "lng": r.get("lng"),
        "daily_sales": r.get("daily_sales"),
    } for r in load_corp_stores(get_gc)]
    all_rows = team_rows + corp_rows

    if not corp_rows:
        st.caption(":material/info: '전사특수점' 탭에서 아직 데이터를 읽지 못했습니다(탭이 비어 있거나 아직 안 만들어졌으면 우리팀 오픈점만 표시됩니다).")

    kinds = ["우리팀 오픈점", "전사 특수점"]
    bizs = sorted({r["biz_major"] for r in all_rows if r["biz_major"]})
    brands = sorted({r["brand"] for r in all_rows if r["brand"]})

    c1, c2, c3 = st.columns(3)
    with c1:
        f_kind = st.multiselect("구분", kinds, default=kinds, key="map_f_kind")
    with c2:
        f_biz = st.selectbox("업종대분류", ["전체"] + bizs, key="map_f_biz")
    with c3:
        f_brand = st.multiselect("브랜드", brands, default=brands, key="map_f_brand")

    filtered = [r for r in all_rows
                if r["구분"] in f_kind
                and (f_biz == "전체" or r["biz_major"] == f_biz)
                and (not brands or r["brand"] in f_brand)]
    st.caption(f"조건에 맞는 점포: {len(filtered)}점 (좌표가 입력된 점포만 지도에 핀으로 표시됩니다)")

    pin_rows = [r for r in filtered if r.get("lat") is not None and r.get("lng") is not None]

    points = [{"name": r["name"], "brand": r["brand"], "kind": r["구분"], "biz": r["biz_major"],
                "daily": r["daily_sales"], "lat": r["lat"], "lng": r["lng"]} for r in pin_rows]
    components.html(_sd_map_component_html(points, height=560), height=560, scrolling=False)
    if pin_rows:
        st.caption(f":material/push_pin: 지도에 핀으로 표시된 점포: {len(pin_rows)}점 (녹색 = 우리팀 오픈점, 네이비 = 전사 특수점)")
    else:
        st.caption(":material/location_off: 아직 위도/경도가 입력된 점포가 없어서 지도에 찍을 핀이 없습니다. 점포마스터/전사특수점 탭에 위도·경도를 채워 주시면 자동으로 지도에 나타납니다.")

    if filtered:
        with st.expander(f"📋 필터링된 목록 보기 ({len(filtered)}점)", expanded=False):
            df = pd.DataFrame([{
                "구분": r["구분"], "점포명": r["name"], "브랜드": r["brand"],
                "업종대분류": r["biz_major"], "업종세부": r["biz_minor"], "주소": r["address"],
                "위도": r["lat"], "경도": r["lng"], "일매출(천원)": r["daily_sales"],
            } for r in filtered])
            st.dataframe(df, use_container_width=True, hide_index=True)

            if st.button(":material/upload_file: 필터링된 목록 구글시트로 내보내기", key="map_export_btn"):
                try:
                    n = export_map_rows(get_gc, filtered)
                    st.success(f"'{MAP_EXPORT_TAB}' 탭에 {n}건을 내보냈습니다.")
                except Exception as e:
                    st.error(f"내보내기에 실패했습니다: {e}")


# ───────────────────────── 입찰 관리 (누계 원장 · 자동 수식 · 엑셀 추출) ─────────────────────────
BID_TRACKER_TAB = "입찰관리"
BID_TRACKER_HEADERS = [
    "입찰ID", "등록일시", "수정일시", "수정자", "사번", "담당자",
    "입찰연월", "공고명", "점포수", "추진타입", "기존운영업체",
    "당사결과", "참여경쟁사", "낙찰업체", "당사순위", "낙찰조건",
    "보증금(천원)", "월임차료(천원)", "가맹수수료율(%)",
    "예상일매출(천원)", "영업일수", "BEP일매출(천원)", "예상매익률(%)", "예상이익(천원)", "비고",
]

BID_RESULT_OPTS = ["진행중", "낙찰", "패찰", "유찰", "미참여"]
BID_COMPETITOR_OPTS = ["CU", "7-11", "emart24", "기타"]
BID_EXISTING_OPTS = ["", "신규", "CU", "7-11", "emart24", "GS25", "기타"]
BID_WINNER_OPTS = ["", "GS25", "CU", "7-11", "emart24", "기타"]
BID_TYPE_DEFAULTS = ["GS1", "GS2", "GS3", "순수직영", "본부임차", "기타"]


def _bid_num_clean(v):
    """입찰 숫자 입력값에서 콤마·공백은 물론 실수로 함께 적은 '%', '천원', '원' 단위도 제거해 숫자로 변환합니다."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return None if (v != v) else float(v)
    s = str(v).strip().replace(",", "")
    s = re.sub(r"(천원|원|%|\s)+$", "", s).strip()
    if not s:
        return None
    try:
        f = float(s)
        return None if (f != f) else f
    except ValueError:
        return None

BID_NUM_FIELDS = (
    "점포수", "보증금(천원)", "월임차료(천원)", "가맹수수료율(%)",
    "예상일매출(천원)", "영업일수", "BEP일매출(천원)", "예상매익률(%)", "예상이익(천원)",
)

_BID_RESULT_BADGE = {
    "낙찰": ("win", "낙찰"),
    "패찰": ("lose", "패찰"),
    "진행중": ("wait", "진행중"),
    "유찰": ("void", "유찰"),
    "미참여": ("skip", "미참여"),
}


def _bid_result_badge_html(status):
    cls, lbl = _BID_RESULT_BADGE.get(str(status or "").strip(), ("wait", status or "-"))
    return f'<span class="wk-badge {cls}">{escape(str(lbl))}</span>'


def _norm_bid_ym(val):
    """어떤 연월 표기('2026-05', '2026년05월', '202611' 등)든 ('202605', '2026년 5월')로 표준화합니다."""
    ym = _ym(val)
    if ym:
        return ym, ym_label(ym)
    s = str(val or "").strip()
    return "", s


def _bid_month_options(extra_labels=()):
    """최근 36개월 ~ 향후 12개월 연월 선택지 생성 (최신 연월이 위쪽에 오도록 정렬)."""
    today = datetime.now(KST).date()
    y, m = today.year, today.month
    yms = set()
    for delta in range(-36, 13):
        total_m = (y * 12 + (m - 1)) + delta
        yy, mm = total_m // 12, (total_m % 12) + 1
        yms.add(f"{yy:04d}{mm:02d}")
    labels = [ym_label(k) for k in sorted(yms, reverse=True)]
    for ex in extra_labels:
        s = str(ex or "").strip()
        if s and s not in labels:
            labels.append(s)
    return labels


def _norm_competitors(val):
    """참여경쟁사 문자열/리스트를 깔끔한 리스트로 정규화합니다 (GS25 본인 제외)."""
    if val is None:
        return []
    if isinstance(val, str):
        parts = [p.strip() for p in re.split(r"[,·/]", val) if p.strip()]
    else:
        try:
            if isinstance(val, float) and val != val:
                return []
            parts = [str(x).strip() for x in val if x is not None and str(x).strip() and str(x).strip() != "<NA>"]
        except TypeError:
            return []
    out = []
    for p in parts:
        if p and p.upper() != "GS25" and p not in out:
            out.append(p)
    return out


def _ensure_bid_ws(get_gc, create=True):
    """'특수팀_실적관리' 시트 내 '입찰관리' 탭을 반환하며, 없으면 헤더와 함께 자동 생성합니다."""
    gc = get_gc()
    doc = gc.open(PERF_SHEET_NAME)
    try:
        ws = doc.worksheet(BID_TRACKER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        ws = doc.add_worksheet(title=BID_TRACKER_TAB, rows=2000, cols=len(BID_TRACKER_HEADERS))
        ws.append_row(BID_TRACKER_HEADERS)
        return ws
    header = ws.row_values(1)
    if not header:
        if ws.col_count < len(BID_TRACKER_HEADERS):
            ws.add_cols(len(BID_TRACKER_HEADERS) - ws.col_count)
        ws.update("A1", [BID_TRACKER_HEADERS])
        return ws
    missing = [h for h in BID_TRACKER_HEADERS if h not in header]
    if missing:
        new_header = header + missing
        if ws.col_count < len(new_header):
            ws.add_cols(len(new_header) - ws.col_count)
        ws.update("A1", [new_header])
    return ws


def _enrich_bid_record(rec):
    """원본 시트 한 줄에 파생 수식(연간총임차료, 연예상매출액, 매출대비임차료율)과 표준 연월 키를 계산해 붙입니다."""
    ym_key, ym_disp = _norm_bid_ym(rec.get("입찰연월", ""))
    stores_num = _item_num(rec.get("점포수"))
    stores = max(1, int(round(stores_num))) if stores_num is not None and stores_num > 0 else 1

    deposit = _bid_num_clean(rec.get("보증금(천원)"))
    m_rent = _bid_num_clean(rec.get("월임차료(천원)"))
    fee_pct = _bid_num_clean(rec.get("가맹수수료율(%)"))
    daily_sales = _bid_num_clean(rec.get("예상일매출(천원)"))
    op_days = _bid_num_clean(rec.get("영업일수"))
    bep_raw = rec.get("BEP일매출(천원)") if rec.get("BEP일매출(천원)", "") != "" else rec.get('"0"일매출(천원)', "")
    bep_daily = _bid_num_clean(bep_raw)
    margin_pct = _bid_num_clean(rec.get("예상매익률(%)"))
    est_profit = _bid_num_clean(rec.get("예상이익(천원)"))

    # 기존 엑셀 수식 100% 자동 계산
    annual_rent = (m_rent * 12.0) if m_rent is not None else None
    annual_sales = (daily_sales * op_days) if (daily_sales is not None and op_days and op_days > 0) else None
    rent_ratio = (annual_rent / annual_sales * 100.0) if (annual_rent is not None and annual_sales and annual_sales > 0) else None

    res_status = str(rec.get("당사결과", "")).strip() or "진행중"
    if res_status not in BID_RESULT_OPTS:
        res_status = "진행중"
    winner = str(rec.get("낙찰업체", "")).strip()
    if res_status == "낙찰" and not winner:
        winner = "GS25"
    comps = _norm_competitors(rec.get("참여경쟁사", ""))

    return {
        "입찰ID": str(rec.get("입찰ID", "")).strip(),
        "등록일시": str(rec.get("등록일시", "")).strip(),
        "수정일시": str(rec.get("수정일시", "")).strip(),
        "수정자": str(rec.get("수정자", "")).strip(),
        "사번": str(rec.get("사번", "")).strip(),
        "담당자": str(rec.get("담당자", "")).strip(),
        "입찰연월": ym_disp,
        "ym_key": ym_key,
        "year": ym_key[:4] if len(ym_key) >= 4 else "",
        "공고명": str(rec.get("공고명", "")).strip(),
        "점포수": stores,
        "추진타입": str(rec.get("추진타입", "")).strip(),
        "기존운영업체": str(rec.get("기존운영업체", "")).strip(),
        "당사결과": res_status,
        "참여경쟁사_list": comps,
        "참여경쟁사": ", ".join(comps),
        "낙찰업체": winner,
        "당사순위": str(rec.get("당사순위", "")).strip(),
        "낙찰조건": str(rec.get("낙찰조건", "")).strip(),
        "보증금(천원)": deposit,
        "월임차료(천원)": m_rent,
        "가맹수수료율(%)": fee_pct,
        "연간임차료(천원)": annual_rent,
        "임차료율(%)": rent_ratio,
        "예상일매출(천원)": daily_sales,
        "영업일수": int(round(op_days)) if op_days is not None and op_days > 0 else None,
        "연예상매출(천원)": annual_sales,
        "BEP일매출(천원)": bep_daily,
        "예상매익률(%)": margin_pct,
        "예상이익(천원)": est_profit,
        "비고": str(rec.get("비고", "")).strip(),
    }


@st.cache_data(ttl=120, show_spinner="입찰 내역을 불러오는 중...")
def load_bid_tracker_all(_get_gc):
    ws = _ensure_bid_ws(_get_gc, create=True)
    if ws is None:
        return []
    raw_rows = _records(ws.get_all_values())
    out = []
    for r in raw_rows:
        if not str(r.get("공고명", "")).strip():
            continue
        if not str(r.get("입찰ID", "")).strip():
            r["입찰ID"] = hashlib.md5(
                f"{r.get('담당자','')}|{r.get('입찰연월','')}|{r.get('공고명','')}".encode("utf-8")
            ).hexdigest()[:10]
        out.append(_enrich_bid_record(r))
    # 최신 입찰연월 -> 최신 등록순 정렬
    out.sort(key=lambda x: (x["ym_key"] or "000000", x["등록일시"]), reverse=True)
    return out


def clear_bid_tracker_cache():
    load_bid_tracker_all.clear()


def _bid_row_from_dict(header, d):
    row = [""] * len(header)
    for col_name in BID_TRACKER_HEADERS:
        if col_name not in header:
            continue
        idx = header.index(col_name)
        val = d.get(col_name, "")
        if val is None or (isinstance(val, float) and pd.isna(val)):
            row[idx] = ""
        elif isinstance(val, float) and val.is_integer() and col_name in ("점포수", "영업일수", "보증금(천원)", "월임차료(천원)", "예상일매출(천원)", "BEP일매출(천원)", "예상이익(천원)"):
            row[idx] = int(val)
        else:
            row[idx] = val
    return row


def append_bid_record(get_gc, payload):
    """새 입찰 건 1건을 시트에 추가합니다."""
    ws = _ensure_bid_ws(get_gc, create=True)
    header = ws.row_values(1) or BID_TRACKER_HEADERS
    now = _weekly_now()
    rec = dict(payload)
    rec["입찰ID"] = rec.get("입찰ID") or uuid.uuid4().hex[:10]
    rec["등록일시"] = now
    rec["수정일시"] = now
    ws.append_row(_bid_row_from_dict(header, rec))
    clear_bid_tracker_cache()
    return rec["입찰ID"]


def apply_bid_changes(get_gc, updates_by_id, delete_ids, editor_name=""):
    """입찰ID 기준으로 수정된 행 일괄 반영 및 삭제 체크된 행 일괄 삭제 (시트 1회 조회)."""
    ws = _ensure_bid_ws(get_gc, create=True)
    values = ws.get_all_values()
    if not values:
        return 0, 0
    header = values[0]
    if "입찰ID" not in header:
        return 0, 0
    id_idx = header.index("입찰ID")
    now = _weekly_now()
    del_set = {str(x).strip() for x in (delete_ids or ()) if str(x).strip()}

    batch_updates = []
    del_rns = []
    for rn in range(2, len(values) + 1):
        row = values[rn - 1]
        bid_id = row[id_idx].strip() if id_idx < len(row) else ""
        if not bid_id:
            continue
        if bid_id in del_set:
            del_rns.append(rn)
            continue
        if bid_id in updates_by_id:
            old_map = dict(zip(header, list(row) + [""] * max(0, len(header) - len(row))))
            merged = dict(old_map)
            merged.update(updates_by_id[bid_id])
            merged["입찰ID"] = bid_id
            merged["등록일시"] = old_map.get("등록일시", "") or now
            merged["수정일시"] = now
            merged["수정자"] = str(editor_name or "").strip()
            batch_updates.append({"range": f"A{rn}", "values": [_bid_row_from_dict(header, merged)]})

    if batch_updates:
        ws.batch_update(batch_updates)
    for rn in sorted(del_rns, reverse=True):
        ws.delete_rows(rn)
    if batch_updates or del_rns:
        clear_bid_tracker_cache()
    return len(batch_updates), len(del_rns)


BID_EXPORT_COLUMNS = [
    ("순번", "no", "int"),
    ("입찰연월", "입찰연월", "str"),
    ("공고명", "공고명", "str"),
    ("점포수", "점포수", "int"),
    ("담당자", "담당자", "str"),
    ("추진타입", "추진타입", "str"),
    ("기존운영업체", "기존운영업체", "str"),
    ("당사결과", "당사결과", "str"),
    ("참여경쟁사", "참여경쟁사", "str"),
    ("낙찰업체", "낙찰업체", "str"),
    ("당사순위", "당사순위", "str"),
    ("낙찰조건/낙찰가율", "낙찰조건", "str"),
    ("보증금(천원)", "보증금(천원)", "int"),
    ("월임차료(천원)", "월임차료(천원)", "int"),
    ("가맹수수료율(%)", "가맹수수료율(%)", "pct"),
    ("연간총임차료(천원)", "연간임차료(천원)", "int"),
    ("매출액 比 임차료율(%)", "임차료율(%)", "pct"),
    ("1년차예상일매출(천원)", "예상일매출(천원)", "int"),
    ("영업일수", "영업일수", "int"),
    ('"0"일매출(천원)', "BEP일매출(천원)", "int"),
    ("예상매익률(%)", "예상매익률(%)", "pct"),
    ("1년차예상이익(천원)", "예상이익(천원)", "int"),
    ("비고", "비고", "str"),
    ("등록일시", "등록일시", "str"),
    ("수정일시", "수정일시", "str"),
]


def _minimal_xlsx_bytes(headers, rows_matrix):
    """openpyxl 이 없는 배포 환경에서도 표준 .xlsx 파일을 생성하기 위한 순수 표준 라이브러리 백업 생성기."""
    def _col_letter(n):
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet_rows = []
    all_rows = [headers] + rows_matrix
    for r_idx, r_vals in enumerate(all_rows, start=1):
        cells = []
        for c_idx, v in enumerate(r_vals, start=1):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if r_idx > 1 and isinstance(v, (int, float)) and not isinstance(v, bool) and not pd.isna(v):
                cells.append(f'<c r="{ref}" t="n"><v>{v}</v></c>')
            else:
                txt = "" if v is None else escape(str(v))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    '</Types>')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                    '</Relationships>')
        zf.writestr("xl/workbook.xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="입찰내역" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                    '</Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def build_bids_xlsx_bytes(enriched_rows):
    """입찰 내역(자동계산 수식 결과 포함)을 서식이 정리된 엑셀(.xlsx) 바이트로 변환합니다."""
    headers = [col_title for col_title, _, _ in BID_EXPORT_COLUMNS]
    matrix = []
    for idx, r in enumerate(enriched_rows, start=1):
        row_vals = []
        for _, key, kind in BID_EXPORT_COLUMNS:
            if key == "no":
                row_vals.append(idx)
                continue
            v = r.get(key)
            if v is None or v == "":
                row_vals.append("")
            elif kind == "int":
                try:
                    row_vals.append(int(round(float(v))))
                except Exception:
                    row_vals.append(v)
            elif kind == "pct":
                try:
                    row_vals.append(round(float(v), 2))
                except Exception:
                    row_vals.append(v)
            else:
                row_vals.append(str(v))
        matrix.append(row_vals)

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "입찰내역"
        ws.append(headers)

        head_fill = PatternFill("solid", fgColor="1F3A73")
        head_font = Font(color="FFFFFF", bold=True, size=10)
        win_fill = PatternFill("solid", fgColor="F1FAF6")
        thin = Side(border_style="thin", color="D5DCEA")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for col_i in range(1, len(headers) + 1):
            c = ws.cell(row=1, column=col_i)
            c.fill = head_fill
            c.font = head_font
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
        ws.row_dimensions[1].height = 24

        for r_i, r_vals in enumerate(matrix, start=2):
            ws.append(r_vals)
            is_win = (len(r_vals) > 7 and r_vals[7] == "낙찰")
            for c_i, (_, _, kind) in enumerate(BID_EXPORT_COLUMNS, start=1):
                cell = ws.cell(row=r_i, column=c_i)
                cell.border = border
                if is_win:
                    cell.fill = win_fill
                if kind == "int" and isinstance(cell.value, (int, float)):
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif kind == "pct" and isinstance(cell.value, (int, float)):
                    cell.number_format = "0.00"
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif headers[c_i - 1] in ("공고명", "낙찰조건/낙찰가율", "비고"):
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center")

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for c_i, h in enumerate(headers, start=1):
            col_let = get_column_letter(c_i)
            max_len = max([len(str(h)) * 1.7] + [len(str(r[c_i - 1])) * 1.3 for r in matrix[:200]])
            ws.column_dimensions[col_let].width = min(max(max_len + 3, 10), 36)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception:
        return _minimal_xlsx_bytes(headers, matrix)


def _canonical_bid_payload(rec, uid, uname):
    """편집기 또는 입력폼 한 줄을 시트 저장용 표준 dict로 정규화합니다."""
    _, ym_disp = _norm_bid_ym(rec.get("입찰연월", ""))
    res_status = str(rec.get("당사결과", "") or "진행중").strip()
    if res_status not in BID_RESULT_OPTS:
        res_status = "진행중"
    comps = _norm_competitors(rec.get("참여경쟁사"))
    winner = str(rec.get("낙찰업체", "") or "").strip()
    if winner.startswith("자동"):
        winner = ""
    if res_status == "낙찰":
        winner = "GS25"
    elif res_status in ("진행중", "유찰") and winner == "GS25":
        winner = ""

    stores_v = _item_num(rec.get("점포수"))
    stores = max(1, int(round(stores_v))) if stores_v is not None and stores_v > 0 else 1
    days_v = _item_num(rec.get("영업일수"))
    days = int(round(days_v)) if days_v is not None and days_v > 0 else None

    return {
        "사번": str(uid or "").strip(),
        "담당자": str(uname or "").strip(),
        "입찰연월": ym_disp,
        "공고명": str(rec.get("공고명") or "").strip(),
        "점포수": stores,
        "추진타입": "" if pd.isna(rec.get("추진타입")) else str(rec.get("추진타입") or "").strip(),
        "기존운영업체": "" if pd.isna(rec.get("기존운영업체")) else str(rec.get("기존운영업체") or "").strip(),
        "당사결과": res_status,
        "참여경쟁사": ", ".join(comps),
        "낙찰업체": winner,
        "당사순위": "" if pd.isna(rec.get("당사순위")) else str(rec.get("당사순위") or "").strip(),
        "낙찰조건": "" if pd.isna(rec.get("낙찰조건")) else str(rec.get("낙찰조건") or "").strip(),
        "보증금(천원)": _bid_num_clean(rec.get("보증금(천원)")),
        "월임차료(천원)": _bid_num_clean(rec.get("월임차료(천원)")),
        "가맹수수료율(%)": _bid_num_clean(rec.get("가맹수수료율(%)")),
        "예상일매출(천원)": _bid_num_clean(rec.get("예상일매출(천원)")),
        "영업일수": days,
        "BEP일매출(천원)": _bid_num_clean(rec.get("BEP일매출(천원)")),
        "예상매익률(%)": _bid_num_clean(rec.get("예상매익률(%)")),
        "예상이익(천원)": _bid_num_clean(rec.get("예상이익(천원)")),
        "비고": "" if pd.isna(rec.get("비고")) else str(rec.get("비고") or "").strip(),
    }


def _render_bid_editor_table(get_gc, rows, editor_prefix, type_opts, editor_user_name, show_owner=False):
    """내 입찰 목록 또는 관리자 전체 수정 모드에서 공통으로 쓰는 엑셀형 편집 표."""
    seed_key = f"{editor_prefix}_seed"
    meta_key = f"{editor_prefix}_meta"
    sig_key = f"{editor_prefix}_sig"

    cur_sig = f"{int(bool(show_owner))}|" + "|".join(f"{r['입찰ID']}:{r['수정일시']}" for r in rows)
    ed_cols = (
        ["삭제"]
        + (["담당자"] if show_owner else [])
        + [
            "입찰연월", "공고명", "점포수", "추진타입", "기존운영업체",
            "당사결과", "참여경쟁사", "낙찰업체", "당사순위", "낙찰조건",
            "보증금(천원)", "월임차료(천원)", "가맹수수료율(%)", "연간임차료(천원)", "임차료율(%)",
            "예상일매출(천원)", "영업일수", "BEP일매출(천원)",
            "예상매익률(%)", "예상이익(천원)", "비고",
        ]
    )

    if st.session_state.get(sig_key) != cur_sig or seed_key not in st.session_state:
        data, meta = [], []
        for r in rows:
            orig_payload = _canonical_bid_payload(r, r["사번"], r["담당자"])
            d_row = {
                "삭제": False,
                "담당자": r["담당자"],
                "입찰연월": r["입찰연월"],
                "공고명": r["공고명"],
                "점포수": r["점포수"],
                "추진타입": r["추진타입"],
                "기존운영업체": r["기존운영업체"],
                "당사결과": r["당사결과"],
                "참여경쟁사": list(r["참여경쟁사_list"]),
                "낙찰업체": r["낙찰업체"],
                "당사순위": r["당사순위"],
                "낙찰조건": r["낙찰조건"],
                "보증금(천원)": r["보증금(천원)"],
                "월임차료(천원)": r["월임차료(천원)"],
                "가맹수수료율(%)": r["가맹수수료율(%)"],
                "연간임차료(천원)": r["연간임차료(천원)"],
                "임차료율(%)": round(r["임차료율(%)"], 2) if r["임차료율(%)"] is not None else None,
                "예상일매출(천원)": r["예상일매출(천원)"],
                "영업일수": r["영업일수"],
                "BEP일매출(천원)": r["BEP일매출(천원)"],
                "예상매익률(%)": r["예상매익률(%)"],
                "예상이익(천원)": r["예상이익(천원)"],
                "비고": r["비고"],
            }
            data.append({c: d_row.get(c, [] if c == "참여경쟁사" else "") for c in ed_cols})
            meta.append(dict(bid_id=r["입찰ID"], uid=r["사번"], uname=r["담당자"], orig=orig_payload))

        df = pd.DataFrame(data, columns=ed_cols)
        num_all = set(BID_NUM_FIELDS) | {"연간임차료(천원)", "임차료율(%)"}
        for c in ed_cols:
            if c == "삭제":
                df[c] = df[c].astype(bool)
            elif c == "참여경쟁사":
                df[c] = _list_str_series(df[c].tolist() if len(df) else [])
            elif c in num_all:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            else:
                df[c] = df[c].apply(lambda v: "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v))
        st.session_state[seed_key] = df.reset_index(drop=True)
        st.session_state[meta_key] = meta
        st.session_state[sig_key] = cur_sig

    ym_opts = _bid_month_options([r["입찰연월"] for r in rows])
    merged_types = _merge_opts([""] + type_opts, [r["추진타입"] for r in rows])
    merged_exist = _merge_opts(BID_EXISTING_OPTS, [r["기존운영업체"] for r in rows])
    merged_win = _merge_opts(BID_WINNER_OPTS, [r["낙찰업체"] for r in rows])
    comp_opts = _merge_opts(BID_COMPETITOR_OPTS, [c for r in rows for c in r["참여경쟁사_list"]])

    cfg = {
        "삭제": st.column_config.CheckboxColumn("삭제", default=False, width="small", help="체크 후 저장하면 해당 입찰 건이 삭제됩니다"),
        "담당자": st.column_config.TextColumn("담당자", disabled=True, width="small"),
        "입찰연월": st.column_config.SelectboxColumn("입찰연월", options=ym_opts, width="small"),
        "공고명": st.column_config.TextColumn("공고명", required=True, width="medium"),
        "점포수": st.column_config.NumberColumn("점포수", format="%d", min_value=1, width="small"),
        "추진타입": st.column_config.SelectboxColumn("추진타입", options=merged_types, width="small"),
        "기존운영업체": st.column_config.SelectboxColumn("기존운영업체", options=merged_exist, width="small"),
        "당사결과": st.column_config.SelectboxColumn("당사결과", options=BID_RESULT_OPTS, required=True, width="small"),
        "참여경쟁사": st.column_config.MultiselectColumn("참여경쟁사", options=comp_opts, default=[], width="medium"),
        "낙찰업체": st.column_config.SelectboxColumn("낙찰업체", options=merged_win, width="small"),
        "당사순위": st.column_config.TextColumn("당사순위", width="small"),
        "낙찰조건": st.column_config.TextColumn("낙찰조건/낙찰가율", width="medium"),
        "보증금(천원)": st.column_config.NumberColumn("보증금(천원)", format="%d", width="small"),
        "월임차료(천원)": st.column_config.NumberColumn("월임차료(천원)", format="%d", width="small"),
        "가맹수수료율(%)": st.column_config.NumberColumn("가맹수수료율(%)", format="%.2f", width="small"),
        "연간임차료(천원)": st.column_config.NumberColumn("연간임차료(자동)", format="%d", disabled=True, width="small"),
        "임차료율(%)": st.column_config.NumberColumn("매출액 比 임차료율(자동)", format="%.2f", disabled=True, width="small"),
        "예상일매출(천원)": st.column_config.NumberColumn("1년차일매출(천원)", format="%d", width="small"),
        "영업일수": st.column_config.NumberColumn("영업일수", format="%d", width="small"),
        "BEP일매출(천원)": st.column_config.NumberColumn('"0"일매출(천원)', format="%d", width="small"),
        "예상매익률(%)": st.column_config.NumberColumn("예상매익률(%)", format="%.1f", width="small"),
        "예상이익(천원)": st.column_config.NumberColumn("1년차예상이익(천원)", format="%d", width="small"),
        "비고": st.column_config.TextColumn("비고", width="medium"),
    }

    edited_df = st.data_editor(
        st.session_state[seed_key], column_config=cfg, num_rows="fixed",
        use_container_width=True, hide_index=True, key=f"{editor_prefix}_widget", column_order=ed_cols,
    )

    bc1, bc2 = st.columns([2.2, 7.8])
    with bc1:
        save_clicked = st.button("변경사항 저장", type="primary", icon=":material/save:",
                                 key=f"{editor_prefix}_save_btn", use_container_width=True)
    if save_clicked:
        updates_by_id = {}
        delete_ids = []
        empty_name = False
        for rec, meta in zip(edited_df.to_dict("records"), st.session_state[meta_key]):
            if bool(rec.get("삭제")):
                delete_ids.append(meta["bid_id"])
                continue
            new_payload = _canonical_bid_payload(rec, meta["uid"], meta["uname"])
            if not new_payload["공고명"]:
                empty_name = True
                break
            if new_payload != meta["orig"]:
                updates_by_id[meta["bid_id"]] = new_payload

        if empty_name:
            st.warning("공고명은 비워둘 수 없습니다. 삭제하려면 맨 앞 '삭제' 칸에 체크해 주세요.")
            return False
        if not updates_by_id and not delete_ids:
            st.info("변경되거나 삭제 선택된 항목이 없습니다.")
            return False

        n_up, n_del = apply_bid_changes(get_gc, updates_by_id, delete_ids, editor_name=editor_user_name)
        for k in (seed_key, meta_key, sig_key):
            st.session_state.pop(k, None)
        parts = []
        if n_up:
            parts.append(f"수정 {n_up}건")
        if n_del:
            parts.append(f"삭제 {n_del}건")
        st.session_state[f"{editor_prefix}_flash"] = f"입찰 내역({', '.join(parts)})이 반영되었습니다."
        st.rerun()
    return False


def _render_bid_mine(get_gc, all_rows, uid, uname, is_admin, type_opts, login_records, active_members=None):
    my_rows = [
        r for r in all_rows
        if (uid and r["사번"] == str(uid).strip()) or (uname and r["담당자"] == str(uname).strip())
    ]

    add_flash = st.session_state.pop("bid_add_flash", None)
    if add_flash:
        st.success(f":material/check_circle: {add_flash}")
    ed_flash = st.session_state.pop(f"bid_my_ed_{uid}_flash", None)
    if ed_flash:
        st.success(f":material/check_circle: {ed_flash}")

    with st.expander(":material/add_circle: 새 입찰 참여 내역 등록하기", expanded=(len(my_rows) == 0)):
        st.caption("연간 총 임차료 · 매출액 比 임차료율(%)은 입력하신 수치를 바탕으로 자동 계산됩니다.")
        cur_ym_label = ym_label(datetime.now(KST).strftime("%Y%m"))
        ym_choices = _bid_month_options([cur_ym_label])
        cur_ym_idx = ym_choices.index(cur_ym_label) if cur_ym_label in ym_choices else 0

        member_uid_map = {}
        all_login_names = []
        for lr in (login_records or []):
            nm = str(lr.get("이름", "")).strip()
            lid = str(lr.get("사번", "")).strip()
            if nm and nm not in all_login_names:
                all_login_names.append(nm)
                member_uid_map[nm] = lid
        # 실제 활동 팀원(팀현황표시 Y) 우선 노출 + 관리자 본인 포함
        base_pool = list(active_members) if active_members else all_login_names
        member_names = [m for m in base_pool if m]
        if uname and uname not in member_names:
            member_names = [uname] + member_names
        if uname:
            member_uid_map.setdefault(uname, uid)

        with st.form("bid_register_form", clear_on_submit=True):
            st.markdown("**1) 기본 정보**")
            if is_admin and member_names:
                r1c0, r1c1, r1c2, r1c3, r1c4, r1c5 = st.columns([1.2, 2.5, 1.4, 0.9, 1.3, 1.3])
                with r1c0:
                    def_idx = member_names.index(uname) if uname in member_names else 0
                    f_owner = st.selectbox("담당자(관리자 지정)", member_names, index=def_idx)
            else:
                f_owner = uname
                r1c1, r1c2, r1c3, r1c4, r1c5 = st.columns([2.8, 1.5, 1.0, 1.4, 1.4])
            with r1c1:
                f_title = st.text_input("공고명 *", placeholder="예: 내트럭부산, 반여농산물시장")
            with r1c2:
                f_ym = st.selectbox("입찰연월", ym_choices, index=cur_ym_idx)
            with r1c3:
                f_stores = st.number_input("점포수", min_value=1, value=1, step=1)
            with r1c4:
                f_type_sel = st.selectbox("추진타입", ["선택 안 함"] + type_opts, index=0)
                f_type = "" if f_type_sel == "선택 안 함" else f_type_sel
            with r1c5:
                f_exist = st.selectbox("기존운영업체", BID_EXISTING_OPTS, index=1)

            st.markdown("**2) 입찰 결과 및 경쟁사 현황** *(개찰 전이면 '진행중'으로 등록 후 나중에 아래 표에서 결과만 바꾸시면 됩니다)*")
            r2c1, r2c2, r2c3, r2c4, r2c5 = st.columns([1.3, 2.4, 1.4, 1.0, 2.2])
            with r2c1:
                f_res = st.selectbox("당사 결과", BID_RESULT_OPTS, index=0)
            with r2c2:
                f_comps = st.multiselect("참여 경쟁사 (복수 선택)", BID_COMPETITOR_OPTS, placeholder="참여한 경쟁사 선택")
            with r2c3:
                f_winner = st.selectbox("낙찰 업체", ["자동(당사 낙찰 시 GS25)", "CU", "7-11", "emart24", "GS25", "기타"])
            with r2c4:
                f_rank = st.text_input("당사 순위", placeholder="예: 1, 2, 3")
            with r2c5:
                f_cond = st.text_input("낙찰조건 / 낙찰가율", placeholder="예: 20,500천원, 상품할인율 15%")

            st.markdown("**3) 투찰 조건 및 1년차 예상 손익** *(본부임차는 보증금·월임차료, 가맹은 수수료율 입력)*")
            r3c1, r3c2, r3c3, r3c4 = st.columns(4)
            with r3c1:
                f_dep = st.text_input("보증금 (천원)", placeholder="예: 50000")
            with r3c2:
                f_rent = st.text_input("월임차료 (천원, 점당)", placeholder="예: 9400")
            with r3c3:
                f_fee = st.text_input("당사 가맹수수료율 (%)", placeholder="예: 83 또는 77.5")
            with r3c4:
                f_days = st.number_input("연간 영업일수", min_value=1, max_value=366, value=365, step=1)

            r4c1, r4c2, r4c3, r4c4 = st.columns(4)
            with r4c1:
                f_daily = st.text_input("1년차 예상 일매출 (천원)", placeholder="예: 2871")
            with r4c2:
                f_bep = st.text_input('"0" 일매출 (천원)', placeholder="예: 2596")
            with r4c3:
                f_margin = st.text_input("예상 매익률 (%)", placeholder="예: 28.3")
            with r4c4:
                f_profit = st.text_input("1년차 예상 이익 (천원)", placeholder="예: 1372")

            f_memo = st.text_input("비고 (특이조건 메모)", placeholder="예: 이행보증증권 대체 등 참고사항")
            submitted = st.form_submit_button("입찰 내역 등록", type="primary", icon=":material/add_task:")

        if submitted:
            if not f_title.strip():
                st.warning("공고명을 입력해 주세요.")
            else:
                target_owner = str(f_owner or uname).strip()
                target_uid = member_uid_map.get(target_owner, uid if target_owner == uname else "")
                payload = _canonical_bid_payload({
                    "입찰연월": f_ym,
                    "공고명": f_title.strip(),
                    "점포수": f_stores,
                    "추진타입": f_type,
                    "기존운영업체": f_exist,
                    "당사결과": f_res,
                    "참여경쟁사": f_comps,
                    "낙찰업체": f_winner,
                    "당사순위": f_rank,
                    "낙찰조건": f_cond,
                    "보증금(천원)": _bid_num_clean(f_dep),
                    "월임차료(천원)": _bid_num_clean(f_rent),
                    "가맹수수료율(%)": _bid_num_clean(f_fee),
                    "예상일매출(천원)": _bid_num_clean(f_daily),
                    "영업일수": f_days,
                    "BEP일매출(천원)": _bid_num_clean(f_bep),
                    "예상매익률(%)": _bid_num_clean(f_margin),
                    "예상이익(천원)": _bid_num_clean(f_profit),
                    "비고": f_memo,
                }, target_uid, target_owner)
                payload["수정자"] = uname
                append_bid_record(get_gc, payload)
                for k in (f"bid_my_ed_{uid}_seed", f"bid_my_ed_{uid}_meta", f"bid_my_ed_{uid}_sig"):
                    st.session_state.pop(k, None)
                st.session_state["bid_add_flash"] = f"'{f_title.strip()}' 입찰 내역이 등록되었습니다."
                st.rerun()

    show_all_for_admin = False
    if is_admin and len(all_rows) > len(my_rows):
        mc1, mc2 = st.columns([4, 6])
        with mc1:
            st.markdown(f"##### :material/list_alt: 내가 등록한 입찰 내역 ({len(my_rows)}건)")
        with mc2:
            show_all_for_admin = st.toggle(
                f"팀원 대리 등록 포함 전체 보기 ({len(all_rows)}건)",
                key="bid_mine_show_all_adm",
                help="관리자가 다른 담당자 명의로 대리 등록한 입찰까지 이 화면에서 바로 확인·수정합니다.",
            )
    else:
        st.markdown(f"##### :material/list_alt: 내가 등록한 입찰 내역 ({len(my_rows)}건)")

    target_rows = all_rows if show_all_for_admin else my_rows
    if not target_rows:
        st.info("아직 내가 등록한 입찰 내역이 없습니다. 위 등록 창에서 첫 입찰 건을 입력해 보세요.")
        return
    st.caption("셀을 더블클릭해 개찰 결과(`진행중` → `낙찰`/`패찰`)나 수치를 바로 수정하고 **변경사항 저장**을 눌러주세요. 잘못 입력한 줄은 맨 앞 `삭제` 체크 후 저장하면 지워집니다.")
    _render_bid_editor_table(
        get_gc, target_rows, f"bid_my_ed_{uid}", type_opts,
        editor_user_name=uname, show_owner=show_all_for_admin,
    )


def _bid_summary_kpis_html(rows):
    """팀 입찰 현황 상단 요약 KPI 4개 카드."""
    participated = [r for r in rows if r["당사결과"] != "미참여"]
    part_cnt = len(participated)
    part_stores = sum(r["점포수"] for r in participated)
    skip_cnt = len(rows) - part_cnt

    wins = [r for r in participated if r["당사결과"] == "낙찰"]
    loses = [r for r in participated if r["당사결과"] == "패찰"]
    voids = [r for r in participated if r["당사결과"] == "유찰"]
    waits = [r for r in participated if r["당사결과"] == "진행중"]

    decided_cnt = len(wins) + len(loses) + len(voids)
    win_rate = (len(wins) / decided_cnt * 100.0) if decided_cnt > 0 else None
    win_stores = sum(r["점포수"] for r in wins)

    comp_stats = []
    for brand in ("CU", "7-11", "emart24"):
        with_b = [r for r in participated if brand in r["참여경쟁사_list"]]
        dec_b = [r for r in with_b if r["당사결과"] in ("낙찰", "패찰", "유찰")]
        win_b = [r for r in dec_b if r["당사결과"] == "낙찰"]
        if dec_b:
            comp_stats.append(f"{brand} {len(with_b)}회(승 {len(win_b)/len(dec_b)*100:.0f}%)")
        else:
            comp_stats.append(f"{brand} {len(with_b)}회")

    top_comp = "-"
    top_comp_unit = ""
    comp_counts = [(b, sum(1 for r in participated if b in r["참여경쟁사_list"])) for b in ("CU", "7-11", "emart24")]
    comp_counts.sort(key=lambda x: x[1], reverse=True)
    if comp_counts and comp_counts[0][1] > 0:
        max_c = comp_counts[0][1]
        tied = [b for b, c in comp_counts if c == max_c]
        if len(tied) == 1:
            top_comp = tied[0]
            top_comp_unit = f"({max_c}회)"
        elif len(tied) == 2:
            top_comp = " · ".join(tied)
            top_comp_unit = f"(각 {max_c}회)"
        else:
            top_comp = f"{len(tied)}개사 동률"
            top_comp_unit = f"(각 {max_c}회)"

    items = [
        dict(
            label="총 입찰 참여",
            value=f"{part_cnt:,}",
            unit="건",
            sub=f"총 {part_stores:,}점 참여 · 진행중 {len(waits)}건" + (f" (미참여 {skip_cnt}건 별도)" if skip_cnt else ""),
            accent=COLOR_NAVY,
            icon="badge",
        ),
        dict(
            label="당사 낙찰 · 낙찰률",
            value=f"{len(wins):,}",
            unit=f"건 ({win_rate:.1f}%)" if win_rate is not None else "건 (-)",
            sub=f"결과 확정 {decided_cnt}건 기준 · 낙찰 점포 {win_stores:,}점",
            accent=COLOR_GOOD if (win_rate is not None and win_rate >= 50) else COLOR_NAVY,
            icon="trophy",
        ),
        dict(
            label="입찰 결과 분포",
            value=f"승 {len(wins)} · 패 {len(loses)}",
            unit="",
            sub=f"유찰 {len(voids)}건 · 진행중 {len(waits)}건",
            accent=COLOR_BLUE,
            icon="bars",
        ),
        dict(
            label="최다 경쟁사 · 상대 전적",
            value=top_comp,
            unit=top_comp_unit,
            sub=" · ".join(comp_stats),
            accent=COLOR_PURPLE,
            icon="target",
        ),
    ]
    return kpi_html(items)


def _bid_team_table_html(rows):
    """팀 입찰 통합 누계 HTML 표 (자동 계산 수식 열 포함)."""
    cols = [
        ("순번", "순번"),
        ("입찰연월", "입찰<br>연월"),
        ("공고명", "공고명"),
        ("점포수", "점포수"),
        ("담당자", "담당자"),
        ("추진타입", "추진<br>타입"),
        ("기존운영업체", "기존<br>운영사"),
        ("당사결과", "당사결과"),
        ("참여경쟁사", "참여 경쟁사"),
        ("낙찰업체", "낙찰<br>업체"),
        ("당사순위", "순위"),
        ("낙찰조건", "낙찰조건 / 가율"),
        ("보증금(천원)", "보증금<br>(천원)"),
        ("조건요약", "월임차료 /<br>수수료율"),
        ("연간임차료(천원)", "연간임차료<br>(천원)"),
        ("임차료율(%)", "매출액 比<br>임차료율"),
        ("예상일매출(천원)", "1년차일매출<br>(천원)"),
        ("BEP일매출(천원)", '"0"일매출<br>(천원)'),
        ("예상매익률(%)", "예상<br>매익률"),
        ("예상이익(천원)", "1년차이익<br>(천원)"),
        ("비고", "비고"),
    ]
    out = ['<div class="wk-tbl-wrap"><table class="wk-tbl"><thead><tr>']
    out += [f"<th>{lbl}</th>" for _, lbl in cols]
    out.append("</tr></thead><tbody>")
    if not rows:
        out.append(f'<tr><td class="wk-empty" colspan="{len(cols)}">조건에 맞는 입찰 내역이 없습니다.</td></tr>')
    else:
        for idx, r in enumerate(rows, start=1):
            out.append("<tr>")
            m_rent = r.get("월임차료(천원)")
            fee_p = r.get("가맹수수료율(%)")
            if m_rent is not None and fee_p is not None:
                cond_str = f"{fnum(m_rent)}천원 / {fee_p:g}%"
            elif m_rent is not None:
                cond_str = f"{fnum(m_rent)}천원"
            elif fee_p is not None:
                cond_str = f"{fee_p:g}%"
            else:
                cond_str = "-"

            comp_html = (
                "".join(f'<span class="bid-comp-chip">{escape(c)}</span>' for c in r["참여경쟁사_list"])
                if r["참여경쟁사_list"] else "-"
            )
            for key, _ in cols:
                if key == "순번":
                    out.append(f'<td class="wk-num">{idx}</td>')
                elif key == "당사결과":
                    out.append(f'<td class="wk-num">{_bid_result_badge_html(r["당사결과"])}</td>')
                elif key == "참여경쟁사":
                    out.append(f"<td>{comp_html}</td>")
                elif key == "조건요약":
                    out.append(f'<td class="wk-num">{escape(cond_str)}</td>')
                elif key in ("보증금(천원)", "연간임차료(천원)", "예상일매출(천원)", "BEP일매출(천원)", "예상이익(천원)"):
                    out.append(f'<td class="wk-num">{fnum(r.get(key))}</td>')
                elif key == "임차료율(%)":
                    v = r.get(key)
                    out.append(f'<td class="wk-num">{f"{v:.2f}%" if v is not None else "-"}</td>')
                elif key == "예상매익률(%)":
                    v = r.get(key)
                    out.append(f'<td class="wk-num">{f"{v:g}%" if v is not None else "-"}</td>')
                elif key in ("공고명", "낙찰조건", "비고"):
                    v = r.get(key, "")
                    out.append(f'<td class="wk-prog">{escape(str(v)) if v else "-"}</td>')
                else:
                    v = r.get(key, "")
                    out.append(f'<td class="wk-num">{escape(str(v)) if v not in (None, "") else "-"}</td>')
            out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _render_bid_team(get_gc, all_rows, uname, is_admin, type_opts, all_members):
    adm_flash = st.session_state.pop("bid_adm_ed_flash", None)
    if adm_flash:
        st.success(f":material/check_circle: {adm_flash}")

    years = sorted({r["year"] for r in all_rows if r["year"]}, reverse=True)
    mgrs_in_data = [m for m in all_members if any(r["담당자"] == m for r in all_rows)]
    extra_mgrs = sorted({r["담당자"] for r in all_rows if r["담당자"] and r["담당자"] not in mgrs_in_data})
    mgr_choices = ["전체"] + mgrs_in_data + extra_mgrs
    types_in_data = sorted({r["추진타입"] for r in all_rows if r["추진타입"]})

    fc1, fc2, fc3, fc4, fc5 = st.columns([1.2, 1.2, 1.2, 1.2, 2.2])
    with fc1:
        f_year = st.selectbox("조회 연도", ["전체 누계"] + [f"{y}년" for y in years], key="bid_f_year")
    with fc2:
        f_mgr = st.selectbox("담당자", mgr_choices, key="bid_f_mgr")
    with fc3:
        f_res = st.selectbox("당사 결과", ["전체"] + BID_RESULT_OPTS, key="bid_f_res")
    with fc4:
        f_type = st.selectbox("추진타입", ["전체"] + types_in_data, key="bid_f_type")
    with fc5:
        f_q = st.text_input("검색", placeholder="공고명 · 낙찰조건 · 경쟁사 · 비고 검색", key="bid_f_q")

    sel_year = f_year[:-1] if f_year != "전체 누계" else ""
    kw = f_q.strip().lower()
    filtered = [
        r for r in all_rows
        if (not sel_year or r["year"] == sel_year)
        and (f_mgr == "전체" or r["담당자"] == f_mgr)
        and (f_res == "전체" or r["당사결과"] == f_res)
        and (f_type == "전체" or r["추진타입"] == f_type)
        and (
            not kw
            or kw in r["공고명"].lower()
            or kw in r["담당자"].lower()
            or kw in r["낙찰조건"].lower()
            or kw in r["참여경쟁사"].lower()
            or kw in r["비고"].lower()
        )
    ]

    today_tag = datetime.now(KST).strftime("%Y%m%d")
    with st.container(key="bid_export_bar"):
        ec1, ec2, ec3 = st.columns([4.2, 2.9, 2.9])
        with ec1:
            st.markdown(
                f'<div class="lib-sel-badge">조회된 입찰 <b>{len(filtered)}</b>건 / 전체 누계 <b>{len(all_rows)}</b>건</div>',
                unsafe_allow_html=True,
            )
        with ec2:
            st.download_button(
                label=f"현재 필터 시트 추출 ({len(filtered)}건)",
                data=lambda rows=list(filtered): build_bids_xlsx_bytes(rows),
                file_name=f"특수개발팀_입찰내역_필터_{today_tag}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                icon=":material/download:",
                help="구글 드라이브에 올리거나 구글 스프레드시트(파일 → 열기 → 업로드)에서 서식 그대로 바로 열립니다.",
                key="bid_dl_filtered_xlsx",
                disabled=(len(filtered) == 0),
                on_click="ignore",
                use_container_width=True,
            )
        with ec3:
            st.download_button(
                label=f"전체 누계 시트 추출 ({len(all_rows)}건)",
                data=lambda rows=list(all_rows): build_bids_xlsx_bytes(rows),
                file_name=f"특수개발팀_입찰내역_전체누계_{today_tag}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                icon=":material/table_view:",
                help="구글 드라이브에 올리거나 구글 스프레드시트(파일 → 열기 → 업로드)에서 서식 그대로 바로 열립니다.",
                key="bid_dl_all_xlsx",
                disabled=(len(all_rows) == 0),
                on_click="ignore",
                use_container_width=True,
            )

    if not all_rows:
        st.info("아직 등록된 입찰 내역이 없습니다. '내 입찰 등록 · 관리' 탭에서 첫 입찰 건을 등록해 주세요.")
        return

    st.markdown(_bid_summary_kpis_html(filtered), unsafe_allow_html=True)

    adm_toggle_key = "bid_adm_edit_toggle"
    admin_editing = False
    if is_admin and filtered:
        ac1, ac2 = st.columns([3, 7])
        with ac1:
            admin_editing = st.toggle(":material/edit_square: 관리자 수정 모드", key=adm_toggle_key,
                                      help="팀원들이 입력한 입찰 내역의 오타·수치·결과를 관리자가 표에서 직접 수정하거나 삭제합니다.")
        with ac2:
            if admin_editing:
                st.caption("셀을 더블클릭해 수정한 뒤 아래 **변경사항 저장** 버튼을 눌러주세요. (원본 시트 반영 + 수정자 기록)")

    if admin_editing:
        _render_bid_editor_table(get_gc, filtered, "bid_adm_ed", type_opts, editor_user_name=uname, show_owner=True)
    else:
        for k in ("bid_adm_ed_seed", "bid_adm_ed_meta", "bid_adm_ed_sig"):
            st.session_state.pop(k, None)
        st.markdown(_bid_team_table_html(filtered), unsafe_allow_html=True)


def view_bid_tracker(get_gc, D=None, user_id="", user_name="", is_admin=False):
    hc1, hc2 = st.columns([5, 1.1])
    with hc1:
        st.markdown(sec_heading("badge", "입찰 관리"), unsafe_allow_html=True)
    with hc2:
        st.write("")
        if icon_button("새로고침", ":material/refresh:", key="bid_refresh_btn", use_container_width=True):
            clear_bid_tracker_cache()
            st.rerun()

    try:
        all_rows = load_bid_tracker_all(get_gc)
    except Exception as e:
        st.error(f"입찰 데이터를 불러오는 중 오류가 발생했습니다: {e}")
        return

    select_lists = load_select_lists(get_gc)
    raw_types = select_lists.get("운영형태") or BID_TYPE_DEFAULTS
    type_opts = [o for o in _merge_opts(raw_types, BID_TYPE_DEFAULTS if not select_lists.get("운영형태") else []) if o != "직영(용역)"]
    login_records = load_login_records(get_gc)
    team_display = load_team_display_members(get_gc)
    login_members = load_login_members(get_gc)
    all_members = team_display or login_members

    my_cnt = sum(
        1 for r in all_rows
        if (user_id and r["사번"] == str(user_id).strip()) or (user_name and r["담당자"] == str(user_name).strip())
    )

    with st.container(key="lvl2tabs_bid"):
        t_mine, t_team = st.tabs([
            f":material/edit_document: 내 입찰 등록 · 관리 ({my_cnt})",
            f":material/analytics: 팀 입찰 현황 · 통계 ({len(all_rows)})",
        ])
    with t_mine:
        _render_bid_mine(get_gc, all_rows, user_id, user_name, is_admin, type_opts, login_records,
                         active_members=all_members)
    with t_team:
        _render_bid_team(get_gc, all_rows, user_name, is_admin, type_opts, all_members)


# ───────────────────────── 자료실 (구글 드라이브 비공개 연동) ─────────────────────────
SD_LIBRARY_ROOT_NAME = "SD_자료실"
DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# 구글 문서/시트/프레젠테이션 원본이 폴더에 들어있을 경우 다운로드 가능한 오피스/PDF 형식으로 자동 변환
GDOC_EXPORT_MAP = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

_EXT_STYLE_MAP = {
    "PDF": "pdf",
    "HWP": "hwp", "HWPX": "hwp",
    "XLSX": "xls", "XLS": "xls", "CSV": "xls",
    "PPTX": "ppt", "PPT": "ppt",
    "DOCX": "doc", "DOC": "doc", "TXT": "doc",
    "JPG": "img", "JPEG": "img", "PNG": "img", "WEBP": "img",
    "ZIP": "zip", "7Z": "zip", "RAR": "zip",
}


def _clean_folder_label(raw_name):
    """폴더 정렬용 접두 번호('1_', '02. ', '3-' 등)만 제거하고 '63빌딩' 같은 고유 숫자는 그대로 보존합니다."""
    s = str(raw_name or "").strip()
    cleaned = re.sub(r"^\s*\d{1,2}\s*[._\-)]\s*", "", s).strip()
    return cleaned or s


def _folder_sort_key(raw_name):
    """앞에 번호('1_', '02.' 등)가 있으면 숫자 순으로 먼저 정렬하고, 없으면 가나다순으로 정렬합니다."""
    s = str(raw_name or "").strip()
    m = re.match(r"^\s*(\d{1,2})\s*[._\-)]\s*(.*)$", s)
    if m:
        return (0, int(m.group(1)), m.group(2).strip())
    return (1, 999, s)


def _format_file_size(size_val):
    try:
        b = int(size_val)
    except (TypeError, ValueError):
        return "-"
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _file_ext_info(download_name):
    nm = str(download_name or "").strip()
    ext = nm.rsplit(".", 1)[1].upper() if "." in nm else "FILE"
    ext_short = ext[:5]
    return ext_short, _EXT_STYLE_MAP.get(ext, "etc")


def _extract_doc_date(filename, modified_iso="", today=None):
    """파일명에 적힌 날짜(YYYYMMDD / YYYY-MM-DD / YYMMDD 등)를 우선 인식하고, 없으면 드라이브 수정일을 기준으로 씁니다."""
    if today is None:
        today = datetime.now(KST).date()
    stem = str(filename or "").rsplit(".", 1)[0]

    # 1) 8자리 날짜 (예: 2026-11-25, 2026.11.25, 20261125)
    for m in re.finditer(r"(?<!\d)(20[2-4]\d)[.\-_/\s]?(0[1-9]|1[0-2])[.\-_/\s]?(0[1-9]|[12]\d|3[01])(?!\d)", stem):
        try:
            d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%Y-%m-%d"), "파일명", max(0, (today - d).days)
        except ValueError:
            pass

    # 2) 6자리 날짜 (예: _261125, (26.11.25))
    for m in re.finditer(r"(?<!\d)(2\d)[.\-_]?(0[1-9]|1[0-2])[.\-_]?(0[1-9]|[12]\d|3[01])(?!\d)", stem):
        try:
            d = _dt.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%Y-%m-%d"), "파일명", max(0, (today - d).days)
        except ValueError:
            pass

    # 3) 드라이브 수정시각(UTC ISO8601 -> KST)
    s = str(modified_iso or "").strip()
    if s:
        try:
            dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
            d = dt_utc.astimezone(KST).date()
            return d.strftime("%Y-%m-%d"), "업로드일", max(0, (today - d).days)
        except Exception:
            if len(s) >= 10:
                return s[:10], "업로드일", None
    return "-", "", None


def _drive_query(gc, q, fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, shortcutDetails)"):
    """Drive v3 files.list 페이지네이션 조회 (서비스 계정에 공유된 폴더·파일만 조회됨)."""
    items = []
    page_token = None
    while True:
        params = {
            "q": q,
            "fields": fields,
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        res = gc.http_client.request("get", DRIVE_FILES_API, params=params)
        payload = res.json()
        items.extend(payload.get("files", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return items


def _drive_list_children(gc, folder_id):
    raw_items = _drive_query(gc, f"'{folder_id}' in parents and trashed = false")
    resolved = []
    for it in raw_items:
        if it.get("mimeType") == DRIVE_SHORTCUT_MIME:
            sc = it.get("shortcutDetails") or {}
            tid, tmime = sc.get("targetId"), sc.get("targetMimeType")
            if not tid or not tmime:
                continue
            it = dict(it, id=tid, mimeType=tmime)
        resolved.append(it)
    return resolved


def _enrich_drive_file(raw_file, group_label="", today=None):
    fid = raw_file.get("id", "")
    name = str(raw_file.get("name", "")).strip()
    mime = raw_file.get("mimeType", "") or "application/octet-stream"
    if mime in GDOC_EXPORT_MAP:
        dl_mime, ext = GDOC_EXPORT_MAP[mime]
        dl_name = name if name.lower().endswith(ext) else f"{name}{ext}"
    else:
        dl_mime = mime
        dl_name = name
    ext_label, ext_cls = _file_ext_info(dl_name)
    date_str, date_src, days_ago = _extract_doc_date(name, raw_file.get("modifiedTime", ""), today=today)
    return {
        "id": fid,
        "name": name,
        "download_name": dl_name,
        "mimeType": mime,
        "download_mime": dl_mime,
        "size_bytes": raw_file.get("size"),
        "size_text": _format_file_size(raw_file.get("size")),
        "modifiedTime": raw_file.get("modifiedTime", ""),
        "date_str": date_str,
        "date_src": date_src,
        "days_ago": days_ago,
        "ext_label": ext_label,
        "ext_cls": ext_cls,
        "group": group_label,
    }


def _collect_files_recursive(gc, folder_id, group_label="", today=None, max_depth=2):
    """폴더 안의 모든 파일을 수집합니다(하위 폴더가 한 단계 더 있어도 누락 없이 가져옵니다)."""
    out = []
    children = _drive_list_children(gc, folder_id)
    subfolders = sorted([c for c in children if c.get("mimeType") == DRIVE_FOLDER_MIME],
                        key=lambda x: _folder_sort_key(x.get("name", "")))
    files = [c for c in children if c.get("mimeType") != DRIVE_FOLDER_MIME]
    for f in files:
        out.append(_enrich_drive_file(f, group_label=group_label, today=today))
    if max_depth > 0:
        for sf in subfolders:
            sub_lbl = _clean_folder_label(sf.get("name", ""))
            merged_lbl = f"{group_label} · {sub_lbl}" if group_label else sub_lbl
            out.extend(_collect_files_recursive(gc, sf["id"], group_label=merged_lbl, today=today, max_depth=max_depth - 1))
    return out


@st.cache_data(ttl=180, show_spinner="자료실 목록을 불러오는 중...")
def load_drive_library(_get_gc):
    """구글 드라이브 'SD_자료실' 폴더 구조를 읽어 입찰 필요서류와 주요 이슈 점포 보고서 목록을 구성합니다."""
    try:
        gc = _get_gc()
    except Exception as e:
        return {"ready": False, "error": f"구글 인증 연결 실패: {e}", "sa_email": ""}

    sa_email = str(getattr(getattr(gc.http_client, "auth", None), "service_account_email", "") or "")
    configured_id = ""
    try:
        configured_id = str(st.secrets.get("sd_library_folder_id", "")).strip()
    except Exception:
        configured_id = ""

    try:
        root_id = configured_id
        if not root_id:
            roots = _drive_query(
                gc,
                f"mimeType = '{DRIVE_FOLDER_MIME}' and name = '{SD_LIBRARY_ROOT_NAME}' and trashed = false",
                fields="files(id, name, modifiedTime)",
            )
            if not roots:
                return {"ready": False, "error": None, "sa_email": sa_email}
            roots.sort(key=lambda r: r.get("modifiedTime", ""), reverse=True)
            root_id = roots[0]["id"]

        today = datetime.now(KST).date()
        top_items = _drive_list_children(gc, root_id)
        top_folders = sorted([c for c in top_items if c.get("mimeType") == DRIVE_FOLDER_MIME],
                             key=lambda x: _folder_sort_key(x.get("name", "")))
        top_files = [c for c in top_items if c.get("mimeType") != DRIVE_FOLDER_MIME]

        bid_categories = []
        bid_files = []
        store_names = []
        store_files = []
        proposal_categories = []
        proposal_files = []

        def _add_cat(lst, name):
            if name and name not in lst:
                lst.append(name)

        for tf in top_folders:
            clean_top = _clean_folder_label(tf.get("name", ""))
            if "입찰" in clean_top:
                # 입찰 필요서류 폴더: 직속 파일 + 소분류 하위 폴더
                sub_items = _drive_list_children(gc, tf["id"])
                sub_folders = sorted([c for c in sub_items if c.get("mimeType") == DRIVE_FOLDER_MIME],
                                     key=lambda x: _folder_sort_key(x.get("name", "")))
                direct_files = [c for c in sub_items if c.get("mimeType") != DRIVE_FOLDER_MIME]
                if direct_files:
                    cat = "일반·공통" if sub_folders else ""
                    if cat:
                        _add_cat(bid_categories, cat)
                    for f in direct_files:
                        bid_files.append(_enrich_drive_file(f, group_label=cat, today=today))
                for sf in sub_folders:
                    cat = _clean_folder_label(sf.get("name", ""))
                    _add_cat(bid_categories, cat)
                    bid_files.extend(_collect_files_recursive(gc, sf["id"], group_label=cat, today=today, max_depth=1))
            elif "제안" in clean_top:
                # 제안서 폴더: 직속 파일로 바로 올려도 되고, 업종/부문/연도 등 하위 폴더로 나누면 자동 분류 알약 생성
                sub_items = _drive_list_children(gc, tf["id"])
                sub_folders = sorted([c for c in sub_items if c.get("mimeType") == DRIVE_FOLDER_MIME],
                                     key=lambda x: _folder_sort_key(x.get("name", "")))
                direct_files = [c for c in sub_items if c.get("mimeType") != DRIVE_FOLDER_MIME]
                for sf in sub_folders:
                    pcat = _clean_folder_label(sf.get("name", ""))
                    _add_cat(proposal_categories, pcat)
                    proposal_files.extend(_collect_files_recursive(gc, sf["id"], group_label=pcat, today=today, max_depth=1))
                if direct_files:
                    pcat = "일반·공통" if sub_folders else ""
                    if pcat:
                        _add_cat(proposal_categories, pcat)
                    for f in direct_files:
                        proposal_files.append(_enrich_drive_file(f, group_label=pcat, today=today))
            elif any(kw in clean_top for kw in ("이슈", "점포", "보고서")):
                # 주요 이슈 보고서 상위 폴더: 하위 폴더 하나하나가 점포/이슈명(한강, 63빌딩, 인천공항 등)
                sub_items = _drive_list_children(gc, tf["id"])
                sub_folders = sorted([c for c in sub_items if c.get("mimeType") == DRIVE_FOLDER_MIME],
                                     key=lambda x: _folder_sort_key(x.get("name", "")))
                direct_files = [c for c in sub_items if c.get("mimeType") != DRIVE_FOLDER_MIME]
                for sf in sub_folders:
                    sname = _clean_folder_label(sf.get("name", ""))
                    _add_cat(store_names, sname)
                    store_files.extend(_collect_files_recursive(gc, sf["id"], group_label=sname, today=today, max_depth=1))
                if direct_files:
                    sname = "기타·공통"
                    _add_cat(store_names, sname)
                    for f in direct_files:
                        store_files.append(_enrich_drive_file(f, group_label=sname, today=today))
            else:
                # SD_자료실 바로 아래에 '한강', '63빌딩', '인천공항' 폴더를 직접 둔 경우에도 점포 폴더로 자동 인식
                _add_cat(store_names, clean_top)
                store_files.extend(_collect_files_recursive(gc, tf["id"], group_label=clean_top, today=today, max_depth=1))

        if top_files:
            cat = "일반·공통" if bid_categories else ""
            if cat:
                _add_cat(bid_categories, cat)
            for f in top_files:
                bid_files.append(_enrich_drive_file(f, group_label=cat, today=today))

        return {
            "ready": True,
            "error": None,
            "sa_email": sa_email,
            "bid_categories": bid_categories,
            "bid_files": bid_files,
            "store_names": store_names,
            "store_files": store_files,
            "proposal_categories": proposal_categories,
            "proposal_files": proposal_files,
        }
    except Exception as e:
        return {"ready": False, "error": f"구글 드라이브 조회 중 오류가 발생했습니다: {e}", "sa_email": sa_email}


@st.cache_data(ttl=600, max_entries=64, show_spinner="파일을 준비하는 중...")
def _cached_drive_file_bytes(_get_gc, file_id, mime_type, mod_token=""):
    """개별 파일 바이트 다운로드(10분 메모리 캐시, 드라이브 파일 수정시각이 바뀌면 캐시 키도 즉시 갱신됨)."""
    gc = _get_gc()
    if mime_type in GDOC_EXPORT_MAP:
        export_mime, _ = GDOC_EXPORT_MAP[mime_type]
        res = gc.http_client.request("get", f"{DRIVE_FILES_API}/{file_id}/export", params={"mimeType": export_mime})
    else:
        res = gc.http_client.request(
            "get", f"{DRIVE_FILES_API}/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
    return res.content


def build_drive_zip_bytes(get_gc, file_tuples):
    """선택한 서류들을 메모리에서 하나의 .zip 바이트로 묶어 반환합니다. 같은 파일명이 여러 개면 자동 번호를 붙입니다."""
    buf = io.BytesIO()
    used_names = {}
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fid, fmime, fmod, dl_name in file_tuples:
            data = _cached_drive_file_bytes(get_gc, fid, fmime, fmod)
            arc = dl_name or f"{fid}.bin"
            if arc in used_names:
                used_names[arc] += 1
                if "." in arc:
                    stem, ext = arc.rsplit(".", 1)
                    arc = f"{stem} ({used_names[arc]}).{ext}"
                else:
                    arc = f"{arc} ({used_names[arc]})"
            else:
                used_names[arc] = 1
            zf.writestr(arc, data)
    return buf.getvalue()


def _freshness_badge_html(days_ago):
    if days_ago is None:
        return ""
    if days_ago == 0:
        return '<span class="lib-fresh new">오늘 갱신</span>'
    if days_ago <= 14:
        return f'<span class="lib-fresh new">D+{days_ago}</span>'
    if days_ago < 90:
        return f'<span class="lib-fresh ok">D+{days_ago}</span>'
    return f'<span class="lib-fresh old">D+{days_ago} · 갱신확인</span>'


def _sort_lib_files(files, sort_mode):
    if sort_mode == "서류명 가나다순":
        return sorted(files, key=lambda f: f["name"].lower())
    # 기본: 기준일 최신순 -> 같은 날짜면 가나다순
    return sorted(
        files,
        key=lambda f: (
            -(int(f["date_str"].replace("-", "")) if re.match(r"^\d{4}-\d{2}-\d{2}$", f["date_str"]) else 0),
            f["name"].lower(),
        ),
    )


@st.fragment
def _render_lib_bid_tab(get_gc, categories, files):
    if not files:
        st.info("아직 '입찰 필요서류' 폴더에 등록된 파일이 없습니다. 구글 드라이브 `SD_자료실 > 1_입찰 필요서류` 폴더에 최신 서류를 넣어 주세요.")
        return

    if categories:
        with st.container(key="lib_pills_bid"):
            sel_cat = st.radio("소분류", ["전체"] + categories, horizontal=True, key="lib_bid_cat", label_visibility="collapsed")
    else:
        sel_cat = "전체"

    fc1, fc2 = st.columns([3, 1.2])
    with fc1:
        q = st.text_input("서류명 검색", key="lib_bid_q", placeholder="찾으실 서류명을 입력하세요 (예: 사업자등록증, 등기부등본, 완납증명서)",
                          label_visibility="collapsed")
    with fc2:
        sort_mode = st.selectbox("정렬", ["기준일 최신순", "서류명 가나다순"], key="lib_bid_sort", label_visibility="collapsed")

    kw = q.strip().lower()
    filtered = [
        f for f in files
        if (sel_cat == "전체" or f["group"] == sel_cat or f["group"].startswith(f"{sel_cat} · "))
        and (not kw or kw in f["name"].lower() or kw in f["group"].lower())
    ]
    filtered = _sort_lib_files(filtered, sort_mode)

    visible_ids = [f["id"] for f in filtered]
    selected_files = [f for f in filtered if st.session_state.get(f"lib_chk_{f['id']}", False)]

    def _set_visible_checks(flag, ids):
        for i in ids:
            st.session_state[f"lib_chk_{i}"] = flag

    with st.container(key="lib_bulkbar"):
        bc1, bc2, bc3, bc4 = st.columns([1.2, 1.2, 2.2, 3.4])
        with bc1:
            st.button("전체 선택", key="lib_sel_all", icon=":material/select_all:",
                      use_container_width=True, disabled=(not visible_ids),
                      on_click=_set_visible_checks, args=(True, visible_ids))
        with bc2:
            st.button("선택 해제", key="lib_sel_none", icon=":material/deselect:",
                      use_container_width=True, disabled=(not selected_files),
                      on_click=_set_visible_checks, args=(False, [f["id"] for f in files]))
        with bc3:
            st.markdown(
                f'<div class="lib-sel-badge">선택 서류 <b>{len(selected_files)}</b> / {len(filtered)}건</div>',
                unsafe_allow_html=True,
            )
        with bc4:
            if selected_files:
                zip_tuples = tuple((f["id"], f["mimeType"], f["modifiedTime"], f["download_name"]) for f in selected_files)
                zip_name = f"입찰필요서류_{datetime.now(KST).strftime('%Y%m%d')}_{len(selected_files)}종.zip"
                st.download_button(
                    label=f"선택 서류 일괄 다운로드 ({len(selected_files)}건 · .zip)",
                    data=lambda items=zip_tuples: build_drive_zip_bytes(get_gc, items),
                    file_name=zip_name,
                    mime="application/zip",
                    type="primary",
                    icon=":material/folder_zip:",
                    key="lib_zip_dl_btn",
                    on_click="ignore",
                    use_container_width=True,
                )
            else:
                st.button("체크박스로 서류 선택 시 ZIP 일괄 다운로드", key="lib_zip_dl_disabled",
                          icon=":material/folder_zip:", disabled=True, use_container_width=True)

    if not filtered:
        st.caption("검색 조건에 맞는 서류가 없습니다.")
        return

    seen_bid_ids = set()
    dedup_filtered = []
    for f in filtered:
        if f["id"] not in seen_bid_ids:
            seen_bid_ids.add(f["id"])
            dedup_filtered.append(f)
    filtered = dedup_filtered

    for idx, f in enumerate(filtered):
        with st.container(key=f"librow_bid_{idx}_{f['id']}"):
            rc1, rc2, rc3, rc4 = st.columns([0.45, 5.15, 2.8, 1.6])
            with rc1:
                st.checkbox("선택", key=f"lib_chk_{f['id']}", label_visibility="collapsed")
            with rc2:
                cat_chip = f'<span class="lib-cat">{escape(f["group"])}</span>' if f["group"] else ""
                st.markdown(
                    f'<div class="lib-item"><span class="lib-ext {f["ext_cls"]}">{escape(f["ext_label"])}</span>'
                    f'<span class="lib-title">{escape(f["name"])}</span>{cat_chip}</div>',
                    unsafe_allow_html=True,
                )
            with rc3:
                fresh = _freshness_badge_html(f["days_ago"])
                st.markdown(
                    f'<div class="lib-meta"><span class="lib-date">{escape(f["date_str"])}</span>'
                    f'{fresh}<span class="lib-size">{escape(f["size_text"])}</span></div>',
                    unsafe_allow_html=True,
                )
            with rc4:
                st.download_button(
                    label="다운로드",
                    data=lambda fid=f["id"], fm=f["mimeType"], fmod=f["modifiedTime"]: _cached_drive_file_bytes(get_gc, fid, fm, fmod),
                    file_name=f["download_name"],
                    mime=f["download_mime"],
                    icon=":material/download:",
                    key=f"lib_dl_bid_{idx}_{f['id']}",
                    on_click="ignore",
                    use_container_width=True,
                )


@st.fragment
def _render_lib_catalog_tab(get_gc, tag, group_names, files, empty_guide, search_placeholder, count_label):
    if not group_names and not files:
        st.info(empty_guide)
        return

    if group_names:
        with st.container(key=f"lib_pills_{tag}"):
            sel_grp = st.radio("분류 선택", ["전체"] + group_names, horizontal=True,
                               key=f"lib_{tag}_pick", label_visibility="collapsed")
    else:
        sel_grp = "전체"

    sc1, sc2 = st.columns([3, 1.2])
    with sc1:
        q = st.text_input("검색", key=f"lib_{tag}_q", placeholder=search_placeholder,
                          label_visibility="collapsed")
    with sc2:
        sort_mode = st.selectbox("정렬", ["기준일 최신순", "서류명 가나다순"], key=f"lib_{tag}_sort", label_visibility="collapsed")

    kw = q.strip().lower()
    filtered = [
        f for f in files
        if (sel_grp == "전체" or f["group"] == sel_grp or f["group"].startswith(f"{sel_grp} · "))
        and (not kw or kw in f["name"].lower() or kw in f["group"].lower())
    ]
    filtered = _sort_lib_files(filtered, sort_mode)

    if not filtered:
        if sel_grp != "전체":
            st.caption(f"'{sel_grp}' 폴더에 등록된 자료가 아직 없습니다.")
        else:
            st.caption("조건에 맞는 자료가 없습니다.")
        return

    st.caption(f"총 {len(filtered)}건의 {count_label}")
    for idx, f in enumerate(filtered):
        with st.container(key=f"librow_{tag}_{idx}_{f['id']}"):
            rc1, rc2, rc3 = st.columns([5.8, 2.6, 1.6])
            with rc1:
                grp_chip = f'<span class="lib-store">{escape(f["group"])}</span>' if f["group"] else ""
                st.markdown(
                    f'<div class="lib-item">{grp_chip}'
                    f'<span class="lib-ext {f["ext_cls"]}">{escape(f["ext_label"])}</span>'
                    f'<span class="lib-title">{escape(f["name"])}</span></div>',
                    unsafe_allow_html=True,
                )
            with rc2:
                st.markdown(
                    f'<div class="lib-meta"><span class="lib-date">{escape(f["date_str"])}</span>'
                    f'<span class="lib-size">{escape(f["size_text"])}</span></div>',
                    unsafe_allow_html=True,
                )
            with rc3:
                st.download_button(
                    label="다운로드",
                    data=lambda fid=f["id"], fm=f["mimeType"], fmod=f["modifiedTime"]: _cached_drive_file_bytes(get_gc, fid, fm, fmod),
                    file_name=f["download_name"],
                    mime=f["download_mime"],
                    icon=":material/download:",
                    key=f"lib_dl_{tag}_{idx}_{f['id']}",
                    on_click="ignore",
                    use_container_width=True,
                )


def view_file_library(get_gc, is_admin=False):
    hc1, hc2 = st.columns([5, 1.1])
    with hc1:
        st.markdown(sec_heading("folder", "자료실"), unsafe_allow_html=True)
    with hc2:
        st.write("")
        if icon_button("새로고침", ":material/refresh:", key="lib_refresh_btn", use_container_width=True):
            load_drive_library.clear()
            _cached_drive_file_bytes.clear()
            st.rerun()

    lib = load_drive_library(get_gc)
    if lib.get("error"):
        st.error(lib["error"])
        return

    if not lib.get("ready"):
        if is_admin:
            sa_email = lib.get("sa_email") or "(서비스 계정 이메일 확인 필요)"
            st.markdown(
                '<div class="lib-setup">'
                '<h4>구글 드라이브 자료실 폴더 연결 안내 (관리자 최초 1회 설정)</h4>'
                '<p>내 구글 드라이브에 <b>SD_자료실</b> 폴더를 만들고 아래 서비스 계정 이메일에만 <b>뷰어</b> 권한을 부여해 주세요.<br>'
                '일반 액세스는 <b>제한됨(링크 공유 꺼짐)</b> 상태로 유지하면 팀원들은 드라이브에 직접 접근할 수 없고, '
                '이 화면에서만 최신 서류를 안전하게 다운로드할 수 있습니다.</p>'
                '<div class="lib-tree">'
                'SD_자료실  (서비스 계정 이메일에 "뷰어" 공유)\n'
                ' ├── 1_입찰 필요서류\n'
                ' │    ├── 법인·행정서류   (하위 폴더를 만들면 소분류 필터가 자동 생성됩니다)\n'
                ' │    └── 세무·재무서류\n'
                ' ├── 2_주요 이슈 보고서\n'
                ' │    ├── 한강            (점포 폴더를 추가하면 메뉴가 자동 생성됩니다)\n'
                ' │    ├── 63빌딩\n'
                ' │    └── 인천공항\n'
                ' └── 3_제안서\n'
                '      └── (제안서 파일을 바로 넣거나 업종·부문별 하위 폴더로 분류 가능)'
                '</div></div>',
                unsafe_allow_html=True,
            )
            st.caption("공유 대상 서비스 계정 이메일 (우측 복사 아이콘 클릭):")
            st.code(sa_email, language=None)
        else:
            st.info("현재 자료실 폴더가 아직 연결되지 않았습니다. 관리자가 폴더 연결을 완료하면 이곳에서 서류를 다운로드할 수 있습니다.")
        return

    bid_files = lib.get("bid_files", [])
    store_files = lib.get("store_files", [])
    prop_files = lib.get("proposal_files", [])
    st.caption(
        f":material/lock: 사내 보안 자료실 · 최신 최종본 전용  |  "
        f"입찰 필요서류 **{len(bid_files)}건** · 주요 이슈 보고서 **{len(store_files)}건** · 제안서 **{len(prop_files)}건** "
        f"(파일명에 날짜를 적으면 기준일로 자동 표시됩니다)"
    )

    with st.container(key="lvl2tabs_lib"):
        t_bid, t_store, t_prop = st.tabs([
            f":material/assignment_turned_in: 입찰 필요서류 ({len(bid_files)})",
            f":material/apartment: 주요 이슈 보고서 ({len(store_files)})",
            f":material/co_present: 제안서 ({len(prop_files)})",
        ])
    with t_bid:
        _render_lib_bid_tab(get_gc, lib.get("bid_categories", []), bid_files)
    with t_store:
        _render_lib_catalog_tab(
            get_gc, "store", lib.get("store_names", []), store_files,
            "아직 '주요 이슈 보고서' 폴더에 등록된 자료가 없습니다. 구글 드라이브 `SD_자료실 > 2_주요 이슈 보고서` 아래에 점포별 폴더(예: 한강, 63빌딩, 인천공항)를 만들고 최종 보고서를 넣어 주세요.",
            "점포명 또는 보고서 제목 검색",
            "최종 보고서",
        )
    with t_prop:
        _render_lib_catalog_tab(
            get_gc, "prop", lib.get("proposal_categories", []), prop_files,
            "아직 '제안서' 폴더에 등록된 파일이 없습니다. 구글 드라이브 `SD_자료실 > 3_제안서` 폴더를 만들고 제안서 파일을 넣어 주세요. (하위 폴더를 만들면 분류 버튼이 자동 생성됩니다)",
            "제안서 제목 또는 분류 검색",
            "제안서",
        )


# ───────────────────────── 진입점 ─────────────────────────
def render_tool(get_gc, user_id="", user_name="", is_admin=False):
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

    yms, period_text, period_year = pick_period(D)
    myms, myear, mfull, mshort = metric_period(D)

    tab_perf, tab_store, tab_weekly, tab_map, tab_bid, tab_files = st.tabs([
        ":material/bar_chart: 실적 현황",
        ":material/storefront: 점포 현황",
        ":material/edit_note: 주간 활동 보고",
        ":material/map: 특수점 지도",
        ":material/gavel: 입찰 관리",
        ":material/folder: 자료실",
    ])
    with tab_perf:
        view_performance(D, yms, period_text, period_year, myms, mfull, mshort, get_gc=get_gc)
    with tab_store:
        # 점포 현황의 상세 매출/영업이익은 조회 연도와 무관하게 항상 '기준연도(오늘 날짜 기준 올해)'
        # 실적을 보여줍니다(= 지금 얼마 팔고 있는지 확인용). 목록 필터(오픈연도 등)는 view_stores 안에서
        # 별도로 처리됩니다.
        view_stores(D, myms, mfull)
    with tab_weekly:
        render_weekly(get_gc, D, user_id, user_name, is_admin)
    with tab_map:
        view_store_map(get_gc, D)
    with tab_bid:
        view_bid_tracker(get_gc, D=D, user_id=user_id, user_name=user_name, is_admin=is_admin)
    with tab_files:
        view_file_library(get_gc, is_admin=is_admin)
