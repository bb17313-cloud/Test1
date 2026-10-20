import time
from flask import Flask, request, jsonify
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

app = Flask(__name__)

# تهيئة المتصفح الحقيقي لتجاوز القيود
print("⏳ جاري فتح المتصفح والاتصال بـ Google AI Studio...")
options = uc.ChromeOptions()
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = uc.Chrome(options=options)
driver.get("https://aistudio.google.com/")
print("✅ المتصفح جاهز! يرجى التأكد من تسجيل الدخول بحسابك في صفحة المتصفح المفتوحة.")

@app.route('/enqueue', methods=['POST'])
def enqueue():
    data = request.json
    prompt = data.get("prompt")
    
    try:
        # البحث عن صندوق الإدخال في Google AI Studio
        wait = WebDriverWait(driver, 20)
        input_box = wait.until(EC.presence_of_element_located((By.TAG_NAME, "textarea")))
        input_box.clear()
        input_box.send_keys(prompt)
        time.sleep(1)
        input_box.send_keys(Keys.ENTER)
        
        # الانتظار حتى يتم توليد الإجابة
        time.sleep(10)
        
        # جلب أحدث رد من الصفحة
        responses = driver.find_elements(By.CSS_SELECTOR, ".model-response-text, .response-container, ms-chat-turn")
        if responses:
            answer = responses[-1].text
            return jsonify({"status": "success", "result": answer})
        else:
            return jsonify({"status": "error", "message": "لم يتم العثور على رد في المتصفح"})
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    # تشغيل خادم محلي يستقبل الطلبات
    app.run(host='0.0.0.0', port=5000)
