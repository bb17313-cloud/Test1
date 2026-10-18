import glob
import io
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs, unquote

import requests
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# إعدادات النماذج (Gemini و Groq)
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-3.5-flash-lite").split(",") if m.strip()]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "20400"))  # مدة بقاء البوت في كل تشغيل

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "TranslatorBot contact@example.com")
URL_RE = re.compile(r"https?://\S+")
YT_RE = re.compile(r"(youtube\.com/(watch|shorts|live)|youtu\.be/)", re.I)
VIDEO_SITES = ("twitter.com", "x.com", "tiktok.com", "facebook.com", "fb.watch", "instagram.com",
               "vimeo.com", "dailymotion.com", "twitch.tv", "reddit.com", "streamable.com", "soundcloud.com")
MEDIA_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac",
              ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp")

MAX_FILE_MB = 15
MAX_SUMMARY_CHARS = 400000
TRANSLATE_LIMIT = 60000        # أقصى عدد أحرف للترجمة الكاملة
CHUNK = 9000                   # حجم جزء الترجمة
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # حد تنزيل البوتات في تيليغرام
TG_UPLOAD_MB = 50              # حد رفع البوتات في تيليغرام
MAX_DOWNLOAD_MB = 300          # أقصى حجم لروابط الملفات المباشرة
SEG_SECONDS = 600              # تقسيم الصوت إلى أجزاء من 10 دقائق
MAX_SEGMENTS = 18              # حتى 3 ساعات
LONG_RESULT = 7000             # فوق هذا الطول يُرسل التفريغ كملف نصي

# ذاكرة المستند الأخير (للأسئلة التفاعلية) - تُمحى عند إعادة تشغيل البوت
LAST = {"title": None, "text": None, "kind": "doc"}
STATE = {"mode": None, "hist": []}

HELP = (
    "👋 أهلاً بك!\n\n"
    "📝 أرسل نصاً بأي لغة ← أترجمه إلى العربية.\n"
    "📄 أرسل ملف PDF / Word / TXT ← ألخصه بالعربية (أضف كلمة «ترجمة» في التعليق لترجمته).\n"
    "🔗 أرسل رابطاً (مثل ملفات SEC) ← أفتحه وألخصه (أضف «ترجمة» لترجمته).\n"
    "🖼 أرسل صورة أو شارت (مثل TradingView) ← أقوم بتحليلها وقراءة البيانات الفنية منها.\n"
    "🎙 أرسل رسالة صوتية أو ملف صوت ← أفرّغه وأترجمه.\n"
    "🎬 أرسل فيديو قصير ← أفرّغ كلامه وأترجمه.\n"
    "▶️ أرسل رابط يوتيوب، أو رابط فيديو من (X / تيك توك / فيسبوك / إنستغرام / فيميو...), أو رابط ملف صوت/فيديو مباشر ← أفرّغه وأترجمه.\n\n"
    "⬇️ أضف كلمة «تحميل» مع رابط (X / تيك توك / إنستغرام / فيسبوك...) ← أحمّل الفيديو وأرسله لك.\n"
    "📊 تحليل سهم أمريكي: أرسل «تحليل UPLD» أو رابط شارت TradingView.\n"
    "💬 بعد أي تلخيص أو تفريغ تظهر أزرار لسؤالي عن أي نقطة في المستند.\n\n"
    "الأوامر:\n"
    "/start - المساعدة\n"
    "/sum نص طويل - تلخيص نص\n"
    "/ask سؤالك - سؤال عن آخر مستند\n"
    "/new - الرجوع للترجمة العادية\n\n"
    "⚠️ حد تيليغرام لحجم الملفات المرسلة للبوت 20 ميجا. للملفات الأكبر أرسل رابطاً."
)


# ---------- تيليغرام ----------
def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=60)
        return r.json()
    except Exception as e:
        print("tg error:", method, e)
        return {}


def split_message(text, limit=4000):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


def send(chat_id, text, reply_to=None, markup=None):
    chunks = split_message(text) or ["—"]
    for i, c in enumerate(chunks):
        p = dict(chat_id=chat_id, text=c, reply_to_message_id=reply_to)
        if markup and i == len(chunks) - 1:
            p["reply_markup"] = markup
        tg("sendMessage", **p)


def status_start(chat_id, text, reply_to=None):
    r = tg("sendMessage", chat_id=chat_id, text=text, reply_to_message_id=reply_to)
    return (r.get("result") or {}).get("message_id")


def status_update(chat_id, message_id, text):
    if message_id:
        tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text)


def status_end(chat_id, message_id):
    if message_id:
        tg("deleteMessage", chat_id=chat_id, message_id=message_id)


def send_document(chat_id, filename, content, caption="", reply_to=None):
    data = {"chat_id": chat_id, "caption": caption[:1000]}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    try:
        requests.post(f"{API}/sendDocument", data=data,
                      files={"document": (filename, content.encode("utf-8"))}, timeout=120)
    except Exception as e:
        print("sendDocument error:", e)


def send_video(chat_id, path, caption="", reply_to=None):
    for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
        data = {"chat_id": chat_id, "caption": caption[:1000]}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        if method == "sendVideo":
            data["supports_streaming"] = "true"
        try:
            with open(path, "rb") as f:
                r = requests.post(f"{API}/{method}", data=data,
                                  files={field: (os.path.basename(path), f)}, timeout=600)
            if r.json().get("ok"):
                return True
        except Exception as e:
            print(f"{method} error:", e)
    return False


