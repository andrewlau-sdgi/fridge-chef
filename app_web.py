import base64
from io import BytesIO
import os
import re
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from html import escape

import streamlit as st
from google import genai
from PIL import Image, ImageOps


candidate_models = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]


def send_feedback_email(rating, comment, contact_email, language):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    admin_email = os.environ["ADMIN_EMAIL"]
    sender_email = os.environ.get("SMTP_FROM_EMAIL", smtp_username)

    message = EmailMessage()
    message["Subject"] = f"LAUS SDGI Fridge Chef User Feedback ({rating}/5)"
    message["From"] = sender_email
    message["To"] = admin_email
    if contact_email:
        message["Reply-To"] = contact_email
    message.set_content(
        f"Rating: {rating}/5\n"
        f"Selected language: {language}\n"
        f"Contact email: {contact_email or 'Not provided'}\n\n"
        f"Feedback:\n{comment}\n"
    )

    if smtp_port == 465:
        with smtplib.SMTP_SSL(
            smtp_host, smtp_port, timeout=20, context=ssl.create_default_context()
        ) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_username, smtp_password)
            server.send_message(message)


def encode_history_image(image):
    image = ImageOps.exif_transpose(image.copy())
    image.thumbnail((768, 768))
    if image.mode != "RGB":
        image = image.convert("RGB")

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=75, optimize=True)
    encoded_image = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded_image}"


_RECIPE_AUDIO_CONTROLS = st.components.v2.component(
    "recipe_audio_controls",
    html="""
    <div style="margin: 5px 0 15px 0; display: flex; align-items: center; gap: 10px; min-height: 40px;">
        <button id="play" type="button" style="background-color:#FF4B4B; color:white; border:none; padding:8px 16px; border-radius:5px; font-size:14px; cursor:pointer; font-weight:bold;"></button>
        <button id="stop" type="button" style="background-color:#6c757d; color:white; border:none; padding:8px 16px; border-radius:5px; font-size:14px; cursor:pointer; font-weight:bold;"></button>
    </div>
    """,
    js="""
    export default function ({ data, parentElement }) {
        const playButton = parentElement.querySelector("#play");
        const stopButton = parentElement.querySelector("#stop");

        if (!playButton || !stopButton) return;

        playButton.textContent = data.playLabel;
        stopButton.textContent = data.stopLabel;

        playButton.onclick = () => {
            if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
                window.alert("This browser does not support speech playback.");
                return;
            }

            const normalizeLanguage = (language) => language.toLowerCase().replace(/_/g, "-");
            const voices = window.speechSynthesis.getVoices();
            const voice = data.voiceLanguages
                .map((language) => voices.find((candidate) => normalizeLanguage(candidate.lang) === normalizeLanguage(language)))
                .find(Boolean);

            if (!voice) {
                window.alert(data.voiceMissing);
                return;
            }

            window.speechSynthesis.cancel();
            const utterance = new window.SpeechSynthesisUtterance(data.text);
            utterance.voice = voice;
            utterance.lang = voice.lang;
            utterance.rate = 1.0;
            window.speechSynthesis.speak(utterance);
        };

        stopButton.onclick = () => window.speechSynthesis?.cancel();
    }
    """,
)

