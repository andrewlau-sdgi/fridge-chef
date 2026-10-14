import os
import re
import time
import streamlit as st
from google import genai
from PIL import Image

# 頁面基本設定
st.set_page_config(page_title="LAUS 冰箱剩食食譜大廚", page_icon="🍳", layout="centered")

# --- 1. 多國語言介面字典 (UI Dictionary) ---
ui_texts = {
    "Traditional Chinese": {
        "title": "🍳 LAUS 冰箱剩食食譜大廚",
        "subtitle": "拍下你的冰箱或現有食材照片，在側邊欄選擇語言與風格後，點擊按鈕讓 LAUS 大廚為你變出創意料理！",
        "sidebar_header": "⚙️ 料理與語言偏好設定",
        "lang_label": "🌍 選擇輸出語言 (Output Language)",
        "cuisine_label": "🍲 選擇喜愛的菜式風格 (Cuisine Style)",
        "upload_instruction": "📷 請透過下方按鈕上傳或拍攝冰箱食材照片：",
        "image_caption": "你上傳的冰箱食材",
        "btn_generate": "🤖 請 LAUS 大廚開立今晚食譜",
        "spinner": "LAUS 大廚正在為你調配食譜中（伺服器若繁忙將自動重試）...",
        "success_msg": "✨ LAUS 冰箱大廚的今晚特餐建議出爐囉！",
        "tts_play": "▶ 播放此道菜語音",
        "tts_stop": "⏹ 停止",
        "btn_clear": "🔄 清除結果並重新上傳食材照片",
        "no_api": "⚠️ 偵測不到 `GEMINI_API_KEY` 環境變數，請確認系統已正確設定。"
    },
    "English": {
        "title": "🍳 LAUS Refrigerator Leftover Chef",
        "subtitle": "Snap a photo of your fridge, choose your preferences in the sidebar, and let Chef LAUS create creative recipes for you!",
        "sidebar_header": "⚙️ Preferences & Language",
        "lang_label": "🌍 Output Language",
        "cuisine_label": "🍲 Cuisine Style",
        "upload_instruction": "📷 Please upload or take a photo of your fridge ingredients below:",
        "image_caption": "Your uploaded fridge ingredients",
        "btn_generate": "🤖 Ask Chef LAUS for Tonight's Recipe",
        "spinner": "Chef LAUS is crafting your recipes (auto-retrying if busy)...",
        "success_msg": "✨ Chef LAUS's special recipes are ready!",
        "tts_play": "▶ Play Audio",
        "tts_stop": "⏹ Stop",
        "btn_clear": "🔄 Clear and Upload New Photo",
        "no_api": "⚠️ `GEMINI_API_KEY` environment variable not detected."
    },
    "Japanese": {
        "title": "🍳 LAUS 冷蔵庫の余り物シェフ",
        "subtitle": "冷蔵庫の写真を撮って、サイドバーで言語やスタイルを選び、シェフLAUSにレシピを作ってもらいましょう！",
        "sidebar_header": "⚙️ 設定と言語",
        "lang_label": "🌍 出力言語 (Output Language)",
        "cuisine_label": "🍲 料理のスタイル",
        "upload_instruction": "📷 下記から冷蔵庫の食材写真をアップロードまたは撮影してください：",
        "image_caption": "アップロードした食材",
        "btn_generate": "🤖 LAUSシェフに今夜のレシピを頼む",
        "spinner": "LAUSシェフがレシピを考えています...",
        "success_msg": "✨ LAUSシェフ特製のレシピが完成しました！",
        "tts_play": "▶ 音声を再生",
        "tts_stop": "⏹ 停止",
        "btn_clear": "🔄 クリアして写真を再アップロード",
        "no_api": "⚠️ `GEMINI_API_KEY` 環境変数が検出されません。"
    },
    "Korean": {
        "title": "🍳 LAUS 냉장고 파먹기 셰프",
        "subtitle": "냉장고 사진을 찍고 사이드바에서 언어와 스타일을 선택한 뒤 LAUS 셰프에게 레시피를 부탁해보세요!",
        "sidebar_header": "⚙️ 설정 및 언어",
        "lang_label": "🌍 출력 언어 (Output Language)",
        "cuisine_label": "🍲 요리 스타일",
        "upload_instruction": "📷 아래에서 냉장고 식재료 사진을 업로드하거나 촬영하세요:",
        "image_caption": "업로드한 식재료",
        "btn_generate": "🤖 LAUS 셰프에게 오늘 저녁 레시피 요청하기",
        "spinner": "LAUS 셰프가 레시피를 구상 중입니다...",
        "success_msg": "✨ LAUS 셰프의 특선 레시피가 준비되었습니다!",
        "tts_play": "▶ 음성 재생",
        "tts_stop": "⏹ 정지",
        "btn_clear": "🔄 초기화 및 다시 업로드",
        "no_api": "⚠️ `GEMINI_API_KEY` 환경 변수를 찾을 수 없습니다."
    }
}