def download(file_id):
    info = tg("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    path = info["result"]["file_path"]
    try:
        return requests.get(f"{FILE_API}/{path}", timeout=180).content
    except Exception as e:
        print("download error:", e)
        return None


def keyboard(translate_btn=True):
    rows = [[{"text": "❓ اسأل عن نقطة", "callback_data": "ask"}]]
    if translate_btn:
        rows.append([{"text": "🌐 ترجمة كاملة", "callback_data": "trans"}])
    rows.append([{"text": "🔢 الأرقام والتواريخ المهمة", "callback_data": "nums"}])
    return {"inline_keyboard": rows}


def remember(title, text, kind="doc"):
    LAST["title"] = title
    LAST["text"] = text[:MAX_SUMMARY_CHARS]
    LAST["kind"] = kind
    STATE["mode"] = None
    STATE["hist"] = []


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


def is_media_url(url):
    return urlparse(url).path.lower().endswith(MEDIA_EXTS)


def download_media_url(url):
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    headers = {"User-Agent": SEC_USER_AGENT}
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        if size > limit:
            raise Exception(f"الملف أكبر من {MAX_DOWNLOAD_MB} ميجا")
        buf = io.BytesIO()
        for chunk in r.iter_content(1024 * 1024):
            buf.write(chunk)
            if buf.tell() > limit:
                raise Exception(f"الملف أكبر من {MAX_DOWNLOAD_MB} ميجا")
        return buf.getvalue()


def is_video_site(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in VIDEO_SITES)


def download_with_ytdlp(url):
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    with tempfile.TemporaryDirectory() as d:
        cmd = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio/best", "--no-playlist",
               "--max-filesize", f"{MAX_DOWNLOAD_MB}M", "--socket-timeout", "30",
               "--no-warnings", "-o", os.path.join(d, "media.%(ext)s"), url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        files = glob.glob(os.path.join(d, "media.*"))
        if r.returncode != 0 or not files:
            lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
            reason = lines[-1][:200] if lines else "سبب غير معروف"
            raise Exception(f"تعذر تحميل الصوت من الرابط: {reason}")
        path = files[0]
        if os.path.getsize(path) > limit:
            raise Exception(f"الملف أكبر من {MAX_DOWNLOAD_MB} ميجا")
        with open(path, "rb") as f:
            return f.read(), os.path.basename(path)


def download_video_ytdlp(url, out_dir):
    lim = TG_UPLOAD_MB - 2
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--socket-timeout", "30", "--no-warnings",
           "-f", f"b[ext=mp4][filesize<{lim}M]/b[filesize<{lim}M]/bv*[ext=mp4]+ba[ext=m4a]/b",
           "--merge-output-format", "mp4", "--max-filesize", f"{TG_UPLOAD_MB}M",
           "-o", os.path.join(out_dir, "video.%(ext)s"), url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    files = glob.glob(os.path.join(out_dir, "video.*"))
    if r.returncode != 0 or not files:
        lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        reason = lines[-1][:200] if lines else "سبب غير معروف"
        if "larger than max-filesize" in (r.stderr or "").lower():
            raise Exception(f"الفيديو أكبر من {TG_UPLOAD_MB} ميجا (حد تيليغرام للبوتات).")
        raise Exception(f"تعذر تحميل الفيديو من الرابط: {reason}")
    path = files[0]
    if os.path.getsize(path) > TG_UPLOAD_MB * 1024 * 1024:
        raise Exception(f"الفيديو أكبر من {TG_UPLOAD_MB} ميجا (حد تيليغرام للبوتات).")
    return path


def process_video_download(chat_id, mid, url):
    sid = status_start(chat_id, "⏳ جاري تحميل الفيديو...", mid)
    try:
        with tempfile.TemporaryDirectory() as d:
            path = download_video_ytdlp(url, d)
            status_update(chat_id, sid, "📤 جاري إرسال الفيديو...")
            ok = send_video(chat_id, path, "✅ تم التحميل\n(أرسل الرابط بدون كلمة «تحميل» لتفريغ كلامه وترجمته)", mid)
        if not ok:
            send(chat_id, "⚠️ تم التحميل لكن تعذر رفع الفيديو إلى تيليغرام.", mid)
    finally:
        status_end(chat_id, sid)


# ---------- محرك النماذج (Gemini / Groq) ----------
class QuotaError(Exception):
    """تجاوز حد الاستخدام في النماذج."""


QUOTA_MSG = (
    "⚠️ تم استهلاك حد الاستخدام المجاني للنموذج مؤقتاً.\n\n"
    "• الحد اليومي يتجدد تلقائياً، فأعد المحاولة بعد قليل.\n"
    "• يمكنك التحويل إلى النموذج الآخر عبر إعدادات البيئة."
)


def friendly_error(e):
    if isinstance(e, QuotaError):
        return QUOTA_MSG
    msg = str(e)
    return f"❌ حدث خطأ: {msg[:300]}"


def call_groq(system, parts, max_tokens=2000, timeout=300):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    messages = [{"role": "system", "content": system}]
    user_content = []
    
    for p in parts:
        if "text" in p:
            user_content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            mime = p["inline_data"]["mime_type"]
            b64 = p["inline_data"]["data"]
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}"
                }
            })
            
    if len(user_content) == 1 and user_content[0]["type"] == "text":
        messages.append({"role": "user", "content": user_content[0]["text"]})
    else:
        messages.append({"role": "user", "content": user_content})

    body = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens
    }
    
    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise Exception(f"Groq API Error: {r.text}")
    data = r.json()
    return data["choices"][0]["message"]["content"].replace("**", "").strip()


def call_gemini(system, parts, max_tokens=2000, timeout=300, tools=None):
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if tools:
        body["tools"] = tools
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    last_status, last_msg, data = None, "Gemini error", {}
    for idx, model in enumerate(models):
        is_last = idx == len(models) - 1
        attempts = 3 if is_last else 1
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(attempts):
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.status_code == 200:
                return _parse_gemini(data)
            last_status = r.status_code
            last_msg = data.get("error", {}).get("message", "Gemini error")
            if r.status_code in (429, 500, 503):
                if attempt < attempts - 1:
                    time.sleep(15 * (attempt + 1))
                    continue
                break
            if r.status_code == 404 and idx > 0:
                break
            raise Exception(last_msg)
    if last_status == 429:
        raise QuotaError(last_msg)
    raise Exception(last_msg)


def gemini(system, parts, max_tokens=2000, timeout=300, tools=None):
    if AI_PROVIDER == "groq" and GROQ_API_KEY:
        try:
            return call_groq(system, parts, max_tokens, timeout)
        except Exception as e:
            print("Groq failed, falling back to Gemini:", e)
    return call_gemini(system, parts, max_tokens, timeout, tools)


