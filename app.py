import streamlit as st
import pandas as pd
import gspread
import json
from datetime import datetime, timedelta
import streamlit.components.v1 as components 
import base64
import urllib.parse

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="전국 휴게소 통합 관리", layout="wide")

# --- 1. 구글 시트 연결 ---
@st.cache_resource
def init_connection():
    key_dict = json.loads(st.secrets["google_secret"])
    return gspread.service_account_from_dict(key_dict)

try:
    gc = init_connection()
    doc = gc.open("휴게소_통합_데이터") 
    df = pd.DataFrame(doc.sheet1.get_all_records())
except Exception as e:
    st.error(f"데이터 로드 오류: {e}")
    st.stop()

# --- 2. 로그인 시스템 (생략) ---
if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False

if not st.session_state['logged_in']:
    st.title("🔐 관리자 로그인")
    if st.text_input("사번", type="password") == "admin": 
        if st.button("로그인"): st.session_state['logged_in'] = True; st.rerun()
else:
    st.title("🛣️ 전국 휴게소 통합 관리")
    
    # 3. 데이터 인코딩 및 지도 로드
    points = []
    for _, r in df.iterrows():
        try:
            points.append({
                "name": r["휴게소명"], "lat": float(r["위도"]), "lng": float(r["경도"]),
                "brand": r.get("브랜드", ""), "manager": r.get("담당자", ""), "progress": r.get("진척률", "")
            })
        except: continue
    
    encoded = base64.b64encode(urllib.parse.quote(json.dumps(points, ensure_ascii=False)).encode()).decode()
    
    # [중요] 담당자님의 깃허브 페이지 주소 적용
    PAGE_URL = "https://k958677wpal-oss.github.io/rest-area-map/"
    map_url = f"{PAGE_URL}?data={encoded}"
    
    # 정식 도메인을 가진 iframe으로 삽입
    components.iframe(src=map_url, height=560, scrolling=False)
    
    st.divider()
    st.dataframe(df, use_container_width=True)
