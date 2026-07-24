import streamlit as st
import pandas as pd
import gspread
import json
import streamlit.components.v1 as components
from datetime import datetime, timezone, timedelta
# --- 페이지 기본 설정 ---
st.set_page_config(page_title="휴게소 데이터 허브", layout="wide")
# 한국 표준시(KST, UTC+9) 시간대 정의
KST = timezone(timedelta(hours=9))
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
# 로그인 시트 키 (사용자 명단 / 접속_로그가 들어있는 문서)
LOGIN_SHEET_KEY = "1zD20Om2mHH7M9ppcialvqB1SxA9IyVq2aCLM9IWoo8c"
# --- 로그인 시트 로더 (구글 시트 연동) ---
@st.cache_data(ttl=300)
def load_login_data():
    """
    로그인 전용 구글 시트를 읽어 DataFrame으로 반환합니다.
    로그인 정보는 "사용자 명단" 탭에 있으므로 그 탭을 정확히 지정해서 엽니다.
    """
    gc = init_connection()
    doc = gc.open_by_key(LOGIN_SHEET_KEY)
    worksheet = doc.worksheet("사용자 명단")
    return pd.DataFrame(worksheet.get_all_records())
def write_login_log(user_name):
    """
    로그인 성공 시 "접속_로그" 시트에 접속시간과 이름을 한 줄 추가합니다.
    접속시간은 서버 시간이 아니라 한국 표준시(KST, UTC+9) 기준으로 기록합니다.
    (캐시를 쓰지 않고 매번 새 연결로 기록해야 실제 시트에 반영됩니다.)
    """
    gc = init_connection()
    doc = gc.open_by_key(LOGIN_SHEET_KEY)
    worksheet = doc.worksheet("접속_로그")
    # 한국 표준시(UTC+9) 기준 현재 시각 문자열
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    # [접속시간, 이름, 상태] 순서로 한 줄 추가
    worksheet.append_row([now_str, user_name, "로그인 성공"])
def normalize_value(value):
    """
    시트에서 읽어온 값(문자열/정수/실수 등 어떤 타입이든)을
    비교 가능한 순수 문자열로 안전하게 통일합니다.
    - 숫자로 자동 변환되어 860422.0 처럼 .0이 붙는 경우를 제거합니다.
    - 앞뒤 공백, 보이지 않는 특수 공백 문자도 제거합니다.
    - 사번처럼 '00501580'이 시트에서 숫자로 읽혀 앞자리 0이 사라지는 문제를
      해결하기 위해, 숫자로만 이루어진 값은 앞자리 0을 떼고(정수화) 비교합니다.
    """
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
    """
    데이터프레임의 컬럼명 중, 공백/대소문자를 무시했을 때
    target_name과 일치하는 실제 컬럼명을 찾아 반환합니다.
    """
    normalized_target = target_name.replace(" ", "").strip()
    for col in df.columns:
        if str(col).replace(" ", "").strip() == normalized_target:
            return col
    return None
# --- 2. 로그인 시스템 (사번 + 생년월일 6자리 / 구글 시트 대조) ---
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
        matched_name = ""  # 로그 기록에 사용할 이름
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
            # 접속_로그 시트에 접속시간 + 이름 기록 (실패해도 로그인은 진행)
            try:
                write_login_log(matched_name)
            except Exception:
                pass
            st.session_state["logged_in"] = True
            st.rerun()
        else:
            st.error("사번 또는 비밀번호가 올바르지 않습니다.")
    st.stop()
# --- 여기부터는 로그인 성공 시에만 실행 ---
st.markdown('<div class="app-title">🛣️ 휴게소 데이터 허브 (Powered by 특수개발팀)</div>', unsafe_allow_html=True)
# 데이터 로드
try:
    df = load_data()
except Exception as e:
    st.error(f"데이터 로드 오류: {e}")
    st.stop()
# --- 3. 위도/경도 값 검증 함수 (기존 로직 그대로 유지) ---
def parse_coordinate(value):
    """
    위도/경도 값을 안전하게 float으로 변환합니다.
    변환에 실패하면(빈칸, None, 문자 오타 등) None을 반환합니다.
    호출부에서 None이면 해당 행을 건너뛰도록 처리합니다.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
# --- 4. 지도용 데이터 구성 (좌표 검증 포함, 기존 스킵 로직 유지) ---
points = []
skipped_rows = []  # 어떤 행이 왜 제외됐는지 기록 (화면 안내용)
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
            "contract": r.get("계약기간", ""),
            "contract_step": r.get("계약차수", ""),
            "manager": r.get("담당자", ""),
            "highway": r.get("고속도로명", ""),
        })
    except Exception as e:
        skipped_rows.append({
            "휴게소명": name,
            "위도_원본값": r.get("위도"),
            "경도_원본값": r.get("경도"),
            "사유": f"알 수 없는 오류: {e}",
        })
        continue
# GitHub Pages 주소와, postMessage 수신 검증에 쓸 출처(origin)
PAGE_URL = "https://k958677wpal-oss.github.io/rest-area-map/"
TARGET_ORIGIN = "https://k958677wpal-oss.github.io"
MAP_HEIGHT = 560
# --- 5. 탭 분리 (지도 vs 데이터) ---
tab_map, tab_data = st.tabs(["🗺️ 스마트 노선 맵", "📊 상세 데이터 및 필터"])
with tab_map:
    valid_names = [p["name"] for p in points]
    name_options = [""] + valid_names
    query = st.selectbox(
        "🔍 휴게소 검색",
        options=name_options,
        index=0,
        help="휴게소명을 타이핑하면 자동완성 목록에서 정확한 휴게소를 선택할 수 있습니다.",
    )
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
            {{
              type: 'INIT_MAP_DATA',
              points: mapPoints,
              query: searchQuery
            }},
            '{TARGET_ORIGIN}'
          );
        }}
        iframe.onload = function () {{
          sendData();
        }};
      }})();
    </script>
    """
    components.html(component_html, height=MAP_HEIGHT, scrolling=False)
    if skipped_rows:
        with st.expander(f"⚠️ 좌표 오류로 지도에 표시되지 않은 휴게소 {len(skipped_rows)}건 (클릭하여 확인)"):
            st.dataframe(pd.DataFrame(skipped_rows), use_container_width=True)
            st.caption("구글 시트에서 해당 휴게소의 '위도'/'경도' 값을 채워주시면 다음 새로고침 시 지도에 반영됩니다.")
with tab_data:
    if "브랜드" in df.columns:
        brands = ["전체"] + sorted(df["브랜드"].dropna().unique().tolist())
        selected = st.selectbox("브랜드 필터", brands)
        view_df = df if selected == "전체" else df[df["브랜드"] == selected]
    else:
        view_df = df
    st.dataframe(view_df, use_container_width=True)