def _parse_gemini(data):
    cands = data.get("candidates") or []
    if not cands:
        raise Exception("لم يُرجع النموذج أي نتيجة (قد يكون المحتوى محجوباً).")
    cand = cands[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    text = text.replace("**", "").strip()
    if cand.get("finishReason") == "MAX_TOKENS":
        text += "\n\n⚠️ (انقطع الرد لطوله)"
    return text


def ask(system, text, max_tokens=2000):
    return gemini(system, [{"text": text}], max_tokens)


def is_arabic(text, threshold=0.5):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ar = sum(1 for c in letters if "\u0600" <= c <= "\u06ff")
    return ar / len(letters) >= threshold


def chunk_text(text, size):
    parts, cur = [], ""
    for para in text.split("\n"):
        while len(para) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def translate(text):
    system = (
        "أنت مترجم محترف. ترجم النص التالي إلى العربية الفصحى بدقة وبأسلوب طبيعي. "
        "حافظ على الأسماء والأرقام والتواريخ والعلامات الزمنية والتنسيق وترتيب الفقرات. "
        "أعد الترجمة فقط بدون أي شرح. إذا كان النص عربياً أصلاً فأعده كما هو."
    )
    return "\n".join(ask(system, p, 8000) for p in chunk_text(text, CHUNK))


def summarize(text, sec=False):
    if sec:
        system = (
            "أنت محلل أسهم أمريكية محترف. اقرأ إفصاح SEC كاملاً ولخّصه بالعربية بشكل مفصّل ودقيق "
            "(وليس مختصراً)، بحيث يغني المتداول عن قراءة المستند.\n\n"
            "ابدأ بسطرين: نوع النموذج وفترته، ثم اسم الشركة ورمزها.\n"
            "ثم اختر الأقسام المناسبة لنوع النموذج من القائمة التالية، واذكر كل قسم بعنوان واضح تسبقه رمز تعبيري:\n"
            "• نبذة عن الشركة: نشاطها وقطاعاتها الرئيسية وما تقدمه كل شركة تابعة أو قطاع.\n"
            "• النتائج المالية: الإيرادات، صافي الدخل، ربحية السهم، الأقساط أو المبيعات، "
            "التدفق النقدي، الديون والسيولة، مع المقارنة بالفترة السابقة ونسبة التغير.\n"
            "• أداء القطاعات: أرقام كل قطاع وأسباب تحسنه أو تراجعه.\n"
            "• الأحداث والقرارات المهمة: صفقات، استحواذات، تمويل، طرح أسهم، تغييرات إدارة، دعاوى قضائية.\n"
            "• المخاطر والتحذيرات: ضعف الرقابة الداخلية، تأخر التقارير، مخاطر الإدراج أو الشطب، شكوك الاستمرارية، "
            "إعادة عرض الأرقام، أي ملاحظات من المدقق.\n"
            "• تفاصيل الملكية والأسهم (لنماذج 13G/13D/Form 4): من الجهة، عدد الأسهم، النسبة، التواريخ، سعر العملية.\n"
            "• تفاصيل الطرح (لنماذج S-3/S-1/424B): حجم الطرح، السعر، الجهة البائعة، الاستخدام المتوقع للأموال، التخفيف المحتمل.\n"
            "• التواريخ المهمة القادمة.\n"
            "💡 الخلاصة: فقرة من 3 إلى 4 جمل تشرح الصورة العامة وما يهم المتداول، بدون توصية شراء أو بيع.\n\n"
            "قواعد: اكتب الأرقام كما وردت في المستند مع الوحدة والسنة، ولا تقرّبها أو تخمّنها. "
            "لا تضف معلومة غير موجودة في المستند، واحذف أي قسم لا تتوفر له بيانات. "
            "استخدم نقاطاً (•) وأسطراً قصيرة، ولا تستخدم رموز التنسيق مثل ** أو #."
        )
    else:
        system = (
            "أنت مساعد متخصص في تلخيص المستندات. لخّص المستند التالي بالعربية بشكل مفصّل ومنظم:\n"
            "📌 الموضوع: سطر أو سطران.\n"
            "📋 أهم النقاط: نقاط واضحة تغطي كل أقسام المستند الرئيسية مع الأرقام والأسماء والتواريخ المهمة.\n"
            "⚠️ ملاحظات أو تحذيرات: إن وجدت.\n"
            "✅ الخلاصة: 3 جمل.\n"
            "لا تضف معلومات غير موجودة في المستند. استخدم نقاطاً (•) ولا تستخدم رموز التنسيق مثل ** أو #."
        )
    return ask(system, text[:MAX_SUMMARY_CHARS], 8000)


# ---------- الأسئلة التفاعلية ----------
def answer_question(question):
    system = (
        "أنت مساعد يجيب عن أسئلة حول مستند معيّن. أجب بالعربية وبدقة اعتماداً على المستند فقط. "
        "إذا لم تكن الإجابة في المستند فقل: «لم أجد هذه المعلومة في المستند». "
        "اذكر الأرقام والأسماء والتواريخ كما وردت، ويمكنك الاستشهاد بعبارة قصيرة من المستند بين علامتي تنصيص. "
        "لا تخمّن ولا تضف معلومات من خارج المستند. لا تستخدم رموز التنسيق مثل ** أو #."
    )
    hist = "\n".join(f"س: {q}\nج: {a}" for q, a in STATE["hist"][-5:])
    text = (
        f"عنوان المستند: {LAST['title']}\n\nالمستند:\n{LAST['text']}\n\n"
        f"المحادثة السابقة:\n{hist or '(لا يوجد)'}\n\nالسؤال: {question}"
    )
    ans = ask(system, text, 3000)
    STATE["hist"].append((question, ans))
    return ans


def key_facts():
    system = (
        "استخرج من المستند التالي كل الأرقام والتواريخ والأسماء المهمة بالعربية في قوائم مرتبة: "
        "🔢 الأرقام والمبالغ والنسب (مع وحدتها وما تشير إليه)، 📅 التواريخ والمواعيد، "
        "🏢 الأسماء (شركات وأشخاص وجهات). انقل القيم كما وردت دون تقريب أو تخمين، "
        "واحذف أي قسم لا توجد له بيانات. لا تستخدم رموز التنسيق مثل ** أو #."
    )
    return ask(system, LAST["text"], 4000)


def no_doc(chat_id, mid=None):
    send(chat_id, "أرسل لي مستنداً أو رابطاً أو ملفاً صوتياً أولاً، ثم اسأل عنه.", mid)


# ---------- الصوت والفيديو ----------
TRANSCRIBE_SYSTEM = (
    "أنت مفرّغ صوتي محترف. فرّغ الكلام المسموع حرفياً بلغته الأصلية وبعلامات ترقيم صحيحة وفقرات قصيرة، "
    "دون تلخيص أو حذف أو إضافة. إذا تعدد المتحدثون فميّزهم بـ (المتحدث 1) و(المتحدث 2). "
    "اترك الأسماء والأرقام كما نُطقت. لا تكتب وصف الأصوات إلا عند الضرورة بين أقواس مثل [موسيقى]. "
    "إذا لم يوجد كلام مسموع فاكتب فقط: [لا يوجد كلام]. أعد النص فقط بدون مقدمات."
)
YT_SYSTEM = (
    "أنت مفرّغ صوتي محترف. فرّغ كلام هذا الفيديو كاملاً حرفياً بلغته الأصلية وبعلامات ترقيم صحيحة، "
    "وضع علامة زمنية بصيغة [MM:SS] في بداية كل فقرة. ميّز المتحدثين بـ (المتحدث 1) و(المتحدث 2) إن تعددوا. "
    "لا تلخّص ولا تحذف. إذا لم يوجد كلام فاكتب فقط: [لا يوجد كلام]. أعد النص فقط بدون مقدمات."
)


def guess_mime(name, default):
    mt, _ = mimetypes.guess_type(name)
    return mt or default


def split_audio(data, name):
    ext = os.path.splitext(name)[1] or ".bin"
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in" + ext)
            with open(src, "wb") as f:
                f.write(data)
            out = os.path.join(d, "seg_%03d.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                 "-b:a", "48k", "-f", "segment", "-segment_time", str(SEG_SECONDS),
                 "-reset_timestamps", "1", out],
                check=True, timeout=900)
            files = sorted(glob.glob(os.path.join(d, "seg_*.mp3")))
            segs = []
            for fp in files:
                with open(fp, "rb") as f:
                    segs.append((f.read(), "audio/mp3"))
            if segs:
                return segs
    except Exception as e:
        print("ffmpeg error:", e)
    return [(data, guess_mime(name, "audio/ogg"))]


def transcribe_segment(seg_bytes, mime):
    import base64
    parts = [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(seg_bytes).decode()}},
        {"text": "فرّغ هذا المقطع."},
    ]
    return gemini(TRANSCRIBE_SYSTEM, parts, 16000, timeout=600)


def deliver_transcript(chat_id, mid, transcript, title, status_id=None):
    if not transcript.strip() or "[لا يوجد كلام]" in transcript and len(transcript) < 60:
        status_end(chat_id, status_id)
        send(chat_id, "⚠️ لم أجد كلاماً مسموعاً في هذا المقطع.", mid)
        return
    if is_arabic(transcript):
        result = f"🎙 التفريغ:\n\n{transcript}"
        stored = transcript
    else:
        status_update(chat_id, status_id, "🌐 جاري الترجمة إلى العربية...")
        ar = translate(transcript)
        result = f"📝 النص الأصلي:\n\n{transcript}\n\n━━━━━━━━\n🌐 الترجمة العربية:\n\n{ar}"
        stored = f"النص الأصلي:\n{transcript}\n\nالترجمة العربية:\n{ar}"
    remember(title, stored, kind="media")
    status_end(chat_id, status_id)
    if len(result) > LONG_RESULT:
        send_document(chat_id, "transcript.txt", result,
                      caption="📄 التفريغ الكامل مع الترجمة", reply_to=mid)
        send(chat_id, "📌 ملخص المحتوى:\n\n" + summarize(stored), mid, markup=keyboard(False))
    else:
        send(chat_id, result, mid, markup=keyboard(False))


def process_media(chat_id, mid, data, name, title):
    status_id = status_start(chat_id, "⏳ جاري تجهيز الملف...", mid)
    segs = split_audio(data, name)
    note = ""
    if len(segs) > MAX_SEGMENTS:
        segs = segs[:MAX_SEGMENTS]
        note = f"\n\n⚠️ تم تفريغ أول {MAX_SEGMENTS * SEG_SECONDS // 3600} ساعات فقط."
    n = len(segs)
    texts = []
    for i, (sb, smime) in enumerate(segs):
        status_update(chat_id, status_id, f"🎙 جاري التفريغ... الجزء {i + 1} من {n}")
        t = transcribe_segment(sb, smime)
        if n > 1:
            mins = i * SEG_SECONDS // 60
            t = f"⏱ {mins // 60:02d}:{mins % 60:02d}:00\n{t}"
        texts.append(t)
    deliver_transcript(chat_id, mid, "\n\n".join(texts) + note, title, status_id)


def process_youtube(chat_id, mid, url):
    status_id = status_start(chat_id, "⏳ جاري تحليل فيديو يوتيوب (قد يستغرق عدة دقائق)...", mid)
    parts = [{"file_data": {"file_uri": url}}, {"text": "فرّغ كلام هذا الفيديو."}]
    transcript = gemini(YT_SYSTEM, parts, 30000, timeout=900)
    deliver_transcript(chat_id, mid, transcript, url, status_id)