_RECIPE_HISTORY = st.components.v2.component(
    "recipe_history",
    html='<div id="recipe-history-root"></div>',
    css="""
    #recipe-history-root {
        color: var(--st-text-color);
        font-family: inherit;
        font-size: 0.875rem;
    }
    .history-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.5rem;
        margin-bottom: 0.5rem;
    }
    .history-title {
        font-weight: 600;
    }
    .history-clear,
    .history-delete {
        color: var(--st-text-color);
        background: transparent;
        border: 1px solid var(--st-border-color);
        border-radius: 0.4rem;
        cursor: pointer;
        padding: 0.2rem 0.45rem;
    }
    .history-item {
        display: flex;
        align-items: flex-start;
        gap: 0.3rem;
        border-top: 1px solid var(--st-border-color);
        padding: 0.45rem 0;
    }
    .history-item details {
        flex: 1;
        min-width: 0;
    }
    .history-item summary {
        cursor: pointer;
        overflow-wrap: anywhere;
    }
    .history-meta,
    .history-empty,
    .history-error {
        color: var(--st-secondary-text-color, var(--st-text-color));
        font-size: 0.75rem;
        margin-top: 0.2rem;
        overflow-wrap: anywhere;
        opacity: 0.75;
    }
    .history-error {
        color: var(--st-danger-color, var(--st-primary-color));
        opacity: 1;
    }
    .history-recipe {
        font-family: inherit;
        font-size: 0.8rem;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        max-height: 18rem;
        overflow-y: auto;
        margin: 0.5rem 0 0;
    }
    .history-image {
        display: block;
        height: auto;
        max-width: 100%;
        max-height: 16rem;
        object-fit: contain;
        margin-top: 0.5rem;
        border-radius: 0.4rem;
    }
    """,
    js="""
    export default function ({ data, parentElement }) {
        const root = parentElement.querySelector("#recipe-history-root");
        if (!root) return;

        const storageKey = "laus-fridge-chef-recipe-history-v1";
        const expiryMs = 90 * 24 * 60 * 60 * 1000;

        const makeElement = (tag, className, text) => {
            const element = document.createElement(tag);
            if (className) element.className = className;
            if (text !== undefined) element.textContent = text;
            return element;
        };

        const showError = () => {
            root.replaceChildren();
            root.appendChild(makeElement("p", "history-error", data.storageError));
        };

        const readHistory = () => {
            const raw = window.localStorage.getItem(storageKey);
            const entries = raw === null ? [] : JSON.parse(raw);
            if (!Array.isArray(entries) || entries.some((entry) =>
                !entry
                || typeof entry.id !== "string"
                || !Number.isFinite(entry.createdAt)
                || typeof entry.recipe !== "string"
                || typeof entry.language !== "string"
                || typeof entry.model !== "string"
                || (entry.image !== undefined
                    && (typeof entry.image !== "string"
                        || !/^data:image\\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(entry.image)))
            )) {
                throw new Error("Invalid recipe history");
            }
            return entries;
        };

        const saveHistory = (entries) => {
            window.localStorage.setItem(storageKey, JSON.stringify(entries));
        };

        const render = (entries) => {
            root.replaceChildren();
            const header = makeElement("div", "history-header");
            header.appendChild(makeElement("span", "history-title", data.title));
            if (entries.length > 0) {
                const clearButton = makeElement("button", "history-clear", data.clear);
                clearButton.type = "button";
                clearButton.onclick = () => {
                    if (!window.confirm(data.confirmClear)) return;
                    try {
                        saveHistory([]);
                        render([]);
                    } catch {
                        showError();
                    }
                };
                header.appendChild(clearButton);
            }
            root.appendChild(header);
            root.appendChild(makeElement("p", "history-meta", data.retention));

            if (entries.length === 0) {
                root.appendChild(makeElement("p", "history-empty", data.empty));
                return;
            }

            entries.forEach((entry) => {
                const row = makeElement("div", "history-item");
                const details = makeElement("details");
                const summary = makeElement("summary");
                const title = entry.recipe
                    .split(/\\r?\\n/)
                    .map((line) => line.trim().replace(/^#+\\s*/, ""))
                    .find(Boolean) || data.recipeFallback;
                summary.appendChild(makeElement("div", "", title.slice(0, 100)));
                const date = new Intl.DateTimeFormat(data.dateLocale, {
                    dateStyle: "medium",
                    timeStyle: "short",
                }).format(new Date(entry.createdAt));
                summary.appendChild(makeElement(
                    "div",
                    "history-meta",
                    `${date} · ${entry.language}${entry.model ? ` · ${data.modelLabel}: ${entry.model}` : ""}`,
                ));
                details.appendChild(summary);
                if (entry.image) {
                    const image = makeElement("img", "history-image");
                    image.src = entry.image;
                    image.alt = data.imageAlt;
                    details.appendChild(image);
                }
                details.appendChild(makeElement("pre", "history-recipe", entry.recipe));

                const deleteButton = makeElement("button", "history-delete", "🗑️");
                deleteButton.type = "button";
                deleteButton.setAttribute("aria-label", data.deleteLabel);
                deleteButton.title = data.deleteLabel;
                deleteButton.onclick = () => {
                    try {
                        const updatedEntries = entries.filter((item) => item.id !== entry.id);
                        saveHistory(updatedEntries);
                        render(updatedEntries);
                    } catch {
                        showError();
                    }
                };

                row.append(details, deleteButton);
                root.appendChild(row);
            });
        };

        try {
            let entries = readHistory();
            const cutoff = Date.now() - expiryMs;
            const unexpiredEntries = entries.filter((entry) => entry.createdAt >= cutoff);
            const wasExpired = unexpiredEntries.length !== entries.length;
            entries = unexpiredEntries;

            if (data.newRecipe && !entries.some((entry) => entry.id === data.newRecipe.id)) {
                entries.push(data.newRecipe);
            }
            entries.sort((left, right) => right.createdAt - left.createdAt);

            if (wasExpired || data.newRecipe) saveHistory(entries);
            render(entries);
        } catch {
            showError();
        }
    }
    """,
)

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
        "spinner": "LAUS 大廚正在智慧切換可用模型並調配食譜中...",
        "success_msg": "✨ LAUS 冰箱大廚的今晚特餐建議出爐囉！",
        "tts_play": "▶ 播放此道菜語音",
        "tts_stop": "⏹ 停止",
        "tts_voice_missing": "找不到所選語言的語音，請在系統語音設定中安裝中文（台灣）語音。",
        "feedback_title": "📝 菜單回饋",
        "feedback_rating": "請為這份菜單評分（1–5）",
        "feedback_rating_placeholder": "請選擇評分",
        "feedback_comment": "您的意見",
        "feedback_comment_placeholder": "分享您對菜單或食譜的想法",
        "feedback_contact": "聯絡信箱（選填）",
        "feedback_submit": "送出回饋",
        "feedback_success": "感謝您的回饋！",
        "feedback_error": "回饋寄送失敗，請稍後再試。",
        "feedback_config_missing": "郵件寄送功能尚未設定，請聯絡管理員。",
        "feedback_rating_required": "請先選擇評分。",
        "feedback_comment_required": "請填寫您的意見。",
        "feedback_email_invalid": "請輸入有效的聯絡信箱。",
        "btn_clear": "🔄 清除結果並重新上傳食材照片",
        "no_api": "⚠️ 偵測不到 `GEMINI_API_KEY` 環境變數，請確認系統已正確設定。",
        "version_caption": "📌 App 版本：v1.0.4\n📌 目前語言：{language}\n📌 使用模型：{model}",
        "history_title": "📚 已生成食譜",
        "history_retention": "記錄保留 90 天，到期後自動刪除。",
        "history_empty": "尚無已儲存的食譜。",
        "history_clear": "清空",
        "history_delete": "刪除此食譜",
        "history_confirm_clear": "確定要清空所有已儲存的食譜嗎？",
        "history_storage_error": "無法讀取或儲存瀏覽器中的食譜記錄。",
        "history_model_label": "模型",
        "history_recipe_fallback": "食譜記錄",
        "history_date_locale": "zh-TW",
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
        "spinner": "Chef LAUS is auto-detecting available models...",
        "success_msg": "✨ Chef LAUS's special recipes are ready!",
        "tts_play": "▶ Play Audio",
        "tts_stop": "⏹ Stop",
        "tts_voice_missing": "No matching speech voice is installed. Install a voice for the selected language in your system settings.",
        "feedback_title": "📝 Menu feedback",
        "feedback_rating": "Rate this menu (1–5)",
        "feedback_rating_placeholder": "Choose a rating",
        "feedback_comment": "Your feedback",
        "feedback_comment_placeholder": "Share your thoughts about the menu or recipes",
        "feedback_contact": "Contact email (optional)",
        "feedback_submit": "Send feedback",
        "feedback_success": "Thank you for your feedback!",
        "feedback_error": "Could not send feedback. Please try again later.",
        "feedback_config_missing": "Email delivery is not configured. Please contact the administrator.",
        "feedback_rating_required": "Please choose a rating.",
        "feedback_comment_required": "Please enter your feedback.",
        "feedback_email_invalid": "Please enter a valid contact email.",
        "btn_clear": "🔄 Clear and Upload New Photo",
        "no_api": "⚠️ `GEMINI_API_KEY` environment variable not detected.",
        "version_caption": "📌 App Version: v1.0.4\n📌 Selected language: {language}\n📌 Model used: {model}",
        "history_title": "📚 Generated recipes",
        "history_retention": "Saved for 90 days, then automatically deleted.",
        "history_empty": "No saved recipes yet.",
        "history_clear": "Clear all",
        "history_delete": "Delete this recipe",
        "history_confirm_clear": "Clear all saved recipes?",
        "history_storage_error": "Could not read or save recipe history in this browser.",
        "history_model_label": "Model",
        "history_recipe_fallback": "Saved recipe",
        "history_date_locale": "en-US",
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
        "spinner": "利用可能なモデルを自動検出してレシピを作成中...",
        "success_msg": "✨ LAUSシェフ特製のレシピが完成しました！",
        "tts_play": "▶ 音声を再生",
        "tts_stop": "⏹ 停止",
        "tts_voice_missing": "選択した言語の音声がありません。システム設定で対応する音声をインストールしてください。",
        "feedback_title": "📝 メニューへのご意見",
        "feedback_rating": "メニューを評価してください（1～5）",
        "feedback_rating_placeholder": "評価を選択してください",
        "feedback_comment": "ご意見",
        "feedback_comment_placeholder": "メニューやレシピについてお聞かせください",
        "feedback_contact": "連絡先メールアドレス（任意）",
        "feedback_submit": "フィードバックを送信",
        "feedback_success": "ご意見ありがとうございます！",
        "feedback_error": "送信できませんでした。時間をおいて再度お試しください。",
        "feedback_config_missing": "メール送信が設定されていません。管理者にお問い合わせください。",
        "feedback_rating_required": "評価を選択してください。",
        "feedback_comment_required": "ご意見を入力してください。",
        "feedback_email_invalid": "有効なメールアドレスを入力してください。",
        "btn_clear": "🔄 クリアして写真を再アップロード",
        "no_api": "⚠️ `GEMINI_API_KEY` 環境変数が検出されません。",
        "version_caption": "📌 アプリバージョン：v1.0.4\n📌 選択中の言語：{language}\n📌 使用モデル：{model}",
        "history_title": "📚 生成したレシピ",
        "history_retention": "記録は90日間保存され、その後自動的に削除されます。",
        "history_empty": "保存されたレシピはありません。",
        "history_clear": "すべて削除",
        "history_delete": "このレシピを削除",
        "history_confirm_clear": "保存したレシピをすべて削除しますか？",
        "history_storage_error": "このブラウザーのレシピ履歴を読み書きできませんでした。",
        "history_model_label": "モデル",
        "history_recipe_fallback": "保存したレシピ",
        "history_date_locale": "ja-JP",
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
        "spinner": "사용 가능한 모델을 자동 감지하여 레시피를 구성 중입니다...",
        "success_msg": "✨ LAUS 셰프의 특선 레시피가 준비되었습니다!",
        "tts_play": "▶ 음성 재생",
        "tts_stop": "⏹ 정지",
        "tts_voice_missing": "선택한 언어의 음성이 없습니다. 시스템 설정에서 해당 음성을 설치해 주세요.",
        "feedback_title": "📝 메뉴 피드백",
        "feedback_rating": "이 메뉴를 평가해 주세요 (1~5)",
        "feedback_rating_placeholder": "평가를 선택하세요",
        "feedback_comment": "의견",
        "feedback_comment_placeholder": "메뉴나 레시피에 대한 의견을 남겨 주세요",
        "feedback_contact": "연락처 이메일 (선택 사항)",
        "feedback_submit": "피드백 보내기",
        "feedback_success": "피드백을 보내 주셔서 감사합니다!",
        "feedback_error": "피드백을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.",
        "feedback_config_missing": "이메일 전송이 설정되지 않았습니다. 관리자에게 문의해 주세요.",
        "feedback_rating_required": "평가를 선택해 주세요.",
        "feedback_comment_required": "의견을 입력해 주세요.",
        "feedback_email_invalid": "올바른 이메일 주소를 입력해 주세요.",
        "btn_clear": "🔄 초기화 및 다시 업로드",
        "no_api": "⚠️ `GEMINI_API_KEY` 환경 변수를 찾을 수 없습니다.",
        "version_caption": "📌 앱 버전: v1.0.4\n📌 선택한 언어: {language}\n📌 사용 모델: {model}",
        "history_title": "📚 생성된 레시피",
        "history_retention": "기록은 90일 동안 저장되며 이후 자동으로 삭제됩니다.",
        "history_empty": "저장된 레시피가 없습니다.",
        "history_clear": "모두 지우기",
        "history_delete": "이 레시피 삭제",
        "history_confirm_clear": "저장된 레시피를 모두 지울까요?",
        "history_storage_error": "이 브라우저에서 레시피 기록을 읽거나 저장할 수 없습니다.",
        "history_model_label": "모델",
        "history_recipe_fallback": "저장된 레시피",
        "history_date_locale": "ko-KR",
    }
}

