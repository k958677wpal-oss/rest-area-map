import streamlit as st
import pandas as pd
import gspread
import json
import io
import base64
import requests
import streamlit.components.v1 as components
from datetime import datetime, timezone, timedelta
import datetime as _dt

from PIL import Image, ImageOps

# iPhone HEIC 사진 지원(선택): requirements.txt 에 pillow-heif 가 있을 때만 활성화
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="휴게소 데이터 허브", layout="wide")
KST = timezone(timedelta(hours=9))

# --- 대시보드에서 제외할 담당자 (임시) ---
DASHBOARD_EXCLUDE = ["임진현", "박영진", "김영진"]

# --- 관리자 사번 목록 (앞자리 0 제외 형태로 입력) ---
ADMIN_IDS = ["507583","501580"]

# --- 관리자 통계 대시보드에서 기본으로 펼쳐 보여줄 최근 주차 수 ---
RECENT_WEEKS_SHOWN = 5

# --- 사진 저장(Apps Script 중계) 설정: Streamlit Secrets 에서 읽음 ---
DRIVE_URL = str(st.secrets.get("drive_script_url", "")).strip()
DRIVE_KEY = str(st.secrets.get("drive_script_key", "")).strip()
DRIVE_READY = bool(DRIVE_URL and DRIVE_KEY)

MATERIAL_SHEET_TAB = "휴게소_자료"
MATERIAL_HEADERS = ["휴게소명", "사진1_ID", "사진2_ID", "담당자이름", "연락처", "메모", "올린사람", "올린시각"]
PHOTO_MAX_SIDE = 1280     # 긴 변 최대 픽셀
PHOTO_QUALITY = 75        # JPEG 품질
IMG_TYPES = ["jpg", "jpeg", "png", "webp"] + (["heic", "heif"] if HEIC_OK else [])

# --- 화면 디자인(테마) ---
# True  : 네이비 헤더 + 카드 스타일 적용 (새 디자인)
# False : 예전 모양 그대로 (이 한 단어만 False 로 바꾸면 원복됩니다)
APPLY_THEME = True

