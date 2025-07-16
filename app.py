import os
import hashlib
import requests
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv
import time
import logging
import logging.handlers
import mariadb
from mariadb import OperationalError


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
DB_CONFIG = {
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'host': os.getenv('DB_HOST'),
    'database': os.getenv('DB_NAME'),
    'port': int(os.getenv('DB_PORT')),
    'autocommit': False
}

MAX_RETRIES = 3  # Максимальное количество попыток подключения к БД
RETRY_DELAY = 1  # Начальная задержка между попытками в секундах


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
        'SuccessURL': f'{SUCCES_URL}/success/{uid}/{amount}',
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
    if request.method == 'POST':
        t_key = request.json.get('TerminalKey')
    if TERMINAL_KEY != t_key:
        logger.info(f'ID Терминала не совпадают, присланый ID {t_key}')
        return "ID Терминала не совпадают", 403

    # Попытки подключения к БД с задержкой
    conn = None
    cursor = None
    retry_delay = RETRY_DELAY

    for attempt in range(MAX_RETRIES):
        try:
            conn = mariadb.connect(**DB_CONFIG)
            cursor = conn.cursor()
            logger.debug(f"Успешное подключение к БД (попытка {attempt + 1})")
            break

        except OperationalError as e:
            logger.warning(f"Ошибка подключения к БД (попытка {attempt + 1}): {e}")
            if attempt < MAX_RETRIES - 1:
                logger.info(f"Повторная попытка через {retry_delay} сек...")
                time.sleep(retry_delay)
                retry_delay *= 2  # задержка
            else:
                logger.error("Не удалось подключиться к БД после нескольких попыток")
                return "Ошибка сервера", 500

    # Обновление записи в БД
    try:
        # Обновляем баланс пользователя
        query = """
        UPDATE users
        SET balance = balance + ?
        WHERE uid = ?
        """
        cursor.execute(query, (amount, uid))

        # Проверяем, была ли обновлена хотя бы одна строка
        if cursor.rowcount == 0:
            logger.error(f"Пользователь с uid={uid} не найден")
            conn.rollback()
            return "Пользователь не найден", 404

        conn.commit()
        logger.info(f"Баланс пользователя {uid} увеличен на {amount}")

    except mariadb.Error as e:
        logger.error(f"Ошибка SQL: {e}")
        if conn:
            conn.rollback()
        return "Ошибка сервера", 500

    finally:
        # закрываем соединение
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return render_template('success.html', uid=uid, amount=amount)

# @app.route('/success')
# def success():
#     return render_template('success.html')
#
# @app.route('/cancel')
# def cancel():
#     return render_template('cancel.html')


if __name__ == '__main__':
    app.run(debug=True)