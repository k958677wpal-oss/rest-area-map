import streamlit as st
import pandas as pd
import gspread
import json
from datetime import datetime, timedelta

# --- 페이지 기본 설정 (가장 먼저 와야 함) ---
st.set_page_config(page_title="전국 휴게소 통합 관리", layout="wide")

# --- 1. 구글 시트 연결 (비밀 금고 키 사용) ---
@st.cache_resource
def init_connection():
    # 이제 key.json 파일 대신 스트림릿 비밀 금고에서 키를 꺼내옵니다.
    key_dict = json.loads(st.secrets["google_secret"])
    return gspread.service_account_from_dict(key_dict)

try:
    gc = init_connection()
    # 어제 에러 로그에 있던 시트 이름 적용
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
            
            # [추가 기능] 로그인 성공 시 구글 시트에 접속 로그 기록하기
            try:
                log_sheet = doc.worksheet("접속_로그")
            except gspread.exceptions.WorksheetNotFound:
                # '접속_로그' 탭이 없으면 알아서 새로 만듭니다.
                log_sheet = doc.add_worksheet(title="접속_로그", rows="1000", cols="3")
                log_sheet.append_row(["접속시간", "사번", "상태"])
            
            # 한국 시간(KST)으로 기록
            now_kst = datetime.utcnow() + timedelta(hours=9)
            now_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
            
            # 시트에 기록 추가
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

    # [추가 기능] 모바일 최적화를 위한 탭(Tab) 분리
    tab1, tab2 = st.tabs(["🗺️ 지도 보기 (검색)", "📊 상세 데이터 및 필터"])
    
    with tab1:
        st.subheader("📍 휴게소 지도 검색")
        
        # [추가 기능] 휴게소 검색 기능 (데이터에 '휴게소명' 컬럼이 있다고 가정)
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
            
        # 지도 출력 (데이터에 '위도', '경도' 컬럼이 있다고 가정)
        if '위도' in map_df.columns and '경도' in map_df.columns:
            st.map(map_df, latitude='위도', longitude='경도')
        else:
            st.info("💡 엑셀(구글 시트)에 '위도'와 '경도' 컬럼이 있어야 지도가 표시됩니다.")
            
    with tab2:
        st.subheader("📋 휴게소 통합 데이터")
        # 이곳에 기존에 쓰시던 필터 기능 등을 자유롭게 추가하시면 됩니다.
        st.dataframe(df, use_container_width=True)