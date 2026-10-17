import base64
from io import BytesIO
import logging
import os
import re
import smtplib
import ssl
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from html import escape

import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from PIL import Image, ImageOps


APP_VERSION = "1.0.7"
RECIPE_REQUEST_TIMEOUT_MS = 180_000
DISH_PREVIEW_TIMEOUT_SECONDS = 120

candidate_models = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

RECIPE_IMAGE_MODEL = "gemini-3.1-flash-lite-image"
RECIPE_INPUT_IMAGE_MAX_SIZE = (1536, 1536)
RECIPE_DISH_PATTERN = (
    r"(?m)(?=^#{1,3}\s*(?:Dish|菜色|料理|Recipe)\s*[1-3一二三一-三]|^[1-3][\.\、]\s*【)"
)


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


def prepare_recipe_input_image(image):
    image = ImageOps.exif_transpose(image.copy())
    image.thumbnail(RECIPE_INPUT_IMAGE_MAX_SIZE)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def format_error_detail(stage, error):
    error_message = str(error).strip() or "No additional error message was provided."
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        error_message = error_message.replace(api_key, "[REDACTED API KEY]")
    error_message = re.sub(
        r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED API KEY]", error_message
    )
    error_message = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        error_message,
    )
    return f"{stage}\n{type(error).__name__}: {error_message[:1200]}"


@contextmanager
def recipe_generation_guard():
    try:
        yield
    except Exception as error:
        logging.exception("Unexpected recipe generation failure")
        st.session_state.recipe_generation_error_message = t["recipe_error"]
        st.session_state.recipe_generation_error_detail = format_error_detail(
            "Unexpected recipe generation failure.", error
        )
    finally:
        if not st.session_state.recipe_previews_pending:
            st.session_state.recipe_generation_in_progress = False


def finalize_recipe_generation():
    generation_duration = round(
        time.perf_counter() - st.session_state.recipe_generation_started_at
    )
    st.session_state.recipe_generation_duration = generation_duration
    st.session_state.pending_history_recipe = {
        "id": uuid.uuid4().hex,
        "createdAt": int(time.time() * 1000),
        "recipe": st.session_state.recipe_result,
        "language": st.session_state.recipe_generation_language,
        "model": st.session_state.current_model,
        "image": st.session_state.recipe_image,
        "generationDurationSeconds": generation_duration,
    }


@st.fragment(run_every=1)
def render_dish_preview_progress():
    if not st.session_state.recipe_previews_pending:
        return

    preview_futures = st.session_state.recipe_dish_preview_futures
    completed_count = sum(future.done() for future in preview_futures)
    total_count = len(preview_futures)
    preview_status = st.status(t["preview_generating"], expanded=False)
    preview_status.update(
        label=t["preview_progress"].format(
            completed=completed_count,
            total=total_count,
        )
    )
    if completed_count < total_count:
        return

    previews = []
    for dish_number, future in enumerate(preview_futures, start=1):
        try:
            previews.append(future.result())
        except genai_errors.APIError as error:
            logging.warning(
                "Dish preview generation failed for dish %s (HTTP %s)",
                dish_number,
                error.code,
            )
            previews.append(None)
        except Exception:
            logging.exception(
                "Unexpected dish preview generation failure for dish %s",
                dish_number,
            )
            previews.append(None)

    st.session_state.recipe_dish_previews = previews
    st.session_state.recipe_dish_preview_futures = []
    st.session_state.recipe_preview_sections = []
    st.session_state.recipe_previews_pending = False
    finalize_recipe_generation()
    st.session_state.recipe_generation_in_progress = False
    preview_status.update(
        label=t["preview_progress"].format(
            completed=total_count,
            total=total_count,
        ),
        state="complete",
    )
    st.rerun()


