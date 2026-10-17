import io
import os
import time

import requests
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "270"))  # مدة بقاء البوت في كل تشغيل

MAX_FILE_MB = 15
MAX_SUMMARY_CHARS = 60000
CHUNK = 3500

HELP = (
    "👋 أهلاً بك!\n\n"
    "📝 أرسل لي أي نص بأي لغة وسأترجمه إلى العربية.\n"
    "📄 أرسل لي ملف (PDF / Word / TXT) وسألخصه لك بالعربية.\n"
    "🌐 أرسل الملف مع كلمة «ترجمة» في التعليق وسأترجمه بدل التلخيص.\n\n"
    "الأوامر:\n"
    "/start - المساعدة\n"
    "/sum نص طويل - تلخيص نص"
)


# ---------- تيليغرام ----------
def tg(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=60)
    return r.json()


def send(chat_id, text, reply_to=None):
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000],
           reply_to_message_id=reply_to)


def download(file_id):
    info = tg("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    path = info["result"]["file_path"]
    return requests.get(f"{FILE_API}/{path}", timeout=120).content


# ---------- قراءة الملفات ----------
def extract_text(name, data):
    name = name.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    if name.endswith((".txt", ".md", ".csv")):
        return data.decode("utf-8", errors="ignore")
    return None


# ---------- Gemini ----------
def ask(system, text, max_tokens=2000):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=120)
    data = r.json()
    if r.status_code != 200:
        raise Exception(data.get("error", {}).get("message", "Gemini error"))
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def translate(text):
    system = (
        "أنت مترجم محترف. ترجم النص التالي إلى العربية الفصحى بدقة وبأسلوب طبيعي. "
        "حافظ على الأسماء والأرقام والتنسيق. أعد الترجمة فقط بدون أي شرح. "
        "إذا كان النص عربياً أصلاً فأعده كما هو."
    )
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    return "\n".join(ask(system, p, 3000) for p in parts)


def summarize(text):
    system = (
        "أنت مساعد متخصص في تلخيص المستندات. لخّص المستند التالي بالعربية "
        "بهذا الشكل:\n"
        "📌 الموضوع: (سطر واحد)\n"
        "📋 أهم النقاط: (نقاط واضحة ومختصرة)\n"
        "✅ الخلاصة: (جملتان)\n"
        "لا تضف معلومات غير موجودة في المستند."
    )
    return ask(system, text[:MAX_SUMMARY_CHARS], 1500)


# ---------- معالجة الرسائل ----------
def handle(msg):
    chat_id = msg["chat"]["id"]
    if str(chat_id) != CHAT_ID:
        return  # تجاهل أي محادثة غير المحددة في CHAT_ID
    mid = msg["message_id"]
    text = msg.get("text")
    doc = msg.get("document")

    try:
        if text:
            if text.startswith("/start") or text.startswith("/help"):
                send(chat_id, HELP)
            elif text.startswith("/sum"):
                body = text[4:].strip()
                if not body:
                    send(chat_id, "اكتب النص بعد الأمر: /sum النص هنا", mid)
                else:
                    send(chat_id, summarize(body), mid)
            else:
                send(chat_id, translate(text), mid)

        elif doc:
            name = doc.get("file_name", "file")
            if doc.get("file_size", 0) > MAX_FILE_MB * 1024 * 1024:
                send(chat_id, f"⚠️ الملف أكبر من {MAX_FILE_MB} ميجا.", mid)
                return
            send(chat_id, "⏳ جاري قراءة الملف...", mid)
            data = download(doc["file_id"])
            content = extract_text(name, data) if data else None
            if content is None:
                send(chat_id, "⚠️ الصيغة غير مدعومة. المدعوم: PDF, DOCX, TXT", mid)
                return
            if not content.strip():
                send(chat_id, "⚠️ لم أجد نصاً داخل الملف (قد يكون صورة ممسوحة).", mid)
                return
            caption = (msg.get("caption") or "").lower()
            if "ترجمة" in caption or "translate" in caption:
                send(chat_id, translate(content[:20000]), mid)
            else:
                send(chat_id, summarize(content), mid)

        else:
            send(chat_id, "أرسل نصاً أو ملف PDF / Word / TXT.", mid)

    except Exception as e:
        send(chat_id, f"❌ حدث خطأ: {str(e)[:200]}", mid)


# ---------- التشغيل ----------
def main():
    end = time.time() + RUN_SECONDS
    offset = None
    while time.time() < end:
        params = {"timeout": 20, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset
        res = tg("getUpdates", **params)
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            if "message" in upd:
                handle(upd["message"])
    # تأكيد الرسائل المعالجة حتى لا تتكرر في التشغيل القادم
    if offset:
        tg("getUpdates", offset=offset, timeout=0)


if __name__ == "__main__":
    main()
