import json
import os
import hashlib
from datetime import datetime

import requests
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv
import time
import logging
import logging.handlers
from sqlalchemy import create_engine, text, exc
from sqlalchemy.orm import scoped_session, sessionmaker
from contextlib import contextmanager
import re

#Настройки логгера
logger = logging.getLogger('my_logger')
logger.setLevel(logging.DEBUG)

#хендлер для ротации, имя файла, кодировка, максимальный размер в байтах, количество файлов
handler = logging.handlers.RotatingFileHandler(
    'tbank.log',
    encoding='utf-8',
    maxBytes=10*1024*1024,
    backupCount=5
)

# формат сообщений
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


# Загружаем переменные окружения
load_dotenv()

timenow = int(time.time())

app = Flask(__name__)

# Настройки Т-Банка
TERMINAL_KEY = os.getenv('TBANK_TERMINAL_KEY')
PASSWORD = os.getenv('TBANK_PASSWORD')
PAYMENT_URL = 'https://securepay.tinkoff.ru/v2/Init'
SUCCES_URL = os.getenv('SUCCESS_URL')

#Настройки базы
DB_URI = f'mysql+pymysql://{os.getenv("DB_USER")}:{os.getenv("DB_PASS")}@{os.getenv("DB_HOST")}/{os.getenv("DB_NAME")}?charset=utf8mb4'
engine = create_engine(
    DB_URI,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800
)
db_session = scoped_session(sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
))

MAX_RETRIES = 3  # Максимальное количество попыток подключения к БД
RETRY_DELAY = 1  # Начальная задержка между попытками в секундах

# Список разрешенных IP-адресов (замените на реальные)
ALLOWED_IPS = {
    '91.194.226.0',
    '91.218.132.0',
    '91.218.133.0',
    '91.218.134.0',
    '91.218.135.0',
    '212.49.24.0',
    '212.233.80.0',
    '212.233.81.0',
    '212.233.82.0',
    '212.233.83.0',
    '91.194.226.181'
}

@app.route('/create')
def create():
    return render_template('index.html')



@app.route('/payment_callback', methods=['POST'])
def payment_callback():
    # Проверка IP-адреса клиента
    client_ip = request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For') or request.remote_addr
    print(f"Real client IP: {client_ip}")
    print(f"X-Real-Ip header: {request.headers.get('X-Real-Ip')}")
    print(f"X-Forwarded-For header: {request.headers.get('X-Forwarded-For')}")

    request_data = {
        'timestamp': datetime.now().isoformat(),
        'client_ip': client_ip,
        'method': request.method,
        'path': request.path,
        'headers': dict(request.headers),
        'data': request.get_data(as_text=True)  # Логируем сырые данные
    }
    logger.info("Incoming request:\n" + json.dumps(request_data, indent=2, ensure_ascii=False))

    if client_ip not in ALLOWED_IPS:
        logger.info(f'{client_ip} вне списка разрешенных')
        return jsonify({
            "code": "FORBIDDEN",
            "message": "Access denied"
        }), 403

    # Получение и проверка JSON-данных
    data = request.get_json()
    if not data:
        logger.info(f'Запрос не содержит JSON')
        return jsonify({
            "code": "BAD_REQUEST",
            "message": "Missing JSON data"
        }), 400

    # Проверка обязательных полей
    required_fields = ['Status', 'OrderId', 'Amount']
    for field in required_fields:
        if field not in data:
            logger.info(f'Запрос не содержит обязательного поля{field}')
            return jsonify({
                "code": "INVALID_DATA",
                "message": f"Missing required field: {field}"
            }), 400

    # Проверка статуса платежа
    if data['Status'] != 'CONFIRMED':
        logger.info(f"Статус платежа {data['Status']}")
        return jsonify({
            "code": "UNSUPPORTED_STATUS",
            "message": f"Ignoring status: {data['Status']}"
        }), 200

    # Извлечение uid из OrderId
    order_id = data['OrderId']
    uid_match = re.match(r'^(\d+)_', order_id)

    if not uid_match:
        logger.info(f"OrderId имеет неожиданный формат - {order_id}")
        return jsonify({
            "code": "INVALID_ORDER_ID",
            "message": "OrderId format is invalid"
        }), 400

    try:
        uid = int(uid_match.group(1))
    except ValueError:
        logger.info(f"UID содержит неожиднные символы - {uid_match.group(1)}")
        return jsonify({
            "code": "INVALID_UID",
            "message": "UID must be numeric"
        }), 400

    retry_count = 0
    retry_delay = RETRY_DELAY

    amount = int(data.get("Amount"))/100
    comment = f'orderId-{data.get("OrderId")}_paymentId-{data.get("PaymentId")}_amount-{amount}_cardId-{data.get("CardId")}'
    what = 'tBank_payment'
    what_id = data.get('OrderId')

    while retry_count <= MAX_RETRIES:
        try:
            with db_session() as session:
                # Обновляем баланс атомарной операцией
                query = text("""
                        UPDATE users
                        SET deposit = deposit + :amount
                        WHERE uid = :uid
                    """)
                result = session.execute(
                    query,
                    {'amount': amount, 'uid': uid}
                )
                query2 = text("""
                        INSERT INTO bugh_plategi_info (plategid, comment, what, what_id) 
                        SELECT 
                        COALESCE(MAX(plategid), 0) + 1,
                        :comment, :what, :what_id
                        FROM bugh_plategi_info
                    """)
                result = session.execute(
                    query2, {'comment': comment, 'what': what, 'what_id': what_id}
                )
                session.commit()

                # Проверяем, была ли обновлена запись
                if result.rowcount == 0:
                    logger.error(f"Пользователь с uid={uid} не найден")
                    return "Пользователь не найден", 404

                logger.info(f"Баланс пользователя {uid} увеличен на {amount}")
                break

        except exc.OperationalError as e:
            retry_count += 1
            if retry_count > MAX_RETRIES:
                logger.error(f"Сетевая ошибка после {MAX_RETRIES} попыток: {e}")
                return "Ошибка сервера", 500

            logger.warning(f"Сетевая ошибка (попытка {retry_count}): {e}. Повтор через {retry_delay} сек")
            time.sleep(retry_delay)
            retry_delay *= 2  # Экспоненциальная задержка

        except exc.SQLAlchemyError as e:
            logger.error(f"Ошибка базы данных: {e}")
            session.rollback()
            return "Ошибка сервера", 500

    # Пример логирования (замените на реальную логику)
    print(f"✅ Подтвержден платеж для uid={uid}")
    print(f"   OrderId: {order_id}")
    print(f"   Сумма: {data['Amount']} копеек")
    print(f"   ID платежа: {data.get('PaymentId')}")

    # Успешный ответ платежной системе
    return jsonify({"code": "SUCCESS"}), 200

