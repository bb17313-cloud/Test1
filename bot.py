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
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
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
    "🎙 أرسل رسالة صوتية أو ملف صوت ← أفرّغه وأترجمه.\n"
    "🎬 أرسل فيديو قصير ← أفرّغ كلامه وأترجمه.\n"
    "▶️ أرسل رابط يوتيوب، أو رابط فيديو من (X / تيك توك / فيسبوك / إنستغرام / فيميو...)، أو رابط ملف صوت/فيديو مباشر ← أفرّغه وأترجمه.\n\n"
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
    """تحميل ملف صوت/فيديو من رابط مباشر مع حد أقصى للحجم."""
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
    """تحميل الصوت من مواقع الفيديو بأداة yt-dlp. يرجع (bytes, اسم الملف)."""
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


# ---------- Gemini ----------
def gemini(system, parts, max_tokens=2000, timeout=300):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    data, r = {}, None
    for attempt in range(4):
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code in (429, 500, 503) and attempt < 3:
            time.sleep(15 * (attempt + 1))  # انتظار ثم إعادة المحاولة
            continue
        break
    if r.status_code != 200:
        raise Exception(data.get("error", {}).get("message", "Gemini error"))
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
    """تقسيم النص عند نهايات الفقرات حتى لا تنقطع الجمل."""
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
    """تحويل الصوت (أو صوت الفيديو) إلى mp3 مضغوط وتقسيمه إلى أجزاء. يرجع قائمة (bytes, mime)."""
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
    """يرجع (file_id, name, size) إن كانت الرسالة صوتاً أو فيديو."""
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


# ---------- معالجة الرسائل ----------
def handle(msg):
    chat_id = msg["chat"]["id"]
    if str(chat_id) != CHAT_ID:
        return  # تجاهل أي محادثة غير المحددة في CHAT_ID
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
            elif URL_RE.search(text):
                url = URL_RE.search(text).group(0)
                if YT_RE.search(url):
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
                send(chat_id, "⚠️ لم أجد نصاً داخل الملف (قد يكون صورة ممسوحة).", mid)
                return
            remember(name, content)
            caption = (msg.get("caption") or "").lower()
            if "ترجمة" in caption or "translate" in caption:
                send(chat_id, translate_limited(content), mid, markup=keyboard(False))
            else:
                send(chat_id, summarize(content), mid, markup=keyboard())

        else:
            send(chat_id, "أرسل نصاً أو ملفاً (PDF / Word / TXT) أو رسالة صوتية أو فيديو.", mid)

    except Exception as e:
        print(traceback.format_exc())
        send(chat_id, f"❌ حدث خطأ: {str(e)[:300]}", mid)


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
        send(chat_id, f"❌ حدث خطأ: {str(e)[:300]}")


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
    # تأكيد الرسائل المعالجة حتى لا تتكرر في التشغيل القادم
    if offset:
        tg("getUpdates", offset=offset, timeout=0)


if __name__ == "__main__":
    main()
