import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

# قراءة الإعدادات
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# سجل التنبيهات: يمنع التكرار إذا لم يتغير السعر ويرقّم التنبيهات لكل سهم
alerts_state = {"date": None, "stocks": {}}

class FinvizRowsParser(HTMLParser):
    """يقرأ صفوف جدول الفاحص مباشرة: كل صف data-row-key وكل خلية data-column-id"""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.cell_id = None
        self.cell_depth = 0
        self.cell_text = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        a = dict(attrs)
        if self.cell_id is not None:
            self.cell_depth += 1
            return
        if "data-row-key" in a:
            self.current_row = {"row_key": a["data-row-key"]}
            self.rows.append(self.current_row)
        elif self.current_row is not None and "data-column-id" in a:
            self.cell_id = a["data-column-id"]
            self.cell_depth = 1
            self.cell_text = []

    def handle_endtag(self, tag):
        if tag != "div" or self.cell_id is None:
            return
        self.cell_depth -= 1
        if self.cell_depth == 0:
            self.current_row[self.cell_id] = "".join(self.cell_text).strip()
            self.cell_id = None

    def handle_data(self, data):
        if self.cell_id is not None:
            self.cell_text.append(data)

def fetch_finviz_signal(signal, limit=20):
    """جلب أسهم Signal معيّن من صفحة الفاحص مباشرة مع فحص إضافي للتأكد من جلب البيانات"""
    url = f"https://finviz.com/screener.ashx?v=111&s={signal}"
    try:
        res = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
        res.raise_for_status()
        parser = FinvizRowsParser()
        parser.feed(res.text)
        
        rows = []
        seen_keys = set()
        for row in parser.rows:
            key = row.get("row_key")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(row)
        return rows[:limit]
    except Exception as e:
        print(f"⚠️ خطأ أثناء الاتصال بـ Finviz للرمز {signal}: {e}")
        return []

def send_telegram_message(message, parse_mode="HTML"):
    """إرسال التقرير المنسق إلى التلغرام"""
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ خطأ: لم يتم العثور على إعدادات BOT_TOKEN أو CHAT_ID.")
        return False
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"❌ فشل إرسال رسالة التلغرام: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")
        return False

def get_alert_state():
    """إرجاع سجل التنبيهات وتصفيره عند بداية يوم جديد (توقيت نيويورك)"""
    today = datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d")
    if alerts_state["date"] != today:
        alerts_state["date"] = today
        alerts_state["stocks"] = {}
    return alerts_state["stocks"]

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None

# بنود نموذج 8-K كما تعرّفها SEC
SEC_8K_ITEMS = {
    "1.01": "اتفاقية جوهرية",
    "1.02": "إنهاء اتفاقية جوهرية",
    "1.03": "إفلاس / حراسة قضائية",
    "2.01": "استحواذ / دمج أو بيع أصول",
    "2.02": "إعلان نتائج مالية",
    "2.03": "التزام مالي / دين جديد",
    "2.04": "تسريع التزام مالي",
    "2.05": "تكاليف إعادة هيكلة",
    "2.06": "انخفاض جوهري في قيمة أصول",
    "3.01": "إشعار شطب / مخالفة شروط الإدراج",
    "3.02": "إصدار أسهم غير مسجل",
    "3.03": "تعديل حقوق المساهمين",
    "4.01": "تغيير المدقق",
    "4.02": "إعادة صياغة قوائم مالية",
    "5.01": "تغيّر السيطرة",
    "5.02": "تغييرات الإدارة",
    "5.03": "حوكمة الشركة",
    "5.05": "حوكمة الشركة",
    "5.07": "تصويت المساهمين",
    "5.08": "ترشيحات مجلس الإدارة",
    "7.01": "إفصاح عادل (Reg FD)",
    "8.01": "أحداث أخرى",
    "9.01": "مرفقات مالية",
}

SEC_FORM_NAMES = {
    "10-K": "التقرير السنوي",
    "10-Q": "التقرير الفصلي",
    "20-F": "التقرير السنوي (مصدر أجنبي)",
    "6-K": "تقرير مصدر أجنبي",
    "3": "بيان ملكية مبدئي لمطلع",
    "4": "تغيّر ملكية مطلع",
    "5": "بيان ملكية سنوي لمطلع",
    "144": "إشعار بيع أسهم لمطلع",
    "S-1": "طرح / تسجيل أوراق مالية",
    "S-3": "طرح / تسجيل أوراق مالية",
    "F-1": "طرح / تسجيل أوراق مالية",
    "F-3": "طرح / تسجيل أوراق مالية",
    "S-8": "تسجيل أسهم برامج الموظفين",
    "DEF 14A": "بيان الجمعية العمومية (حوكمة)",
    "PRE 14A": "بيان الجمعية العمومية (حوكمة)",
}