# --- 側邊欄佈局：偏好設定在最上，版本號推到最底 ---
st.sidebar.markdown("""
    <style>
        section[data-testid="stSidebar"] > div:first-child {
            display: flex;
            flex-direction: column;
            height: 100vh;
        }
        .sidebar-version-footer {
            margin-top: auto;
            padding-bottom: 2rem;
        }
    </style>
""", unsafe_allow_html=True)

with st.sidebar:
    # 1. 料理與語言偏好設定（固定在最上方）
    st.header("⚙️ 料理與語言偏好設定")

    lang_options = {
        "繁體中文": "Traditional Chinese",
        "English": "English",
        "日本語": "Japanese",
        "한국어": "Korean"
    }
    selected_lang_label = st.selectbox("🌍 選擇輸出語言 (Output Language)", list(lang_options.keys()))
    target_lang = lang_options[selected_lang_label]

    t = ui_texts[target_lang]

    cuisine_options = {
        "不拘 / 綜合家常 (Any / Fusion Home Style)": "General home-style cooking",
        "🇯🇵 日式居酒屋 / 和風 (Japanese Izakaya)": "Japanese style (Izakaya, light and delicate)",
        "🇰🇷 韓式風味 (Korean Style)": "Korean style (bold, spicy, fermented notes)",
        "🇹🇼/🇨🇳 中式熱炒 / 經典家常 (Chinese Stir-fry & Home-style)": "Chinese home-style & stir-fry",
        "🇮🇹/🇪🇸 地中海 / 西式輕食 (Mediterranean / Western)": "Mediterranean / Western light meal"
    }
    selected_cuisine_label = st.sidebar.selectbox(t["cuisine_label"], list(cuisine_options.keys()))
    target_cuisine = cuisine_options[selected_cuisine_label]

    # 2. 版本號（推到側邊欄最底部）
    st.markdown("""
        <div class="sidebar-version-footer">
            <hr style="margin: 10px 0;">
            <p style="color: #6c757d; font-size: 0.85rem; margin: 0;">📌 App Version: v1.0.2</p>
        </div>
    """, unsafe_allow_html=True)


# --- 主畫面介面 ---
st.title(t["title"])
st.write(t["subtitle"])

# 初始化 Session State
if "recipe_result" not in st.session_state:
    st.session_state.recipe_result = None
if "stored_image" not in st.session_state:
    st.session_state.stored_image = None

# 檢查環境變數
if not os.environ.get("GEMINI_API_KEY"):
    st.error(t["no_api"])