def get_media(msg):
    for key, default_name in (("voice", "voice.ogg"), ("audio", "audio.mp3"),
                              ("video", "video.mp4"), ("video_note", "note.mp4")):
        m = msg.get(key)
        if m:
            return m["file_id"], m.get("file_name", default_name), m.get("file_size", 0)
    d = msg.get("document")
    if d:
        name = d.get("file_name", "file")
        mt = d.get("mime_type", "")
        if mt.startswith(("audio/", "video/")) or name.lower().endswith(MEDIA_EXTS):
            return d["file_id"], name, d.get("file_size", 0)
    return None


# ---------- تحليل الأسهم الأمريكية ----------
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TV_COLUMNS_FULL = ["name", "description", "exchange", "type", "country", "sector", "industry", "close",
                   "change", "volume", "average_volume_10d_calc", "market_cap_basic",
                   "float_shares_outstanding", "beta_1_year", "earnings_release_next_date",
                   "premarket_close", "premarket_change", "postmarket_close", "postmarket_change"]
TV_COLUMNS_CORE = ["name", "description", "exchange", "type", "close", "change", "volume",
                   "market_cap_basic"]
CIK_CACHE = {}
SYMBOL_RE = re.compile(r"^\s*(?:/analy[sz]e|/analysis|تحليل|حلل|analy[sz]e)\s+(?:([A-Za-z]+):)?\$?([A-Za-z][A-Za-z0-9.\-]{0,9})\s*$",
                       re.I)
UNAVAILABLE = ("Dark Pool اللحظي، وL2/L3/MBO وBookmap، وIceberg وSweep وWhale Watch وLarge Lot "
               "وTape Speed وVolume Stripes، والنقاط الكبيرة (Dots)")
DISCLAIMER = ("⚠️ تنبيه: المستويات محسوبة آلياً من بيانات تاريخية وقد تتأخر الأسعار دقائق، "
              "وهي أداة مساعدة وليست توصية شراء أو بيع. تأكد من السعر الحي من وسيطك قبل أي قرار.")


def http_get_json(url, params=None, headers=None, timeout=30):
    h = {"User-Agent": UA_BROWSER, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def to_num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).replace(",", "").replace("$", "").strip()
    if t in ("", "N/A", "--", "NaN", "null"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fmt_p(x):
    if x is None:
        return "غير متاح"
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:.4f}"


def fmt_n(x):
    if x is None:
        return "غير متاح"
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    return f"{x:,.0f}"


def fmt_pct(x, signed=False):
    if x is None:
        return "غير متاح"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def short_err(e):
    msg = str(e)
    m = re.search(r"(\d{3}) (Client|Server) Error", msg)
    if m:
        return f"رُفض الطلب (HTTP {m.group(1)})"
    return msg[:80]


def fetch_tv(symbol, exchange=None):
    def scan(columns):
        payload = {"filter": [{"left": "name", "operation": "equal", "right": symbol}],
                   "options": {"lang": "en"}, "markets": ["america"],
                   "symbols": {"query": {"types": []}, "tickers": []}, "columns": columns,
                   "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}, "range": [0, 10]}
        r = requests.post("https://scanner.tradingview.com/america/scan", json=payload,
                          headers={"User-Agent": UA_BROWSER}, timeout=30)
        r.raise_for_status()
        return r.json().get("data") or []

    try:
        cols, rows = TV_COLUMNS_FULL, scan(TV_COLUMNS_FULL)
    except Exception:
        cols, rows = TV_COLUMNS_CORE, scan(TV_COLUMNS_CORE)
    if not rows:
        raise Exception("الرمز غير موجود في TradingView")
    pick = None
    if exchange:
        pick = next((r for r in rows if r["s"].upper().startswith(exchange.upper() + ":")), None)
    if not pick:
        pick = next((r for r in rows if "stock" in str(r["d"][cols.index("type")]).lower()), rows[0])
    d = dict(zip(cols, pick["d"]))
    d["ticker"] = pick["s"]
    return d


def fetch_yahoo(symbol, interval, range_, prepost=False):
    sym = symbol.replace(".", "-")
    j = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                      params={"interval": interval, "range": range_,
                              "includePrePost": "true" if prepost else "false"})
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
        if None in (o, h, l, c):
            continue
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    return bars, res.get("meta") or {}


def session_label(meta, t):
    try:
        cp = meta["currentTradingPeriod"]
        if cp["regular"]["start"] <= t < cp["regular"]["end"]:
            return "الجلسة العادية"
        if t < cp["regular"]["start"]:
            return "ما قبل الافتتاح"
        return "ما بعد الإغلاق"
    except Exception:
        return "غير محددة"


def fetch_nasdaq_short(symbol):
    j = http_get_json(f"https://api.nasdaq.com/api/quote/{symbol}/short-interest",
                      params={"assetClass": "stocks"},
                      headers={"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"},
                      timeout=25)
    rows = (((j.get("data") or {}).get("shortInterestTable") or {}).get("rows")) or []
    out = []
    for r in rows:
        out.append({"date": r.get("settlementDate"), "si": to_num(r.get("interest")),
                    "adv": to_num(r.get("avgDailyShareVolume")), "dtc": to_num(r.get("daysToCover"))})
    return [o for o in out if o["si"] is not None]


def calc_atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    last = trs[-n:]
    return sum(last) / len(last)


def swing_points(bars, w=3):
    pts = []
    for i in range(w, len(bars) - w):
        win = bars[i - w:i + w + 1]
        if bars[i]["h"] == max(b["h"] for b in win):
            pts.append((i, bars[i]["h"], "H"))
        if bars[i]["l"] == min(b["l"] for b in win):
            pts.append((i, bars[i]["l"], "L"))
    return pts


def cluster_levels(prices, tol):
    clusters = []
    for p in sorted(prices):
        if clusters and p - clusters[-1]["max"] <= tol:
            clusters[-1]["vals"].append(p)
            clusters[-1]["max"] = p
        else:
            clusters.append({"vals": [p], "max": p})
    return [{"price": sum(c["vals"]) / len(c["vals"]), "touches": len(c["vals"])} for c in clusters]


def volume_profile(bars, bins=24):
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    if hi <= lo:
        return None, []
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        vol[min(int((tp - lo) / step), bins - 1)] += b["v"]
    order = sorted(range(bins), key=lambda i: -vol[i])
    center = lambda i: lo + (i + 0.5) * step
    return center(order[0]), [center(i) for i in order[1:3]]


def compute_levels(bars, price):
    a = calc_atr(bars) or price * 0.03
    recent = bars[-120:]
    tol = max(0.5 * a, price * 0.01)
    cand = [p for _, p, _ in swing_points(recent, 3)]
    year = bars[-252:]
    hi52, lo52 = max(b["h"] for b in year), min(b["l"] for b in year)
    cand += [hi52, lo52]
    poc, hvn = volume_profile(recent)
    if poc:
        cand += [poc, poc] + hvn
    cl = cluster_levels(cand, tol)
    sup = sorted([c for c in cl if c["price"] < price - 0.1 * a], key=lambda c: -c["price"])
    res = sorted([c for c in cl if c["price"] > price + 0.1 * a], key=lambda c: c["price"])

    s1 = sup[0]["price"] if sup else price - a
    near = [c for c in sup if price - 5 * a <= c["price"] < s1 - 0.1 * a]
    strong = max(near, key=lambda c: c["touches"])["price"] if near else (
        sup[1]["price"] if len(sup) > 1 else s1 - a)
    targets, atr_based = [c["price"] for c in res[:4]], []
    last = targets[-1] if targets else price
    while len(targets) < 4:
        last += a
        targets.append(last)
        atr_based.append(len(targets))
    extra = hi52 if hi52 > targets[3] else targets[3] + 1.5 * a
    stop1 = s1 - 0.5 * a
    stop2 = min(strong, s1) - 0.5 * a
    if stop2 >= stop1:
        stop2 = stop1 - 0.5 * a
    stop3 = max(min(b["l"] for b in bars[-20:]) - 0.25 * a, stop2 - 2.0 * a)
    if stop3 >= stop2:
        stop3 = stop2 - 0.5 * a
    breakout = res[0]["price"] if res else price + a
    rr = (targets[0] - s1) / (s1 - stop1) if s1 > stop1 else None
    return {"atr": a, "s1": s1, "strong": strong, "breakout": breakout, "targets": targets,
            "atr_based": atr_based, "extra": extra, "stops": [stop1, stop2, stop3], "rr": rr,
            "hi52": hi52, "lo52": lo52, "poc": poc}


