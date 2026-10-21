# -*- coding: utf-8 -*-
"""
بوت تلغرام: ترجمة / ملخص / تنزيل وتفريغ فيديو وصوت / تحليل أسهم / قراءة ملفات SEC / بحث ويكيبيديا
المحرك: مواقع Gemini و Grok و Wikipedia عبر المتصفح (Playwright) بدون API وبدون أي سر (دخول زائر).
(GEMINI_STATE اختياري: إن وُجد يُستخدم حساب Gemini المسجّل لحدود أوسع).

أوضاع التشغيل:
  python bot.py         -> تشغيل البوت (على GitHub Actions)
  python bot.py login     -> تسجيل دخول Gemini مرة واحدة على جهازك وتوليد الجلسة

المطلوب فقط: BOT_TOKEN, CHAT_ID (تلغرام) + SEC_USER_AGENT كنص عادي في bot.yml
"""
import os
import re
import sys
import json
import time
import glob
import gzip
import base64
import shutil
import tempfile
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, quote_plus

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("CHAT_ID", ""))
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:5000/ask")  # رابط الجسر المحلي الخاص بك
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "1800"))
BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "420"))
SEC_UA = os.environ.get("SEC_USER_AGENT") or "StockBot admin@example.com"
PROVIDERS = [x.strip() for x in (os.environ.get("PROVIDERS") or "gemini,grok").split(",") if x.strip()]
GROK_URL = "https://grok.com"
WIKI_URL = "https://ar.wikipedia.org"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
STATE_FILE = "gemini_state.json"
GEMINI_URL = "https://gemini.google.com/app"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BROWSER_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]

TRANSLATE_CHUNK = 8000
SUMMARY_CHUNK = 30000
SEC_CHUNK = 25000
SEC_MAX_CHUNKS = 6

# ============================================================
# 1) جسر المتصفح (Gemini, Grok, Wikipedia عبر Playwright)
# ============================================================


class SessionExpired(Exception):
    pass


def _convert_cookies(arr):
    """يحوّل تصدير Cookie-Editor (من الجوال) إلى صيغة Playwright."""
    ss = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None"}
    out = []
    for c in arr:
        d = c.get("domain", "")
        if "google" not in d:
            continue
        exp = c.get("expirationDate") or c.get("expires")
        out.append({
            "name": c["name"], "value": c["value"], "domain": d,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure")),
            "sameSite": ss.get(str(c.get("sameSite", "")).lower(), "Lax"),
            "expires": float(exp) if exp and not c.get("session") else -1,
        })
    return {"cookies": out, "origins": []}


def _parse_state(raw):
    raw = raw.strip()
    if raw.startswith("["):
        return _convert_cookies(json.loads(raw))
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(gzip.decompress(base64.b64decode(raw)).decode("utf-8"))


