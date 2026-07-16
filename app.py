import streamlit as st
import pandas as pd
import gspread
import json
import streamlit.components.v1 as components

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
tab_map, tab_data = st.tabs(["🗺️ 카카오맵 지도 보기", "📊 상세 데이터 및 필터"])

with tab_map:
    query = st.text_input("🔍 휴게소 검색", placeholder="휴게소명을 입력하세요")

    # [핵심 변경] URL 파라미터(base64 인코딩) 방식을 완전히 제거했습니다.
    # 대신 points 데이터를 이 컴포넌트 HTML 안에 JS 변수로 직접 심어두고,
    # iframe이 로드되면 postMessage로 자식(GitHub Pages) 창에 통째로 전달합니다.
    # 이 방식은 URL 길이 제한(브라우저마다 대략 2000~8000자 수준)의 영향을 받지 않으므로
    # 데이터가 수백~수천 건으로 늘어나도 'I/O error'가 발생하지 않습니다.
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
        // Streamlit 쪽에서 준비한 실제 데이터 (URL이 아니라 JS 변수로 직접 전달)
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
            '{TARGET_ORIGIN}'   // 데이터를 받을 자식(GitHub Pages) 출처를 명시해 보안 확보
          );
        }}

        // iframe이 로드되면 즉시 데이터를 전달합니다.
        iframe.onload = function () {{
          sendData();
        }};
      }})();
    </script>
    """

    components.html(component_html, height=MAP_HEIGHT, scrolling=False)

    # 좌표 문제로 지도에서 제외된 행이 있으면 안내 (기존 로직 유지)
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