def find_fvg(bars, lookback=40):
    out = []
    start = max(2, len(bars) - lookback)
    for i in range(start, len(bars)):
        b0, b2 = bars[i - 2], bars[i]
        if b2["l"] > b0["h"]:
            lo, hi, kind = b0["h"], b2["l"], "صاعدة"
            filled = any(bars[j]["l"] <= lo for j in range(i + 1, len(bars)))
        elif b2["h"] < b0["l"]:
            lo, hi, kind = b2["h"], b0["l"], "هابطة"
            filled = any(bars[j]["h"] >= hi for j in range(i + 1, len(bars)))
        else:
            continue
        if not filled:
            out.append({"kind": kind, "lo": lo, "hi": hi, "t": bars[i]["t"]})
    return out[-3:]


def detect_structure(bars):
    recent = bars[-90:]
    sw = swing_points(recent, 2)
    highs = [p for _, p, k in sw if k == "H"]
    lows = [p for _, p, k in sw if k == "L"]
    trend, choch, level = "عرضي", None, None
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            trend = "صاعد (قمم وقيعان أعلى)"
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            trend = "هابط (قمم وقيعان أدنى)"
        last_close = recent[-1]["c"]
        if trend.startswith("هابط") and last_close > highs[-1]:
            choch, level = "صاعد (Bullish CHOCH)", highs[-1]
        elif trend.startswith("صاعد") and last_close < lows[-1]:
            choch, level = "هابط (Bearish CHOCH)", lows[-1]
    return {"trend": trend, "choch": choch, "level": level}


def find_gaps(bars, lookback=30, min_pct=2.0):
    out = []
    for i in range(max(1, len(bars) - lookback), len(bars)):
        pc, o = bars[i - 1]["c"], bars[i]["o"]
        pct = (o - pc) / pc * 100
        if abs(pct) < min_pct:
            continue
        if pct > 0:
            filled = any(bars[j]["l"] <= pc for j in range(i, len(bars)))
        else:
            filled = any(bars[j]["h"] >= pc for j in range(i, len(bars)))
        out.append({"pct": pct, "t": bars[i]["t"], "filled": filled, "level": pc})
    return out[-3:]


def estimate_cvd(bars, n=60):
    win = bars[-n:]
    cvd, series = 0.0, []
    for b in win:
        rng = b["h"] - b["l"]
        delta = b["v"] * (((b["c"] - b["l"]) - (b["h"] - b["c"])) / rng) if rng > 0 else 0
        cvd += delta
        series.append(cvd)
    return {"value": series[-1], "above_zero": series[-1] > 0,
            "slope_up": len(series) > 5 and series[-1] > series[-6]}


def volume_spikes(bars, lookback=20, mult=2.0):
    base = bars[-50:-lookback] or bars[:-lookback]
    avg = sum(b["v"] for b in base) / len(base) if base else 0
    out = []
    for b in bars[-lookback:]:
        if avg and b["v"] > mult * avg:
            out.append({"t": b["t"], "x": b["v"] / avg, "up": b["c"] >= b["o"]})
    return out[-4:]


def sparkline(values):
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return blocks[0] * len(values)
    return "".join(blocks[min(int((v - lo) / (hi - lo) * 7), 7)] for v in values)


def d_str(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def sec_get(url):
    return requests.get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                        timeout=40)


def sec_cik(sym):
    if not CIK_CACHE:
        r = sec_get("https://www.sec.gov/files/company_tickers.json")
        r.raise_for_status()
        for v in r.json().values():
            CIK_CACHE[v["ticker"].upper()] = int(v["cik_str"])
    return CIK_CACHE.get(sym.upper())


def sec_recent(cik):
    r = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r.raise_for_status()
    rec = r.json()["filings"]["recent"]
    n = len(rec["form"])
    items = rec.get("items") or [""] * n
    return [{"form": rec["form"][i], "date": rec["filingDate"][i], "acc": rec["accessionNumber"][i],
             "doc": rec["primaryDocument"][i], "items": items[i]} for i in range(n)]


def sec_url(cik, f, raw=False):
    doc = f["doc"].split("/")[-1] if raw else f["doc"]
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc'].replace('-', '')}/{doc}"


def sec_text(url):
    r = sec_get(url)
    r.raise_for_status()
    raw = r.text
    if raw.lstrip().startswith("<?xml"):
        try:
            return xml_to_text(raw)
        except Exception:
            pass
    return html_to_text(raw)


def recent_enough(date_str, days):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return d >= datetime.now(timezone.utc) - timedelta(days=days)


def pick_dilution_filings(filings):
    off = [f for f in filings if f["form"].startswith(("424B", "S-3", "S-1", "F-3", "F-1"))
           and recent_enough(f["date"], 400)][:2]
    ev = [f for f in filings if f["form"] == "8-K" and recent_enough(f["date"], 400)
          and {"1.01", "3.02", "2.03", "3.03", "1.02"} & set((f["items"] or "").split(","))][:2]
    q = [f for f in filings if f["form"] in ("10-Q", "10-K")][:1]
    return off + ev + q


DIL_KEYWORDS = ("warrant", "convertible", "conversion price", "exercise price", "at-the-market",
                "at the market", "shelf", "offering", "dilut", "going concern", "preferred",
                "senior notes", "term loan", "authorized shares", "registered direct", "private placement")


def relevant_excerpts(text, limit=25000):
    keep = [p for p in text.split("\n") if p.strip() and any(k in p.lower() for k in DIL_KEYWORDS)]
    out = "\n".join(keep) if keep else text
    return out[:limit]


