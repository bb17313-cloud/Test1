import os
import json
import re
import html
import time
import requests
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

# ================================
# إعدادات التلغرام والبيئة
# ================================
TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكن_البوت_هنا")
CHAT_ID = os.environ.get("CHAT_ID", "ضع_معرف_الشات_هنا")

RIYADH = ZoneInfo("Asia/Riyadh")
NEW_YORK = ZoneInfo("America/New_York")
SEEN_FILE = "seen_stocks.json"
MAX_SHOWN = 40   # ← الحد الأقصى للأسهم المسترجعة

# ================================
# إدارة جلسات السوق الثلاث
# ================================
def get_current_session():
    now_ny = datetime.now(NEW_YORK)
    
    # أيام عطلة نهاية الأسبوع في أمريكا (السبت والأحد)
    if now_ny.weekday() in:
        return "closed", "⏸️ المغلق (عطلة نهاية الأسبوع)"

    time_num_ny = now_ny.hour * 100 + now_ny.minute

    if 400 <= time_num_ny < 930:
        return "premarket", "🌅 Pre-Market (ما قبل الافتتاح)"
    elif 930 <= time_num_ny < 1600:
        return "market", "🔔 Main Session (السوق الرئيسي)"
    elif 1600 <= time_num_ny < 2000:
        return "postmarket", "🌙 Post-Market (ما بعد الإغلاق)"
    else:
        return "closed", "⏸️ المغلق (خارج أوقات التداول)"

def load_seen():
    today = datetime.now(RIYADH).strftime("%Y-%m-%d")
    session_key, _ = get_current_session()
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today and data.get("session") == session_key:
                return data.get("stocks", {})
    except Exception as e:
        print(f"⚠️ خطأ في قراءة ملف السجل: {e}")
    return {}

def save_seen(stocks_data):
    today = datetime.now(RIYADH).strftime("%Y-%m-%d")
    session_key, _ = get_current_session()
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "date": today,
                "session": session_key,
                "stocks": stocks_data,
                "last_update": datetime.now(RIYADH).strftime("%H:%M:%S")
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ خطأ في حفظ ملف السجل: {e}")

# ================================
# تحليل ووصف إفصاحات SEC
# ================================
def get_sec_description(f_form, item_val="", doc_text=""):
    f_upper = f_form.upper()
    item_str = str(item_val)
    text_lower = doc_text.lower() if doc_text else ""

    if f_upper == "10-K":
        if "item 1a" in text_lower or "risk factors" in text_lower:
            return "عوامل المخاطرة"
        elif "item 7" in text_lower:
            return "تحليل الإدارة للوضع المالي"
        elif "item 8" in text_lower:
            return "القوائم المالية"
        elif "item 3" in text_lower:
            return "القضايا والإجراءات القانونية"
        elif "item 9a" in text_lower:
            return "الضوابط والإجراءات الرقابية"
        return "التقرير المالي السنوي"

    if f_upper == "10-Q":
        if "item 2" in text_lower or "md&a" in text_lower:
            return "تحليل الإدارة الفصلي"
        elif "item 1a" in text_lower:
            return "عوامل المخاطرة الجديدة"
        return "القوائم المالية الفصلية"

    if f_upper == "8-K":
        if "1.01" in item_str:
            return "اتفاقية جوهرية جديدة"
        elif "2.01" in item_str:
            return "استحواذ أو بيع أصول"
        elif "2.02" in item_str:
            return "نتائج الأرباح الأولية"
        elif "5.02" in item_str:
            return "تغييرات الإدارة التنفيذية"
        elif "3.02" in item_str:
            return "مبيعات أسهم غير مسجلة"
        elif "8.01" in item_str:
            return "أحداث جوهرية أخرى"
        return "إفصاح جوهري طارئ"

    if "14A" in f_upper:
        if "compensation" in text_lower:
            return "مكافآت التنفيذيين"
        elif "ownership" in text_lower:
            return "ملكية كبار الملاك"
        return "حوكمة الشركات"

    if f_upper == "6-K":
        return "تقرير شركة أجنبية"

    if f_upper.startswith("424B") or any(f_upper.startswith(p) for p in ["S-1", "S-3", "F-1", "F-3"]):
        if "warrant" in text_lower or "exhibit" in text_lower:
            return "اتفاقيات ومرفقات الإصدار"
        return "نشرة طرح واكتتاب أسهم"

    return "إفصاح رسمي معتمد"