def get_sec_description(f_form, f_items=""):
    f_upper = str(f_form).upper().strip()
    is_amend = f_upper.endswith("/A")
    base = f_upper[:-2] if is_amend else f_upper
    prefix = "تعديل: " if is_amend else ""

    if base == "8-K":
        names = []
        for code in str(f_items or "").split(","):
            name = SEC_8K_ITEMS.get(code.strip())
            if name and name not in names:
                names.append(name)
        if len(names) > 1 and "مرفقات مالية" in names:
            names.remove("مرفقات مالية")
        if names:
            return prefix + " / ".join(names[:2])
        return prefix + "إفصاح جوهري"

    if "13D" in base:
        return prefix + "ملكية جوهرية 5%+ (نية نشطة)"
    if "13G" in base:
        return prefix + "ملكية جوهرية 5%+ (استثمار سلبي)"
    if base.startswith("424B"):
        return prefix + "طرح / تسجيل أوراق مالية"
    if base.startswith("NT "):
        return "إشعار تأخر تقديم التقرير"
    if base in SEC_FORM_NAMES:
        return prefix + SEC_FORM_NAMES[base]
    return prefix + "إفصاح رسمي"

def fetch_sec_filings_only(symbol):
    symbol = symbol.upper().strip()
    sec_headers = {"User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"}
    filings = []
    cik_str_val = None
    try:
        tickers_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers, timeout=5)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for idx, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik_raw = str(val.get("cik_str"))
                    cik = cik_raw.zfill(10)
                    cik_str_val = cik_raw
                    break
            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=5)
                if sub_res.status_code == 200:
                    recent = sub_res.json().get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accession_numbers = recent.get("accessionNumber", [])
                    primary_docs = recent.get("primaryDocument", [])
                    filing_dates = recent.get("filingDate", [])
                    filing_items = recent.get("items", [])
                    for i in range(min(5, len(forms))):
                        f_form = forms[i]
                        f_items = filing_items[i] if i < len(filing_items) else ""
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]
                        if p_doc:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{acc_num}-index.htm"
                        filings.append({"form": f_form, "date": f_date, "url": filing_url, "accession": acc_num, "brief": get_sec_description(f_form, f_items)})
    except Exception as e:
        print(f"⚠️ خطأ في جلب إفصاحات SEC لـ {symbol}: {e}")
    
    sec_lines = []
    seen_accessions = set()
    latest_accession = ""
    if filings:
        latest_accession = filings[0]["accession"]
        for f in filings:
            if f["accession"] in seen_accessions:
                continue
            seen_accessions.add(f["accession"])
            sec_lines.append(f"• <a href='{f['url']}'>{html.escape(str(f['form']))}</a> · <code>{f['date']}</code> — {html.escape(f['brief'])}")
            if len(sec_lines) >= 4:
                break
    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession

def is_excluded_sector(stock):
    """استبعاد أسهم البنوك والمطاعم والمشروبات والتأمين"""
    sector = str(stock.get('sector', '')).lower()
    industry = str(stock.get('industry', '')).lower()
    exclude_keywords = ['bank', 'insurance', 'restaurant', 'beverage']
    for kw in exclude_keywords:
        if kw in sector or kw in industry:
            return True
    return False

def send_stock_cards(session_en, session_ar, stocks, signal_label, skip_tickers=None):
    """إرسال البطاقات مع فصل النصوص لمنع مشكلة انعكاس الاتجاهات في تيليجرام"""
    state = get_alert_state()
    sent = 0
    skip_tickers = skip_tickers or set()
    
    for stock in stocks:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        if not ticker or price is None or price <= 0:
            continue
        
        if ticker in skip_tickers:
            continue
        
        volume = to_float(stock.get('volume'))
        if volume is None or volume <= 10000:
            continue
        
        if is_excluded_sector(stock):
            continue
        
        key = f"{session_en}:{ticker}"
        last = state.get(key)
        
        if last and price <= last["price"]:
            continue
        
        alert_no = (last["alert_no"] + 1) if last else 1
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)
        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        sec_page_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_val or ticker}&type=&dateb=&owner=include&count=40"
        )
        
        # تنسيق مرتب ومنفصل لتجنب مشاكل الاتجاهات (Bidi) في تيليجرام
        message = (
            f"🚨 <b>تنبيه رقم {alert_no}</b>\n"
            f"🌐 <code>{html.escape(session_en)}</code>\n"
            f"📌 <i>{html.escape(session_ar)}</i>\n\n"
            f"📡 Pattern: <b>{html.escape(signal_label)}</b>\n\n"
            f"🔹 <b>{html.escape(ticker)}</b>\n"
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(str(stock.get('change', '-')))}</code>\n"
            f"📊 Vol: <code>{html.escape(str(stock.get('volume', '-')))}</code>\n"
            f"🏭 القطاع: <code>{html.escape(str(stock.get('sector', '-')))}</code>\n"
            f"🏷️ الصناعة: <code>{html.escape(str(stock.get('industry', '-')))}</code>\n\n"
            f"📄 <b>آخر إفصاحات SEC</b>\n"
            f"{sec_summary}\n\n"
            f"📈 <a href='{tv_url}'>TradingView</a> | 🏛️ <a href='{html.escape(sec_page_url)}'>SEC</a>"
        )
        
        if send_telegram_message(message, parse_mode="HTML"):
            state[key] = {"price": price, "alert_no": alert_no}
            sent += 1
            time.sleep(1)
    
    return sent