def parse_form4(xml_text):
    root = ET.fromstring(xml_text)
    g = lambda el, path: (el.findtext(path) or "").strip()
    name = g(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    role = ""
    if rel is not None:
        role = g(rel, "officerTitle") or ("عضو مجلس إدارة" if g(rel, "isDirector") in ("1", "true") else "")
        if not role and g(rel, "isTenPercentOwner") in ("1", "true"):
            role = "مالك +10%"
    txs = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = g(t, "transactionCoding/transactionCode")
        sh = to_num(g(t, "transactionAmounts/transactionShares/value"))
        px = to_num(g(t, "transactionAmounts/transactionPricePerShare/value"))
        txs.append({"date": g(t, "transactionDate/value"), "code": code, "shares": sh, "price": px})
    return name, role, txs


def insider_section(cik, filings):
    f4 = [f for f in filings if f["form"] == "4" and recent_enough(f["date"], 120)][:8]
    lines, other = [], 0
    for f in f4:
        try:
            r = sec_get(sec_url(cik, f, raw=True))
            r.raise_for_status()
            name, role, txs = parse_form4(r.text)
        except Exception:
            continue
        for t in txs:
            if t["code"] in ("P", "S") and t["shares"]:
                kind = "🟢 شراء" if t["code"] == "P" else "🔴 بيع"
                val = f" (≈ {fmt_n(t['shares'] * t['price'] * 1.0)}$)" if t["price"] else ""
                lines.append(f"• {t['date']} {kind}: {name}{' - ' + role if role else ''} — "
                             f"{fmt_n(t['shares'])} سهم @ {fmt_p(t['price'])}{val}")
            else:
                other += 1
    if not f4:
        return "لا توجد نماذج Form 4 خلال آخر 120 يوماً."
    if not lines:
        return "لا توجد عمليات شراء أو بيع في السوق المفتوح ضمن آخر النماذج" + (
            f" (يوجد {other} عملية أخرى مثل منح أسهم أو ضرائب)." if other else ".")
    return "\n".join(lines[:8]) + (f"\n(+{other} عملية أخرى غير سوقية)" if other else "")


DIL_SYSTEM = (
    "أنت محلل إفصاحات SEC. اعتماداً على مقتطفات الإفصاحات التالية فقط، استخرج بالعربية:\n"
    "🧾 الوارنتس: العدد وسعر التنفيذ والأجل وتاريخ الإصدار.\n"
    "📜 الأوراق والسندات القابلة للتحويل: المبلغ وسعر التحويل والفائدة والاستحقاق.\n"
    "🏦 الطروحات (ATM / Shelf / طرح مباشر / PIPE): الحجم وسعر الطرح والجهة.\n"
    "📉 السيولة ومخاطر التخفيف: النقد والدين وشكوك الاستمرارية والأسهم المصرح بها.\n"
    "اذكر بعد كل معلومة النموذج وتاريخه بين قوسين، مثل (424B5 بتاريخ 2026-09-01). "
    "إن لم يرد بند فاكتب: لم يرد في الإفصاحات المفحوصة. لا تخمّن ولا تكمل من ذاكرتك. "
    "اختم بسطر: ⚠️ مستوى مخاطر التخفيف: منخفض/متوسط/مرتفع مع جملة تبرير مستندة للنص. "
    "اكتب نقاطاً قصيرة بدون رموز تنسيق مثل ** أو #."
)


def news_section(sym, name):
    system = (
        "أنت محلل أسهم. استخدم البحث في الإنترنت للإجابة بالعربية بإيجاز ودقة مع ذكر التاريخ لكل خبر، "
        "ولا تخترع معلومات؛ إن لم تجد شيئاً فاكتب «لم أجد». اتبع هذا القالب:\n"
        "📰 أهم الأخبار (آخر 30 يوماً)\n"
        "🚀 المحفزات القادمة (Forward Catalysts): أرباح، توجيهات، موافقات، عقود\n"
        "📊 أداء الأرباح: هل تجاوزت الشركة التوقعات (Beat) آخر ربعين، وهل توجيهاتها متحفظة (Sandbagging)\n"
        "🏢 نشاط الشركة في سطرين\n"
        "🌍 الدولة (مقر الشركة)\n"
        "🔁 هل هي مدرجة في بورصات أخرى؟ اذكرها\n"
        "بدون رموز تنسيق مثل ** أو #."
    )
    q = f"السهم الأمريكي {sym}" + (f" ({name})" if name else "")
    return gemini(system, [{"text": q}], 3000, timeout=300, tools=[{"google_search": {}}])


def short_interest_section(rows, float_sh):
    if not rows:
        return "غير متاح (تعذر جلب بيانات NASDAQ)."
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    dtc = cur["dtc"] if cur["dtc"] is not None else (cur["si"] / cur["adv"] if cur["adv"] else None)
    pct = cur["si"] / float_sh * 100 if float_sh else None
    lines = [f"• Short Interest Ratio (أيام التغطية): {dtc:.2f}" if dtc is not None
             else "• Short Interest Ratio: غير متاح",
             f"• Short Interest %: {pct:.2f}% من الأسهم الحرة (Float)" if pct is not None
             else "• Short Interest %: غير متاح (الفلوت غير متوفر)",
             f"• الأسهم المباعة على المكشوف: {fmt_n(cur['si'])} • متوسط الحجم اليومي: {fmt_n(cur['adv'])}",
             f"• تاريخ التسوية: {cur['date']}"]
    if prev and prev["si"]:
        ch = (cur["si"] - prev["si"]) / prev["si"] * 100
        lines.append(f"• التغير عن الفترة السابقة ({prev['date']}): {ch:+.1f}% "
                     f"({'ارتفع' if ch > 0 else 'انخفض'} {fmt_n(abs(cur['si'] - prev['si']))} سهم)")
    lines.append("المصدر: NASDAQ (يصدر مرتين شهرياً، فهو ليس لحظياً). النسبة المئوية = الأسهم المكشوفة ÷ الفلوت.")
    return "\n".join(lines)


def build_plan(lv, price):
    t = lv["targets"]
    mark = lambda i: " (تقديري بـ ATR)" if (i + 1) in lv["atr_based"] else ""
    L = ["🎯 خطة التداول (مستويات فنية محسوبة)",
         f"📍 دخول من الدعم: {fmt_p(lv['s1'])} • الدعم القوي: {fmt_p(lv['strong'])}",
         f"📍 أو اختراق فوق: {fmt_p(lv['breakout'])}",
         f"🎯 هدف أول: {fmt_p(t[0])}{mark(0)}",
         f"🎯 هدف ثاني: {fmt_p(t[1])}{mark(1)}",
         f"🎯 هدف ثالث: {fmt_p(t[2])}{mark(2)}",
         f"🎯 هدف رابع: {fmt_p(t[3])}{mark(3)}",
         f"🟢 وقد يصل إلى: {fmt_p(lv['extra'])}",
         f"⛔ وقف أولي: {fmt_p(lv['stops'][0])} • ثاني: {fmt_p(lv['stops'][1])} • دموي: {fmt_p(lv['stops'][2])}"]
    if lv["rr"]:
        L.append(f"⚖️ العائد/المخاطرة (دعم ← هدف 1 مقابل الوقف الأولي): {lv['rr']:.1f} : 1")
    L.append(f"(ATR(14) = {fmt_p(lv['atr'])} • قمة 52 أسبوع {fmt_p(lv['hi52'])} • قاع {fmt_p(lv['lo52'])})")
    return "\n".join(L)


def build_action(daily, price, cvd, structure, fvg, gaps, spikes):
    L = ["🔍 سلوك السعر (Price Action Audit)",
         f"• الاتجاه: {structure['trend']}"]
    if structure["choch"]:
        L.append(f"• CHOCH: نعم — {structure['choch']} عند {fmt_p(structure['level'])}")
    else:
        L.append("• CHOCH: لا")
    if fvg:
        L.append("• FVG: نعم — " + "، ".join(
            f"{f['kind']} {fmt_p(f['lo'])}–{fmt_p(f['hi'])} ({d_str(f['t'])})" for f in fvg))
    else:
        L.append("• FVG: لا توجد فجوات غير مغلقة حديثة")
    if gaps:
        L.append("• الفجوات السعرية: " + "، ".join(
            f"{g['pct']:+.1f}% في {d_str(g['t'])} ({'مغلقة' if g['filled'] else 'مفتوحة'})" for g in gaps))
    else:
        L.append("• الفجوات السعرية (≥2%): لا توجد خلال 30 يوماً")
    L.append(f"• CVD (تقدير من الشموع) فوق الصفر؟ {'نعم' if cvd['above_zero'] else 'لا'} "
             f"• الميل: {'صاعد' if cvd['slope_up'] else 'هابط'}")
    if spikes:
        L.append("• شموع بحجم ضخم (مؤشر على نشاط الحيتان): " + "، ".join(
            f"{d_str(s['t'])} ({s['x']:.1f}× المتوسط {'↑' if s['up'] else '↓'})" for s in spikes))
    spark = sparkline([b["c"] for b in daily[-30:]])
    L.append(f"• آخر 30 يوماً: {spark}")
    return "\n".join(L)


def analyze_stock(chat_id, mid, exch, sym):
    sym = sym.upper()
    sid = status_start(chat_id, f"⏳ جاري تحليل {sym} (جلب الأسعار)...", mid)
    notes = []
    try:
        tv = daily = intra = None
        meta = {}
        try:
            tv = fetch_tv(sym, exch)
        except Exception as e:
            notes.append(f"TradingView: {short_err(e)}")
        try:
            daily, _ = fetch_yahoo(sym, "1d", "1y")
        except Exception as e:
            notes.append(f"Yahoo (يومي): {short_err(e)}")
        try:
            intra, meta = fetch_yahoo(sym, "5m", "5d", prepost=True)
        except Exception as e:
            notes.append(f"Yahoo (ممتد): {short_err(e)}")
        if not tv and not daily:
            raise Exception(f"تعذر جلب بيانات {sym}. تأكد من الرمز، أو حاول بعد قليل.")

        name = (tv or {}).get("description") or meta.get("longName") or ""
        exch_name = ((tv or {}).get("ticker") or "").split(":")[0] or exch or ""
        reg = meta.get("regularMarketPrice") or (tv or {}).get("close")
        if intra:
            last = intra[-1]
            now_price, label, when = last["c"], session_label(meta, last["t"]), last["t"]
        else:
            now_price, label, when = reg, "غير محددة", None
        if now_price is None and daily:
            now_price = daily[-1]["c"]
        L = [f"📊 تحليل {sym}" + (f" ({exch_name})" if exch_name else "") + (f" — {name}" if name else "")]
        price_line = f"💵 السعر: {fmt_p(now_price)} ({label})"
        if reg and now_price and abs(now_price - reg) > 1e-9:
            price_line += f" • إغلاق/سعر الجلسة العادية {fmt_p(reg)} ({fmt_pct((now_price - reg) / reg * 100, True)} ممتد)"
        if tv and tv.get("change") is not None:
            price_line += f" • تغير اليوم {fmt_pct(to_num(tv['change']), True)}"
        L.append(price_line)
        if when:
            L.append(f"🕒 آخر تحديث للبيانات: {datetime.fromtimestamp(when, tz=timezone.utc):%Y-%m-%d %H:%M} UTC (قد يتأخر دقائق)")
        info = []
        if tv:
            if tv.get("volume") is not None:
                info.append(f"الحجم {fmt_n(to_num(tv['volume']))}")
            if tv.get("market_cap_basic"):
                info.append(f"القيمة السوقية {fmt_n(to_num(tv['market_cap_basic']))}$")
            if tv.get("float_shares_outstanding"):
                info.append(f"الفلوت {fmt_n(to_num(tv['float_shares_outstanding']))}")
            if tv.get("beta_1_year") is not None:
                info.append(f"بيتا {to_num(tv['beta_1_year']):.2f}")
            if tv.get("earnings_release_next_date"):
                info.append("الأرباح القادمة " + d_str(int(tv["earnings_release_next_date"])))
            if tv.get("country"):
                info.append(f"الدولة {tv['country']}")
            if tv.get("sector"):
                info.append(f"القطاع {tv['sector']}")
        if info:
            L.append("ℹ️ " + " • ".join(info))

        if daily and len(daily) >= 30 and now_price:
            status_update(chat_id, sid, f"⏳ جاري حساب المستويات والمؤشرات ({sym})...")
            lv = compute_levels(daily, now_price)
            L += ["", build_plan(lv, now_price), "",
                  build_action(daily, now_price, estimate_cvd(daily), detect_structure(daily),
                               find_fvg(daily), find_gaps(daily), volume_spikes(daily))]
        else:
            L += ["", "🎯 خطة التداول: غير متاحة (لا توجد شموع كافية)."]
        L += ["", "🩳 Short Interest (المصدر: NASDAQ)"]
        
        status_update(chat_id, sid, f"⏳ جاري جلب Short Interest وإفصاحات SEC ({sym})...")
        try:
            rows = fetch_nasdaq_short(sym)
        except Exception as e:
            rows = []
            notes.append(f"NASDAQ: {short_err(e)}")
        L.append(short_interest_section(rows, to_num((tv or {}).get("float_shares_outstanding"))))

        dil_text, insider_text, cik = None, None, None
        try:
            cik = sec_cik(sym)
            if cik:
                filings = sec_recent(cik)
                docs = []
                for f in pick_dilution_filings(filings):
                    try:
                        docs.append(f"=== {f['form']} بتاريخ {f['date']} ===\n"
                                    + relevant_excerpts(sec_text(sec_url(cik, f))))
                    except Exception as e:
                        notes.append(f"SEC {f['form']}: {short_err(e)}")
                if docs:
                    status_update(chat_id, sid, f"⏳ جاري تحليل الوارنتس والتخفيف ({sym})...")
                    dil_text = gemini(DIL_SYSTEM, [{"text": "\n\n".join(docs)[:200000]}], 4000)
                insider_text = insider_section(cik, filings)
            else:
                notes.append("SEC: الرمز غير موجود في قائمة الشركات")
        except Exception as e:
            if isinstance(e, QuotaError):
                raise
            notes.append(f"SEC: {short_err(e)}")
        L += ["", "📑 الوارنتس والإصدارات والتخفيف (من إفصاحات SEC)",
              dil_text or "غير متاح (لم تتوفر إفصاحات كافية)."]
        L += ["", "👔 رادار صفقات المطلعين (Form 4)", insider_text or "غير متاح."]

        status_update(chat_id, sid, f"⏳ جاري البحث عن الأخبار والمحفزات ({sym})...")
        try:
            L += ["", "📰 الأخبار والمحفزات ونبذة عن الشركة (بحث Google)", news_section(sym, name)]
        except QuotaError:
            raise
        except Exception as e:
            notes.append(f"الأخبار: {short_err(e)}")
            L += ["", "📰 الأخبار والمحفزات: غير متاحة حالياً."]

        L += ["", f"🚫 غير متاح لأنه يتطلب بيانات مدفوعة: {UNAVAILABLE}."]
        if notes:
            L += ["", "ملاحظات المصادر: " + " | ".join(notes)]
        L += ["", DISCLAIMER]
        report = "\n".join(L)
    finally:
        status_end(chat_id, sid)
    remember(f"تحليل {sym}", report)
    send(chat_id, report, mid, markup=keyboard(False))


def parse_tradingview(url):
    p = urlparse(url)
    q = parse_qs(p.query).get("symbol")
    if q:
        sym = unquote(q[0])
        if ":" in sym:
            e, s = sym.split(":", 1)
            return e, s
        return None, sym
    m = re.search(r"/symbols/([A-Za-z0-9_]+)-([A-Za-z0-9._]+)", p.path)
    if m:
        return m.group(1), m.group(2)
    return None, None


# ---------- الصور وتحليل الشارتات (Live Vision) ----------
CHART_VISION_SYSTEM = (
    "أنت محلل فني محترف لأسواق الأسهم. قمت بتلقي صورة شارت (مثل TradingView) أو لقطة شاشة سهم. "
    "قم باستخراج ومعالجة تفاصيل الشارت بدقة وفق النقاط التالية:\n"
    "1. 🏷️ اسم أو رمز السهم والمنصة الظاهرة في الشارت.\n"
    "2. 💵 السعر الحالي ومستويات الدعم (Support) والمقاومة (Resistance) المحددة على الشارت.\n"
    "3. 📈 المؤشرات التقنية الظاهرة (مثل EMA, VWAP, الحجم/Volume) وقراءاتها الحالية.\n"
    "4. 🔍 قراءة حركة السعر (Price Action) والاتجاه العام بناءً على الشارت المرئي.\n"
    "5. 🎯 تقييم سريع وتحليل فني مبني على ما تراه في الصورة مع تنبيه المخاطر.\n"
    "أجب باللغة العربية الفصحى بشكل منظم وبدون استخدام رموز التنسيق مثل ** أو #."
)

IMG_SYSTEM = (
    "أنت مترجم محترف ومتخصص في قراءة النصوص من الصور. اقرأ كل النص الظاهر في الصورة بدقة "
    "(عناوين وفقرات وأزرار وجداول وأرقام)، ثم ترجمه إلى العربية الفصحى بأسلوب طبيعي مع الحفاظ على "
    "ترتيبه وتنسيقه. اترك الأسماء والأرقام والرموز كما هي. أعد الترجمة فقط بدون شرح. "
    "إذا كان النص عربياً أصلاً فاكتبه كما هو. إذا لم يوجد نص في الصورة فصفها بإيجاز بالعربية."
)

IMG_SUMMARY_SYSTEM = (
    "اقرأ كل النص الظاهر في الصورة بدقة ثم لخّصه بالعربية بشكل منظم: "
    "📌 الموضوع، 📋 أهم النقاط (مع الأرقام والأسماء والتواريخ كما وردت)، ✅ الخلاصة. "
    "لا تضف معلومات غير موجودة. لا تستخدم رموز التنسيق مثل ** أو #. "
    "إذا لم يوجد نص فصف الصورة بإيجاز بالعربية."
)


def wants_summary(caption):
    c = (caption or "").lower()
    return any(k in c for k in ("تلخيص", "لخص", "لخّص", "summar", "تحليل", "شارت", "chart"))


def get_image(msg):
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], "image/jpeg"
    d = msg.get("document")
    if d:
        name = d.get("file_name", "")
        mt = d.get("mime_type", "") or guess_mime(name, "")
        if mt.startswith("image/"):
            return d["file_id"], mt
    return None