# ================================
# جلب إفصاحات SEC فقط
# ================================
def fetch_sec_filings_only(symbol):
    symbol = symbol.upper().strip()
    sec_headers = {"User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"}
    filings = []
    cik_str_val = None

    try:
        tickers_res = requests.get("https://sec.gov", headers=sec_headers, timeout=5)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for idx, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik = str(val.get("cik_str")).zfill(10)
                    cik_raw = str(val.get("cik_str"))
                    cik_str_val = cik_raw
                    break

            if cik:
                sub_url = f"https://sec.gov{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=5)
                if sub_res.status_code == 200:
                    recent = sub_res.json().get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accession_numbers = recent.get("accessionNumber", [])
                    primary_docs = recent.get("primaryDocument", [])
                    filing_dates = recent.get("filingDate", [])
                    items_list = recent.get("items", [])

                    for i in range(min(10, len(forms))):
                        f_form = forms[i]
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]
                        item_val = items_list[i] if i < len(items_list) else ""

                        if p_doc:
                            filing_url = f"https://sec.gov{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://sec.gov{cik_raw}/{acc_clean}/{acc_num}-index.htm"

                        doc_text = ""
                        if f_form.upper() in ["10-K", "10-Q", "8-K", "6-K"] or f_form.upper().startswith("424B"):
                            try:
                                doc_res = requests.get(filing_url, headers=sec_headers, timeout=3)
                                if doc_res.status_code == 200:
                                    doc_text = doc_res.text
                            except Exception:
                                pass

                        brief_desc = get_sec_description(f_form, item_val, doc_text)
                        filings.append({
                            "form": f_form, "date": f_date, "url": filing_url,
                            "accession": acc_num, "brief": brief_desc,
                        })
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
            sec_lines.append(f"• <b>{f['form']}</b> ({f['date']}): <a href='{f['url']}'>{f['brief']}</a>")
            if len(sec_lines) >= 5:
                break

    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession

# ========================================
# جلب وتصفية الأسهم من TradingView API
# ========================================
def fetch_filtered_stocks(session_type):
    url = "https://tradingview.com"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    # تخصيص الحقول برمجياً بناءً على وضع الجلسة لتتبع الأسعار الصحيحة
    if session_type == "premarket":
        price_field = "premarket_close"
        change_field = "premarket_change"
        volume_field = "premarket_volume"
    elif session_type == "postmarket":
        price_field = "postmarket_close"
        change_field = "postmarket_change"
        volume_field = "postmarket_volume"
    else:
        price_field = "close"
        change_field = "change"
        volume_field = "volume"

    columns = ["name", "description", price_field, change_field, volume_field, "type", "subtype"]

    # طلب الفحص مدمجاً فيه شروط كامل السوق (فحص واحد = طلب واحد)
    payload = {
        "filter": [
            {"left": "type", "operation": "in_range", "right": ["stock"]},       # أسهم فقط
            {"left": "subtype", "operation": "in_range", "right": ["common"]},   # أسهم عادية
            {"left": price_field, "operation": "above", "right": 1.0},           # السعر فوق 1 دولار
            {"left": volume_field, "operation": "above", "right": 50000},        # حجم التداول فوق 50 ألف
            {"left": change_field, "operation": "above", "right": 3.0}           # نسبة الارتفاع فوق 3% كمثال
        ],
        "options": {"lang": "en"},
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": columns,
        "sort": {"sortBy": change_field, "sortOrder": "desc"},                  # الترتيب من الأعلى ارتفاعاً للأقل
        "range": [0, MAX_SHOWN]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
يُرجى استخدام الرمز البرمجي بحذر.
if response.status_code == 200:
result_data = response.json()
stocks_list = []
for item in result_data.get("data", []):
full_ticker = item.get("s", "")
ticker = full_ticker.split(":")[-1] if ":" in full_ticker else full_ticker
d_vals = item.get("d", [])
if len(d_vals) >= 5:
stocks_list.append({
"ticker": ticker,
"company_name": d_vals[1],
"price": d_vals[2] if d_vals[2] is not None else 0.0,
"change_percent": d_vals[3] if d_vals[3] is not None else 0.0,
"volume": d_vals[4] if d_vals[4] is not None else 0
})
return stocks_list
except Exception as e:
print(f"❌ خطأ في مسح TradingView: {e}")
return []

================================


إرسال التنبيهات إلى تلغرام


================================

def send_telegram_message(text):
if not TOKEN or not CHAT_ID:
print("⚠️ إعدادات التلغرام غير مكتملة. تم تخطي الإرسال.")
return
url = f"telegram.org{TOKEN}/sendMessage"
payload = {
"chat_id": CHAT_ID,
"text": text,
"parse_mode": "HTML",
"disable_web_page_preview": True
}
try:
res = requests.post(url, json=payload, timeout=10)
if res.status_code != 200:
print(f"❌ فشل إرسال رسالة التلغرام: {res.text}")
except Exception as e:
print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")

================================


المحرك الرئيسي للفحص والربط


================================

def run_radar():
session_key, session_desc = get_current_session()
print(f"⏳ الجلسة الحالية: {session_desc}")
if session_key == "closed":
print("⏸️ السوق مغلق حالياً. سيتم إيقاف الفحص تلقائياً.")
return
# 1. تحميل سجل الأسهم التي تم فحصها وإرسالها مسبقاً في هذه الجلسة
seen_stocks = load_seen()
# 2. فحص كامل السوق بطلب واحد جلب سريع
print("🔍 جاري جلب ومسح كامل السوق الأمريكي عبر خوادم التصفية السريعة...")
matched_stocks = fetch_filtered_stocks(session_key)
print(f"✅ العثور على {len(matched_stocks)} سهم يطابق الشروط الأساسية.")
any_new_stock = False
# 3. معالجة الأسهم وفلترة الأسهم الجديدة فقط
for stock in matched_stocks:
symbol = stock["ticker"]
# جلب إفصاحات SEC للسهم لمعرفة آخر رقم مستند (Accession)
sec_summary, cik, latest_acc = fetch_sec_filings_only(symbol)
# إذا تم رؤية هذا السهم مسبقاً بنفس آخر إفصاح، يتم تخطيه لمنع التكرار
if symbol in seen_stocks and seen_stocks[symbol] == latest_acc:
continue
# تحديث السجل للسهم الجديد
seen_stocks[symbol] = latest_acc
any_new_stock = True
# 4. بناء رسالة التنبيه الاحترافية للتصاعد المفاجئ
message_text = (
f"🚨 رادار السوق الأمريكي - سهم جديد مطابق!\n\n"
f"🎫 الشركة: {stock['company_name']} ({symbol})\n"
f"💰 السعر الحالي: {stock['price']:.2f}$\n"
f"📈 نسبة التغير: {stock['change_percent']:.2f}%\n"
f"📊 حجم التداول: {stock['volume']:,}\n"
f"⏰ الجلسة: {session_desc}\n\n"
f"📋 آخر إفصاحات SEC المعتمدة:\n{sec_summary}\n\n"
f"📅 توقيت الرصد: {datetime.now(RIYADH).strftime('%Y-%m-%d %H:%M:%S')} بتوقيت الرياض"
)
# إرسال الرسالة فوراً عبر التلغرام
print(f"🚀 إرسال تنبيه للسهم المكتشف حديثاً: {symbol}")
send_telegram_message(message_text)
time.sleep(1) # تأخير بسيط لتجنب حظر التلغرام عند إرسال رسائل متعددة متتالية
# 5. حفظ السجل المحدث إذا تم اكتشاف أي جديد
if any_new_stock:
save_seen(seen_stocks)
else:
print("👍 لا توجد أسهم جديدة تطابق الشروط لم يتم إرسالها من قبل.")
if name == "main":
run_radar()