def generate_dish_preview(client, recipe):
    interaction = client.interactions.create(
        model=RECIPE_IMAGE_MODEL,
        input=(
            "Create a photorealistic, appetizing reference photo of the finished dish "
            "described in this recipe. Show the dish plated naturally on a table, "
            "with no text, labels, logos, collage, or extra dishes. Treat the recipe "
            "as the source of truth for the ingredients and presentation.\n\n"
            f"Recipe:\n{recipe}"
        ),
        store=False,
        timeout=DISH_PREVIEW_TIMEOUT_SECONDS,
    )
    output_image = interaction.output_image
    if not output_image or not output_image.data:
        raise ValueError("Gemini image generation returned no image")

    image = Image.open(BytesIO(base64.b64decode(output_image.data, validate=True)))
    image = ImageOps.exif_transpose(image)
    image.thumbnail((768, 768))
    if image.mode != "RGB":
        image = image.convert("RGB")

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue()


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

_RECIPE_PDF_EXPORT = st.components.v2.component(
    "recipe_pdf_export",
    html='<button id="export-pdf" type="button"></button>',
    css="""
    #export-pdf {
        color: white;
        background: #ff4b4b;
        border: 0;
        border-radius: 0.4rem;
        cursor: pointer;
        font: inherit;
        font-weight: 600;
        padding: 0.5rem 1rem;
    }
    """,
    js="""
    export default function ({ data, parentElement }) {
        const button = parentElement.querySelector("#export-pdf");
        if (!button) return;

        button.textContent = data.buttonLabel;
        button.onclick = () => {
            const printWindow = window.open("", "_blank");
            if (!printWindow) {
                window.alert(data.popupBlocked);
                return;
            }

            const document = printWindow.document;
            document.open();
            document.write(`<!doctype html>
                <html lang="${data.language}">
                    <head>
                        <meta charset="utf-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1">
                        <title></title>
                        <style>
                            @page { size: A4; margin: 18mm; }
                            body {
                                color: #111;
                                font-family: Arial, "Noto Sans CJK TC", sans-serif;
                                line-height: 1.5;
                                margin: 0 auto;
                                max-width: 800px;
                            }
                            h1 { font-size: 1.5rem; }
                            img {
                                display: block;
                                height: auto;
                                max-height: 70vh;
                                max-width: 100%;
                                object-fit: contain;
                                margin: 1rem auto;
                            }
                            pre {
                                font-family: inherit;
                                font-size: 11pt;
                                white-space: pre-wrap;
                                overflow-wrap: anywhere;
                            }
                            @media print {
                                body { max-width: none; }
                                img { break-inside: avoid; }
                            }
                        </style>
                    </head>
                    <body></body>
                </html>`);
            document.close();
            document.documentElement.lang = data.language;
            document.title = data.title;

            const heading = document.createElement("h1");
            heading.textContent = data.title;
            document.body.appendChild(heading);

            let hasPrinted = false;
            const printDocument = () => {
                if (hasPrinted) return;
                hasPrinted = true;
                printWindow.focus();
                printWindow.print();
            };
            let image;
            if (data.image) {
                image = document.createElement("img");
                image.alt = data.imageAlt;
                document.body.appendChild(image);
            }

            const recipe = document.createElement("pre");
            recipe.textContent = data.recipe;
            document.body.appendChild(recipe);
            if (image) {
                image.onload = printDocument;
                image.onerror = printDocument;
                image.src = data.image;
                if (image.complete) printDocument();
            } else {
                printDocument();
            }
        };
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
                || (entry.generationDurationSeconds !== undefined
                    && (!Number.isFinite(entry.generationDurationSeconds)
                        || entry.generationDurationSeconds < 0))
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
                const duration = Number.isFinite(entry.generationDurationSeconds)
                    ? ` · ${data.durationLabel}: ${data.durationFormat
                        .replace("{minutes}", String(Math.floor(entry.generationDurationSeconds / 60)))
                        .replace("{seconds}", String(entry.generationDurationSeconds % 60))}`
                    : "";
                summary.appendChild(makeElement(
                    "div",
                    "history-meta",
                    `${date} · ${entry.language}${entry.model ? ` · ${data.modelLabel}: ${entry.model}` : ""}${duration}`,
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
st.markdown(
    """
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        [data-testid="stToolbarActions"] {display: none;}
    </style>
    """,
    unsafe_allow_html=True,
)

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
        "preview_caption": "AI 成果預覽（僅供參考）",
        "preview_option": "產生每道菜的 AI 預覽圖",
        "preview_option_help": "勾選後會為每道菜產生 AI 預覽圖，整體等待時間會較長。",
        "preview_generating": "正在自動產生三道菜的 AI 預覽圖...",
        "preview_progress": "已完成 {completed} / {total} 道菜的 AI 預覽圖...",
        "preview_error": "目前無法產生這道菜的 AI 預覽圖。",
        "generation_duration_footnote": "食譜生成耗時：{duration}",
        "duration_format": "{minutes} 分 {seconds} 秒",
        "btn_generate": "🤖 請 LAUS 大廚開立今晚食譜",
        "spinner": "LAUS 大廚正在偵測可用模型並調配食譜...",
        "success_msg": "✨ LAUS 冰箱大廚的今晚特餐建議出爐囉！",
        "model_list_error": "目前無法確認可用的食譜模型，請稍後再試。如果問題持續，請聯絡管理員。",
        "no_available_models": "目前沒有可用的食譜模型。請稍後再試，或聯絡管理員確認 API 模型存取權限。",
        "model_access_error": "目前無法連線至食譜服務。請稍後再試，或聯絡管理員確認 API 設定。",
        "model_quota_error": "食譜服務目前忙碌或已達使用上限。請稍候幾分鐘後再試。",
        "recipe_error": "暫時無法產生食譜。請稍後再試；您上傳的照片仍保留，可以直接重試。如問題持續，請聯絡管理員。",
        "error_details": "🔎 錯誤詳情（點擊展開）",
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
        "version_caption": "📌 App 版本：v{version}\n📌 目前語言：{language}\n📌 使用模型：{model}",
        "history_title": "📚 已生成食譜",
        "history_retention": "記錄保留 90 天，到期後自動刪除。",
        "history_empty": "尚無已儲存的食譜。",
        "history_clear": "清空",
        "history_delete": "刪除此食譜",
        "history_confirm_clear": "確定要清空所有已儲存的食譜嗎？",
        "history_storage_error": "無法讀取或儲存瀏覽器中的食譜記錄。",
        "history_model_label": "模型",
        "history_duration_label": "耗時",
        "history_recipe_fallback": "食譜記錄",
        "history_date_locale": "zh-TW",
        "export_pdf": "📄 匯出 PDF / 列印",
        "pdf_popup_blocked": "請允許此網站開啟彈出視窗，才能匯出 PDF。",
    },
    "English": {
        "title": "🍳 LAUS Refrigerator Leftover Chef",
        "subtitle": "Snap a photo of your fridge, choose your preferences in the sidebar, and let Chef LAUS create creative recipes for you!",
        "sidebar_header": "⚙️ Preferences & Language",
        "lang_label": "🌍 Output Language",
        "cuisine_label": "🍲 Cuisine Style",
        "upload_instruction": "📷 Please upload or take a photo of your fridge ingredients below:",
        "image_caption": "Your uploaded fridge ingredients",
        "preview_caption": "AI dish preview (for reference)",
        "preview_option": "Generate AI previews for each dish",
        "preview_option_help": "When checked, AI preview images are generated for each dish and the overall wait will be longer.",
        "preview_generating": "Automatically generating AI previews for the dishes...",
        "preview_progress": "Generated {completed} of {total} dish previews...",
        "preview_error": "An AI preview could not be generated for this dish.",
        "generation_duration_footnote": "Recipe generation time: {duration}",
        "duration_format": "{minutes}m {seconds}s",
        "btn_generate": "🤖 Ask Chef LAUS for Tonight's Recipe",
        "spinner": "Chef LAUS is detecting available models and preparing recipes...",
        "success_msg": "✨ Chef LAUS's special recipes are ready!",
        "model_list_error": "We couldn't check which recipe models are available. Please try again shortly. Contact the administrator if this continues.",
        "no_available_models": "No recipe models are currently available. Please try again later or ask the administrator to check API model access.",
        "model_access_error": "The recipe service couldn't be reached. Please try again later or ask the administrator to check the API configuration.",
        "model_quota_error": "The recipe service is busy or has reached its usage limit. Please wait a few minutes and try again.",
        "recipe_error": "We couldn't create your recipes just now. Please try again shortly; your uploaded photo is still available. Contact the administrator if this continues.",
        "error_details": "🔎 Error details (click to expand)",
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
        "version_caption": "📌 App Version: v{version}\n📌 Selected language: {language}\n📌 Model used: {model}",
        "history_title": "📚 Generated recipes",
        "history_retention": "Saved for 90 days, then automatically deleted.",
        "history_empty": "No saved recipes yet.",
        "history_clear": "Clear all",
        "history_delete": "Delete this recipe",
        "history_confirm_clear": "Clear all saved recipes?",
        "history_storage_error": "Could not read or save recipe history in this browser.",
        "history_model_label": "Model",
        "history_duration_label": "Time",
        "history_recipe_fallback": "Saved recipe",
        "history_date_locale": "en-US",
        "export_pdf": "📄 Export PDF / Print",
        "pdf_popup_blocked": "Allow pop-ups for this site to export the PDF.",
    },
    "Japanese": {
        "title": "🍳 LAUS 冷蔵庫の余り物シェフ",
        "subtitle": "冷蔵庫の写真を撮って、サイドバーで言語やスタイルを選び、シェフLAUSにレシピを作ってもらいましょう！",
        "sidebar_header": "⚙️ 設定と言語",
        "lang_label": "🌍 出力言語 (Output Language)",
        "cuisine_label": "🍲 料理のスタイル",
        "upload_instruction": "📷 下記から冷蔵庫の食材写真をアップロードまたは撮影してください：",
        "image_caption": "アップロードした食材",
        "preview_caption": "AI料理イメージ（参考）",
        "preview_option": "各料理のAIプレビューを生成",
        "preview_option_help": "チェックすると各料理のAIプレビュー画像を生成するため、全体の待ち時間が長くなります。",
        "preview_generating": "料理のAIプレビューを自動生成しています...",
        "preview_progress": "{total}品中{completed}品のAIプレビューを生成しました...",
        "preview_error": "この料理のAIプレビューを生成できませんでした。",
        "generation_duration_footnote": "レシピ生成時間：{duration}",
        "duration_format": "{minutes}分 {seconds}秒",
        "btn_generate": "🤖 LAUSシェフに今夜のレシピを頼む",
        "spinner": "利用可能なモデルを確認してレシピを作成しています...",
        "success_msg": "✨ LAUSシェフ特製のレシピが完成しました！",
        "model_list_error": "利用可能なレシピモデルを確認できませんでした。しばらくしてから再度お試しください。問題が続く場合は管理者にお問い合わせください。",
        "no_available_models": "現在利用できるレシピモデルがありません。後でもう一度お試しいただくか、管理者に API モデルへのアクセスを確認してください。",
        "model_access_error": "レシピサービスに接続できませんでした。後でもう一度お試しいただくか、管理者に API 設定を確認してください。",
        "model_quota_error": "レシピサービスが混み合っているか、利用上限に達しました。数分後にもう一度お試しください。",
        "recipe_error": "ただいまレシピを作成できませんでした。写真はそのまま残っていますので、しばらくしてから再度お試しください。問題が続く場合は管理者にお問い合わせください。",
        "error_details": "🔎 エラーの詳細（クリックして展開）",
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
        "version_caption": "📌 アプリバージョン：v{version}\n📌 選択中の言語：{language}\n📌 使用モデル：{model}",
        "history_title": "📚 生成したレシピ",
        "history_retention": "記録は90日間保存され、その後自動的に削除されます。",
        "history_empty": "保存されたレシピはありません。",
        "history_clear": "すべて削除",
        "history_delete": "このレシピを削除",
        "history_confirm_clear": "保存したレシピをすべて削除しますか？",
        "history_storage_error": "このブラウザーのレシピ履歴を読み書きできませんでした。",
        "history_model_label": "モデル",
        "history_duration_label": "所要時間",
        "history_recipe_fallback": "保存したレシピ",
        "history_date_locale": "ja-JP",
        "export_pdf": "📄 PDFをエクスポート / 印刷",
        "pdf_popup_blocked": "PDFをエクスポートするには、このサイトのポップアップを許可してください。",
    },
    "Korean": {
        "title": "🍳 LAUS 냉장고 파먹기 셰프",
        "subtitle": "냉장고 사진을 찍고 사이드바에서 언어와 스타일을 선택한 뒤 LAUS 셰프에게 레시피를 부탁해보세요!",
        "sidebar_header": "⚙️ 설정 및 언어",
        "lang_label": "🌍 출력 언어 (Output Language)",
        "cuisine_label": "🍲 요리 스타일",
        "upload_instruction": "📷 아래에서 냉장고 식재료 사진을 업로드하거나 촬영하세요:",
        "image_caption": "업로드한 식재료",
        "preview_caption": "AI 요리 미리보기 (참고용)",
        "preview_option": "각 요리의 AI 미리보기 생성",
        "preview_option_help": "선택하면 각 요리의 AI 미리보기 이미지를 생성하므로 전체 대기 시간이 길어집니다.",
        "preview_generating": "요리의 AI 미리보기를 자동 생성하고 있습니다...",
        "preview_progress": "{total}개 중 {completed}개 요리의 AI 미리보기를 생성했습니다...",
        "preview_error": "이 요리의 AI 미리보기 이미지를 만들 수 없습니다.",
        "generation_duration_footnote": "레시피 생성 시간: {duration}",
        "duration_format": "{minutes}분 {seconds}초",
        "btn_generate": "🤖 LAUS 셰프에게 오늘 저녁 레시피 요청하기",
        "spinner": "사용 가능한 모델을 확인하고 레시피를 준비하고 있습니다...",
        "success_msg": "✨ LAUS 셰프의 특선 레시피가 준비되었습니다!",
        "model_list_error": "사용 가능한 레시피 모델을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요. 문제가 계속되면 관리자에게 문의해 주세요.",
        "no_available_models": "현재 사용 가능한 레시피 모델이 없습니다. 나중에 다시 시도하거나 관리자에게 API 모델 접근 권한을 확인해 주세요.",
        "model_access_error": "레시피 서비스에 연결할 수 없습니다. 나중에 다시 시도하거나 관리자에게 API 설정을 확인해 주세요.",
        "model_quota_error": "레시피 서비스가 혼잡하거나 사용 한도에 도달했습니다. 몇 분 후 다시 시도해 주세요.",
        "recipe_error": "지금은 레시피를 만들 수 없습니다. 업로드한 사진은 그대로 유지되니 잠시 후 다시 시도해 주세요. 문제가 계속되면 관리자에게 문의해 주세요.",
        "error_details": "🔎 오류 세부 정보 (클릭하여 펼치기)",
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
        "version_caption": "📌 앱 버전: v{version}\n📌 선택한 언어: {language}\n📌 사용 모델: {model}",
        "history_title": "📚 생성된 레시피",
        "history_retention": "기록은 90일 동안 저장되며 이후 자동으로 삭제됩니다.",
        "history_empty": "저장된 레시피가 없습니다.",
        "history_clear": "모두 지우기",
        "history_delete": "이 레시피 삭제",
        "history_confirm_clear": "저장된 레시피를 모두 지울까요?",
        "history_storage_error": "이 브라우저에서 레시피 기록을 읽거나 저장할 수 없습니다.",
        "history_model_label": "모델",
        "history_duration_label": "소요 시간",
        "history_recipe_fallback": "저장된 레시피",
        "history_date_locale": "ko-KR",
        "export_pdf": "📄 PDF 내보내기 / 인쇄",
        "pdf_popup_blocked": "PDF를 내보내려면 이 사이트의 팝업을 허용해 주세요.",
    }
}

# --- 側邊欄佈局：偏好設定在最上，版本號推到最底 ---
st.session_state.setdefault("current_model", "")
st.session_state.setdefault("pending_history_recipe", None)
st.session_state.setdefault("recipe_generation_in_progress", False)

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
    selected_lang_label = st.selectbox(
        "🌍 選擇輸出語言 (Output Language)",
        list(lang_options.keys()),
        disabled=st.session_state.recipe_generation_in_progress,
    )
    target_lang = lang_options[selected_lang_label]

    t = ui_texts[target_lang]

    def render_version_caption():
        version_caption = escape(
            t["version_caption"].format(
                version=APP_VERSION,
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
    selected_cuisine_label = st.sidebar.selectbox(
        t["cuisine_label"],
        list(cuisine_options.keys()),
        disabled=st.session_state.recipe_generation_in_progress,
    )
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
            "durationLabel": t["history_duration_label"],
            "durationFormat": t["duration_format"],
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
st.session_state.setdefault("recipe_image", None)
st.session_state.setdefault("recipe_dish_previews", [])
st.session_state.setdefault("recipe_dish_preview_futures", [])
st.session_state.setdefault("recipe_generation_duration", None)
st.session_state.setdefault("recipe_preview_sections", [])
st.session_state.setdefault("recipe_previews_pending", False)
st.session_state.setdefault("recipe_generation_started_at", None)
st.session_state.setdefault("recipe_generation_language", "")
st.session_state.setdefault("recipe_generation_in_progress", False)
st.session_state.setdefault("recipe_generation_requested", False)
st.session_state.setdefault("recipe_generation_error_message", None)
st.session_state.setdefault("recipe_generation_error_detail", None)
st.session_state.setdefault("feedback_sent", False)


def start_recipe_generation():
    if st.session_state.recipe_generation_in_progress:
        return
    st.session_state.recipe_generation_in_progress = True
    st.session_state.recipe_generation_requested = True
    st.session_state.recipe_generation_error_message = None
    st.session_state.recipe_generation_error_detail = None
    st.session_state.recipe_result = None
    st.session_state.recipe_image = None
    st.session_state.recipe_dish_previews = []
    st.session_state.recipe_dish_preview_futures = []
    st.session_state.recipe_preview_sections = []
    st.session_state.recipe_previews_pending = False
    st.session_state.recipe_generation_duration = None
    st.session_state.pending_history_recipe = None
    st.session_state.feedback_sent = False


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
        st.image(st.session_state.stored_image, caption=t["image_caption"], width="stretch")

        # 產生食譜按鈕
        generate_col, preview_option_col = st.columns([1.4, 1])
        with generate_col:
            st.button(
                t["btn_generate"],
                type="primary",
                width="stretch",
                disabled=st.session_state.recipe_generation_in_progress,
                on_click=start_recipe_generation,
            )
        with preview_option_col:
            generate_previews = st.checkbox(
                t["preview_option"],
                value=True,
                key="generate_dish_previews",
                help=t["preview_option_help"],
                disabled=st.session_state.recipe_generation_in_progress,
            )

        if st.session_state.recipe_generation_requested:
            st.session_state.recipe_generation_requested = False
            generation_started = time.perf_counter()
            st.session_state.current_model = ""
            render_version_caption()
            with st.spinner(t["spinner"]), recipe_generation_guard():
                success = False
                failure_message = None
                failure_detail = None
                client = genai.Client(
                    http_options=genai_types.HttpOptions(
                        timeout=RECIPE_REQUEST_TIMEOUT_MS
                    )
                )
                recipe_input_image = prepare_recipe_input_image(
                    st.session_state.stored_image
                )
                available_models = candidate_models

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

                if not failure_message and not available_models:
                    failure_message = t["no_available_models"]
                    failure_detail = (
                        "Gemini model discovery completed, but none of the "
                        f"configured recipe models are available: {', '.join(candidate_models)}"
                    )

                unavailable_model_count = 0
                last_generation_error = None
                for model_name in available_models:
                    try:
                        recipe_started = time.perf_counter()
                        response = client.models.generate_content(
                            model=model_name,
                            contents=[recipe_input_image, prompt],
                        )
                        logging.info(
                            "Recipe request for %s completed in %.2f seconds",
                            model_name,
                            time.perf_counter() - recipe_started,
                        )
                        if response and response.text:
                            recipe_sections = [
                                section.strip()
                                for section in re.split(
                                    RECIPE_DISH_PATTERN, response.text
                                )[1:]
                                if section.strip()
                            ]
                            preview_sections = (
                                recipe_sections[:3] if generate_previews else []
                            )
                            recipe_image = encode_history_image(
                                st.session_state.stored_image
                            )
                            st.session_state.recipe_result = response.text
                            st.session_state.recipe_image = recipe_image
                            st.session_state.recipe_dish_previews = [
                                None
                            ] * len(preview_sections)
                            st.session_state.recipe_preview_sections = (
                                preview_sections
                            )
                            st.session_state.recipe_previews_pending = bool(
                                preview_sections
                            )
                            st.session_state.recipe_dish_preview_futures = []
                            st.session_state.recipe_generation_started_at = (
                                generation_started
                            )
                            st.session_state.recipe_generation_language = (
                                selected_lang_label
                            )
                            st.session_state.current_model = model_name
                            if preview_sections:
                                preview_client = genai.Client(
                                    http_options=genai_types.HttpOptions(
                                        timeout=RECIPE_REQUEST_TIMEOUT_MS
                                    )
                                )
                                preview_executor = ThreadPoolExecutor(
                                    max_workers=len(preview_sections)
                                )
                                st.session_state.recipe_dish_preview_futures = [
                                    preview_executor.submit(
                                        generate_dish_preview,
                                        preview_client,
                                        dish_section,
                                    )
                                    for dish_section in preview_sections
                                ]
                                preview_executor.shutdown(wait=False)
                            else:
                                finalize_recipe_generation()
                            render_version_caption()
                            st.session_state.feedback_sent = False
                            success = True
                            break
                        failure_detail = (
                            f"Recipe generation returned no text for {model_name}. "
                            "The response may have been blocked or empty."
                        )
                    except genai_errors.APIError as error:
                        last_generation_error = error
                        logging.warning(
                            "Gemini generation failed for %s (HTTP %s)",
                            model_name,
                            error.code,
                        )
                        if error.code == 404:
                            unavailable_model_count += 1
                            continue
                        if error.code in (401, 403):
                            failure_message = t["model_access_error"]
                            failure_detail = format_error_detail(
                                f"Recipe generation failed for {model_name}.", error
                            )
                            break
                        if error.code == 429:
                            failure_message = t["model_quota_error"]
                            failure_detail = format_error_detail(
                                f"Recipe generation failed for {model_name}.", error
                            )
                            break
                        failure_message = t["recipe_error"]
                        failure_detail = format_error_detail(
                            f"Recipe generation failed for {model_name}.", error
                        )
                        break
                    except Exception as error:
                        last_generation_error = error
                        logging.exception(
                            "Unexpected Gemini generation failure for %s",
                            model_name,
                        )
                        failure_detail = format_error_detail(
                            f"Recipe generation failed for {model_name}.", error
                        )
                        break

                if not success:
                    if failure_message is None:
                        if unavailable_model_count == len(available_models):
                            failure_message = t["no_available_models"]
                            if last_generation_error is not None:
                                failure_detail = format_error_detail(
                                    "All available candidate models returned "
                                    "not-found errors.",
                                    last_generation_error,
                                )
                        else:
                            failure_message = t["recipe_error"]
                            if last_generation_error is not None:
                                failure_detail = format_error_detail(
                                    "Recipe generation failed for the available "
                                    "candidate models.",
                                    last_generation_error,
                                )
                    st.session_state.recipe_generation_error_message = (
                        failure_message
                    )
                    st.session_state.recipe_generation_error_detail = (
                        failure_detail
                    )

            st.rerun()

        if st.session_state.recipe_generation_error_message:
            st.error(st.session_state.recipe_generation_error_message, icon="ℹ️")
            if st.session_state.recipe_generation_error_detail:
                with st.expander(t["error_details"]):
                    st.code(st.session_state.recipe_generation_error_detail)

    # 如果有食譜結果，進行精準分段與語音按鈕渲染
    if st.session_state.recipe_result is not None:
        st.success(t["success_msg"])
        st.markdown("---")

        full_text = st.session_state.recipe_result
        _RECIPE_PDF_EXPORT(
            data={
                "buttonLabel": t["export_pdf"],
                "popupBlocked": t["pdf_popup_blocked"],
                "title": t["title"],
                "recipe": full_text,
                "image": st.session_state.recipe_image,
                "imageAlt": t["image_caption"],
                "language": t["history_date_locale"],
            },
            key="recipe-pdf-export",
        )

        # 分割菜色區段
        sections = re.split(RECIPE_DISH_PATTERN, full_text)
        speech_settings = {
            "Traditional Chinese": ("zh-TW", ("zh-TW", "zh-CN", "zh")),
            "English": ("en-US", ("en-US", "en-GB", "en-AU", "en-CA")),
            "Japanese": ("ja-JP", ("ja-JP", "ja")),
            "Korean": ("ko-KR", ("ko-KR", "ko")),
        }
        lang_code, voice_languages = speech_settings[target_lang]

        def render_audio_controls(text, key):
            _RECIPE_AUDIO_CONTROLS(
                data={
                    "text": text,
                    "lang": lang_code,
                    "voiceLanguages": voice_languages,
                    "voiceMissing": t["tts_voice_missing"],
                    "playLabel": t["tts_play"],
                    "stopLabel": t["tts_stop"],
                },
                key=key,
            )

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

            st.markdown(clean_section)

            if dish_count <= len(st.session_state.recipe_dish_previews):
                preview_col, audio_col = st.columns([1, 1])
                preview = st.session_state.recipe_dish_previews[dish_count - 1]
                with preview_col:
                    if preview is not None:
                        st.image(
                            preview,
                            caption=t["preview_caption"],
                            width="stretch",
                        )
                    else:
                        if st.session_state.recipe_previews_pending:
                            st.info(t["preview_generating"], icon="ℹ️")
                        else:
                            st.info(t["preview_error"], icon="ℹ️")
                with audio_col:
                    render_audio_controls(plain_text, f"recipe-audio-{dish_count}")
            else:
                render_audio_controls(plain_text, f"recipe-audio-{dish_count}")

            st.markdown("---")

        if dish_count == 0:
            plain_text = (
                full_text.replace('\\', '')
                .replace('`', '')
                .replace('"', ' ')
                .replace("'", " ")
                .replace('*', '')
                .replace('#', '')
                .replace('\n', ' ')
                .replace('\r', '')
            )
            render_audio_controls(plain_text, "recipe-audio-full")
            st.markdown(full_text)

        if st.session_state.recipe_previews_pending:
            render_dish_preview_progress()

        generation_duration = st.session_state.recipe_generation_duration
        if generation_duration is not None:
            duration_text = t["duration_format"].format(
                minutes=generation_duration // 60,
                seconds=generation_duration % 60,
            )
            st.caption(
                t["generation_duration_footnote"].format(
                    duration=duration_text
                )
            )

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
            st.session_state.recipe_image = None
            st.session_state.recipe_dish_previews = []
            st.session_state.recipe_dish_preview_futures = []
            st.session_state.recipe_preview_sections = []
            st.session_state.recipe_previews_pending = False
            st.session_state.recipe_generation_duration = None
            st.session_state.recipe_generation_started_at = None
            st.session_state.recipe_generation_language = ""
            st.session_state.current_model = ""
            st.session_state.stored_image = None
            st.session_state.feedback_sent = False
            st.rerun()