# -*- coding: utf-8 -*-
"""
بوت تلغرام (يعمل على GitHub Actions) - مصادر مجانية بدون أي API مدفوع
------------------------------------------------------------------
1) ترجمة أي نص / ملف / صورة / رابط إلى العربية   (Google Translate المجاني)
2) تلخيص أي مستند (PDF, Word, PowerPoint, Excel, TXT, HTML, صور)  (تلخيص استخراجي محلي)
3) تحليل أي سهم: النشاط، نتائج الربع، المستهدفات، المشاريع والعقود، الاتجاه،
   زيادة/خفض رأس المال، التوزيعات، التقسيم العكسي، الخسائر المتراكمة، الأخبار
   (Yahoo Finance + SEC EDGAR - بدون مفاتيح)

الأسرار المطلوبة: BOT_TOKEN, CHAT_ID
اختياري: SEC_USER_AGENT (مثال: "MyBot myemail@gmail.com")
"""
import os
import re
import sys
import time
import math
import logging
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

# ──────────────────────────── الإعدادات ────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = str(os.environ["CHAT_ID"]).strip()
RUN_SECONDS = int(os.environ.get("RUN_SECONDS") or 21000)
SEC_UA = os.environ.get("SEC_USER_AGENT") or "TelegramStockBot admin@example.com"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

HELP = (
    "👋 أهلاً! أرسل لي:\n\n"
    "• أي نص ← أترجمه إلى العربية\n"
    "• ملف (PDF / Word / PowerPoint / Excel / TXT / HTML) ← ألخّصه بالعربية\n"
    "   ↳ اكتب «ترجم» في وصف الملف لترجمته كاملاً\n"
    "• رابط مقال ← ألخّصه (أو «ترجم + الرابط» لترجمته)\n"
    "• صورة ← أستخرج النص وأترجمه (أو «لخص» في الوصف)\n"
    "• رمز سهم مثل AAPL أو رابط TradingView أو «تحليل AAPL» ← تحليل شامل\n\n"
    "الأوامر: /tr ترجمة  |  /sum تلخيص  |  /stock تحليل سهم\n"
    "يمكنك أيضاً الرد على أي رسالة أو ملف بكلمة «ترجم» أو «لخص»."
)


