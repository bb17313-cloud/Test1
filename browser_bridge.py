import time
from flask import Flask, request, jsonify
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

app = Flask(__name__)

print("⏳ جاري فتح المتصفح والاتصال بـ Google AI Studio...")
options = uc.ChromeOptions()
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')

# فتح المتصفح
driver = uc.Chrome(options=options)
driver.get("https://aistudio.google.com/")
print("✅ تم فتح المتصفح! يرجى التأكد من تسجيل الدخول بحساب Google الخاص بك في الصفحة المفتوحة.")

@app.route('/ask', methods=['POST'])
def ask():
    data = request.json or {}
    prompt = data.get("prompt", "")
    
    if not prompt:
        return jsonify({"status": "error", "message": "النص فارغ"}), 400

    try:
        wait = WebDriverWait(driver, 20)
        input_box = wait.until(EC.presence_of_element_located((By.TAG_NAME, "textarea")))
        input_box.clear()
        input_box.send_keys(prompt)
        time.sleep(1)
        input_box.send_keys(Keys.ENTER)
        
        print("⏳ جاري انتظار التوليد في المتصفح...")
        time.sleep(10)
        
        responses = driver.find_elements(By.CSS_SELECTOR, ".model-response-text, .response-container, ms-chat-turn")
        if responses:
            return jsonify({"status": "success", "result": responses[-1].text})
        else:
            return jsonify({"status": "error", "message": "تعذر قراءة الرد من الصفحة"})
    except Exception as e:
        print("خطأ في المتصفح:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # يعمل على المنفذ 5000 ويستقبل الطلبات
    app.run(host='0.0.0.0', port=5000)
