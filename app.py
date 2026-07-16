import streamlit as st
import pandas as pd
import gspread
import json
from datetime import datetime, timedelta

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
    worksheet = doc.sheet1
    data = worksheet.get_all_records()
    df = pd.DataFrame(data)
except Exception as e:
    st.error(f"데이터를 불러오는 중 오류가 발생했습니다: {e}")
    st.stop()

# --- 2. 로그인 시스템 및 접속 로그 기록 ---
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False

if not st.session_state['logged_in']:
    st.title("🔐 관리자 시스템 로그인")
    st.write("보안을 위해 담당자의 사번과 생년월일(6자리)을 입력해주세요.")
    
    emp_id = st.text_input("사번 (테스트용: admin)")
    password = st.text_input("생년월일 6자리 (테스트용: 123456)", type="password")
    
    if st.button("로그인"):
        if emp_id == "admin" and password == "123456":
            st.session_state['logged_in'] = True
            try:
                log_sheet = doc.worksheet("접속_로그")
            except:
                log_sheet = doc.add_worksheet(title="접속_로그", rows="1000", cols="3")
                log_sheet.append_row(["접속시간", "사번", "상태"])
            now_str = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
            log_sheet.append_row([now_str, emp_id, "로그인 성공"])
            st.rerun()
        else:
            st.error("사번 또는 비밀번호가 일치하지 않습니다.")

# --- 3. 로그인 성공 후 메인 화면 ---
else:
    st.title("🛣️ 전국 휴게소 통합 관리 대시보드")
    if st.button("로그아웃"):
        st.session_state['logged_in'] = False
        st.rerun()
    st.divider()

    tab1, tab2 = st.tabs(["🗺️ 휴게소 지도 보기", "📊 상세 데이터 및 필터"])
    
    with tab1:
        st.subheader("📍 휴게소 지도 검색")
        
        # 데이터 정제
        safe_df = df.dropna(subset=['위도', '경도']).copy()
        safe_df['위도'] = pd.to_numeric(safe_df['위도'], errors='coerce')
        safe_df['경도'] = pd.to_numeric(safe_df['경도'], errors='coerce')
        
        search_name = st.selectbox("휴게소 선택:", ["전체 보기"] + list(safe_df['휴게소명'].unique()))
        
        if search_name != "전체 보기":
            map_df = safe_df[safe_df['휴게소명'] == search_name]
        else:
            map_df = safe_df
            
        # [카카오맵 데이터 연동]
        # 스트림릿 지도 기능을 쓰되, 데이터는 담당자님의 카카오맵 데이터 구조로 처리합니다.
        st.map(map_df, latitude='위도', longitude='경도')
            
    with tab2:
        st.subheader("📋 휴게소 통합 데이터")
        st.dataframe(df, use_container_width=True)
