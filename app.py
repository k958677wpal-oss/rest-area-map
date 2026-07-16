import streamlit as st
import pandas as pd
import gspread
import json
import streamlit.components.v1 as components
import base64
import urllib.parse

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="전국 휴게소 통합 관리", layout="wide")

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
    /* 모바일에서 한 줄로 유지 */
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

# --- 2. 로그인 시스템 (사번 + 생년월일 6자리) ---
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

if not st.session_state["logged_in"]:
    st.title("🔐 관리자 로그인")
    with st.form("login_form"):
        user_id = st.text_input("사번 (ID)")
        user_pw = st.text_input("생년월일 6자리 (PW)", type="password", max_chars=6)
        submitted = st.form_submit_button("로그인")
    if submitted:
        if user_id == "admin" and user_pw == "123456":
            st.session_state["logged_in"] = True
            st.rerun()
        else:
            st.error("사번 또는 비밀번호가 올바르지 않습니다.")
    st.stop()

# --- 여기부터는 로그인 성공 시에만 실행 ---
st.markdown('<div class="app-title">🛣️ 전국 휴게소 통합 관리</div>', unsafe_allow_html=True)

# 데이터 로드
try:
    df = load_data()
except Exception as e:
    st.error(f"데이터 로드 오류: {e}")
    st.stop()

# --- 3. 지도용 데이터 인코딩 ---
points = []
for _, r in df.iterrows():
    try:
        points.append({
            "name": r["휴게소명"],
            "lat": float(r["위도"]),
            "lng": float(r["경도"]),
            "brand": r.get("브랜드", ""),
            "operator": r.get("운영사", ""),
            "revenue": r.get("연매출액", ""),
            "contract": r.get("계약기간", ""),
            "manager": r.get("담당자", ""),
            "highway": r.get("고속도로명", ""),   # 노선 그룹핑용
            # "seq": int(r.get("노선순서", 0)),   # 정확한 노선 순서가 있으면 주석 해제
        })
    except (ValueError, KeyError):
        continue  # 좌표가 비었거나 형식이 틀린 행은 스킵

encoded = base64.b64encode(
    urllib.parse.quote(json.dumps(points, ensure_ascii=False)).encode()
).decode()

PAGE_URL = "https://k958677wpal-oss.github.io/rest-area-map/"

# --- 4. 탭 분리 (지도 vs 데이터) ---
tab_map, tab_data = st.tabs(["🗺️ 카카오맵 지도 보기", "📊 상세 데이터 및 필터"])

with tab_map:
    # 검색창: 입력값을 iframe URL 파라미터(q)로 전달
    query = st.text_input("🔍 휴게소 검색", placeholder="휴게소명을 입력하세요")

    map_url = f"{PAGE_URL}?data={encoded}"
    if query:
        map_url += "&q=" + urllib.parse.quote(query)

    components.iframe(src=map_url, height=560, scrolling=False)

with tab_data:
    if "브랜드" in df.columns:
        brands = ["전체"] + sorted(df["브랜드"].dropna().unique().tolist())
        selected = st.selectbox("브랜드 필터", brands)
        view_df = df if selected == "전체" else df[df["브랜드"] == selected]
    else:
        view_df = df

    st.dataframe(view_df, use_container_width=True)
