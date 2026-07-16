import streamlit as st
import pandas as pd
import gspread
import json
from datetime import datetime, timedelta
import streamlit.components.v1 as components 
import os
import time

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

# --- 3. 메인 대시보드 화면 (담당자님 카카오맵 코드 적용!) ---
else:
    st.title("🛣️ 전국 휴게소 통합 관리 대시보드")
    
    if st.button("로그아웃"):
        st.session_state['logged_in'] = False
        st.rerun()
        
    st.divider()

    tab1, tab2 = st.tabs(["🗺️ 카카오맵 지도 보기", "📊 상세 데이터 및 필터"])
    
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
            # 1. 정식 HTML 파일을 저장할 폴더 생성
            os.makedirs("static", exist_ok=True)
            
            # 2. 담당자님이 작성하셨던 마커(핀) 데이터 생성 로직 그대로 적용
            markers_js = ""
            for index, row in map_df.iterrows():
                name = row.get('휴게소명', '이름없음')
                lat = row.get('위도', 0)
                lng = row.get('경도', 0)
                brand = row.get('브랜드', '')
                manager = row.get('담당자', '')
                progress = row.get('진척률', '')
                
                try:
                    lat, lng = float(lat), float(lng)
                    if pd.isna(lat) or pd.isna(lng): continue
                except:
                    continue
                
                markers_js += f"""
                {{
                    title: '{name}', 
                    brand: '{brand}',
                    manager: '{manager}',
                    progress: '{progress}',
                    latlng: new kakao.maps.LatLng({lat}, {lng})
                }},
                """
            
            center_lat = float(map_df['위도'].mean()) if not pd.isna(map_df['위도'].mean()) else 36.5
            center_lon = float(map_df['경도'].mean()) if not pd.isna(map_df['경도'].mean()) else 127.5
            zoom_level = 4 if search_name != "전체 보기" else 13
            
            try:
                kakao_key = st.secrets["kakao_key"]
            except KeyError:
                st.error("⚠️ 카카오 키를 찾을 수 없습니다.")
                st.stop()

            # 3. 담당자님의 완벽한 HTML 구조 + 정보창(말풍선) 코드 반영
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <style>
                    html, body {{width:100%; height:100%; margin:0; padding:0;}} 
                    #map {{width:100%; height:450px; border-radius:10px;}}
                </style>
            </head>
            <body>
            <div id="map"></div>
            <script type="text/javascript" src="//dapi.kakao.com/v2/maps/sdk.js?appkey={kakao_key}"></script>
            <script>
            var mapContainer = document.getElementById('map');
            var mapOption = {{
                center: new kakao.maps.LatLng({center_lat}, {center_lon}),
                level: {zoom_level}
            }};
            var map = new kakao.maps.Map(mapContainer, mapOption);
            
            var positions = [{markers_js}];
            
            for (var i = 0; i < positions.length; i ++) {{
                var marker = new kakao.maps.Marker({{
                    map: map,
                    position: positions[i].latlng,
                    title : positions[i].title,
                }});
                
                // 마커 마우스오버 정보창 (담당자님 코드 완벽 적용)
                var infowindow = new kakao.maps.InfoWindow({{
                    content: '<div style="padding:10px;font-size:12px;line-height:1.5;min-width:150px;">' + 
                             '<b>' + positions[i].title + '</b><br>' + 
                             '브랜드: ' + positions[i].brand + '<br>' + 
                             '담당자: ' + positions[i].manager + '<br>' + 
                             '진척률: ' + positions[i].progress + '</div>'
                }});
                
                kakao.maps.event.addListener(marker, 'mouseover', makeOverListener(map, marker, infowindow));
                kakao.maps.event.addListener(marker, 'mouseout', makeOutListener(infowindow));
            }}
            
            function makeOverListener(map, marker, infowindow) {{
                return function() {{ infowindow.open(map, marker); }};
            }}
            function makeOutListener(infowindow) {{
                return function() {{ infowindow.close(); }};
            }}
            </script>
            </body>
            </html>
            """
            
            # 4. 가짜 도화지가 아닌 '진짜 HTML 파일'을 스트림릿 서버에 저장
            with open("static/map.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            
            # 5. 저장된 진짜 HTML 파일을 정식 주소로 불러오기 (카카오 차단 완벽 우회!)
            # 브라우저가 새 데이터를 인식하도록 뒤에 시간(?t=...)을 붙입니다.
            components.iframe(f"/app/static/map.html?t={time.time()}", height=470)
            
        else:
            st.info("💡 엑셀에 '위도'와 '경도' 컬럼이 있어야 지도가 표시됩니다.")
            
    with tab2:
        st.subheader("📋 휴게소 통합 데이터")
        st.dataframe(df, use_container_width=True)