else:
    st.markdown(f"**{t['upload_instruction']}**")
    
    uploaded_file = st.file_uploader(
        label="", 
        type=["jpg", "jpeg", "png", "heic"]
    )

    if uploaded_file is not None:
        image = Image.open(uploaded_file)
        st.session_state.stored_image = image

    if st.session_state.stored_image is not None:
        st.image(st.session_state.stored_image, caption=t["image_caption"], use_container_width=True)
        
        # 產生食譜按鈕
        if st.button(t["btn_generate"], type="primary"):
            with st.spinner(t["spinner"]):
                max_retries = 3
                retry_delay = 2
                
                for attempt in range(max_retries):
                    try:
                        client = genai.Client()
                        
                        prompt = f"""
                        You are a professional, humorous, and resourceful refrigerator leftover cooking chef named LAUS.
                        
                        Please execute the following tasks based on the attached refrigerator/ingredient photo:
                        1. **Ingredient Inventory**: Carefully observe the photo and list all primary ingredients and seasonings you can identify.
                        2. **Recipe Planning**: Using these available ingredients, design **3 quick and delicious home-cooked dishes** matching the requested cuisine style: '{target_cuisine}'.
                        
                        FORMAT REQUIREMENT:
                        Start the response with the Ingredient Inventory section.
                        Then, format EACH of the 3 dishes clearly starting with `### Dish 1:`, `### Dish 2:`, `### Dish 3:` (or localized equivalent like `### 菜色一：`, `### 菜色二：`, `### 菜色三：`).
                        
                        Each dish must include:
                        - Dish Name
                        - Estimated Cooking Time
                        - Ingredients Used
                        - Step-by-Step Instructions
                        
                        CRITICAL CONSTRAINT: You MUST write your ENTIRE response in **{target_lang}**.
                        """
                        
                        # 使用最新且高效能的 gemini-3.8-flash 模型
                        response = client.models.generate_content(
                            model='gemini-3.8-flash',
                            contents=[st.session_state.stored_image, prompt]
                        )
                        
                        st.session_state.recipe_result = response.text
                        break
                        
                    except Exception as e:
                        if "503" in str(e) or "UNAVAILABLE" in str(e):
                            if attempt < max_retries - 1:
                                time.sleep(retry_delay)
                                retry_delay *= 2
                                continue
                        st.error(f"呼叫 API 時發生錯誤：{e}")
                        break

    # 如果有食譜結果，進行精準分段與語音按鈕渲染
    if st.session_state.recipe_result is not None:
        st.success(t["success_msg"])
        st.markdown("---")
        
        full_text = st.session_state.recipe_result
        
        # 分割菜色區段
        dish_pattern = r'(?m)(?=^#{1,3}\s*(?:Dish|菜色|料理|Recipe)\s*[1-3一二三一-三]|^[1-3][\.\、]\s*【)'
        sections = re.split(dish_pattern, full_text)
        
        # 1. 顯示食材清單與導言
        if len(sections) > 0 and sections[0].strip():
            st.markdown(sections[0].strip())
            st.markdown("---")
            
        # 2. 逐一顯示每道菜與其專屬的朗讀按鈕
        dish_count = 0
        for section in sections[1:]:
            clean_section = section.strip()
            if not clean_section:
                continue
            
            dish_count += 1
            
            # 顯示該道菜完整 Markdown
            st.markdown(clean_section)
            
            # 清理純文字
            plain_text = (
                clean_section.replace('\\', '')
                .replace('`', '')
                .replace('"', ' ')
                .replace("'", " ")
                .replace('*', '')
                .replace('#', '')
                .replace('\n', ' ')
                .replace('\r', '')
            )
            
            # 中文強制指定為廣東話 zh-HK
            lang_code = "zh-HK" if target_lang == "Traditional Chinese" else ("ja-JP" if target_lang == "Japanese" else ("ko-KR" if target_lang == "Korean" else "en-US"))
            
            st.components.v1.html(f"""
                <div style="margin: 5px 0 15px 0; display: flex; align-items: center;">
                    <button onclick="
                        if ('speechSynthesis' in window) {{
                            window.speechSynthesis.cancel();
                            var utterance = new SpeechSynthesisUtterance("{plain_text}");
                            utterance.lang = '{lang_code}';
                            utterance.rate = 1.0;
                            window.speechSynthesis.speak(utterance);
                        }} else {{
                            alert('瀏覽器不支援語音功能');
                        }}
                    " style="background-color:#FF4B4B; color:white; border:none; padding:8px 16px; border-radius:5px; font-size:14px; cursor:pointer; font-weight:bold;">
                        {t["tts_play"]}
                    </button>
                    <button onclick="
                        if ('speechSynthesis' in window) {{
                            window.speechSynthesis.cancel();
                        }}
                    " style="background-color:#6c757d; color:white; border:none; padding:8px 16px; border-radius:5px; font-size:14px; cursor:pointer; margin-left:10px; font-weight:bold;">
                        {t["tts_stop"] if "tts_stop" in t else "⏹ 停止"}
                    </button>
                </div>
            """, height=45)
            
            st.markdown("---")

        if dish_count == 0:
            st.markdown(full_text)
            
        # 清除重置按鈕
        if st.button(t["btn_clear"]):
            st.session_state.recipe_result = None
            st.session_state.stored_image = None
            st.rerun()