def fetch_and_alert(session_en, session_ar):
    seen_tickers = set()

    # الأنماط الصحيحة والمعتمدة في الفاحص
    patterns = [
        ('ta_headandshoulders', "Head & Shoulders"),
        ('ta_headandshoulders_weekly', "Head & Shoulders (Weekly)"),
        ('ta_doublebottom', "Double Bottom"),
        ('ta_doublebottom_weekly', "Double Bottom (Weekly)"),
        ('ta_multiplebottom', "Multiple Bottom"),
        ('ta_multiplebottom_weekly', "Multiple Bottom (Weekly)"),
    ]

    for sig, label in patterns:
        try:
            print(f"⏳ [{session_en}] فحص نمط {label}...")
            data_rows = fetch_finviz_signal(sig)
            if not data_rows:
                print(f"⚠️ لم يتم العثور على أسهم في {label}.")
                continue
            
            sent = send_stock_cards(session_en, session_ar, data_rows, label, skip_tickers=seen_tickers)
            print(f"✅ {label}: تم إرسال {sent} بطاقة.")
            for s in data_rows:
                t = str(s.get('row_key', '')).strip().upper()
                if t:
                    seen_tickers.add(t)
        except Exception as e:
            print(f"❌ خطأ في جلب {label}: {e}")

        time.sleep(4)

def main_loop():
    print("🚀 تم تشغيل الرادار ليعمل 3 مرات فقط يومياً حسب المواعيد المحددة...")
    send_telegram_message("🟢 <b>تم تشغيل رادار الأنماط الفنية بنجاح</b>\n📌 (يعمل 3 مرات يومياً بالمواعيد المحددة)")
    
    executed_slots = {"date": None, "slots": set()}
    
    riyadh_tz = pytz.timezone('Asia/Riyadh')
    ny_tz = pytz.timezone('America/New_York')

    while True:
        try:
            now_riyadh = datetime.now(riyadh_tz)
            now_ny = datetime.now(ny_tz)
            
            today_str = now_riyadh.strftime("%Y-%m-%d")
            if executed_slots["date"] != today_str:
                executed_slots["date"] = today_str
                executed_slots["slots"] = set()
            
            # 1. قبل افتتاح السوق بساعتين (07:30 صباحاً بتوقيت نيويورك)
            if now_ny.hour == 7 and now_ny.minute == 30 and "slot1" not in executed_slots["slots"]:
                print("⏰ تنفيذ موعد: قبل افتتاح السوق بساعتين")
                fetch_and_alert("Pre-Market Scan", "قبل الافتتاح بـ ساعتين 🌅")
                executed_slots["slots"].add("slot1")
                
            # 2. بعد إغلاق السوق بساعة (17:00 مساءً بتوقيت نيويورك)
            if now_ny.hour == 17 and now_ny.minute == 0 and "slot2" not in executed_slots["slots"]:
                print("⏰ تنفيذ موعد: بعد إغلاق السوق بساعة")
                fetch_and_alert("Post-Market Scan", "بعد الإغلاق بساعة 🌆")
                executed_slots["slots"].add("slot2")
                
            # 3. الساعة 17:00 بتوقيت السعودية كل أيام الأسبوع
            if now_riyadh.hour == 17 and now_riyadh.minute == 0 and "slot3" not in executed_slots["slots"]:
                print("⏰ تنفيذ موعد: الساعة 17:00 بتوقيت السعودية")
                fetch_and_alert("Daily KSA Scan", "تحديث السوق اليومي 17:00 📊")
                executed_slots["slots"].add("slot3")
                
        except Exception as e:
            print(f"❌ خطأ في حلقة المواعيد: {e}")
            
        time.sleep(30)

if __name__ == "__main__":
    main_loop()