@app.route('/', methods=['POST'])
def create_payment():
    ip = request.remote_addr
    logger.info(f'{ip} - Получен запрос: {request.form}')
    uid = request.form['uid']
    # Получаем данные из формы ImmutableMultiDict([('uid', '6343'), ('fio', 'Ветюгов Константин Александрович'),
    # ('amount', '5.00'), ('paygateway', 'unknow'), ('summa', '5')])
    amount = request.form.get('amount', '1000')  # Сумма в копейках (например, 100.00 RUB = 10000 копеек)
    amount = int(float(amount))
    order_id = uid + "_" + str(timenow)  # Уникальный ID заказа

    logger.info(f'{ip} - Полученны данные, Order_ID: {order_id}, Amount: {amount}')

    # Формируем параметры запроса для API Т-Банка
    payload = {
        'TerminalKey': TERMINAL_KEY,
        'Amount': amount * 100,
        'OrderId': order_id,
        'NotificationURL': f'{SUCCES_URL}/payment_callback',
        'SuccessURL': f'{SUCCES_URL}/{uid}/{amount}'
    }

    logger.info(f'Сформирован запрос для генерации токена: {payload}')

    # Генерация токена для подписи запроса
    token = generate_token(payload)
    logger.info(f'Сгенерирован токен: {token}')

    payload['Token'] = token
    logger.info(f'Сформирован запрос в Т-Банк: {payload}')

    try:
        # Отправляем запрос к API Т-Банка
        response = requests.post(PAYMENT_URL, json=payload)
        logger.info(f'Подготовлен для отправки запрос в Т-Банк: {payload}')
        response_data = response.json()

        if response_data.get('Success'):
            # Перенаправляем пользователя на страницу оплаты
            return redirect(response_data['PaymentURL'])
        else:
            return jsonify({'error': response_data.get('Message', 'Ошибка создания платежа')}), 400
    except Exception as e:
        logger.error(f'Ошибка: {e}')
        return jsonify({'error': str(e)}), 500

def generate_token(payload):
    # Собираем параметры для токена
    token_data = {
        'TerminalKey': payload['TerminalKey'],
        'Amount': payload['Amount'],
        'NotificationURL': payload['NotificationURL'],
        'OrderId': payload['OrderId'],
        'Password': PASSWORD,
        'SuccessURL': payload['SuccessURL']
    }

    m_token_data = token_data.copy()
    m_key = {"Password"}
    for key in m_key:
        if key in m_token_data:
            m_token_data[key] = "***"
    logger.info(f'Собран запрос для генерации токена: {m_token_data}')

    # Сортируем ключи и объединяем значения
    sorted_values = ''.join(str(token_data[key]) for key in sorted(token_data.keys()))
    # Генерируем SHA-256 хеш
    return hashlib.sha256(sorted_values.encode('utf-8')).hexdigest()


@app.route('/success/<int:uid>/<int:amount>', methods=['POST'])
def success(uid, amount):
    logger.info(f'Получен ответ об операции: {request.json}')
    # Проверка TerminalKey
    data = request.get_json()
    if not data:
        logger.error("Отсутствует JSON в запросе")
        return "Некорректный запрос", 400


    t_key = data.get('TerminalKey')
    if TERMINAL_KEY != t_key:
        logger.error(f'ID Терминала не совпадают, присланый ID {t_key}')
        return "ID Терминала не совпадают", 403

    status_pay = data.get('Status')
    if status_pay != 'CONFIRMED':
        logger.info(f'Статус платежа не CONFIRMED, status = {status_pay}')
        return "Статус платежа не CONFIRMED"



    return render_template('success.html', uid=uid, amount=amount)


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()
    logger.info(f"Закрытие сессии БД")

if __name__ == '__main__':
    logger.info(f"Запуск приложения")
    app.run(debug=True)