def process_visual(chat_id, mid, data, mime, title, caption=""):
    import base64
    sid = status_start(chat_id, "⏳ جاري قراءة وتحليل الشارت/الصورة بالذكاء الاصطناعي...", mid)
    try:
        is_chart = wants_summary(caption) or "صورة" not in title
        system = CHART_VISION_SYSTEM if is_chart else (IMG_SUMMARY_SYSTEM if wants_summary(caption) else IMG_SYSTEM)
        
        parts = [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
            {"text": "قم بتحليل هذا الشارت/الصورة المالية واستخرج كافة البيانات بدقة." if is_chart else "اقرأ النص في هذا الملف."},
        ]
        result = gemini(system, parts, 16000, timeout=600)
    finally:
        status_end(chat_id, sid)
    remember(title, result)
    send(chat_id, result, mid, markup=keyboard(False))


# ---------- معالجة الرسائل ----------
def handle(msg):
    chat_id = msg["chat"]["id"]
    if str(chat_id) != CHAT_ID:
        return
    mid = msg["message_id"]
    text = msg.get("text")
    doc = msg.get("document")

    try:
        media = get_media(msg)
        if media:
            file_id, name, size = media
            if size > TG_DOWNLOAD_LIMIT:
                send(chat_id, "⚠️ حد تيليغرام للبوتات 20 ميجا. أرسل رابط يوتيوب أو رابطاً مباشراً للملف.", mid)
                return
            data = download(file_id)
            if not data:
                send(chat_id, "⚠️ تعذر تنزيل الملف (الحد 20 ميجا). أرسل رابطاً بدلاً منه.", mid)
                return
            process_media(chat_id, mid, data, name, name)

        elif get_image(msg):
            file_id, mime = get_image(msg)
            data = download(file_id)
            if not data:
                send(chat_id, "⚠️ تعذر تنزيل الصورة.", mid)
                return
            process_visual(chat_id, mid, data, mime, "شارت سهم", msg.get("caption") or "")

        elif text:
            if text.startswith("/start") or text.startswith("/help"):
                send(chat_id, HELP)
            elif text.startswith("/new"):
                STATE["mode"] = None
                send(chat_id, "✅ رجعنا للترجمة العادية. أرسل أي نص لأترجمه.", mid)
            elif text.startswith("/ask"):
                q = text[4:].strip()
                if not LAST["text"]:
                    no_doc(chat_id, mid)
                elif not q:
                    STATE["mode"] = "ask"
                    send(chat_id, "💬 اكتب سؤالك عن المستند. أرسل /new للعودة للترجمة العادية.", mid)
                else:
                    STATE["mode"] = "ask"
                    send(chat_id, answer_question(q), mid)
            elif text.startswith("/sum"):
                body = text[4:].strip()
                if not body:
                    send(chat_id, "اكتب النص بعد الأمر: /sum النص هنا", mid)
                else:
                    remember("نص مرسل", body)
                    send(chat_id, summarize(body), mid, markup=keyboard())
            elif not URL_RE.search(text) and SYMBOL_RE.match(text):
                m = SYMBOL_RE.match(text)
                analyze_stock(chat_id, mid, m.group(1), m.group(2))
            elif URL_RE.search(text):
                url = URL_RE.search(text).group(0)
                want_download = "تحميل" in text or "download" in text.lower()
                if "tradingview.com" in url.lower():
                    e, sy = parse_tradingview(url)
                    if sy:
                        analyze_stock(chat_id, mid, e, sy)
                    else:
                        send(chat_id, "⚠️ هذا الرابط لا يحتوي على رمز السهم. أرسل: تحليل UPLD", mid)
                elif want_download and (is_video_site(url) or YT_RE.search(url)):
                    process_video_download(chat_id, mid, url)
                elif YT_RE.search(url):
                    process_youtube(chat_id, mid, url)
                elif is_video_site(url):
                    sid = status_start(chat_id, "⏳ جاري تحميل الصوت من الرابط...", mid)
                    data, fname = download_with_ytdlp(url)
                    status_end(chat_id, sid)
                    process_media(chat_id, mid, data, fname, url)
                elif is_media_url(url):
                    sid = status_start(chat_id, "⏳ جاري تحميل الملف من الرابط...", mid)
                    data = download_media_url(url)
                    status_end(chat_id, sid)
                    process_media(chat_id, mid, data, os.path.basename(urlparse(url).path), url)
                else:
                    send(chat_id, "⏳ جاري فتح الرابط...", mid)
                    content = fetch_url(url)
                    if not content.strip():
                        send(chat_id, "⚠️ لم أجد نصاً في الرابط.", mid)
                    else:
                        remember(url, content)
                        if "ترجمة" in text or "translate" in text.lower():
                            send(chat_id, translate_limited(content), mid, markup=keyboard(False))
                        else:
                            send(chat_id, summarize(content, sec="sec.gov" in url), mid, markup=keyboard())
            elif STATE["mode"] == "ask" and LAST["text"]:
                send(chat_id, answer_question(text), mid)
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
                send(chat_id, "⚠️ الصيغة غير مدعومة. المدعوم: PDF, DOCX, TXT وملفات الصوت والفيديو.", mid)
                return
            if not content.strip():
                if name.lower().endswith(".pdf"):
                    process_visual(chat_id, mid, data, "application/pdf", name, msg.get("caption") or "")
                else:
                    send(chat_id, "⚠️ لم أجد نصاً داخل الملف.", mid)
                return
            remember(name, content)
            caption = (msg.get("caption") or "").lower()
            if "ترجمة" in caption or "translate" in caption:
                send(chat_id, translate_limited(content), mid, markup=keyboard(False))
            else:
                send(chat_id, summarize(content), mid, markup=keyboard())

        else:
            send(chat_id, "أرسل نصاً أو صورة أو ملفاً (PDF / Word / TXT) أو رسالة صوتية أو فيديو.", mid)

    except Exception as e:
        print(traceback.format_exc())
        send(chat_id, friendly_error(e), mid)


