import streamlit as st
import pandas as pd
import gspread
import json
import streamlit.components.v1 as components
from datetime import datetime, timezone, timedelta
import datetime as _dt

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="휴게소 데이터 허브", layout="wide")
KST = timezone(timedelta(hours=9))

# --- 대시보드에서 제외할 담당자 (임시) ---
DASHBOARD_EXCLUDE = ["임진현", "박영진"]

# --- 관리자 사번 목록 (앞자리 0 제외 형태로 입력) ---
ADMIN_IDS = ["507583"]

# --- 모바일 헤더 최적화 CSS ---
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
    s = s.replace("\u00a0", "").replace("\u200b", "")
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

# --- 2. 로그인 시스템 ---
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

if not st.session_state["logged_in"]:
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
# '방문휴게소'가 채워진 행만 방문 이력으로 취급 (추진물건 행은 자동 제외됨)
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

# --- 탭 구성: 관리자면 상세 데이터 탭 포함, 아니면 제외 ---
if is_admin:
    tab_map, tab_weekly, tab_data = st.tabs(
        ["🗺️ 스마트 노선 맵", "🗒️ 주간 활동 보고", "📊 상세 데이터 및 필터"]
    )
else:
    tab_map, tab_weekly = st.tabs(
        ["🗺️ 스마트 노선 맵", "🗒️ 주간 활동 보고"]
    )
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
        var iframe = document.getElementById('kakaoMapFrame');
        function sendData() {{
          iframe.contentWindow.postMessage(
            {{ type: 'INIT_MAP_DATA', points: mapPoints, query: searchQuery }},
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

with tab_weekly:
    cur_code, cur_label = get_week_info()

    # ── 주차별 활동 대시보드 ──
    st.subheader("📋 주차별 활동 대시보드")
    try:
        wdf = load_weekly_reports()
    except Exception as e:
        st.error(f"활동 보고 로드 오류: {e}")
        wdf = pd.DataFrame()

    if wdf.empty or "주차표시" not in wdf.columns:
        st.info("아직 작성된 활동 보고가 없습니다.")
    else:
        week_labels = wdf["주차표시"].dropna().astype(str).unique().tolist()
        week_labels = sorted(week_labels, reverse=True)
        selected_week = st.selectbox("주차 선택", options=week_labels, index=0)

        week_df = wdf[wdf["주차표시"].astype(str) == selected_week]

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
                        # 추진물건 먼저 표시
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

                        # 방문 이력
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

        # -- 1) 휴게소 방문 보고 (방문한 곳마다 반복 제출) --
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

        # -- 2) 추진물건 등록 (이번 주 1회) --
        st.markdown("**📦 추진 물건 등록** (이번 주 전체 기준으로 1회만 작성)")
        with st.form("item_form", clear_on_submit=True):
            item = st.text_input("추진 물건", placeholder="예: OO휴게소 브랜드 교체 협의")
            item_sent = st.form_submit_button("추진물건 등록")
        if item_sent:
            if not item.strip():
                st.warning("추진 물건 내용을 입력해 주세요.")
            else:
                try:
                    add_weekly_report(
                        cur_code, cur_label, current_uid, current_name,
                        "", "", item.strip(), ""
                    )
                    load_weekly_reports.clear()
                    st.success("추진물건이 등록되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"등록 중 오류: {e}")

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