def _prepare_state():
    raw = os.environ.get("GEMINI_STATE", "").strip()
    if raw:
        path = os.path.join(tempfile.gettempdir(), "gemini_state_runtime.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_parse_state(raw), f)
        return path
    if os.path.exists(STATE_FILE):
        return STATE_FILE
    return None  # وضع الزائر: بدون جلسة


class GeminiBrowser:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.gpage = None
        self.wpage = None

    def start(self):
        from playwright.sync_api import sync_playwright
        state = _prepare_state()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        kw = dict(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="en-US")
        if state:
            kw["storage_state"] = state
        self.ctx = self.browser.new_context(**kw)
        self.page = self.ctx.new_page()

    def _attach(self, path):
        pg = self.page
        try:
            with pg.expect_file_chooser(timeout=8000) as fc:
                pg.locator("button[aria-label*='upload' i], button[aria-label*='Open upload' i]").first.click(timeout=5000)
                pg.locator("[role='menuitem']:has-text('Upload files'), button:has-text('Upload files')").first.click(timeout=5000)
            fc.value.set_files(path)
        except Exception:
            pg.set_input_files("input[type='file']", path)
        pg.wait_for_timeout(5000)

    def ask(self, prompt, image_path=None):
        errors = []
        for prov in PROVIDERS:
            try:
                if prov == "gemini":
                    return self._ask_gemini(prompt, image_path)
                if prov == "grok":
                    return self._ask_grok(prompt, image_path)
                if prov == "wiki":
                    return self._ask_wiki(prompt)
            except Exception as e:
                errors.append(f"{prov}: {str(e)[:120]}")
        raise RuntimeError(" | ".join(errors) or "لا يوجد مزوّد مفعّل")

    def _ask_wiki(self, prompt, timeout=60):
        """متصفح ويكيبيديا: يبحث في ويكيبيديا العربية ويستخرج الملخص من الصفحات."""
        if self.wpage is None:
            self.wpage = self.ctx.new_page()
        pg = self.wpage
        
        # تنظيف الاستعلام أو استخدام محرك البحث الداخلي لويكيبيديا
        search_url = f"{WIKI_URL}/w/index.php?search={quote_plus(prompt)}"
        pg.goto(search_url, wait_until="domcontentloaded", timeout=timeout*1000)
        pg.wait_for_timeout(2000)
        
        # إذا وجدنا مقالة مباشرة أو نتائج بحث
        content_sel = "div.mw-parser-output p, div.searchresults"
        try:
            pg.locator(content_sel).first.wait_for(state="visible", timeout=10000)
        except Exception:
            pass
            
        # محاولة استخراج النصوص الأولى للمقالة أو نتائج البحث
        paragraphs = pg.locator("div.mw-parser-output > p").all_inner_texts()
        if not paragraphs:
            paragraphs = pg.locator(".searchresult, .mw-search-result-heading").all_inner_texts()
            
        if paragraphs:
            result_text = "\n".join([p.strip() for p in paragraphs[:5] if p.strip()])
            if result_text:
                return f"📖 **من ويكيبيديا:**\n\n{result_text[:3500]}"
                
        return "⚠️ لم أجد نتائج مطابقة في ويكيبيديا لهذا الطلب."

    def _ask_grok(self, prompt, image_path=None, timeout=360):
        if self.gpage is None:
            self.gpage = self.ctx.new_page()
        pg = self.gpage
        fresh = False
        if pg.url.startswith(GROK_URL):
            try:
                pg.locator("a[href='/'], button[aria-label*='New chat' i]").first.click(timeout=3000)
                pg.wait_for_timeout(500)
                fresh = True
            except Exception:
                pass
        if not fresh:
            pg.goto(GROK_URL, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(4000)
        if "just a moment" in (pg.title() or "").lower():
            pg.wait_for_timeout(10000)
            if "just a moment" in (pg.title() or "").lower():
                raise RuntimeError("Cloudflare حجب الدخول إلى Grok")
        box = pg.locator("textarea, div[contenteditable='true']").first
        box.wait_for(state="visible", timeout=30000)
        if image_path:
            pg.set_input_files("input[type='file']", image_path)
            pg.wait_for_timeout(5000)
        sel = ".response-content-markdown"
        before = pg.locator(sel).count()
        box.click()
        box.fill(prompt)
        pg.wait_for_timeout(800)
        try:
            pg.locator("button[type='submit'], button[aria-label*='Submit' i], button[aria-label*='Send' i]").first.click(timeout=5000)
        except Exception:
            pg.keyboard.press("Enter")
        deadline = time.time() + timeout
        last, stable = "", 0
        while time.time() < deadline:
            pg.wait_for_timeout(1000)
            if pg.locator(sel).count() <= before:
                continue
            txt = pg.locator(sel).last.inner_text().strip()
            generating = pg.locator("button[aria-label*='Stop' i]").count() > 0
            if txt and txt == last and not generating:
                stable += 1
            else:
                stable = 0
            last = txt
            if stable >= 2:
                return txt
        raise TimeoutError("انتهت مهلة انتظار رد Grok")

    def _ask_gemini(self, prompt, image_path=None, timeout=360):
        pg = self.page
        fresh = False
        if pg.url.startswith(GEMINI_URL):
            try:
                pg.locator("a[aria-label*='New chat' i], button[aria-label*='New chat' i]").first.click(timeout=3000)
                pg.wait_for_timeout(500)
                fresh = True
            except Exception:
                pass
        if not fresh:
            pg.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
        if "consent.google" in pg.url:
            try:
                pg.locator("button:has-text('Reject all'), button:has-text('Accept all')").first.click(timeout=5000)
                pg.wait_for_timeout(3000)
            except Exception:
                pass
        box = pg.locator("rich-textarea div.ql-editor, div.ql-editor[contenteditable='true']").first
        try:
            box.wait_for(state="visible", timeout=30000)
        except Exception:
            if "accounts.google" in pg.url:
                raise SessionExpired()
            raise
        if "accounts.google" in pg.url:
            raise SessionExpired()

        if image_path:
            self._attach(image_path)

        before = pg.locator("message-content").count()
        box.click()
        box.fill(prompt)
        pg.wait_for_timeout(800)
        try:
            pg.locator("button.send-button").first.click(timeout=5000)
        except Exception:
            pg.keyboard.press("Enter")

        deadline = time.time() + timeout
        last, stable = "", 0
        while time.time() < deadline:
            pg.wait_for_timeout(1000)
            if pg.locator("message-content").count() <= before:
                continue
            txt = pg.locator("message-content").last.inner_text().strip()
            generating = pg.locator("button.send-button.stop, button[aria-label*='Stop' i]").count() > 0
            if txt and txt == last and not generating:
                stable += 1
            else:
                stable = 0
            last = txt
            if stable >= 2:
                return txt
        raise TimeoutError("انتهت مهلة انتظار رد Gemini")


_BROWSER = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            res = _BROWSER.ask(data.get("prompt", ""), data.get("image_path"))
            self._reply({"status": "success", "result": res})
        except SessionExpired:
            self._reply({"status": "error", "message": "Gemini طلب تسجيل دخول (وضع الزائر غير متاح لك)"})
        except Exception as e:
            self._reply({"status": "error", "message": str(e)[:300]})


def start_bridge():
    """يشغّل الجسر داخل خيط مستقل إذا كان BRIDGE_URL محلياً."""
    global _BROWSER
    u = urlparse(BRIDGE_URL)
    if u.hostname not in ("localhost", "127.0.0.1"):
        return True
    ready, err = threading.Event(), []

    def run():
        global _BROWSER
        try:
            _BROWSER = GeminiBrowser()
            _BROWSER.start()
            srv = HTTPServer(("127.0.0.1", u.port or 5000), _Handler)
        except Exception as e:
            err.append(str(e))
            ready.set()
            return
        ready.set()
        srv.serve_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(120)
    if err:
        print("Bridge start error:", err[0])
        return False
    return True


def login_mode():
    """تسجيل دخول Gemini مرة واحدة على جهازك وتوليد الجلسة."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False, args=BROWSER_ARGS,
                                        ignore_default_args=["--enable-automation"])
        except Exception:
            browser = p.chromium.launch(headless=False, args=BROWSER_ARGS,
                                        ignore_default_args=["--enable-automation"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="en-US")
        page = ctx.new_page()
        page.goto(GEMINI_URL)
        input("سجّل دخولك في Gemini داخل النافذة، وعندما تظهر صفحة المحادثة اضغط Enter هنا... ")
        st = ctx.storage_state()
        st["cookies"] = [c for c in st["cookies"] if "google" in c.get("domain", "")]
        st["origins"] = []
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        b64 = base64.b64encode(gzip.compress(json.dumps(st).encode("utf-8"))).decode()
        with open("gemini_state.b64.txt", "w") as f:
            f.write(b64)
        print("تم. انسخ محتوى الملف gemini_state.b64.txt كاملاً إلى سر GitHub باسم GEMINI_STATE")
        browser.close()


# ============================================================
# 2) تلغرام
# ============================================================


def send_message(chat_id, text, reply_to=None):
    try:
        requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to
        }, timeout=30)
    except Exception as e:
        print("Telegram send error:", e)


def send_long(chat_id, text, reply_to=None, limit=3900):
    text = (text or "").strip() or "⚠️ لا يوجد نص."
    while text:
        if len(text) <= limit:
            send_message(chat_id, text, reply_to)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        send_message(chat_id, text[:cut], reply_to)
        text = text[cut:].lstrip("\n")


def send_file(chat_id, path, kind="document", caption="", reply_to=None):
    ep = {"video": ("sendVideo", "video"), "audio": ("sendAudio", "audio"),
          "document": ("sendDocument", "document")}[kind]
    try:
        with open(path, "rb") as f:
            requests.post(f"{API}/{ep[0]}", data={"chat_id": chat_id, "caption": caption[:900],
                                                 "reply_to_message_id": reply_to or ""},
                        files={ep[1]: f}, timeout=300)
    except Exception as e:
        print("Telegram file error:", e)


def tg_download(file_id, dest_dir, name=None):
    """تنزيل ملف من تلغرام (الحد 20MB). يرجع المسار أو None."""
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30).json()
        if not r.get("ok"):
            return None
        fp = r["result"]["file_path"]
        dest = os.path.join(dest_dir, name or os.path.basename(fp))
        data = requests.get(f"{FILE_API}/{fp}", timeout=180)
        with open(dest, "wb") as f:
            f.write(data.content)
        return dest
    except Exception as e:
        print("tg_download error:", e)
        return None


def process_via_bridge(prompt_text, image_path=None):
    try:
        payload = {"prompt": prompt_text}
        if image_path:
            payload["image_path"] = image_path
        r = requests.post(BRIDGE_URL, json=payload, timeout=BRIDGE_TIMEOUT)
        res = r.json()
        if res.get("status") == "success":
            return res.get("result", "⚠️ تم التوليد بنجاح ولكن النص فارغ.")
        else:
            return f"❌ خطأ من الجسر: {res.get('message')}"
    except Exception as e:
        return f"❌ تعذر الاتصال بالجسر المحلي: {str(e)}"


def is_err(s):
    return isinstance(s, str) and s.startswith("❌")


# ============================================================
# 3) أدوات النصوص: تقسيم / ترجمة / ملخص
# ============================================================


def chunk_text(text, size):
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        while len(para) > size:
            parts.append(para[:size])
            para = para[size:]
        cur += para + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def is_arabic(t):
    ar = len(re.findall(r"[\u0600-\u06FF]", t))
    al = len(re.findall(r"\w", t))
    return al > 0 and ar / al > 0.5


def translate_to_arabic(text):
    out = []
    for ch in chunk_text(text, TRANSLATE_CHUNK):
        ans = process_via_bridge(
            "ترجم النص التالي إلى العربية الفصحى بدقة وأمانة دون إضافة أو حذف، "
            "وأعد الترجمة فقط بدون مقدمات:\n\n" + ch)
        if is_err(ans):
            return ans
        out.append(ans)
    return "\n\n".join(out)


def summarize_text(text):
    chunks = chunk_text(text, SUMMARY_CHUNK)
    if len(chunks) == 1:
        return process_via_bridge(
            "لخّص النص التالي باللغة العربية في نقاط واضحة تشمل الأفكار الرئيسية والأرقام والتواريخ "
            "المهمة، ثم خلاصة في سطرين:\n\n" + text)
    partial = []
    for i, ch in enumerate(chunks, 1):
        ans = process_via_bridge(f"لخّص الجزء {i} من {len(chunks)} بالعربية مع الاحتفاظ بالأرقام والتواريخ:\n\n" + ch)
        if is_err(ans):
            return ans
        partial.append(ans)
    return process_via_bridge(
        "هذه ملخصات أجزاء مستند واحد. ادمجها في ملخص عربي واحد منظم بنقاط، ثم خلاصة في سطرين:\n\n"
        + "\n\n".join(partial))


# ============================================================
# 4) الفيديو والصوت: yt-dlp + faster-whisper
# ============================================================

MEDIA_DOMAINS = ("youtube.com", "youtu.be", "x.com", "twitter.com", "tiktok.com", "instagram.com",
                 "facebook.com", "fb.watch", "vimeo.com", "twitch.tv", "soundcloud.com", "dailymotion.com")
_whisper = None


def _run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def download_media(url, workdir, want_video, want_audio):
    video = audio = None
    base = os.path.join(workdir, "%(title).60s.%(ext)s")
    if want_video:
        _run(["yt-dlp", "--no-playlist", "--restrict-filenames", "-f",
              "bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4",
              "--max-filesize", "49M", "-o", base, url])
        vids = [p for p in glob.glob(os.path.join(workdir, "*.mp4"))]
        video = vids[0] if vids else None
    if want_audio:
        _run(["yt-dlp", "--no-playlist", "--restrict-filenames", "-x", "--audio-format", "mp3",
              "--audio-quality", "5", "-o", base, url])
        auds = glob.glob(os.path.join(workdir, "*.mp3"))
        audio = auds[0] if auds else None
    return video, audio


def transcribe(path):
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = _whisper.transcribe(path, vad_filter=True, beam_size=1)
    text = "\n".join(s.text.strip() for s in segments)
    return text.strip(), info.language


def transcript_flow(chat_id, mid, audio_path):
    send_message(chat_id, "🎙️ جاري التفريغ...", mid)
    text, lang = transcribe(audio_path)
    if not text:
        send_message(chat_id, "⚠️ لم أستطع استخراج كلام من الملف.", mid)
        return
    d = os.path.dirname(audio_path)
    orig = os.path.join(d, "transcript_original.txt")
    with open(orig, "w", encoding="utf-8") as f:
        f.write(text)
    send_file(chat_id, orig, "document", f"📝 التفريغ الأصلي (اللغة: {lang})", mid)
    if lang == "ar":
        return
    send_message(chat_id, "🌐 جاري الترجمة للعربية...", mid)
    ar = translate_to_arabic(text)
    if is_err(ar):
        send_message(chat_id, ar, mid)
        return
    arp = os.path.join(d, "transcript_arabic.txt")
    with open(arp, "w", encoding="utf-8") as f:
        f.write(ar)
    send_file(chat_id, arp, "document", "📝 التفريغ مترجماً للعربية", mid)
    send_long(chat_id, ar[:3800], mid)


def media_flow(chat_id, mid, url, mode="all"):
    wd = tempfile.mkdtemp(prefix="media_")
    try:
        send_message(chat_id, "⬇️ جاري التنزيل...", mid)
        want_video = mode in ("all", "video")
        want_audio = mode in ("all", "audio", "transcript")
        video, audio = download_media(url, wd, want_video, want_audio)
        if not video and not audio:
            send_message(chat_id, "❌ تعذر تنزيل الرابط (قد يكون خاصاً أو غير مدعوم).", mid)
            return
        if video and os.path.getsize(video) <= 49 * 1024 * 1024:
            send_file(chat_id, video, "video", "🎬 الفيديو", mid)
        elif want_video:
            send_message(chat_id, "⚠️ الفيديو أكبر من حد تلغرام (50MB) فلم يُرسل.", mid)
        if audio and mode in ("all", "audio"):
            if os.path.getsize(audio) <= 49 * 1024 * 1024:
                send_file(chat_id, audio, "audio", "🎧 الصوت", mid)
        if audio and mode in ("all", "transcript"):
            transcript_flow(chat_id, mid, audio)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ============================================================
# 5) SEC EDGAR (بدون API key - يلزم User-Agent)
# ============================================================

_sec_tickers = None
DILUTION_PREFIX = ("424B", "S-1", "S-3", "F-1", "F-3", "8-K", "6-K")
WATCH_PREFIX = DILUTION_PREFIX + ("10-K", "10-Q", "20-F", "SC 13", "SCHEDULE 13", "EFFECT", "DEF 14", "S-8")


def sec_get(url, **kw):
    time.sleep(0.2)
    return requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=40, **kw)


def sec_cik(sym):
    global _sec_tickers
    try:
        if _sec_tickers is None:
            _sec_tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
        for v in _sec_tickers.values():
            if v["ticker"].upper() == sym.upper():
                return str(v["cik_str"]).zfill(10)
    except Exception as e:
        print("sec_cik error:", e)
    return None


def sec_recent_filings(cik, limit=25):
    out = []
    try:
        j = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        rec = j["filings"]["recent"]
        for i in range(min(len(rec["form"]), 120)):
            form = rec["form"][i]
            if form == "4" or form == "4/A" or form.upper().startswith(WATCH_PREFIX):
                acc = rec["accessionNumber"][i].replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{rec['primaryDocument'][i]}"
                out.append({"form": form, "date": rec["filingDate"][i], "url": url})
            if len(out) >= limit:
                break
    except Exception as e:
        print("sec_recent error:", e)
    return out


def html_to_text(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    return re.sub(r"\n\s*\n+", "\n", soup.get_text("\n")).strip()


def pdf_to_text(path):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def fetch_url_text(url):
    r = sec_get(url) if "sec.gov" in url else requests.get(url, headers={"User-Agent": UA}, timeout=40)
    if url.lower().endswith(".pdf") or "pdf" in r.headers.get("Content-Type", ""):
        p = os.path.join(tempfile.gettempdir(), "dl.pdf")
        with open(p, "wb") as f:
            f.write(r.content)
        return pdf_to_text(p)
    return html_to_text(r.text)


def sec_context(sym):
    cik = sec_cik(sym)
    if not cik:
        return "لا توجد بيانات SEC لهذا الرمز (قد يكون أجنبياً أو غير مسجل في EDGAR)."
    filings = sec_recent_filings(cik)
    if not filings:
        return "تعذر جلب قائمة إفصاحات SEC."
    lines = ["أحدث إفصاحات SEC (تاريخ | نموذج | رابط):"]
    lines += [f"{f['date']} | {f['form']} | {f['url']}" for f in filings]
    texts, n = [], 0
    for f in filings:
        if n >= 2:
            break
        if f["form"].upper().startswith(DILUTION_PREFIX):
            try:
                t = fetch_url_text(f["url"])[:12000]
                texts.append(f"\n--- نص مقتطف من {f['form']} بتاريخ {f['date']} ---\n{t}")
                n += 1
            except Exception as e:
                print("filing fetch error:", e)
    return "\n".join(lines) + "\n" + "\n".join(texts)


SEC_PROMPT = """حلّل إفصاح SEC التالي وأجب بالعربية بهذا الترتيب:
1) نوع الوثيقة والشركة وتاريخها.
2) ملخص عربي واضح.
3) ✅ الإيجابيات.
4) ❌ السلبيات والمخاطر.
5) الورنتس (Warrants): العدد، سعر التنفيذ، تاريخ الانتهاء، شروط التنفيذ الإجباري/Cashless، أي آلية إعادة تسعير.
6) زيادة الأسهم / الطرح: سعر الطرح، عدد الأسهم، المستثمرون، استخدام العوائد، نسبة التخفيف المتوقعة.
7) 🎯 المحفزات والأسعار: متى وعند أي سعر يصبح تنفيذ الورنتس أو زيادة الأسهم مجدياً أو ضاغطاً على السعر، وأي تواريخ مهمة (Lock-up، سريان التسجيل، انتهاء الورنتس).
8) ترجمة عربية للفقرات الجوهرية فقط.
لا تخترع أي رقم؛ إن لم يكن مذكوراً اكتب «غير مذكور».
"""


def analyze_sec_text(text):
    chunks = chunk_text(text, SEC_CHUNK)[:SEC_MAX_CHUNKS]
    if not chunks:
        return "⚠️ لم أستطع قراءة نص الوثيقة."
    if len(chunks) == 1:
        return process_via_bridge(SEC_PROMPT + "\nالنص:\n" + chunks[0])
    facts = []
    for i, ch in enumerate(chunks, 1):
        ans = process_via_bridge(
            f"هذا الجزء {i} من {len(chunks)} من إفصاح SEC. استخرج منه فقط الحقائق والأرقام المتعلقة بـ: "
            "الورنتس وأسعار تنفيذها، الطرح وزيادة الأسهم وأسعارها، التخفيف، الديون القابلة للتحويل، "
            "التواريخ المهمة، المخاطر، النقاط الإيجابية. اكتبها بالعربية في نقاط مختصرة:\n\n" + ch)
        if is_err(ans):
            return ans
        facts.append(ans)
    return process_via_bridge(SEC_PROMPT + "\nهذه الحقائق المستخرجة من أجزاء الوثيقة:\n\n" + "\n\n".join(facts))


# ============================================================
# 6) تحليل الأسهم
# ============================================================

STOCK_RULES = """أولاً وبقوة تصفير القراءات السابقة التاريخية للسعر عندك خذ سعر لحظي الآن GOOGLEFINANCE("NASDAQ:{SYM}", "price") استخدم الأداة "Google Search" Analytical Engine & (Agentic Capabilities) طبق بروتوكول [L3-MBO] المحدث: اسحب السعر اللحظي (Tick-by-Tick) لهذا السهم من بيانات Nasdaq TotalView nasdaq و Webull L2 المتقاطعة للحظة الحالية [2026]. تأكد من السيولة العميقة (Level 3) قبل تحليل الأهداف والدخول. حتى مع التداولات الساعات الممتدة وحدد:- دخول من الدعم والدعم القوي… 📍 أو اختراق 🎯 هدف أول، هدف ثاني، هدف ثالث، هدف رابع 🟢 وقد يصل إلى (لا تتجاهلها أساسية) ⛔ وقف الخسارة أولي / ثاني / دموي. هذه قاعدة إذا أعطيتك سهم في الأمريكي طبقها مع التأكيد (L3) أضف:
1- بيانات Dark Pool الحقيقية
2- أماكن السيولة level 2 في live fitch (ELZ3/ILZ) bookmap Level 2 (MBO (High Liquidity Clusters) Limit Orders Absorption Spoofing Trap Tape Speed Exhaustion (Iceberg Orders). Stop & Iceberg Tracker Large Lot Tracker (LLT) Sweep Indicator Advanced Volume Delta Whale Watch ThiccZones Tape Speed CVD (Cumulative Volume Delta) Volume Stripes Slope Up. راقب بدقة ظهور النقاط الكبيرة ((Price Action Audit)
3- الورنتس والإصدارات مع تسعيرها (Liquidity & Dilution Risk & Convertible Notes) نماذج إفصاح (SEC Filing) حديث وفعال بيتا +-
4- الأخبار والمحفزات Forward Catalysts Earnings Beat / Guidance / رادار صفقات المطلعين (Insider Trading) شراء أو بيع The Guidance Sandbagging
5- نشاط الشركة باختصار Sparkline
6- الدولة
7- وهل مدرجة في بورصات أخرى؟ اذكرها."""

STOCK_TAIL = """
اكتب الإجابة بالعربية بهذا الترتيب: (1) السعر اللحظي مع قبل/بعد الجلسة وقراءة الشارت، (2) الدخول والأهداف الأربعة والوقف الثلاثي،
(3) L3 / Dark Pool / مناطق السيولة، (4) الورنتس والإصدارات وتسعيرها ومخاطر التخفيف، (5) الأخبار والمحفزات والمطلعين،
(6) نشاط الشركة، الدولة، البورصات الأخرى.
مهم: أي بيان لا تستطيع الوصول إليه فعلياً (مثل Dark Pool الحقيقي أو بيانات Level 3 / MBO الحية) اكتب أمامه «غير متاح» أو «تقدير» ولا تخترع أرقاماً.
"""


def live_quote(sym):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"interval": "1m", "range": "1d", "includePrePost": "true"},
                         headers={"User-Agent": UA}, timeout=20)
        res = r.json()["chart"]["result"][0]
        m = res["meta"]
        closes = res["indicators"]["quote"][0].get("close", [])
        ts = res.get("timestamp", [])
        last_px, last_t = None, None
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] is not None:
                last_px, last_t = closes[i], ts[i]
                break
        t_txt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_t)) if last_t else "؟"
        return (f"Yahoo Finance: آخر سعر شمعة 1د = {last_px} ({t_txt}) | سعر الجلسة الرسمية = {m.get('regularMarketPrice')} | "
                f"الإغلاق السابق = {m.get('chartPreviousClose')} | أعلى اليوم = {m.get('regularMarketDayHigh')} | "
                f"أدنى اليوم = {m.get('regularMarketDayLow')} | الحجم = {m.get('regularMarketVolume')} | "
                f"البورصة = {m.get('exchangeName')} | العملة = {m.get('currency')}")
    except Exception as e:
        print("live_quote error:", e)
        return "تعذر جلب سعر Yahoo - اعتمد على البحث الحي."


def build_stock_prompt(sym, quote, sec_block, has_image):
    p = STOCK_RULES.replace("{SYM}", sym)
    if has_image:
        p = "هذه صورة شارت للسهم. استخدمها في قراءة الشارت والدعوم والمقاومات.\n" + p
    p += f"\n\nالسهم: {sym}\nبيانات لحظية جُلبت للتو: {quote}\n"
    if sec_block:
        p += f"\nبيانات SEC المجلوبة:\n{sec_block}\n"
    return p + STOCK_TAIL


def analyze_stock(sym, image_path=None):
    quote = live_quote(sym) if sym else "غير متاح"
    sec_block = sec_context(sym) if sym else ""
    prompt = build_stock_prompt(sym or "السهم الظاهر في الشارت", quote, sec_block, bool(image_path))
    return process_via_bridge(prompt, image_path)


TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


def ticker_from_url(url):
    pats = [r"[?&]symbol=(?:[A-Za-z]+(?:%3A|:))?([A-Za-z.\-]{1,8})",
            r"symbols/(?:[A-Za-z]+[-:])?([A-Za-z.]{1,6})/?",
            r"google\.com/finance/quote/([A-Za-z.\-]{1,8})",
            r"quote/([A-Za-z.\-]{1,8})", r"[?&]t=([A-Za-z.\-]{1,8})",
            r"stocks/([A-Za-z.\-]{1,8})", r"symbol/([A-Za-z.\-]{1,8})"]
    for pat in pats:
        m = re.search(pat, url)
        if m:
            s = m.group(1).upper()
            if TICKER_RE.match(s):
                return s
    return None


def ticker_from_image(image_path):
    ans = process_via_bridge("ما رمز السهم (Ticker) الظاهر في هذا الشارت؟ أجب بالرمز فقط بالأحرف الإنجليزية الكبيرة، "
                             "وإن لم تجده اكتب NONE.", image_path)
    s = (ans or "").strip().upper().split()[0] if ans and not is_err(ans) else ""
    s = s.strip("$:.,")
    return s if TICKER_RE.match(s) and s != "NONE" else None


# ============================================================
# 7) توجيه الرسائل
# ============================================================

HELP = """🤖 الأوامر:
- أي نص عادي ← ترجمة للعربية (أو بحث ويكيبيديا إذا تطلب الأمر)
- /wiki أو /ويكي + موضوع ← البحث في ويكيبيديا
- /ملخص + نص (أو بالرد على رسالة) ← تلخيص
- رابط فيديو (يوتيوب/X/تيك توك...) ← تنزيل الفيديو والصوت + تفريغ مترجم
- /فيديو رابط | /صوت رابط | /تفريغ رابط ← خدمة واحدة فقط
- صورة شارت ← تحليل السهم (الرمز في الكابشن اختياري)
- $AAPL أو /سهم AAPL أو رابط سهم (TradingView/Finviz/Yahoo...) ← تحليل السهم
- رابط sec.gov أو /sec AAPL ← قراءة وتحليل الإفصاحات
- ملف PDF/TXT/HTML ← تلخيص
- ملف صوت/فيديو ← تفريغ وترجمة"""

URL_RE = re.compile(r"https?://\S+")


def is_media_url(u):
    h = (urlparse(u).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in MEDIA_DOMAINS)


STOCK_HOSTS = ("tradingview.com", "finviz.com", "finance.yahoo.com", "stocktwits.com", "nasdaq.com",
               "google.com", "webull.com", "marketwatch.com", "investing.com")


def is_stock_url(u):
    h = (urlparse(u).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in STOCK_HOSTS)


def handle_text(chat_id, mid, text, reply_text=None):
    t = text.strip()
    low = t.lower()
    cmd, _, rest = t.partition(" ")
    cmd = cmd.lower()
    rest = rest.strip()

    if cmd in ("/start", "/help", "/مساعدة"):
        send_message(chat_id, HELP, mid)
        return

    if cmd in ("/wiki", "/ويكي"):
        if rest:
            # توجيه الطلب خصيصاً لمتصفح ويكيبيديا عبر الجسر أو الدالة
            if _BROWSER:
                res = _BROWSER._ask_wiki(rest)
                send_long(chat_id, res, mid)
            else:
                send_message(chat_id, "⚠️ متصفح ويكيبيديا غير جاهز بعد.", mid)
        else:
            send_message(chat_id, "⚠️ أضف الكلمة المراد البحث عنها بعد الأمر، مثال: `/wiki الذكاء الاصطناعي`", mid)
        return

    url_m = URL_RE.search(t)
    url = url_m.group(0) if url_m else None

    if cmd in ("/فيديو", "/صوت", "/تفريغ") and url:
        media_flow(chat_id, mid, url, {"/فيديو": "video", "/صوت": "audio", "/تفريغ": "transcript"}[cmd])
        return

    if cmd in ("/sec", "/سيك"):
        if url:
            send_long(chat_id, analyze_sec_text(fetch_url_text(url)), mid)
        else:
            sym = rest.upper().lstrip("$")
            blk = sec_context(sym)
            send_long(chat_id, process_via_bridge(SEC_PROMPT + "\nهذه إفصاحات الشركة:\n" + blk), mid)
        return

    if cmd in ("/سهم", "/stock"):
        sym = rest.upper().lstrip("$") or None
        if url and not sym:
            sym = ticker_from_url(url)
        send_long(chat_id, analyze_stock(sym), mid)
        return

    if cmd in ("/ملخص", "/sum", "/لخص"):
        src = rest or reply_text or ""
        if url and not rest.replace(url, "").strip():
            src = fetch_url_text(url)
        if not src:
            send_message(chat_id, "⚠️ أرسل النص أو رد على رسالة لتلخيصها.", mid)
            return
        send_long(chat_id, summarize_text(src), mid)
        return

    if url and is_media_url(url):
        media_flow(chat_id, mid, url, "all")
        return

    if url and ("sec.gov" in url or is_stock_url(url)):
        if "sec.gov" in url:
            send_long(chat_id, analyze_sec_text(fetch_url_text(url)), mid)
        else:
            sym = ticker_from_url(url)
            send_long(chat_id, analyze_stock(sym), mid)
        return

    if t.startswith("$") and TICKER_RE.match(t.upper().lstrip("$")):
        send_long(chat_id, analyze_stock(t.upper().lstrip("$")), mid)
        return

    # المعالجة الافتراضية للنص العادي
    if is_arabic(t):
        send_long(chat_id, process_via_bridge(f"أجب عن التالي بدقة واحترافية:\n\n{t}"), mid)
    else:
        send_long(chat_id, translate_to_arabic(t), mid)


def handle_document(chat_id, mid, file_id, name, caption):
    wd = tempfile.mkdtemp(prefix="doc_")
    try:
        p = tg_download(file_id, wd, name)
        if not p:
            send_message(chat_id, "❌ تعذر تنزيل الملف.", mid)
            return
        ext = os.path.splitext(p)[1].lower()
        if ext in (".mp3", ".wav", ".m4a", ".ogg", ".mp4", ".mkv"):
            if ext in (".mp4", ".mkv"):
                aud = os.path.join(wd, "extracted.mp3")
                _run(["ffmpeg", "-i", p, "-q:a", "2", "-map", "a", aud])
                if os.path.exists(aud):
                    p = aud
            transcript_flow(chat_id, mid, p)
            return
        txt = ""
        if ext == ".pdf":
            txt = pdf_to_text(p)
        elif ext in (".txt", ".html", ".htm", ".csv", ".json"):
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        else:
            txt = f"ملف مرفق: {name}"
        if caption:
            txt = f"ملاحظة المستخدم: {caption}\n\n" + txt
        if "sec" in name.lower() or "10-k" in name.lower() or "10-q" in name.lower():
            send_long(chat_id, analyze_sec_text(txt), mid)
        else:
            send_long(chat_id, summarize_text(txt), mid)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def handle_photo(chat_id, mid, photo_list, caption):
    best = max(photo_list, key=lambda x: x.get("file_size", 0))
    wd = tempfile.mkdtemp(prefix="photo_")
    try:
        p = tg_download(best["file_id"], wd, "chart.jpg")
        if not p:
            send_message(chat_id, "❌ تعذر تنزيل الصورة.", mid)
            return
        sym = (caption or "").upper().lstrip("$").strip()
        if not TICKER_RE.match(sym):
            sym = ticker_from_image(p)
        send_long(chat_id, analyze_stock(sym, p), mid)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ============================================================
# 8) حلقة الاستطلاع (Polling) الرئيسية لتلغرام
# ============================================================


def telegram_polling():
    offset = 0
    print("🤖 البوت يعمل ويستطلع رسائل تلغرام...")
    start_time = time.time()
    while time.time() - start_time < RUN_SECONDS:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=45)
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                cid = str(msg.get("chat", {}).get("id", ""))
                if CHAT_ID and cid != CHAT_ID:
                    continue  # حماية: الرد فقط على الشات المخصص
                mid = msg.get("message_id")
                txt = msg.get("text") or msg.get("caption") or ""
                reply_to = msg.get("reply_to_message", {})
                reply_text = reply_to.get("text") or reply_to.get("caption")

                if "photo" in msg:
                    threading.Thread(target=handle_photo, args=(cid, mid, msg["photo"], txt), daemon=True).start()
                elif "document" in msg:
                    d = msg["document"]
                    threading.Thread(target=handle_document, args=(cid, mid, d["file_id"], d.get("file_name", "file"), txt), daemon=True).start()
                elif "audio" in msg or "voice" in msg:
                    audio_obj = msg.get("audio") or msg.get("voice")
                    threading.Thread(target=handle_document, args=(cid, mid, audio_obj["file_id"], audio_obj.get("file_name", "audio.ogg"), txt), daemon=True).start()
                elif "video" in msg or "video_note" in msg:
                    v_obj = msg.get("video") or msg.get("video_note")
                    threading.Thread(target=handle_document, args=(cid, mid, v_obj["file_id"], v_obj.get("file_name", "video.mp4"), txt), daemon=True).start()
                elif txt:
                    threading.Thread(target=handle_text, args=(cid, mid, txt, reply_text), daemon=True).start()
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login_mode()
        return
    if not BOT_TOKEN:
        print("خطأ: BOT_TOKEN غير موجود.")
        sys.exit(1)
    if not start_bridge():
        print("خطأ في تشغيل جسر المتصفح.")
        sys.exit(1)
    telegram_polling()


if __name__ == "__main__":
    main()
