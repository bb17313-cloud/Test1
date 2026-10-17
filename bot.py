import io
import os
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

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

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "TranslatorBot contact@example.com")
URL_RE = re.compile(r"https?://\S+")

MAX_FILE_MB = 15
MAX_SUMMARY_CHARS = 60000
CHUNK = 3500

HELP = (
    "👋 أهلاً بك!\n\n"
    "📝 أرسل لي أي نص بأي لغة وسأترجمه إلى العربية.\n"
    "📄 أرسل لي ملف (PDF / Word / TXT) وسألخصه لك بالعربية.\n"
    "🌐 أرسل الملف مع كلمة «ترجمة» في التعليق وسأترجمه بدل التلخيص.\n"
    "🔗 أرسل رابطاً (مثل ملفات SEC) وسأفتحه وألخصه لك. أضف كلمة «ترجمة» مع الرابط لترجمته.\n\n"
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


# ---------- فتح الروابط ----------
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip() + " ")


def html_to_text(raw):
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.out)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def xml_to_text(raw):
    root = ET.fromstring(raw.encode("utf-8"))
    lines = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        val = (el.text or "").strip()
        if val:
            lines.append(f"{tag}: {val}")
    return "\n".join(lines)


def fetch_url(url):
    headers = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return extract_text("x.pdf", r.content) or ""
    raw = r.text
    if raw.lstrip().startswith("<?xml") and "html" not in ctype:
        try:
            return xml_to_text(raw)
        except Exception:
            pass
    return html_to_text(raw)


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


def summarize(text, sec=False):
    if sec:
        system = (
            "أنت محلل أسهم أمريكية. لخّص إفصاح SEC التالي بالعربية بهذا الشكل:\n"
            "📄 نوع النموذج: (مثل 13G / 8-K / 10-Q / Form 4)\n"
            "🏢 الشركة: (الاسم والرمز إن وجد)\n"
            "👤 مقدم الإفصاح: (الجهة أو الشخص)\n"
            "📌 أهم ما ورد: (نقاط واضحة: عدد الأسهم، النسبة، المبالغ، التواريخ، القرار أو الحدث)\n"
            "💡 المعنى للمتداول: (جملتان بدون توصية شراء أو بيع)\n"
            "استخدم فقط المعلومات الموجودة في المستند ولا تخمّن."
        )
    else:
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
            elif URL_RE.search(text):
                url = URL_RE.search(text).group(0)
                send(chat_id, "⏳ جاري فتح الرابط...", mid)
                content = fetch_url(url)
                if not content.strip():
                    send(chat_id, "⚠️ لم أجد نصاً في الرابط.", mid)
                elif "ترجمة" in text or "translate" in text.lower():
                    send(chat_id, translate(content[:20000]), mid)
                else:
                    send(chat_id, summarize(content, sec="sec.gov" in url), mid)
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
