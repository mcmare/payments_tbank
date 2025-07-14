import os
import hashlib
import requests
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv
import time

# Загружаем переменные окружения
load_dotenv()

timenow = int(time.time())

app = Flask(__name__)

# Настройки Т-Банка
TERMINAL_KEY = os.getenv('TBANK_TERMINAL_KEY')
PASSWORD = os.getenv('TBANK_PASSWORD')
PAYMENT_URL = 'https://securepay.tinkoff.ru/v2/Init'

# @app.route('/')
# def index():
#     return render_template('index.html')

@app.route('/', methods=['POST'])
def create_payment():
    # Получаем данные из формы ImmutableMultiDict([('uid', '6343'), ('fio', 'Ветюгов Константин Александрович'),
    # ('amount', '5.00'), ('paygateway', 'unknow'), ('summa', '5')])
    amount = request.form.get('amount', '1000')  # Сумма в копейках (например, 100.00 RUB = 10000 копеек)
    order_id = request.form.get('uid', 'order_123') + "_" + str(timenow)  # Уникальный ID заказа
    print(amount, order_id)
    # Формируем параметры запроса для API Т-Банка
    payload = {
        'TerminalKey': TERMINAL_KEY,
        'Amount': int(float(amount)) * 100,
        'OrderId': order_id
    }

    print(payload)

    # Генерация токена для подписи запроса
    token = generate_token(payload)
    print(token)

    payload['Token'] = token
    print(payload)

    try:
        # Отправляем запрос к API Т-Банка
        response = requests.post(PAYMENT_URL, json=payload)
        print(response.text)
        response_data = response.json()

        if response_data.get('Success'):
            # Перенаправляем пользователя на страницу оплаты
            return redirect(response_data['PaymentURL'])
        else:
            return jsonify({'error': response_data.get('Message', 'Ошибка создания платежа')}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def generate_token(payload):
    # Собираем параметры для токена
    token_data = {
        'TerminalKey': payload['TerminalKey'],
        'Amount': payload['Amount'],
        'OrderId': payload['OrderId'],
        'Password': PASSWORD
    }
    print(token_data)
    # Сортируем ключи и объединяем значения
    sorted_values = ''.join(str(token_data[key]) for key in sorted(token_data.keys()))
    # Генерируем SHA-256 хеш
    return hashlib.sha256(sorted_values.encode('utf-8')).hexdigest()

@app.route('/success')
def success():
    return render_template('success.html')

@app.route('/cancel')
def cancel():
    return render_template('cancel.html')

if __name__ == '__main__':
    app.run(debug=True)