def translate_limited(content):
    out = translate(content[:TRANSLATE_LIMIT])
    if len(content) > TRANSLATE_LIMIT:
        out += f"\n\n⚠️ تُرجم أول {TRANSLATE_LIMIT} حرفاً فقط من المستند."
    return out


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if str(chat_id) != CHAT_ID:
        return
    action = cb.get("data")
    try:
        if not LAST["text"]:
            no_doc(chat_id)
        elif action == "ask":
            STATE["mode"] = "ask"
            send(chat_id, "💬 اكتب سؤالك عن المستند وسأجيب منه. أرسل /new للعودة للترجمة العادية.")
        elif action == "trans":
            send(chat_id, "⏳ جاري الترجمة الكاملة...")
            send(chat_id, translate_limited(LAST["text"]))
        elif action == "nums":
            send(chat_id, "⏳ جاري استخراج الأرقام والتواريخ...")
            send(chat_id, key_facts())
    except Exception as e:
        print(traceback.format_exc())
        send(chat_id, friendly_error(e))


# ---------- التشغيل ----------
def main():
    end = time.time() + RUN_SECONDS
    offset = None
    while time.time() < end:
        params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
        if offset:
            params["offset"] = offset
        res = tg("getUpdates", **params)
        if not res.get("ok", True) or "result" not in res:
            time.sleep(5)
            continue
        for upd in res["result"]:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    handle(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception:
                print(traceback.format_exc())
    if offset:
        tg("getUpdates", offset=offset, timeout=0)


if __name__ == "__main__":
    main()