if APPLY_THEME:
    st.markdown(
        """
        <style>
        /* 상단 여백 줄이기 */
        .block-container { padding-top: 3.2rem !important; }

        /* 헤더 배너 */
        .hub-header {
            background: linear-gradient(135deg, #1f3a73 0%, #2c4a8a 55%, #3a5fa8 100%);
            color: #ffffff;
            border-radius: 14px;
            padding: 18px 24px;
            margin: 0 0 14px 0;
            box-shadow: 0 4px 14px rgba(31, 58, 115, 0.22);
        }
        .hub-header .hub-title {
            font-size: 26px;
            font-weight: 800;
            letter-spacing: -0.5px;
            line-height: 1.3;
        }
        .hub-header .hub-sub {
            font-size: 13px;
            opacity: 0.82;
            margin-top: 4px;
        }
        @media (max-width: 640px) {
            .hub-header { padding: 14px 16px; border-radius: 12px; }
            .hub-header .hub-title { font-size: 18px; }
            .hub-header .hub-sub { font-size: 11.5px; }
        }

        /* 탭: 선택색을 네이비로 */
        button[data-baseweb="tab"][aria-selected="true"] {
            color: #2c4a8a !important;
            font-weight: 700;
        }
        div[data-baseweb="tab-highlight"] { background-color: #2c4a8a !important; }

        /* 검색창·선택 상자: 둥글게 + 얇은 테두리 */
        div[data-baseweb="select"] > div {
            border-radius: 10px !important;
            border: 1px solid #d5dcea !important;
            background-color: #f6f8fc !important;
        }

        /* 접이식 메뉴(expander): 카드 모양 */
        div[data-testid="stExpander"] {
            border: 1px solid #d5dcea !important;
            border-radius: 12px !important;
            background: #ffffff;
            box-shadow: 0 1px 4px rgba(31, 58, 115, 0.06);
        }
        div[data-testid="stExpander"] summary { font-weight: 600; color: #1f3a73; }

        /* 지도 카드 */
        iframe[title="streamlit_component"],
        iframe#kakaoMapFrame {
            border-radius: 14px;
        }

        /* 버튼: 네이비 포인트 */
        div.stButton > button, div[data-testid="stFormSubmitButton"] > button {
            border-radius: 10px;
            border: 1px solid #2c4a8a;
            color: #2c4a8a;
            font-weight: 600;
        }
        div.stButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover {
            background-color: #2c4a8a;
            color: #ffffff;
            border-color: #2c4a8a;
        }

        /* 로그인 폼: 카드 모양 */
        div[data-testid="stForm"] {
            border: 1px solid #d5dcea !important;
            border-radius: 14px !important;
            background: #ffffff;
            padding: 20px 22px !important;
            box-shadow: 0 2px 10px rgba(31, 58, 115, 0.08);
        }

        /* 진행 막대 */
        div[data-testid="stProgress"] div[role="progressbar"] > div { background-color: #2c4a8a !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        """
        <style>
        .app-title {
            font-size: 28px;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin: 0 0 8px 0;
        }
        @media (max-width: 640px) {
            .app-title { font-size: 18px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# --- 1. 구글 시트 연결 ---
@st.cache_resource
def init_connection():
    key_dict = json.loads(st.secrets["google_secret"])
    return gspread.service_account_from_dict(key_dict)

@st.cache_data(ttl=300)
def load_data():
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    return pd.DataFrame(doc.sheet1.get_all_records())

LOGIN_SHEET_KEY = "1zD20Om2mHH7M9ppcialvqB1SxA9IyVq2aCLM9IWoo8c"
REQUEST_SHEET_TAB = "수정_요청_이력"
WEEKLY_SHEET_TAB = "주간_활동보고"

@st.cache_data(ttl=300)
def load_login_data():
    gc = init_connection()
    doc = gc.open_by_key(LOGIN_SHEET_KEY)
    worksheet = doc.worksheet("사용자 명단")
    return pd.DataFrame(worksheet.get_all_records())

def write_login_log(user_name):
    gc = init_connection()
    doc = gc.open_by_key(LOGIN_SHEET_KEY)
    worksheet = doc.worksheet("접속_로그")
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    worksheet.append_row([now_str, user_name, "로그인 성공"])

# --- 수정 요청 이력 ---
@st.cache_data(ttl=60)
def load_edit_requests():
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    ws = doc.worksheet(REQUEST_SHEET_TAB)
    return pd.DataFrame(ws.get_all_records())

def add_edit_request(rest_name, user_id, memo):
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    ws = doc.worksheet(REQUEST_SHEET_TAB)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([rest_name, user_id, memo, "대기중", now_str])

def get_pending_rest_names():
    try:
        rdf = load_edit_requests()
    except Exception:
        return set()
    if rdf.empty or "상태" not in rdf.columns or "휴게소명" not in rdf.columns:
        return set()
    pending = rdf[rdf["상태"].astype(str).str.strip() == "대기중"]
    return set(pending["휴게소명"].astype(str).str.strip())

# --- 휴게소 자료(사진·연락처) ---
def get_material_ws(create=False):
    """'휴게소_자료' 탭을 반환. create=True 이면 없을 때 자동으로 만들고 헤더를 넣습니다."""
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    try:
        return doc.worksheet(MATERIAL_SHEET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        ws = doc.add_worksheet(title=MATERIAL_SHEET_TAB, rows=500, cols=len(MATERIAL_HEADERS))
        ws.append_row(MATERIAL_HEADERS)
        return ws

@st.cache_data(ttl=60)
def load_materials():
    """{휴게소명: {컬럼: 값}} 형태로 반환. 전화번호 앞자리 0이 사라지지 않도록 문자열 그대로 읽습니다."""
    ws = get_material_ws(create=False)
    if ws is None:
        return {}
    values = ws.get_all_values()
    result = {}
    for row in values[1:]:
        row = list(row) + [""] * (len(MATERIAL_HEADERS) - len(row))
        rec = dict(zip(MATERIAL_HEADERS, [str(c).strip() for c in row]))
        if rec["휴게소명"]:
            result[rec["휴게소명"]] = rec
    return result

@st.cache_data(ttl=600)
def load_route_lengths():
    """
    노선별 총 길이를 {고속도로명: 길이(km)} 로 반환합니다.
    시트 탭 이름이 무엇이든, 첫 줄이 '고속도로명 | 총길이(km)' 인 탭을 자동으로 찾아 읽습니다.
    """
    try:
        gc = init_connection()
        doc = gc.open("휴게소_통합_데이터")
        for ws in doc.worksheets():
            head = [str(c).strip().replace(" ", "") for c in ws.row_values(1)[:2]]
            if len(head) == 2 and head[0] == "고속도로명" and head[1].startswith("총길이"):
                result = {}
                for row in ws.get_all_values()[1:]:
                    if len(row) >= 2 and str(row[0]).strip() and str(row[1]).strip():
                        result[str(row[0]).strip()] = str(row[1]).strip().replace(",", "")
                return result
    except Exception:
        pass
    return {}

def save_material(rest_name, photo1_id, photo2_id, c_name, c_phone, c_memo, uploader):
    """휴게소당 1행 유지: 새 행을 추가한 뒤 기존 행을 삭제합니다. (중간에 실패해도 데이터가 사라지지 않게 추가를 먼저 함)"""
    ws = get_material_ws(create=True)
    values = ws.get_all_values()
    old_row_num = None
    for i, row in enumerate(values[1:], start=2):
        if row and str(row[0]).strip() == rest_name:
            old_row_num = i
            break
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([rest_name, photo1_id, photo2_id, c_name, c_phone, c_memo, uploader, now_str])
    if old_row_num is not None:
        ws.delete_rows(old_row_num)

def make_thumb(uploaded_file, max_side=480):
    """미리보기용 작은 이미지(원본 대용량 사진을 화면에 그대로 보내지 않기 위함)."""
    try:
        uploaded_file.seek(0)
        img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        uploaded_file.seek(0)
        return buf.getvalue()
    except Exception:
        return None

def process_image(uploaded_file):
    """업로드된 사진을 회전 보정 후 긴 변 1280px, JPEG 품질 75로 줄여 bytes 반환."""
    try:
        uploaded_file.seek(0)
        img = Image.open(uploaded_file)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=PHOTO_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        raise ValueError("사진을 읽을 수 없습니다. JPG 또는 PNG 사진인지 확인해 주세요.")

def drive_call(payload, timeout=90):
    """Apps Script 웹 앱 호출. text/plain 으로 보내 Apps Script가 안정적으로 받도록 합니다."""
    body = dict(payload)
    body["key"] = DRIVE_KEY
    res = requests.post(
        DRIVE_URL,
        data=json.dumps(body),
        headers={"Content-Type": "text/plain;charset=utf-8"},
        timeout=timeout,
    )
    res.raise_for_status()
    return res.json()

def upload_photo(jpeg_bytes, rest_name, slot):
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out = drive_call({
        "action": "upload",
        "name": f"{rest_name}_{slot}_{ts}.jpg",
        "mime": "image/jpeg",
        "data": base64.b64encode(jpeg_bytes).decode("ascii"),
    })
    if not out.get("ok"):
        raise RuntimeError(f"사진 저장 실패: {out.get('error', '알 수 없는 오류')}")
    return out["id"]

@st.cache_data(ttl=3600, show_spinner="사진을 불러오는 중...")
def fetch_photos(ids):
    """파일 ID 튜플 -> [bytes 또는 None, ...] (한 번의 호출로 여러 장)"""
    out = drive_call({"action": "get_many", "ids": list(ids)})
    if not out.get("ok"):
        return [None] * len(ids)
    by_id = {it.get("id"): it for it in out.get("items", [])}
    result = []
    for i in ids:
        it = by_id.get(i)
        if it and it.get("ok"):
            result.append(base64.b64decode(it["data"]))
        else:
            result.append(None)
    return result

# --- 주간 활동 보고 ---
def get_week_info(target_date=None):
    """
    주어진 날짜(기본: 오늘 KST)가 속한 주의 정보를 반환.
    - 주는 월요일 시작 ~ 일요일 끝
    - '그 달 1일이 포함된 주 = 1주차' 기준
    """
    if target_date is None:
        target_date = datetime.now(KST).date()

    monday = target_date - _dt.timedelta(days=target_date.weekday())
    sunday = monday + _dt.timedelta(days=6)

    year = monday.year
    month = monday.month

    first_day = _dt.date(year, month, 1)
    first_monday = first_day - _dt.timedelta(days=first_day.weekday())
    week_num = ((monday - first_monday).days // 7) + 1

    code = f"{year}-{month:02d}-{week_num}"
    label = f"{year}년 {month}월 {week_num}주차 ({monday.strftime('%m/%d')}~{sunday.strftime('%m/%d')})"
    return code, label

def parse_week_code(code):
    """'2026-10-4' -> (2026, 10, 4). 형식이 맞지 않으면 None."""
    try:
        y, m, w = str(code).strip().split("-")
        return int(y), int(m), int(w)
    except Exception:
        return None

def short_week_label(label):
    """'2026년 10월 4주차 (10/19~10/25)' -> '4주차 (10/19~10/25)'"""
    s = str(label)
    if "월 " in s:
        return s.split("월 ", 1)[1]
    return s

def build_week_options(wdf, cur_code, cur_label):
    """
    주차 선택용 목록을 만듭니다. (최신 주차가 앞)
    - 정렬은 표시 문자열이 아니라 주차코드(연, 월, 주)의 숫자 기준
    - 아직 아무도 작성하지 않은 '이번 주'도 항상 포함
    """
    label_map = {}
    if (not wdf.empty) and "주차코드" in wdf.columns and "주차표시" in wdf.columns:
        for c, l in zip(wdf["주차코드"], wdf["주차표시"]):
            c = str(c).strip()
            l = str(l).strip()
            if c and c not in label_map:
                label_map[c] = l
    label_map.setdefault(cur_code, cur_label)

    entries = []
    for c, l in label_map.items():
        k = parse_week_code(c)
        if k is None:
            continue
        entries.append({"code": c, "label": l, "y": k[0], "m": k[1], "w": k[2]})
    entries.sort(key=lambda e: (e["y"], e["m"], e["w"]), reverse=True)
    return entries

# --- 주차 선택 콜백 (세션 상태 'dash_code'가 선택된 주차의 단일 기준) ---
def _dash_by_code():
    return {e["code"]: e for e in st.session_state.get("_dash_entries", [])}

def _dash_on_year():
    entries = st.session_state.get("_dash_entries", [])
    by_code = _dash_by_code()
    cur = by_code.get(st.session_state.get("dash_code"))
    new_year = st.session_state.get("dash_year")
    cands = [e for e in entries if e["y"] == new_year]
    if not cands:
        return
    # 같은 월이 있으면 유지, 없으면 그 해의 가장 최근 주차
    same_month = [e for e in cands if cur and e["m"] == cur["m"]]
    st.session_state["dash_code"] = (same_month[0] if same_month else cands[0])["code"]

def _dash_on_month():
    entries = st.session_state.get("_dash_entries", [])
    by_code = _dash_by_code()
    cur = by_code.get(st.session_state.get("dash_code"))
    new_month = st.session_state.get("dash_month")
    if cur is None:
        return
    cands = [e for e in entries if e["y"] == cur["y"] and e["m"] == new_month]
    if cands:
        st.session_state["dash_code"] = cands[0]["code"]

def _dash_on_week():
    st.session_state["dash_code"] = st.session_state.get("dash_week")

def _dash_step(delta):
    """delta=+1: 이전 주(더 과거), delta=-1: 다음 주(더 최근)"""
    entries = st.session_state.get("_dash_entries", [])
    codes = [e["code"] for e in entries]
    cur = st.session_state.get("dash_code")
    if cur in codes:
        idx = codes.index(cur) + delta
        if 0 <= idx < len(codes):
            st.session_state["dash_code"] = codes[idx]

@st.cache_data(ttl=60)
def load_weekly_reports():
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    ws = doc.worksheet(WEEKLY_SHEET_TAB)
    return pd.DataFrame(ws.get_all_records())

def add_weekly_report(week_code, week_label, user_id, user_name, visited, activity, item, note):
    """
    visited·activity·note 가 채워지면 '방문 보고' 행,
    item 만 채워지고 나머지가 비면 '추진물건' 행이 됩니다. (같은 시트, 컬럼 값으로 구분)
    """
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    ws = doc.worksheet(WEEKLY_SHEET_TAB)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([week_code, week_label, user_id, user_name,
                   visited, activity, item, note, now_str])

def delete_weekly_report(week_code, user_id, created_at):
    """
    주차코드 + 사번 + 작성시각이 모두 일치하는 행을 찾아 시트에서 삭제.
    """
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터")
    ws = doc.worksheet(WEEKLY_SHEET_TAB)
    all_values = ws.get_all_values()
    if not all_values:
        return False
    header = all_values[0]

    def col_idx(name):
        for i, h in enumerate(header):
            if str(h).strip() == name:
                return i
        return -1
    ci_code = col_idx("주차코드")
    ci_uid = col_idx("사번")
    ci_time = col_idx("작성시각")

    for row_num in range(2, len(all_values) + 1):
        row = all_values[row_num - 1]
        def cell(i):
            return str(row[i]).strip() if 0 <= i < len(row) else ""
        if (cell(ci_code) == str(week_code).strip()
                and cell(ci_uid) == str(user_id).strip()
                and cell(ci_time) == str(created_at).strip()):
            ws.delete_rows(row_num)
            return True
    return False

@st.cache_data(ttl=300)
def load_all_member_names():
    gc = init_connection()
    doc = gc.open_by_key(LOGIN_SHEET_KEY)
    ws = doc.worksheet("사용자 명단")
    df = pd.DataFrame(ws.get_all_records())
    name_col = find_column(df, "이름")
    if name_col is None:
        return []
    return [str(n).strip() for n in df[name_col].tolist() if str(n).strip()]

def normalize_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value)
    s = s.strip()
    s = s.replace(" ", "").replace("​", "")
    if s.isdigit():
        s = str(int(s))
    return s

def find_column(df, target_name):
    normalized_target = target_name.replace(" ", "").strip()
    for col in df.columns:
        if str(col).replace(" ", "").strip() == normalized_target:
            return col
    return None

def format_thousands(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.strip() == "":
        return ""
    try:
        num = float(value)
        if num.is_integer():
            return f"{int(num):,}"
        return f"{num:,.2f}"
    except (ValueError, TypeError):
        return value

def _render_delete_button(row_code, row_uid, row_time, current_uid, is_admin):
    """
    확인 절차가 있는 삭제 버튼을 렌더링합니다.
    본인 글이거나 관리자일 때만 호출부에서 이 함수를 부릅니다.
    """
    uid_key = f"{row_code}_{row_uid}_{row_time}"
    confirm_key = "confirm_del_" + uid_key

    if st.session_state.get(confirm_key, False):
        st.warning("정말 삭제하시겠습니까? 되돌릴 수 없습니다.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 예, 삭제", key="yes_" + uid_key):
                try:
                    ok = delete_weekly_report(row_code, row_uid, row_time)
                    st.session_state[confirm_key] = False
                    if ok:
                        load_weekly_reports.clear()
                        st.success("삭제되었습니다.")
                        st.rerun()
                    else:
                        st.error("해당 항목을 찾지 못했습니다.")
                except Exception as e:
                    st.error(f"삭제 중 오류: {e}")
        with c2:
            if st.button("취소", key="no_" + uid_key):
                st.session_state[confirm_key] = False
                st.rerun()
    else:
        if st.button("🗑️ 삭제", key="del_" + uid_key):
            st.session_state[confirm_key] = True
            st.rerun()

def build_admin_stats(wdf, members):
    """
    관리자 통계 대시보드용 데이터를 만듭니다.
    """
    if wdf.empty or "주차코드" not in wdf.columns or "주차표시" not in wdf.columns:
        return pd.DataFrame(), [], []

    week_map = (
        wdf[["주차코드", "주차표시"]]
        .drop_duplicates()
        .dropna()
    )
    week_map = week_map[week_map["주차코드"].astype(str).str.strip() != ""]

    def week_sort_key(code):
        try:
            y, m, w = str(code).split("-")
            return (int(y), int(m), int(w))
        except Exception:
            return (0, 0, 0)

    week_map["_sort"] = week_map["주차코드"].apply(week_sort_key)
    week_map = week_map.sort_values("_sort", ascending=False)
    all_week_codes = week_map["주차코드"].tolist()
    code_to_label = dict(zip(week_map["주차코드"], week_map["주차표시"]))

    visits = wdf[wdf["방문휴게소"].astype(str).str.strip() != ""]
    items = wdf[
        (wdf["방문휴게소"].astype(str).str.strip() == "")
        & (wdf["추진물건"].astype(str).str.strip() != "")
    ]

    rows = []
    for member in members:
        row = {"담당자": member}
        for code in all_week_codes:
            v_cnt = len(visits[(visits["이름"].astype(str).str.strip() == member) & (visits["주차코드"].astype(str) == str(code))])
            it_cnt = len(items[(items["이름"].astype(str).str.strip() == member) & (items["주차코드"].astype(str) == str(code))])
            total_cnt = v_cnt + it_cnt
            label = code_to_label.get(code, code)
            if v_cnt == 0 and total_cnt == 0:
                cell = "미작성"
            elif v_cnt == 0:
                cell = f"방문 0 · 전체 {total_cnt} (방문 미작성)"
            else:
                cell = f"방문 {v_cnt} · 전체 {total_cnt}"
            row[label] = cell
        rows.append(row)

    result_df = pd.DataFrame(rows)
    recent_labels = [code_to_label[c] for c in all_week_codes[:RECENT_WEEKS_SHOWN]]
    older_labels = [code_to_label[c] for c in all_week_codes[RECENT_WEEKS_SHOWN:]]
    return result_df, recent_labels, older_labels

@st.cache_data(ttl=300)
def load_distances_data():
    """
    로컬 build_distances.py 로 미리 만들어 GitHub Pages(docs/)에 올려둔
    distances.json 을 읽어옵니다. app.py 는 이 파일을 직접 갖고 있지 않으므로,
    지도(index.html)가 fetch로 읽도록 하고, app.py 에서는 휴게소별 prev/next만
    보조적으로 구성해 지도로 넘겨줍니다. (routes/segments 원본은 index.html이 직접 로드)
    이 함수는 로컬에 distances.json 파일이 app.py와 같은 위치에 있을 때만 사용됩니다.
    없으면 빈 데이터로 처리되고 index.html이 자체적으로 fetch합니다.
    """
    try:
        with open("distances.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"routes": {}, "segments": {}}

def get_prev_next_map(distances_data):
    """
    {휴게소명: {"prev": {...} or None, "next": {...} or None}} 형태로 변환.
    같은 이름이 여러 노선/방향에 나오면 마지막 것으로 덮어써질 수 있으므로,
    노선명+방향까지 포함한 세부 정보는 history처럼 리스트로 관리합니다.
    """
    pn_map = {}
    routes = distances_data.get("routes", {})
    for highway, directions in routes.items():
        for direction, entries in directions.items():
            for entry in entries:
                name = entry.get("name")
                if not name:
                    continue
                pn_map.setdefault(name, []).append({
                    "highway": highway,
                    "direction": direction,
                    "prev": entry.get("prev"),
                    "next": entry.get("next"),
                })
    return pn_map

# --- 2. 로그인 시스템 ---
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

if not st.session_state["logged_in"]:
    if APPLY_THEME:
        st.markdown(
            '<div class="hub-header">'
            '<div class="hub-title">🛣️ 휴게소 데이터 허브</div>'
            '<div class="hub-sub">Powered by 특수개발팀 · GS25 점포개발 &nbsp;|&nbsp; 🔐 로그인</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.title("🔐 로그인 (Powered by 특수개발팀)")
    with st.form("login_form"):
        user_id = st.text_input("사번 (ID)")
        user_pw = st.text_input("생년월일 6자리 (PW)", type="password", max_chars=6)
        submitted = st.form_submit_button("로그인")
    if submitted:
        input_id = normalize_value(user_id)
        input_pw = normalize_value(user_pw)
        try:
            login_df = load_login_data()
        except Exception as e:
            st.error(f"로그인 정보 로드 오류: {e}")
            st.stop()
        id_col = find_column(login_df, "사번")
        pw_col = find_column(login_df, "생년월일")
        name_col = find_column(login_df, "이름")
        matched = False
        matched_name = ""
        if id_col is not None and pw_col is not None:
            for _, row in login_df.iterrows():
                sheet_id = normalize_value(row.get(id_col, ""))
                sheet_pw = normalize_value(row.get(pw_col, ""))
                if input_id == sheet_id and input_pw == sheet_pw:
                    matched = True
                    if name_col is not None:
                        matched_name = str(row.get(name_col, "")).strip()
                    break
        if matched:
            st.session_state["logged_in"] = True
            st.session_state["user_id"] = input_id
            st.session_state["user_name"] = matched_name
            try:
                write_login_log(matched_name)
            except Exception:
                pass
            st.rerun()
        else:
            st.error("사번 또는 비밀번호가 올바르지 않습니다.")
    st.stop()

# --- 로그인 성공 시 ---
if APPLY_THEME:
    st.markdown(
        '<div class="hub-header">'
        '<div class="hub-title">🛣️ 휴게소 데이터 허브</div>'
        '<div class="hub-sub">Powered by 특수개발팀 · GS25 점포개발</div>'
        '</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown('<div class="app-title">🛣️ 휴게소 데이터 허브 (Powered by 특수개발팀)</div>', unsafe_allow_html=True)

current_uid = st.session_state.get("user_id", "")
current_name = st.session_state.get("user_name", "")
is_admin = current_uid in ADMIN_IDS

try:
    df = load_data()
except Exception as e:
    st.error(f"데이터 로드 오류: {e}")
    st.stop()

def parse_coordinate(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

pending_names = get_pending_rest_names()

# --- 휴게소별 방문 이력 인덱스 구성 (지도 '이력 보기'용) ---
try:
    wdf_all = load_weekly_reports()
except Exception:
    wdf_all = pd.DataFrame()

history_index = {}
if not wdf_all.empty and "방문휴게소" in wdf_all.columns:
    visit_rows = wdf_all[wdf_all["방문휴게소"].astype(str).str.strip() != ""]
    for _, row in visit_rows.iterrows():
        key = str(row.get("방문휴게소", "")).strip()
        if not key:
            continue
        entry = {
            "date": str(row.get("주차표시", "")).strip(),
            "manager": str(row.get("이름", "")).strip(),
            "activity": str(row.get("활동내용", "")).strip(),
            "note": str(row.get("특이사항", "")).strip(),
            "created_at": str(row.get("작성시각", "")).strip(),
        }
        history_index.setdefault(key, []).append(entry)
    for k in history_index:
        history_index[k].sort(key=lambda e: e["created_at"], reverse=True)

# --- 휴게소별 전/후 거리 정보 (지도 '거리 보기'용) ---
# distances.json 자체(routes/segments)는 index.html이 직접 fetch로 읽으므로,
# app.py는 여기서 별도로 무겁게 실을 필요가 없습니다. (선택적 보조 정보만 시도)
distances_data = load_distances_data()
prev_next_map = get_prev_next_map(distances_data)

# --- 휴게소별 자료(사진 ID·연락처) 인덱스 (지도 '사진 보기'용) ---
# 사진 자체는 여기서 보내지 않고 파일 ID만 보냅니다. 사진은 팝업에서 '사진 보기'를 누를 때만 불러옵니다.
try:
    materials_map = load_materials()
except Exception:
    materials_map = {}

def build_material_payload(rec):
    if not rec:
        return None
    ids = [i for i in (rec.get("사진1_ID", ""), rec.get("사진2_ID", "")) if i]
    has_contact = any(rec.get(k) for k in ("담당자이름", "연락처", "메모"))
    if not ids and not has_contact:
        return None
    return {
        "ids": ids,
        "contact_name": rec.get("담당자이름", ""),
        "phone": rec.get("연락처", ""),
        "memo": rec.get("메모", ""),
        "uploader": rec.get("올린사람", ""),
        "uploaded_at": rec.get("올린시각", ""),
    }

points = []
skipped_rows = []
for idx, r in df.iterrows():
    name = r.get("휴게소명", f"{idx}행")
    lat = parse_coordinate(r.get("위도"))
    lng = parse_coordinate(r.get("경도"))
    if lat is None or lng is None:
        skipped_rows.append({
            "휴게소명": name,
            "위도_원본값": r.get("위도"),
            "경도_원본값": r.get("경도"),
            "사유": "위도 또는 경도 값이 비어있거나 숫자로 변환할 수 없음",
        })
        continue
    try:
        points.append({
            "name": name,
            "lat": lat,
            "lng": lng,
            "brand": r.get("브랜드", ""),
            "operator": r.get("운영사", ""),
            "revenue": r.get("연매출액", ""),
            "estimated_revenue": format_thousands(r.get("추정매출", "")),
            "contract": r.get("계약기간", ""),
            "contract_step": r.get("계약차수", ""),
            "manager": r.get("담당자", ""),
            "highway": r.get("고속도로명", ""),
            "has_pending": (str(name).strip() in pending_names),
            "history": history_index.get(str(name).strip(), []),
            "distance_info": prev_next_map.get(str(name).strip(), []),
            "material": build_material_payload(materials_map.get(str(name).strip())),
        })
    except Exception as e:
        skipped_rows.append({
            "휴게소명": name,
            "위도_원본값": r.get("위도"),
            "경도_원본값": r.get("경도"),
            "사유": f"알 수 없는 오류: {e}",
        })
        continue

PAGE_URL = "https://k958677wpal-oss.github.io/rest-area-map/"
TARGET_ORIGIN = "https://k958677wpal-oss.github.io"
MAP_HEIGHT = 560

# --- 탭 구성: 관리자면 통계 대시보드 + 상세 데이터 탭 포함, 아니면 제외 ---
if is_admin:
    tab_map, tab_weekly, tab_admin_stats, tab_data = st.tabs(
        ["🗺️ 스마트 노선 맵", "🗒️ 주간 활동 보고", "📈 담당자 작성 현황 (관리자)", "📊 상세 데이터 및 필터"]
    )
else:
    tab_map, tab_weekly = st.tabs(
        ["🗺️ 스마트 노선 맵", "🗒️ 주간 활동 보고"]
    )
    tab_admin_stats = None
    tab_data = None

with tab_map:
    valid_names = [p["name"] for p in points]
    name_options = [""] + valid_names

    query = st.selectbox(
        "🔍 휴게소 검색",
        options=name_options,
        index=0,
        help="휴게소명을 타이핑하면 자동완성 목록에서 정확한 휴게소를 선택할 수 있습니다.",
    )

    with st.expander("✏️ 이 휴게소 정보 수정 요청하기"):
        with st.form("edit_request_form", clear_on_submit=True):
            target = st.selectbox("수정 요청할 휴게소", options=[""] + valid_names)
            memo = st.text_area("수정 요청 메모", placeholder="예: 브랜드가 CU에서 GS25로 변경되었습니다.")
            sent = st.form_submit_button("요청 제출")
        if sent:
            if not target or not memo.strip():
                st.warning("휴게소와 메모를 모두 입력해 주세요.")
            else:
                try:
                    add_edit_request(target, current_uid, memo.strip())
                    load_edit_requests.clear()
                    st.success(f"'{target}' 수정 요청이 접수되었습니다. (상태: 대기중)")
                    st.rerun()
                except Exception as e:
                    st.error(f"요청 저장 중 오류: {e}")

    points_json = json.dumps(points, ensure_ascii=False)
    query_json = json.dumps(query if query else "")
    # 로그인한 사용자의 화면에서만 사진 서버 주소/키를 지도에 전달 (index.html 파일에는 적어두지 않음)
    drive_json = json.dumps({"url": DRIVE_URL, "key": DRIVE_KEY}) if DRIVE_READY else "null"
    # 노선별 총 길이(km): 지도의 노선 이름표에 "총 길이 123km" 로 표시됩니다
    route_lengths_json = json.dumps(load_route_lengths(), ensure_ascii=False)

    component_html = f"""
    <iframe
        id="kakaoMapFrame"
        src="{PAGE_URL}"
        style="width:100%; height:{MAP_HEIGHT}px; border:none;"
        scrolling="no"
    ></iframe>
    <script>
      (function () {{
        var mapPoints = {points_json};
        var searchQuery = {query_json};
        var driveCfg = {drive_json};
        var routeLengths = {route_lengths_json};
        var iframe = document.getElementById('kakaoMapFrame');
        function sendData() {{
          iframe.contentWindow.postMessage(
            {{ type: 'INIT_MAP_DATA', points: mapPoints, query: searchQuery, drive: driveCfg, routeLengths: routeLengths }},
            '{TARGET_ORIGIN}'
          );
        }}
        iframe.onload = function () {{ sendData(); }};
      }})();
    </script>
    """
    components.html(component_html, height=MAP_HEIGHT, scrolling=False)

    if skipped_rows:
        with st.expander(f"⚠️ 좌표 오류로 지도에 표시되지 않은 휴게소 {len(skipped_rows)}건 (클릭하여 확인)"):
            st.dataframe(pd.DataFrame(skipped_rows), use_container_width=True)
            st.caption("구글 시트에서 해당 휴게소의 '위도'/'경도' 값을 채워주시면 다음 새로고침 시 지도에 반영됩니다.")

    # ── 📎 휴게소 자료 등록 (사진 2장 + 현장 담당자 연락처) ──
    # form 을 쓰지 않는 이유: form 안에서는 제출 전까지 파일이 앱에 전달되지 않아 미리보기를 보여줄 수 없습니다.
    # st.fragment: 이 영역 안의 조작(사진 선택, 글자 입력)은 이 영역만 다시 실행합니다.
    # (지도·시트 데이터 전체가 다시 실행되지 않아 휴대폰에서 사진 업로드가 훨씬 안정적입니다)
    @st.fragment
    def material_register_fragment():
      with st.expander("📎 휴게소 자료 등록 (사진·연락처)", expanded=False):
        rc = st.session_state.get("mat_reset", 0)

        flash = st.session_state.pop("mat_flash", None)
        if flash:
            st.success(flash)

        if not DRIVE_READY:
            st.warning("사진 저장 설정(drive_script_url / drive_script_key)이 아직 없어 사진 등록은 사용할 수 없습니다. 연락처만 등록할 수 있습니다.")
        elif not HEIC_OK:
            st.caption("JPG·PNG 사진을 올려 주세요.")

        try:
            all_materials = load_materials()
        except Exception as e:
            all_materials = {}
            st.error(f"자료 목록 로드 오류: {e}")

        # ── 등록 현황: 사진 또는 연락처가 하나라도 있으면 '등록됨' ──
        done_names = [n for n in valid_names if build_material_payload(all_materials.get(str(n).strip())) is not None]
        done_set = set(done_names)
        missing_names = [n for n in valid_names if n not in done_set]
        total_cnt = len(valid_names)
        done_cnt = len(done_names)

        st.markdown(f"**📊 등록 현황: {done_cnt} / {total_cnt}**  (미등록 {len(missing_names)}곳)")
        st.progress(done_cnt / total_cnt if total_cnt else 0.0)

        # 노선별 현황 (접어두기)
        with st.expander("노선별 등록 현황 보기"):
            hw_of = {p["name"]: (str(p.get("highway", "")).strip() or "(노선 없음)") for p in points}
            stat = {}
            for n in valid_names:
                hw = hw_of.get(n, "(노선 없음)")
                s = stat.setdefault(hw, {"전체": 0, "등록": 0})
                s["전체"] += 1
                if n in done_set:
                    s["등록"] += 1
            stat_rows = [
                {"노선": hw, "등록": s["등록"], "전체": s["전체"], "미등록": s["전체"] - s["등록"]}
                for hw, s in stat.items()
            ]
            stat_rows.sort(key=lambda r: (-r["미등록"], r["노선"]))
            st.dataframe(pd.DataFrame(stat_rows), use_container_width=True, hide_index=True)

        only_missing = st.checkbox("미등록 휴게소만 보기", key="mat_only_missing")
        if only_missing:
            select_options = [""] + missing_names
            if not missing_names:
                st.success("🎉 모든 휴게소의 자료가 등록되었습니다.")
        else:
            select_options = [""] + valid_names

        mat_target = st.selectbox("휴게소 선택", options=select_options, key=f"mat_target_{rc}")
        old_rec = all_materials.get(mat_target) if mat_target else None
        if old_rec:
            n_photos = sum(1 for k in ("사진1_ID", "사진2_ID") if old_rec.get(k))
            st.caption(
                f"현재 등록: 사진 {n_photos}장 · 담당자 {old_rec.get('담당자이름') or '-'} · "
                f"연락처 {old_rec.get('연락처') or '-'} · {old_rec.get('올린사람') or '-'} ({old_rec.get('올린시각') or '-'}). "
                f"비워 둔 항목은 기존 값이 유지되고, 새로 올린 항목만 교체됩니다."
            )

        pc1, pc2 = st.columns(2)
        with pc1:
            up1 = st.file_uploader("휴게소 외부사진", type=IMG_TYPES, key=f"mat_p1_{rc}")
            if up1 is not None:
                st.image(make_thumb(up1), caption="미리보기", width=240)
        with pc2:
            up2 = st.file_uploader("점포 사진", type=IMG_TYPES, key=f"mat_p2_{rc}")
            if up2 is not None:
                st.image(make_thumb(up2), caption="미리보기", width=240)

        c_name = st.text_input("현장 담당자 이름", key=f"mat_cname_{rc}", placeholder="예: 홍길동 점장")
        c_phone = st.text_input("연락처", key=f"mat_cphone_{rc}", placeholder="예: 010-1234-5678")
        c_memo = st.text_input("메모", key=f"mat_cmemo_{rc}", placeholder="예: 평일 오전 통화 가능")

        if st.button("이대로 등록", key=f"mat_submit_{rc}", type="primary"):
            has_photo = (up1 is not None) or (up2 is not None)
            has_text = any(v.strip() for v in (c_name, c_phone, c_memo))
            if not mat_target:
                st.warning("휴게소를 선택해 주세요.")
            elif not has_photo and not has_text:
                st.warning("사진이나 연락처 중 하나 이상을 입력해 주세요.")
            elif has_photo and not DRIVE_READY:
                st.error("사진 저장 설정이 없어 사진을 올릴 수 없습니다. 관리자에게 문의해 주세요.")
            else:
                try:
                    with st.spinner("사진을 줄여서 올리는 중입니다... (잠시만 기다려 주세요)"):
                        old = all_materials.get(mat_target, {})
                        id1 = old.get("사진1_ID", "")
                        id2 = old.get("사진2_ID", "")
                        if up1 is not None:
                            id1 = upload_photo(process_image(up1), mat_target, 1)
                        if up2 is not None:
                            id2 = upload_photo(process_image(up2), mat_target, 2)
                        save_material(
                            mat_target, id1, id2,
                            c_name.strip() or old.get("담당자이름", ""),
                            c_phone.strip() or old.get("연락처", ""),
                            c_memo.strip() or old.get("메모", ""),
                            current_name,
                        )
                    load_materials.clear()
                    st.session_state["mat_reset"] = rc + 1
                    st.session_state["mat_flash"] = f"'{mat_target}' 자료가 등록되었습니다."
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"등록 중 오류: {e}")

    material_register_fragment()

with tab_weekly:
    cur_code, cur_label = get_week_info()

    # ── 주차별 활동 대시보드 ──
    st.subheader("📋 주차별 활동 대시보드")
    try:
        wdf = load_weekly_reports()
    except Exception as e:
        st.error(f"활동 보고 로드 오류: {e}")
        wdf = pd.DataFrame()

    if wdf.empty or "주차표시" not in wdf.columns or "주차코드" not in wdf.columns:
        st.info("아직 작성된 활동 보고가 없습니다.")
    else:
        # ---- 주차 선택 영역: [◀ 이전] [연] [월] [주차] [다음 ▶] ----
        entries = build_week_options(wdf, cur_code, cur_label)
        by_code = {e["code"]: e for e in entries}
        st.session_state["_dash_entries"] = entries

        # 선택 상태 초기화 (기본값: 이번 주)
        if st.session_state.get("dash_code") not in by_code:
            st.session_state["dash_code"] = cur_code if cur_code in by_code else entries[0]["code"]

        sel = by_code[st.session_state["dash_code"]]

        # 위젯 값을 단일 기준(dash_code)에 맞춰 동기화 (위젯 생성 전에 설정)
        st.session_state["dash_year"] = sel["y"]
        st.session_state["dash_month"] = sel["m"]
        st.session_state["dash_week"] = sel["code"]

        years = sorted({e["y"] for e in entries}, reverse=True)
        months = sorted({e["m"] for e in entries if e["y"] == sel["y"]}, reverse=True)
        week_codes = [e["code"] for e in entries if e["y"] == sel["y"] and e["m"] == sel["m"]]

        codes_all = [e["code"] for e in entries]
        pos = codes_all.index(sel["code"])

        def _week_fmt(code):
            e = by_code[code]
            text = short_week_label(e["label"])
            if code == cur_code:
                text += "  · 이번 주"
            return text

        multi_year = len(years) > 1   # 연도가 2개 이상일 때만 연도 선택 표시
        if multi_year:
            c_prev, c_year, c_month, c_week, c_next = st.columns([1.3, 1.1, 1, 3, 1.3])
        else:
            c_prev, c_month, c_week, c_next = st.columns([1.3, 1, 3, 1.3])
            c_year = None

        with c_prev:
            st.write("")
            st.button("◀ 이전 주", key="dash_prev", use_container_width=True,
                      disabled=(pos >= len(codes_all) - 1),
                      on_click=_dash_step, args=(1,))
        if c_year is not None:
            with c_year:
                st.selectbox("연도", options=years, key="dash_year",
                             format_func=lambda y: f"{y}년", on_change=_dash_on_year)
        with c_month:
            st.selectbox("월", options=months, key="dash_month",
                         format_func=lambda m: f"{m}월", on_change=_dash_on_month)
        with c_week:
            st.selectbox("주차", options=week_codes, key="dash_week",
                         format_func=_week_fmt, on_change=_dash_on_week)
        with c_next:
            st.write("")
            st.button("다음 주 ▶", key="dash_next", use_container_width=True,
                      disabled=(pos <= 0),
                      on_click=_dash_step, args=(-1,))

        selected_code = st.session_state["dash_code"]
        st.caption(f"📅 {by_code[selected_code]['label']}")

        week_df = wdf[wdf["주차코드"].astype(str).str.strip() == selected_code]
        if week_df.empty:
            st.caption("이 주차에는 아직 작성된 보고가 없습니다.")

        members = load_all_member_names()
        if not members:
            members = week_df["이름"].dropna().astype(str).unique().tolist()
        members = [m for m in members if m not in DASHBOARD_EXCLUDE]

        cols = st.columns(2)
        for i, member in enumerate(members):
            with cols[i % 2]:
                mrow = week_df[week_df["이름"].astype(str).str.strip() == member]
                visits = mrow[mrow["방문휴게소"].astype(str).str.strip() != ""]
                items = mrow[
                    (mrow["방문휴게소"].astype(str).str.strip() == "")
                    & (mrow["추진물건"].astype(str).str.strip() != "")
                ]

                with st.container(border=True):
                    st.markdown(f"**👤 {member}**")
                    if visits.empty and items.empty:
                        st.caption("미작성")
                    else:
                        if not items.empty:
                            for _, row in items.iterrows():
                                it = str(row.get("추진물건", "")).strip()
                                row_uid = str(row.get("사번", "")).strip()
                                row_code = str(row.get("주차코드", "")).strip()
                                row_time = str(row.get("작성시각", "")).strip()
                                st.markdown(f"📦 **추진물건**: {it}")
                                can_delete = (row_uid == current_uid) or is_admin
                                if can_delete:
                                    _render_delete_button(row_code, row_uid, row_time, current_uid, is_admin)
                            st.markdown("---")

                        for _, row in visits.iterrows():
                            v = str(row.get("방문휴게소", "")).strip()
                            a = str(row.get("활동내용", "")).strip()
                            n = str(row.get("특이사항", "")).strip()
                            row_uid = str(row.get("사번", "")).strip()
                            row_code = str(row.get("주차코드", "")).strip()
                            row_time = str(row.get("작성시각", "")).strip()

                            st.markdown(f"🏠 **방문**: {v}")
                            if a: st.markdown(f"📝 **활동**: {a}")
                            if n: st.markdown(f"❗ **특이사항**: {n}")

                            can_delete = (row_uid == current_uid) or is_admin
                            if can_delete:
                                _render_delete_button(row_code, row_uid, row_time, current_uid, is_admin)
                            st.markdown("---")

    st.divider()

    # ── 활동 작성 (기본 접힘) ──
    with st.expander("✏️ 이번 주 활동 작성하기"):
        st.caption(f"작성 주차: {cur_label}")

        st.markdown("**📍 휴게소 방문 보고** (방문한 휴게소마다 각각 제출해 주세요)")
        with st.form("visit_form", clear_on_submit=True):
            visit_target = st.selectbox("방문 휴게소", options=[""] + valid_names)
            activity = st.text_area("활동 내용", placeholder="이번 방문에서 수행한 활동을 적어주세요.")
            note = st.text_area("특이 사항", placeholder="공유할 이슈나 참고사항이 있으면 적어주세요.")
            visit_sent = st.form_submit_button("방문 보고 제출")
        if visit_sent:
            if not visit_target:
                st.warning("방문 휴게소를 선택해 주세요.")
            elif not activity.strip():
                st.warning("활동 내용을 입력해 주세요.")
            else:
                try:
                    add_weekly_report(
                        cur_code, cur_label, current_uid, current_name,
                        visit_target, activity.strip(), "", note.strip()
                    )
                    load_weekly_reports.clear()
                    st.success(f"'{visit_target}' 방문 보고가 제출되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"제출 중 오류: {e}")

        st.markdown("---")

        st.markdown("**✅ 운영사 미팅 및 추진 물건** (이번 주 전체 기준으로 1회만 작성)")
        with st.form("item_form", clear_on_submit=True):
            item = st.text_input("내용 작성", placeholder="예: OO휴게소 브랜드 교체 협의")
            item_sent = st.form_submit_button("내용 등록")
        if item_sent:
            if not item.strip():
                st.warning("내용을 입력해 주세요.")
            else:
                try:
                    add_weekly_report(
                        cur_code, cur_label, current_uid, current_name,
                        "", "", item.strip(), ""
                    )
                    load_weekly_reports.clear()
                    st.success("등록되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"등록 중 오류: {e}")

# --- 담당자 작성 현황 (관리자 전용) ---
if tab_admin_stats is not None:
    with tab_admin_stats:
        st.subheader("📈 담당자별 주간 활동 보고 작성 현황")
        st.caption("각 칸: '방문 보고 건수 · 방문+추진물건 전체 건수'. 방문 보고가 0건이면 미작성으로 표시됩니다.")

        try:
            stat_wdf = load_weekly_reports()
        except Exception as e:
            st.error(f"활동 보고 로드 오류: {e}")
            stat_wdf = pd.DataFrame()

        stat_members = load_all_member_names()
        if not stat_members:
            stat_members = []
        stat_members = [m for m in stat_members if m not in DASHBOARD_EXCLUDE]

        if stat_wdf.empty or not stat_members:
            st.info("표시할 데이터가 없습니다.")
        else:
            result_df, recent_labels, older_labels = build_admin_stats(stat_wdf, stat_members)

            if result_df.empty:
                st.info("표시할 데이터가 없습니다.")
            else:
                display_cols = ["담당자"] + [c for c in recent_labels if c in result_df.columns]
                st.markdown("**🗓️ 최근 " + str(RECENT_WEEKS_SHOWN) + "주**")
                st.dataframe(result_df[display_cols], use_container_width=True, hide_index=True)

                if older_labels:
                    with st.expander(f"📂 이전 주차 더보기 ({len(older_labels)}개 주차)"):
                        older_cols = ["담당자"] + [c for c in older_labels if c in result_df.columns]
                        st.dataframe(result_df[older_cols], use_container_width=True, hide_index=True)

# --- 상세 데이터 및 필터 (관리자 전용) ---
if tab_data is not None:
    with tab_data:
        if "브랜드" in df.columns:
            brands = ["전체"] + sorted(df["브랜드"].dropna().unique().tolist())
            selected = st.selectbox("브랜드 필터", brands)
            view_df = df if selected == "전체" else df[df["브랜드"] == selected]
        else:
            view_df = df
        st.dataframe(view_df, use_container_width=True)
