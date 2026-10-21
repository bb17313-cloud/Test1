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
from urllib.parse import urlparse
import requests

# إعدادات البيئة والتليجرام
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("CHAT_ID", ""))
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "1800"))
BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "420"))
SEC_UA = os.environ.get("SEC_USER_AGENT", "StockBot admin@example.com")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
STATE_FILE = "gemini_state.json"
GEMINI_URL = "https://gemini.google.com/app"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BROWSER_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]

TRANSLATE_CHUNK = 8000
SUMMARY_CHUNK = 30000
SEC_CHUNK = 25000
SEC_MAX_CHUNKS = 6

class SessionExpired(Exception):
    pass

def _parse_state(raw):
    raw = raw.strip()
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
    raise RuntimeError("لا توجد جلسة Gemini (GEMINI_STATE). شغّل: python bot.py login")

class GeminiBrowser:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    def start(self):
        from playwright.sync_api import sync_playwright
        state = _prepare_state()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        self.ctx = self.browser.new_context(
            storage_state=state, user_agent=UA,
            viewport={"width": 1280, "height": 900}, locale="en-US")
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

    def ask(self, prompt, image_path=None, timeout=360):
        pg = self.page
        pg.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
        box = pg.locator("rich-textarea div.ql-editor, div.ql-editor[contenteditable='true']").first
        try:
            box.wait_for(state="visible", timeout=30000)
        except Exception:
            if "accounts.google" in pg.url:
                raise SessionExpired()
            raise
        if "accounts.google" in pg.url or pg.locator("a[href*='ServiceLogin'], a:has-text('Sign in')").count() > 0:
            raise SessionExpired()

        if image_path and os.path.exists(image_path):
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
            pg.wait_for_timeout(2000)
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
            self._reply({"status": "error", "message": "SESSION_EXPIRED: انتهت الجلسة"}, code=401)
        except Exception as e:
            self._reply({"status": "error", "message": str(e)[:300]}, code=500)

def start_bridge():
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

def call_gemini_bridge(prompt, image_path=None):
    try:
        r = requests.post(BRIDGE_URL, json={"prompt": prompt, "image_path": image_path}, timeout=BRIDGE_TIMEOUT)
        res = r.json()
        if res.get("status") == "success":
            return res.get("result")
        return f"خطأ من الخادم الوسيط: {res.get('message')}"
    except Exception as e:
        return f"فشل الاتصال بالمتصفح: {str(e)}"

# ============================================================
# الميزات والوظائف (التنزيل، التفريغ، وسحب الـ SEC)
# ============================================================

def download_and_transcribe(url):
    """تنزيل الصوت عبر yt-dlp وتفريغه عبر Whisper مجاناً محلياً"""
    out_dir = tempfile.mkdtemp()
    out_template = os.path.join(out_dir, "audio.%(ext)s")
    
    # 1. التنزيل
    cmd_dl = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", out_template, url]
    subprocess.run(cmd_dl, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    mp3_files = glob.glob(os.path.join(out_dir, "*.mp3"))
    if not mp3_files:
        return "فشل تحميل مقطع الصوت/الفيديو، تأكد من الرابط."
    
    audio_path = mp3_files[0]
    
    # 2. التفريغ الصوتي عبر Whisper (أداة سطر الأوامر المجانية)
    # ملاحظة: يتطلب تثبيت openai-whisper عبر pip في بيئة التشغيل
    output_txt_path = os.path.join(out_dir, "audio.txt")
    cmd_whisper = ["whisper", audio_path, "--model", WHISPER_MODEL, "--output_dir", out_dir, "--output_format", "txt"]
    subprocess.run(cmd_whisper, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(output_txt_path):
        with open(output_txt_path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        shutil.rmtree(out_dir)
        return text
    else:
        shutil.rmtree(out_dir)
        return "تم تحميل الملف ولكن فشل استخراج النص عبر Whisper."

def fetch_sec_filing(ticker):
    """جلب أحدث تقرير مالي 10-K أو 10-Q لرمز سهم معين من موقع SEC مجاناً"""
    try:
        # جلب الـ CIK الخاص بالشركة أولاً
        headers = {"User-Agent": SEC_UA}
        ticker_clean = ticker.upper().strip()
        tik_res = requests.get("https://sec.gov", headers=headers).json()
        cik = None
        for k, v in tik_res.items():
            if v["ticker"] == ticker_clean:
                cik = str(v["cik_str"]).zfill(10)
                break
        if not cik:
            return f"لم يتم العثور على رمز السهم {ticker_clean} في الـ SEC."
            
        # جلب التقديمات (Submissions)
        sub_res = requests.get(f"https://sec.gov{cik}.json", headers=headers).json()
        filings = sub_res["filings"]["recent"]
        
        # البحث عن أحدث تقرير مالي رئيسي
        idx = -1
        for i, form in enumerate(filings["form"]):
            if form in ["10-K", "10-Q"]:
                idx = i
                break
        if idx == -1:
            return "لم يتم العثور على تقارير 10-K أو 10-Q حديثة."
            
        acc_num = filings["accessionNumber"][idx].replace("-", "")
        doc_name = filings["primaryDocument"][idx]
        
        # سحب صفحة التقرير النصي/HTML
        report_url = f"https://sec.gov{int(cik)}/{acc_num}/{doc_name}"
        rep_res = requests.get(report_url, headers=headers)
        
        # تنظيف الـ HTML للحصول على نص خام فقط
        clean_text = re.sub(r'<[^>]+>', ' ', rep_res.text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        return clean_text[:SEC_CHUNK * SEC_MAX_CHUNKS] # إرجاع جزء كافي لتحليله
    except Exception as e:
        return f"فشل جلب بيانات SEC: {str(e)}"