# ──────────────────────────── تلغرام ────────────────────────────
def tg(method, files=None, timeout=60, **params):
    params = {k: v for k, v in params.items() if v is not None}
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/{method}", data=params, files=files, timeout=timeout)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            log.warning("tg %s failed: %s", method, j)
            if r.status_code == 429:
                time.sleep(int(j.get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            return None
        except Exception as e:  # noqa
            log.warning("tg %s error: %s", method, e)
            time.sleep(2)
    return None


def send(chat_id, text, reply_to=None):
    text = (text or "").strip()
    if not text:
        return
    while text:
        if len(text) <= 3900:
            part, text = text, ""
        else:
            cut = text.rfind("\n", 0, 3900)
            cut = cut if cut > 1500 else 3900
            part, text = text[:cut], text[cut:].lstrip("\n")
        tg("sendMessage", chat_id=chat_id, text=part, reply_to_message_id=reply_to,
           disable_web_page_preview="true")
        reply_to = None


def send_doc(chat_id, content, filename, caption=None):
    data = content.encode("utf-8") if isinstance(content, str) else content
    tg("sendDocument", files={"document": (filename, data)}, chat_id=chat_id,
       caption=caption, timeout=120)


def typing(chat_id):
    tg("sendChatAction", chat_id=chat_id, action="typing", timeout=15)


def download_file(file_id):
    info = tg("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        raise RuntimeError("تعذّر تنزيل الملف (حدّ تلغرام للبوتات 20MB)")
    r = requests.get(f"{FILE_API}/{info['file_path']}", timeout=120)
    r.raise_for_status()
    return r.content


# ──────────────────────────── أدوات النص ────────────────────────────
def is_arabic(text, threshold=0.5):
    letters = re.findall(r"[^\W\d_]", text or "")
    if not letters:
        return False
    ar = sum(1 for c in letters if "\u0600" <= c <= "\u06FF")
    return ar / len(letters) >= threshold


def chunk_text(text, limit=4000):
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = max(line.rfind(". ", 0, limit), line.rfind(" ", 0, limit))
            cut = cut if cut > limit // 2 else limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def _gtx(chunk, sl):
    r = requests.post(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": sl, "tl": "ar", "dt": "t"},
        data={"q": chunk}, headers=WEB_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return "".join(x[0] for x in data[0] if x and x[0])


def _translate_chunk(chunk):
    if not chunk.strip():
        return chunk
    needs = bool(re.search(r"[^\W\d_]", chunk)) and not is_arabic(chunk, 0.9)
    has_latin = bool(re.search(r"[A-Za-z]{3}", chunk))
    best, last_err = None, None
    for sl in ("auto", "en"):
        out = None
        for attempt in range(3):
            try:
                out = _gtx(chunk, sl)
                break
            except Exception as e:  # noqa
                last_err = e
                log.warning("gtx %s: %s", sl, e)
                time.sleep(1.5 * (attempt + 1))
        if out and out.strip() and (out.strip() != chunk.strip() or not needs):
            best = out
            if is_arabic(out, 0.6) or not has_latin:
                return out
    if best:
        return best
    try:  # احتياطي ثانٍ
        from deep_translator import GoogleTranslator
        for sl in ("auto", "en"):
            out = GoogleTranslator(source=sl, target="ar").translate(chunk)
            if out and out.strip() != chunk.strip():
                return out
    except Exception as e:  # noqa
        last_err = e
        log.warning("deep_translator: %s", e)
    raise RuntimeError(f"فشلت الترجمة (Google لم يُرجع ترجمة). {last_err or ''}"[:250])


def translate_ar(text):
    text = (text or "").strip()
    if not text or is_arabic(text, 0.9):
        return text
    return "\n".join(_translate_chunk(c) for c in chunk_text(text, 1800))


def translate_lines(lines):
    """ترجمة قائمة أسطر مع الحفاظ على الترتيب."""
    lines = [l.strip() for l in lines]
    result, group, idx = [None] * len(lines), [], []

    def flush():
        if not group:
            return
        out = _translate_chunk("\n".join(group)).split("\n")
        if len(out) == len(group):
            for i, o in zip(idx, out):
                result[i] = o.strip()
        else:
            for i, g in zip(idx, group):
                result[i] = _translate_chunk(g).strip()
        group.clear()
        idx.clear()

    size = 0
    for i, l in enumerate(lines):
        if not l or is_arabic(l, 0.9):
            result[i] = l
            continue
        if size + len(l) > 3500:
            flush()
            size = 0
        group.append(l.replace("\n", " "))
        idx.append(i)
        size += len(l) + 1
    flush()
    return [r if r is not None else l for r, l in zip(result, lines)]


# ──────────────────────────── التلخيص ────────────────────────────
def split_sentences(text):
    text = re.sub(r"[ \t\u00a0]+", " ", text or "")
    text = re.sub(r"(?<![.!?؟:\n])\n(?!\n)", " ", text)  # دمج أسطر PDF المكسورة
    parts = re.split(r"(?<=[.!?؟])\s+|\n+", text)
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 700]


def summarize_extractive(text, n=None):
    sents = split_sentences(text)
    if len(sents) <= 6:
        return sents
    n = n or max(5, min(14, len(sents) // 10))
    toks = [set(re.findall(r"\w{3,}", s.lower())) for s in sents]
    df = Counter(w for t in toks for w in t)
    limit = max(3, int(len(sents) * 0.4))
    freq = Counter()
    for s in sents:
        for w in re.findall(r"\w{3,}", s.lower()):
            if df[w] <= limit:  # استبعاد الكلمات الشائعة جداً
                freq[w] += 1
    scores = []
    for i, s in enumerate(sents):
        words = re.findall(r"\w{3,}", s.lower())
        if not words:
            scores.append(0)
            continue
        sc = sum(freq[w] for w in set(words)) / (len(words) ** 0.7 + 1)
        if i < 3:
            sc *= 1.3
        if re.search(r"\d", s):
            sc *= 1.1
        scores.append(sc)
    chosen = []
    for i in sorted(range(len(sents)), key=lambda k: scores[k], reverse=True):
        if len(chosen) >= n:
            break
        if any(len(toks[i] & toks[j]) / max(1, len(toks[i] | toks[j])) > 0.55 for j in chosen):
            continue
        chosen.append(i)
    return [sents[i] for i in sorted(chosen)]


def key_figures(text, exclude, limit=5):
    pat = re.compile(r"(\$|€|£|%|\d[\d,\.]*\s?(million|billion|مليون|مليار|M|B)\b)", re.I)
    out = []
    for s in split_sentences(text):
        if s not in exclude and pat.search(s):
            out.append(s)
        if len(out) >= limit:
            break
    return out


def build_summary(text, title):
    words = len(text.split())
    sents = summarize_extractive(text)
    if not sents:
        return "⚠️ النص قصير أو غير قابل للتلخيص."
    ar = translate_lines(sents)
    out = [f"📝 ملخص: {title}", f"(الحجم الأصلي ≈ {words:,} كلمة)", ""]
    out += [f"• {s}" for s in ar]
    figs = key_figures(text, set(sents))
    if figs:
        out += ["", "📌 أرقام ومبالغ بارزة:"] + [f"• {s}" for s in translate_lines(figs)]
    return "\n".join(out)


# ──────────────────────────── استخراج النصوص ────────────────────────────
def extract_text(data, name):
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    with tempfile.NamedTemporaryFile(suffix="." + (ext or "bin"), delete=False) as f:
        f.write(data)
        path = f.name
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            pages = [(p.extract_text() or "") for p in reader.pages[:300]]
            text = "\n\n".join(pages)
            if len(text.strip()) < 50:
                raise RuntimeError("ملف PDF ممسوح ضوئياً (صور بلا نص). أرسل صفحاته كصور ليتم استخراج النص منها.")
            return text
        if ext == "docx":
            import docx
            d = docx.Document(path)
            parts = [p.text for p in d.paragraphs]
            for tb in d.tables:
                for row in tb.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return "\n".join(parts)
        if ext == "pptx":
            from pptx import Presentation
            prs, parts = Presentation(path), []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"\nشريحة {i}")
                for sh in slide.shapes:
                    if sh.has_text_frame:
                        parts.append(sh.text_frame.text)
            return "\n".join(parts)
        if ext in ("xlsx", "xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                parts.append(f"\nورقة: {ws.title}")
                for n, row in enumerate(ws.iter_rows(values_only=True)):
                    if n > 2000:
                        break
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        parts.append(" | ".join(cells))
            return "\n".join(parts)
        if ext in ("html", "htm"):
            soup = BeautifulSoup(data, "lxml")
            for t in soup(["script", "style", "noscript"]):
                t.decompose()
            return soup.get_text("\n")
        if ext in ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"):
            return ocr_image(path)
        if ext in ("doc", "xls", "ppt"):
            raise RuntimeError("الصيغ القديمة (doc/xls/ppt) غير مدعومة، احفظ الملف بصيغة docx/xlsx/pptx.")
        for enc in ("utf-8", "cp1256", "latin-1"):
            try:
                return data.decode(enc)
            except Exception:
                continue
        raise RuntimeError("صيغة الملف غير مدعومة.")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def ocr_image(path):
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(path)
        try:
            txt = pytesseract.image_to_string(img, lang="ara+eng")
        except Exception:
            txt = pytesseract.image_to_string(img)
        if not txt.strip():
            raise RuntimeError("لم أجد نصاً داخل الصورة.")
        return txt
    except ImportError:
        raise RuntimeError("OCR غير مثبّت على السيرفر.")


def fetch_article(url):
    r = requests.get(url, headers=SEC_HEADERS if "sec.gov" in url else WEB_HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")
    title = (soup.title.string or url).strip() if soup.title else url
    for t in soup(["script", "style", "noscript", "nav", "footer", "aside", "form", "header"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "h1", "h2", "h3", "li"])]
    text = "\n".join(p for p in paras if len(p) > 25)
    if len(text) < 200:
        text = root.get_text("\n", strip=True)
    return title, text


# ──────────────────────────── تحليل الأسهم ────────────────────────────
def num(x):
    try:
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else v
    except Exception:
        return None


def money(x, cur="USD"):
    v = num(x)
    if v is None:
        return "غير متوفر"
    a, sign = abs(v), "-" if v < 0 else ""
    for div, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{a / div:.2f}{s} {cur}"
    return f"{sign}{a:,.2f} {cur}"


def pct(a, b):
    a, b = num(a), num(b)
    if a is None or b in (None, 0):
        return "—"
    return f"{(a - b) / abs(b) * 100:+.1f}%"


def get_row(df, names):
    try:
        if df is None or df.empty:
            return None
        for n in names:
            if n in df.index:
                s = df.loc[n].dropna()
                if len(s):
                    return s.sort_index(ascending=False)
    except Exception:
        pass
    return None


def find_symbol(text, strict):
    t = (text or "").strip()
    if "tradingview.com" in t.lower():
        m = re.search(r"symbol=(?:[A-Za-z_]+(?:%3A|:))?([A-Za-z0-9\.\-]+)", t) or \
            re.search(r"/symbols/(?:[A-Za-z_]+-)?([A-Za-z0-9\.]+)", t)
        if m:
            return norm_symbol(m.group(1))
    m = re.search(r"\$([A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?)\b", t)
    if m:
        return norm_symbol(m.group(1))
    tick = r"([A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?|\d{4}(?:\.SR)?)"
    if strict:
        m = re.fullmatch(tick, t)
        if m and (m.group(1).isupper() or m.group(1)[0].isdigit()):
            return norm_symbol(m.group(1))
        return None
    m = re.search(r"(?<![\w])" + tick + r"(?![\w])", t)
    return norm_symbol(m.group(1)) if m else None


def norm_symbol(s):
    s = s.upper()
    if re.fullmatch(r"\d{4}", s):
        return s + ".SR"
    m = re.fullmatch(r"([A-Z]{1,5})\.([A-Z])", s)
    return f"{m.group(1)}-{m.group(2)}" if m else s


_SEC_MAP = {}


def sec_cik(sym):
    if not _SEC_MAP:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=30)
        for v in r.json().values():
            _SEC_MAP[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
    return _SEC_MAP.get(sym.upper())


def sec_recent(cik, days=180):
    j = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_HEADERS, timeout=30).json()
    r = j["filings"]["recent"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    items_list = r.get("items") or []
    out = []
    for i in range(len(r["form"])):
        if r["filingDate"][i] < cutoff:
            break
        acc = r["accessionNumber"][i].replace("-", "")
        out.append({
            "form": r["form"][i],
            "date": r["filingDate"][i],
            "items": items_list[i] if i < len(items_list) else "",
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{r['primaryDocument'][i]}",
        })
    return out


ITEM_AR = {
    "1.01": "اتفاقية/عقد جوهري", "1.02": "إنهاء اتفاقية", "2.02": "نتائج مالية",
    "2.03": "التزام/دين جديد", "3.01": "إشعار إدراج (مخالفة/نقص امتثال)",
    "3.02": "إصدار أسهم غير مسجّل (زيادة رأس المال)", "3.03": "تعديل حقوق المساهمين",
    "5.02": "تغيير إداري", "5.03": "تعديل النظام الأساسي (قد يتضمن تقسيم/تغيير أسهم)",
    "5.07": "نتائج تصويت المساهمين", "7.01": "إفصاح/عرض", "8.01": "حدث آخر",
}
CAP_PREFIX = ("S-1", "S-3", "F-1", "F-3", "424B", "S-11")


def filing_label(f):
    form = f["form"]
    if form.startswith(CAP_PREFIX):
        return f"{form} — تسجيل/طرح أوراق مالية (احتمال تخفيف)"
    if form.startswith("8-K"):
        labels = [ITEM_AR[i] for i in f["items"].split(",") if i in ITEM_AR]
        return f"{form} — " + ("، ".join(labels) if labels else "إفصاح")
    if form in ("DEF 14A", "PRE 14A"):
        return f"{form} — بيان تفويض مساهمين (قد يتضمن زيادة أسهم/تقسيم عكسي)"
    return form


def filing_lines(fl, limit=5):
    return [f"• {f['date']} — {filing_label(f)}\n  {f['url']}" for f in fl[:limit]]


def filing_brief(url):
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        soup = BeautifulSoup(r.content, "lxml")
        text = soup.get_text("\n")
        text = text[text.find("Item"):] if "Item" in text else text
        sents = split_sentences(text)[:3]
        return translate_ar(" ".join(sents))[:450] if sents else ""
    except Exception:
        return ""


def parse_dt(ts):
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def get_news(t, sym):
    items = []
    try:
        for n in (t.news or []):
            c = n.get("content") or n
            title = c.get("title")
            if not title:
                continue
            link = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                    or c.get("link") or "")
            pub = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
            items.append((parse_dt(c.get("pubDate") or c.get("providerPublishTime")), title, pub, link))
    except Exception as e:  # noqa
        log.warning("yf news: %s", e)
    try:
        r = requests.get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US",
                         headers=WEB_HEADERS, timeout=20)
        for it in ET.fromstring(r.content).iter("item"):
            title = (it.findtext("title") or "").strip()
            if not title:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                dt = None
            items.append((dt, title, "Yahoo Finance", (it.findtext("link") or "").strip()))
    except Exception as e:  # noqa
        log.warning("rss: %s", e)
    seen, uniq = set(), []
    for it in items:
        k = it[1].lower()[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    uniq.sort(key=lambda x: x[0] or epoch, reverse=True)
    return uniq


NEWS_KW = re.compile(r"contract|agreement|award|order|partner|deal|project|collaborat|acquir|acquisition|"
                     r"license|supply|FDA|approv|launch|grant|reverse split|offering|private placement", re.I)


def analyze_stock(sym):
    import yfinance as yf
    t = yf.Ticker(sym)
    hist = t.history(period="1y", interval="1d")
    if hist is None or hist.empty:
        return [f"❌ لم أجد بيانات للرمز {sym}. تأكد من الرمز (مثال: AAPL أو 2222 للسوق السعودي)."]
    try:
        info = t.info or {}
    except Exception:
        info = {}
    cur = info.get("currency") or "USD"
    name = info.get("longName") or info.get("shortName") or sym
    c = hist["Close"].dropna()
    price = num(c.iloc[-1])
    now = datetime.now(timezone.utc)

    # بيانات SEC (للأسهم الأمريكية)
    filings, sec_note = [], ""
    try:
        cik = sec_cik(sym)
        if cik:
            filings = sec_recent(cik, 180)
        else:
            sec_note = "ℹ️ لا توجد بيانات SEC لهذا الرمز (سهم غير مدرج أمريكياً أو غير مسجّل)."
    except Exception as e:  # noqa
        sec_note = "ℹ️ تعذّر الوصول إلى SEC حالياً."
        log.warning("sec: %s", e)

    sections = []

    # ── الترويسة
    sections.append(
        f"📊 {name} ({sym})\n"
        f"السعر: {price:.2f} {cur} | القيمة السوقية: {money(info.get('marketCap'), cur)}\n"
        f"البورصة: {info.get('exchange') or '—'} | الدولة: {info.get('country') or '—'}")

    # ── 1) النشاط
    try:
        cat = " / ".join(x for x in (info.get("sector"), info.get("industry")) if x)
        desc_sents = split_sentences(info.get("longBusinessSummary") or "")[:2]
        tr = translate_lines([cat, " ".join(desc_sents)]) if (cat or desc_sents) else ["", ""]
        sections.append("🏢 النشاط\n"
                        f"• الأساسي: {tr[0] or 'غير متوفر'}\n"
                        f"• الفرعي/التفاصيل: {tr[1] or 'غير متوفر'}")
    except Exception as e:  # noqa
        log.warning("activity: %s", e)

    # ── 2) نتائج الربع
    try:
        qi = t.quarterly_income_stmt
        rev = get_row(qi, ["Total Revenue", "Operating Revenue"])
        ni = get_row(qi, ["Net Income", "Net Income Common Stockholders"])
        eps = get_row(qi, ["Diluted EPS", "Basic EPS"])
        lines = []
        if rev is not None or ni is not None:
            base = (rev if rev is not None else ni)
            lines.append(f"آخر ربع مُعلن: {base.index[0].strftime('%Y-%m-%d')}")

            def cmp(label, s, is_money=True):
                v = s.iloc[0]
                txt = f"• {label}: {money(v, cur) if is_money else f'{v:.2f}'}"
                if len(s) > 1:
                    txt += f" | عن الربع السابق {pct(v, s.iloc[1])}"
                if len(s) > 4:
                    txt += f" | عن نفس الربع العام الماضي {pct(v, s.iloc[4])}"
                return txt
            if rev is not None:
                lines.append(cmp("الإيرادات", rev))
            if ni is not None:
                lines.append(cmp("صافي الربح/الخسارة", ni))
            if eps is not None:
                lines.append(cmp("ربحية السهم", eps, False))
        else:
            lines.append("لا تتوفر نتائج ربعية.")
        try:
            ed = t.get_earnings_dates(limit=6)
            rep = ed[ed["Reported EPS"].notna()].iloc[0]
            lines.append(f"• مفاجأة آخر نتائج: الفعلي {rep['Reported EPS']} مقابل المتوقع {rep['EPS Estimate']}")
        except Exception:
            pass
        try:
            nxt = (t.calendar or {}).get("Earnings Date")
            if nxt:
                lines.append(f"• موعد النتائج القادم: {nxt[0]}")
        except Exception:
            pass
        sections.append("📈 نتائج الربع\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("results: %s", e)

    # ── 3) المستهدفات
    try:
        lines = []
        tm = num(info.get("targetMeanPrice"))
        if tm:
            lines.append(f"• متوسط مستهدف المحللين: {tm:.2f} ({pct(tm, price)} عن السعر)")
            lines.append(f"• الأعلى: {info.get('targetHighPrice')} | الأدنى: {info.get('targetLowPrice')} "
                         f"| عدد المحللين: {info.get('numberOfAnalystOpinions')}")
            if info.get("recommendationKey"):
                lines.append(f"• التوصية العامة: {info['recommendationKey']}")
        else:
            lines.append("• لا توجد تغطية محللين (شائع في الأسهم الصغيرة).")
        hi52, lo52 = num(hist["High"].max()), num(hist["Low"].min())
        hi3m = num(hist["High"].tail(63).max())
        lines.append(f"• مقاومات فنية: أعلى 3 أشهر {hi3m:.2f} | أعلى 52 أسبوع {hi52:.2f}")
        lines.append(f"• أدنى 52 أسبوع: {lo52:.2f}")
        sections.append("🎯 المستهدفات\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("targets: %s", e)

    # ── 4) المشاريع والعقود
    news = get_news(t, sym)
    try:
        lines = []
        contracts = [f for f in filings if f["form"].startswith("8-K") and "1.01" in f["items"]]
        if contracts:
            lines.append("عقود/اتفاقيات جوهرية (SEC آخر 6 أشهر):")
            for f in contracts[:3]:
                lines.append(f"• {f['date']} — {f['form']}\n  {f['url']}")
            for f in contracts[:2]:
                b = filing_brief(f["url"])
                if b:
                    lines.append(f"  ↳ {b}")
        kw = [n for n in news if NEWS_KW.search(n[1]) and (not n[0] or now - n[0] < timedelta(days=45))][:4]
        if kw:
            lines.append("من العناوين الأخيرة (مشاريع/عقود/شراكات):")
            for (dt, title, pub, link), ar in zip(kw, translate_lines([k[1] for k in kw])):
                lines.append(f"• {ar}\n  {link}")
        if not lines:
            lines.append("لا توجد عقود أو مشاريع مُعلنة حديثاً في المصادر المتاحة.")
        sections.append("🤝 المشاريع والعقود\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("contracts: %s", e)

    # ── 5) الاتجاه العام
    try:
        sma = {n: num(c.rolling(n).mean().iloc[-1]) for n in (20, 50, 200)}
        above = [n for n, v in sma.items() if v and price > v]
        valid = [n for n, v in sma.items() if v]
        if valid and len(above) == len(valid):
            trend = "صاعد 🟢 (فوق جميع المتوسطات)"
        elif not above:
            trend = "هابط 🔴 (تحت جميع المتوسطات)"
        else:
            trend = "مختلط/عرضي 🟡"
        d = c.diff()
        up, dn = d.clip(lower=0), -d.clip(upper=0)
        rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean()
        rsi = num((100 - 100 / (1 + rs)).iloc[-1])

        def chg(n):
            return pct(price, c.iloc[-n - 1]) if len(c) > n else "—"
        vol_avg = num(hist["Volume"].tail(30).mean())
        vol_now = num(hist["Volume"].iloc[-1])
        lines = [f"• الاتجاه: {trend}",
                 "• المتوسطات: " + " | ".join(f"MA{n}: {v:.2f}" for n, v in sma.items() if v),
                 f"• التغير: شهر {chg(21)} | 3 أشهر {chg(63)} | 6 أشهر {chg(126)}"]
        if rsi is not None:
            state = "تشبع شراء" if rsi > 70 else "تشبع بيع" if rsi < 30 else "محايد"
            lines.append(f"• RSI(14): {rsi:.0f} ({state})")
        if vol_avg and vol_now:
            lines.append(f"• حجم آخر جلسة: {vol_now / vol_avg:.1f}× متوسط 30 يوم")
        sections.append("🧭 الاتجاه العام\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("trend: %s", e)

    # ── 6) رأس المال / توزيعات / تقسيم عكسي
    try:
        lines = []
        bs = t.quarterly_balance_sheet
        if bs is None or bs.empty:
            bs = t.balance_sheet
        sh = get_row(bs, ["Ordinary Shares Number", "Share Issued"])
        if sh is not None and len(sh) > 1:
            old = sh.iloc[min(4, len(sh) - 1)]
            ch = pct(sh.iloc[0], old)
            lines.append(f"• الأسهم القائمة: {sh.iloc[0]:,.0f} (تغير {ch} خلال {min(4, len(sh) - 1)} أرباع)"
                         + (" ⚠️ تخفيف ملحوظ" if (num(sh.iloc[0]) or 0) > (num(old) or 1) * 1.1 else ""))
        elif info.get("sharesOutstanding"):
            lines.append(f"• الأسهم القائمة: {info['sharesOutstanding']:,.0f}")
        cap = [f for f in filings if f["form"].startswith(CAP_PREFIX)
               or (f["form"].startswith("8-K") and "3.02" in f["items"])]
        if cap:
            lines.append("• طرح/زيادة رأس المال (SEC آخر 6 أشهر):")
            lines += ["  " + l.replace("\n", "\n  ") for l in filing_lines(cap, 5)]
        else:
            lines.append("• لا توجد إيداعات طرح/زيادة رأس مال في آخر 6 أشهر." if filings else "• زيادة رأس المال: غير متاح.")
        # التقسيم
        try:
            sp = t.splits
            if sp is not None and len(sp):
                tz = sp.index.tz
                recent = sp[sp.index > (pd_now(tz) - timedelta(days=5 * 365))]
                for dt, ratio in list(recent.items())[-3:]:
                    r = float(ratio)
                    desc = f"تقسيم عكسي 1:{round(1 / r)}" if r < 1 else f"تقسيم أسهم {round(r, 2)}:1"
                    lines.append(f"• {dt.strftime('%Y-%m-%d')} — {desc}")
            else:
                lines.append("• لا يوجد تقسيم (عادي/عكسي) خلال 5 سنوات.")
        except Exception:
            pass
        rs_f = [f for f in filings if (f["form"].startswith("8-K") and "5.03" in f["items"])
                or f["form"] in ("DEF 14A", "PRE 14A")]
        if rs_f:
            lines.append("• إيداعات قد ترتبط بتقسيم عكسي/تعديل أسهم (تحقق من المستند):")
            lines += ["  " + l.replace("\n", "\n  ") for l in filing_lines(rs_f, 3)]
        rs_news = [n for n in news if re.search(r"reverse (stock )?split", n[1], re.I)][:2]
        for n in rs_news:
            lines.append(f"• خبر: {translate_ar(n[1])}\n  {n[3]}")
        warn = [f for f in filings if f["form"].startswith("8-K") and "3.01" in f["items"]]
        if warn:
            lines.append("• ⚠️ إشعار إدراج/امتثال:")
            lines += ["  " + l.replace("\n", "\n  ") for l in filing_lines(warn, 2)]
        # التوزيعات
        dv = t.dividends
        if dv is not None and len(dv):
            last = dv.iloc[-1]
            lines.append(f"• آخر توزيع: {last:.4f} {cur} بتاريخ {dv.index[-1].strftime('%Y-%m-%d')}"
                         + (f" | العائد {info['dividendYield']:.2f}%" if num(info.get("dividendYield")) else ""))
        else:
            lines.append("• لا توجد توزيعات أرباح.")
        sections.append("🏦 رأس المال والتوزيعات والتقسيم\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("capital: %s", e)

    # ── 7) الخسائر المتراكمة
    try:
        bs = t.quarterly_balance_sheet
        if bs is None or bs.empty:
            bs = t.balance_sheet
        re_ = get_row(bs, ["Retained Earnings"])
        lines = []
        if re_ is not None:
            v = re_.iloc[0]
            if v < 0:
                lines.append(f"• نعم، لديها خسائر متراكمة بقيمة {money(abs(v), cur)} (بتاريخ {re_.index[0].strftime('%Y-%m-%d')})")
            else:
                lines.append(f"• لا، لديها أرباح محتجزة {money(v, cur)}")
        else:
            lines.append("• الأرباح المحتجزة غير متاحة في المصدر.")
        qi = t.quarterly_income_stmt
        ni = get_row(qi, ["Net Income", "Net Income Common Stockholders"])
        if ni is not None and len(ni) >= 4:
            lines.append(f"• صافي آخر 4 أرباع: {money(ni.iloc[:4].sum(), cur)}")
        cash = get_row(bs, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"])
        ocf = get_row(t.quarterly_cashflow, ["Operating Cash Flow"])
        if cash is not None:
            lines.append(f"• النقد المتاح: {money(cash.iloc[0], cur)}")
            if ocf is not None and len(ocf) >= 4 and ocf.iloc[:4].sum() < 0:
                monthly = -ocf.iloc[:4].sum() / 12
                lines.append(f"• حرق نقدي ≈ {money(monthly, cur)}/شهر → يكفي نحو {cash.iloc[0] / monthly:.1f} شهر")
        sections.append("📉 الخسائر المتراكمة\n" + "\n".join(lines))
    except Exception as e:  # noqa
        log.warning("deficit: %s", e)

    # ── 8) الأخبار
    try:
        today = [n for n in news if n[0] and now - n[0] < timedelta(hours=24)][:6]
        shown, title = (today, "📰 أخبار اليوم") if today else (news[:6], "📰 آخر الأخبار المتاحة (لا أخبار خلال 24 ساعة)")
        if shown:
            ar = translate_lines([n[1] for n in shown])
            body = []
            for (dt, _, pub, link), a in zip(shown, ar):
                stamp = dt.strftime("%m-%d %H:%M UTC") if dt else ""
                body.append(f"• {a}\n  {pub} {stamp}\n  {link}")
            sections.append(title + "\n" + "\n".join(body))
        else:
            sections.append("📰 الأخبار\n• لا توجد أخبار متاحة.")
    except Exception as e:  # noqa
        log.warning("news: %s", e)

    if sec_note:
        sections.append(sec_note)
    sections.append("⚠️ المعلومات للاطلاع فقط وليست توصية بيع أو شراء.")
    return ["\n\n".join(sections)]


def pd_now(tz):
    import pandas as pd
    return pd.Timestamp.now(tz=tz) if tz else pd.Timestamp.now()


# ──────────────────────────── توجيه الرسائل ────────────────────────────
CMD_ALIAS = {"tr": "tr", "translate": "tr", "sum": "sum", "summary": "sum", "summarize": "sum",
             "stock": "stock", "s": "stock", "analyze": "stock", "start": "help", "help": "help"}
AR_RE = re.compile(r"^(تلخيص|لخص|ترجمة|ترجم|تحليل|حلل)(?:\s+|$)(.*)", re.S)
AR_MAP = {"تلخيص": "sum", "لخص": "sum", "ترجمة": "tr", "ترجم": "tr", "تحليل": "stock", "حلل": "stock"}
URL_RE = re.compile(r"https?://\S+")


def parse_intent(text):
    t = (text or "").strip()
    if t.startswith("/"):
        parts = t.split(None, 1)
        cmd = CMD_ALIAS.get(parts[0][1:].split("@")[0].lower())
        return cmd, (parts[1] if len(parts) > 1 else "")
    m = AR_RE.match(t)
    if m:
        return AR_MAP[m.group(1)], m.group(2).strip()
    return None, t


def media_of(m):
    if not m:
        return None
    if m.get("document"):
        d = m["document"]
        return d["file_id"], d.get("file_name") or "file.bin"
    if m.get("photo"):
        return m["photo"][-1]["file_id"], "photo.jpg"
    return None


def deliver_translation(chat_id, text, title):
    if len(text) > 120000:
        text = text[:120000]
        send(chat_id, "ℹ️ النص طويل جداً، تمت ترجمة أول 120 ألف حرف فقط.")
    out = translate_ar(text)
    if len(out) <= 3500:
        send(chat_id, out)
    else:
        send_doc(chat_id, out, "translation_ar.txt", caption=f"🌐 الترجمة الكاملة: {title}")


def handle(msg):
    chat_id = msg["chat"]["id"]
    if str(chat_id) != CHAT_ID:
        return
    mid = msg.get("message_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    cmd, arg = parse_intent(text)
    reply = msg.get("reply_to_message") or {}
    reply_text = (reply.get("text") or reply.get("caption") or "").strip()

    if cmd == "help" or (not text and not media_of(msg) and not media_of(reply)):
        send(chat_id, HELP)
        return

    media = media_of(msg) or (media_of(reply) if cmd in ("tr", "sum") and not arg else None)

    # ─ تحليل سهم
    if not media:
        sym = None
        if cmd == "stock":
            sym = find_symbol(arg or reply_text, strict=False)
        elif cmd is None:
            sym = find_symbol(text, strict=True)
        if cmd == "stock" and not sym:
            send(chat_id, "اكتب رمز السهم، مثال: /stock AAPL أو تحليل 2222")
            return
        if sym:
            send(chat_id, f"⏳ جاري تحليل {sym} ... (قد يستغرق دقيقة)", reply_to=mid)
            for part in analyze_stock(sym):
                send(chat_id, part)
            return

    typing(chat_id)

    # ─ جمع المحتوى
    title, content = "النص", ""
    if media:
        file_id, fname = media
        send(chat_id, f"⏳ جاري قراءة «{fname}» ...", reply_to=mid)
        content = extract_text(download_file(file_id), fname)
        title = fname
        action = cmd or ("tr" if fname == "photo.jpg" else "sum")
    else:
        src = arg or reply_text
        m = URL_RE.search(src)
        if m and len(src.strip()) < len(m.group(0)) + 20:
            send(chat_id, "⏳ جاري قراءة الرابط ...", reply_to=mid)
            title, content = fetch_article(m.group(0))
            action = cmd or "sum"
        else:
            content = src
            action = cmd or "tr"

    if len(content.strip()) < 2:
        send(chat_id, "⚠️ لم أجد نصاً لمعالجته.")
        return
    if action == "sum" and len(content) < 400:
        action = "tr"  # النص قصير: الأفضل ترجمته
    if action == "sum":
        content = content[:600000]
        send(chat_id, build_summary(content, title))
    else:
        deliver_translation(chat_id, content, title)


# ──────────────────────────── الحلقة الرئيسية ────────────────────────────
def main():
    log.info("Bot started. Running for %s seconds", RUN_SECONDS)
    deadline = time.time() + RUN_SECONDS
    offset = None
    while time.time() < deadline:
        try:
            r = requests.post(f"{API}/getUpdates", timeout=40, data={
                k: v for k, v in {"offset": offset, "timeout": 25,
                                  "allowed_updates": '["message"]'}.items() if v is not None})
            updates = r.json().get("result", [])
        except Exception as e:  # noqa
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg:
                continue
            try:
                handle(msg)
            except Exception as e:  # noqa
                log.exception("handle error")
                if str(msg["chat"]["id"]) == CHAT_ID:
                    send(msg["chat"]["id"], f"⚠️ حدث خطأ: {str(e)[:300]}")
    log.info("Run window finished, exiting (the workflow will restart).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