# --- 側邊欄佈局：偏好設定在最上，版本號推到最底 ---
st.session_state.setdefault("current_model", "")
st.session_state.setdefault("pending_history_recipe", None)

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

    def render_version_caption():
        version_caption = escape(
            t["version_caption"].format(
                language=selected_lang_label,
                model=st.session_state.current_model,
            )
        )
        version_footer.markdown(
            """
            <div class="sidebar-version-footer">
                <hr style="margin: 10px 0;">
                <p style="color: #6c757d; font-size: 0.85rem; margin: 0; white-space: pre-line;">{version_caption}</p>
            </div>
            """.format(version_caption=version_caption),
            unsafe_allow_html=True,
        )

    cuisine_options = {
        "不拘 / 綜合家常 (Any / Fusion Home Style)": "General home-style cooking",
        "🇯🇵 日式居酒屋 / 和風 (Japanese Izakaya)": "Japanese style (Izakaya, light and delicate)",
        "🇰🇷 韓式風味 (Korean Style)": "Korean style (bold, spicy, fermented notes)",
        "🇹🇼/🇨🇳 中式熱炒 / 經典家常 (Chinese Stir-fry & Home-style)": "Chinese home-style & stir-fry",
        "🇮🇹/🇪🇸 地中海 / 西式輕食 (Mediterranean / Western)": "Mediterranean / Western light meal"
    }
    selected_cuisine_label = st.sidebar.selectbox(t["cuisine_label"], list(cuisine_options.keys()))
    target_cuisine = cuisine_options[selected_cuisine_label]

    _RECIPE_HISTORY(
        key="recipe-history",
        data={
            "newRecipe": st.session_state.pending_history_recipe,
            "title": t["history_title"],
            "retention": t["history_retention"],
            "empty": t["history_empty"],
            "clear": t["history_clear"],
            "deleteLabel": t["history_delete"],
            "confirmClear": t["history_confirm_clear"],
            "storageError": t["history_storage_error"],
            "modelLabel": t["history_model_label"],
            "recipeFallback": t["history_recipe_fallback"],
            "dateLocale": t["history_date_locale"],
            "imageAlt": t["image_caption"],
        },
    )
    st.session_state.pending_history_recipe = None

    # 2. 版本號（推到側邊欄最底部）
    version_footer = st.empty()
    render_version_caption()


