import streamlit as st
import gspread
import pandas as pd
import streamlit.components.v1 as components

# 1. 웹사이트 기본 설정 (항상 맨 위에 있어야 해)
st.set_page_config(page_title="전국 휴게소 통합 관리", layout="wide")

# --- 🔐 로그인 상태 기억하기 ---
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False

# ==========================================
# 🛑 로그인 안 했을 때 보여줄 화면 (로그인 창)
# ==========================================
if not st.session_state['logged_in']:
    st.title("🔐 관리자 시스템 로그인")
    st.write("보안을 위해 담당자의 사번과 생년월일(6자리)을 입력해주세요.")
    
    # 로그인 입력 박스 만들기
    with st.form("login_form"):
        emp_id = st.text_input("사번 (테스트용: admin)")
        birth_date = st.text_input("생년월일 6자리 (테스트용: 123456)", type="password")
        submit_button = st.form_submit_button("로그인")
        
        if submit_button:
            # ⭐️ 여기서 실제 사번과 생년월일을 검사해! (나중에 팀원 정보로 바꿀 수 있음)
            if emp_id == 'admin' and birth_date == '123456':
                st.session_state['logged_in'] = True
                st.rerun() # 로그인 성공하면 화면을 새로고침해서 지도를 띄움
            else:
                st.error("사번 또는 생년월일이 일치하지 않습니다.")

# ==========================================
# 🟢 로그인 성공했을 때 보여줄 화면 (대시보드)
# ==========================================
else:
    # 우측 상단에 로그아웃 버튼 만들기
    col_title, col_logout = st.columns([9, 1])
    with col_title:
        st.title("🛣️ 전국 휴게소 통합 관리 대시보드")
    with col_logout:
        if st.button("로그아웃"):
            st.session_state['logged_in'] = False
            st.rerun()
            
    # 2. 구글 시트 데이터 불러오기
    gc = gspread.service_account(filename='key.json')
    sh = gc.open("휴게소_통합_데이터")
    worksheet = sh.sheet1
    data = worksheet.get_all_records()
    df = pd.DataFrame(data)

    # 3. 화면 나누기
    col1, col2 = st.columns([2, 1])

    # --- 오른쪽 칸: 데이터 표 ---
    with col2:
        st.subheader("📋 실시간 데이터")
        st.dataframe(df)

    # --- 왼쪽 칸: 카카오 지도 ---
    with col1:
        st.subheader("🗺️ 휴게소 위치 현황")
        
        # ★ 여기에 담당자님의 카카오 자바스크립트 키를 다시 넣어주세요! ★
        KAKAO_APP_KEY = "fbd1e96068b754f1939c7e11a2790ce1"

        logo_urls = {
            "GS25": "https://t1.daumcdn.net/localimg/localimages/07/mapapidoc/markerStar.png", 
            "CU": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/CU_BI.svg/200px-CU_BI.svg.png",
            "세븐일레븐": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/40/7-eleven_logo.svg/200px-7-eleven_logo.svg.png",
            "이마트24": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/05/Emart24_BI.png/200px-Emart24_BI.png",
            "기본": "https://t1.daumcdn.net/localimg/localimages/07/mapapidoc/marker_red.png" 
        }

        markers_js = ""
        for index, row in df.iterrows():
            if pd.notna(row.get('위도')) and pd.notna(row.get('경도')):
                title = str(row.get('휴게소명', '휴게소'))
                lat = row.get('위도')
                lng = row.get('경도')
                
                brand = str(row.get('브랜드', '')).replace(" ", "").upper()
                operator = str(row.get('운영사', ''))
                sales = str(row.get('연매출액', ''))
                contract = str(row.get('계약기간', ''))
                manager = str(row.get('담당자', ''))
                
                brand_logo = logo_urls["기본"]
                if 'GS25' in brand: brand_logo = logo_urls['GS25']
                elif 'CU' in brand: brand_logo = logo_urls['CU']
                elif '세븐일레븐' in brand or '7ELEVEN' in brand: brand_logo = logo_urls['세븐일레븐']
                elif '이마트' in brand or 'EMART' in brand: brand_logo = logo_urls['이마트24']

                info_html = f"""
                <div style='padding:10px; width:220px; font-size:13px; line-height:1.5;'>
                    <strong style='font-size:15px; color:#333;'>{title}</strong><br>
                    <hr style='margin:5px 0;'>
                    <b>운영사:</b> {operator}<br>
                    <b>브랜드:</b> {row.get('브랜드', '')}<br>
                    <b>연매출액:</b> {sales}<br>
                    <b>계약기간:</b> {contract}<br>
                    <b>담당자:</b> {manager}
                </div>
                """.replace('\n', '')

                markers_js += f"{{ title: '{title}', latlng: new kakao.maps.LatLng({lat}, {lng}), logo: '{brand_logo}', info: \"{info_html}\" }},"

        map_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <script type="text/javascript" src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_APP_KEY}"></script>
        </head>
        <body>
            <div id="map" style="width:100%;height:500px;border-radius:10px;"></div>
            <script>
                var mapContainer = document.getElementById('map'),
                    mapOption = {{
                        center: new kakao.maps.LatLng(36.3, 127.5),
                        level: 13
                    }};
                var map = new kakao.maps.Map(mapContainer, mapOption);

                var positions = [
                    {markers_js}
                ];

                for (var i = 0; i < positions.length; i ++) {{
                    var imageSize = new kakao.maps.Size(24, 35);
                    var markerImage = new kakao.maps.MarkerImage(positions[i].logo, imageSize); 

                    var marker = new kakao.maps.Marker({{
                        map: map,
                        position: positions[i].latlng,
                        title : positions[i].title,
                        image : markerImage
                    }});

                    var infowindow = new kakao.maps.InfoWindow({{
                        content: positions[i].info
                    }});

                    (function(marker, infowindow) {{
                        kakao.maps.event.addListener(marker, 'click', function() {{
                            infowindow.open(map, marker);
                        }});
                    }})(marker, infowindow);
                }}
            </script>
        </body>
        </html>
        """
        
        components.html(map_html, height=520)