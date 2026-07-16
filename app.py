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

# --- 3. 로그인 성공 후 메인 대시보드 화면 ---
else:
    st.title("🛣️ 전국 휴게소 통합 관리 대시보드")
    
    if st.button("로그아웃"):
        st.session_state['logged_in'] = False
        st.rerun()
        
    st.divider()

    # 모바일 최적화를 위해 탭 분리
    tab1, tab2 = st.tabs(["🗺️ 휴게소 지도 보기", "📊 상세 데이터 및 필터"])
    
    with tab1:
        st.subheader("📍 휴게소 지도 검색")
        
        search_name = "전체 보기"
        if '휴게소명' in df.columns:
            search_name = st.selectbox(
                "검색할 휴게소를 선택하거나 이름을 직접 입력하세요:", 
                ["전체 보기"] + list(df['휴게소명'].unique())
            )
            
            if search_name != "전체 보기":
                map_df = df[df['휴게소명'] == search_name]
            else:
                map_df = df
        else:
            map_df = df
            
        if '위도' in map_df.columns and '경도' in map_df.columns:
            # 안전장치: 결측치 제거 및 숫자로 변환
            safe_df = map_df.dropna(subset=['위도', '경도']).copy()
            safe_df['위도'] = pd.to_numeric(safe_df['위도'], errors='coerce')
            safe_df['경도'] = pd.to_numeric(safe_df['경도'], errors='coerce')
            safe_df = safe_df.dropna(subset=['위도', '경도'])
            
            # [핵심] 대한민국 좌표만 남겨서 외국 지도가 나오는 것을 원천 차단
            safe_df = safe_df[(safe_df['위도'] > 32.0) & (safe_df['위도'] < 39.0) & 
                              (safe_df['경도'] > 124.0) & (safe_df['경도'] < 132.0)]
            
            if not safe_df.empty:
                # 어제 완벽하게 작동했던 바로 그 지도 명령어입니다!
                st.map(safe_df, latitude='위도', longitude='경도')
            else:
                st.warning("선택하신 휴게소의 정확한 좌표(한국 내 위치) 데이터가 엑셀에 없습니다.")
        else:
            st.info("💡 엑셀(구글 시트)에 '위도'와 '경도' 컬럼이 있어야 지도가 표시됩니다.")
            
    with tab2:
        st.subheader("📋 휴게소 통합 데이터")
        st.dataframe(df, use_container_width=True)