# --- 主畫面介面 ---
st.title(t["title"])
st.write(t["subtitle"])

# 初始化 Session State
if "recipe_result" not in st.session_state:
    st.session_state.recipe_result = None
if "stored_image" not in st.session_state:
    st.session_state.stored_image = None
st.session_state.setdefault("feedback_sent", False)

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
            st.session_state.current_model = ""
            render_version_caption()
            with st.spinner(t["spinner"]):
                success = False
                last_error = None
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
                
                # 自動走訪備援清單，尋找當下可用的模型
                for model_name in candidate_models:
                    try:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=[st.session_state.stored_image, prompt]
                        )
                        if response and response.text:
                            st.session_state.recipe_result = response.text
                            st.session_state.current_model = model_name
                            st.session_state.pending_history_recipe = {
                                "id": uuid.uuid4().hex,
                                "createdAt": int(time.time() * 1000),
                                "recipe": response.text,
                                "language": selected_lang_label,
                                "model": model_name,
                                "image": encode_history_image(st.session_state.stored_image),
                            }
                            render_version_caption()
                            st.session_state.feedback_sent = False
                            success = True
                            break
                    except Exception as e:
                        last_error = e
                        time.sleep(1)
                        continue
                
                if not success:
                    st.error(f"所有可用模型目前皆繁忙或發生錯誤，請稍後再試。\n詳細錯誤：{last_error}")
                else:
                    st.rerun()

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
            
            speech_settings = {
                "Traditional Chinese": ("zh-TW", ("zh-TW", "zh-CN", "zh")),
                "English": ("en-US", ("en-US", "en-GB", "en-AU", "en-CA")),
                "Japanese": ("ja-JP", ("ja-JP", "ja")),
                "Korean": ("ko-KR", ("ko-KR", "ko")),
            }
            lang_code, voice_languages = speech_settings[target_lang]
            
            _RECIPE_AUDIO_CONTROLS(
                data={
                    "text": plain_text,
                    "lang": lang_code,
                    "voiceLanguages": voice_languages,
                    "voiceMissing": t["tts_voice_missing"],
                    "playLabel": t["tts_play"],
                    "stopLabel": t["tts_stop"],
                },
                key=f"recipe-audio-{dish_count}",
            )
            
            st.markdown("---")

        if dish_count == 0:
            st.markdown(full_text)

        if dish_count > 0:
            st.markdown("---")
            st.subheader(t["feedback_title"])

            if st.session_state.feedback_sent:
                st.success(t["feedback_success"])
            else:
                with st.form("recipe_feedback_form"):
                    rating = st.selectbox(
                        t["feedback_rating"],
                        options=[1, 2, 3, 4, 5],
                        index=None,
                        placeholder=t["feedback_rating_placeholder"],
                        key="recipe_feedback_rating",
                    )
                    comment = st.text_area(
                        t["feedback_comment"],
                        placeholder=t["feedback_comment_placeholder"],
                        max_chars=2000,
                        key="recipe_feedback_comment",
                    )
                    contact_email = st.text_input(
                        t["feedback_contact"],
                        key="recipe_feedback_contact",
                    )
                    feedback_submitted = st.form_submit_button(
                        t["feedback_submit"],
                        type="primary",
                    )

                if feedback_submitted:
                    comment = comment.strip()
                    contact_email = contact_email.strip()
                    if rating is None:
                        st.error(t["feedback_rating_required"])
                    elif not comment:
                        st.error(t["feedback_comment_required"])
                    elif contact_email and not re.fullmatch(
                        r"[^@\s]+@[^@\s]+\.[^@\s]+", contact_email
                    ):
                        st.error(t["feedback_email_invalid"])
                    elif not all(
                        os.environ.get(name)
                        for name in (
                            "SMTP_HOST",
                            "SMTP_PORT",
                            "SMTP_USERNAME",
                            "SMTP_PASSWORD",
                            "ADMIN_EMAIL",
                        )
                    ):
                        st.error(t["feedback_config_missing"])
                    else:
                        try:
                            send_feedback_email(
                                rating,
                                comment,
                                contact_email,
                                target_lang,
                            )
                            st.session_state.feedback_sent = True
                            st.success(t["feedback_success"])
                        except Exception:
                            st.error(t["feedback_error"])
            
        # 清除重置按鈕
        if st.button(t["btn_clear"]):
            st.session_state.recipe_result = None
            st.session_state.current_model = ""
            st.session_state.stored_image = None
            st.session_state.feedback_sent = False
            st.rerun()