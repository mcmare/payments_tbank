import os
import hashlib
import requests
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv
import time
import logging
import logging.handlers
from sqlalchemy import create_engine, text, exc
from sqlalchemy.orm import scoped_session, sessionmaker


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


@app.route('/create')
def create():
    return render_template('index.html')

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
        'NotificationURL': f'{SUCCES_URL}/success/{uid}/{amount}',
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
        'NotificationURL': payload['NotificationURL']
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


@app.route('/success/<int:uid>/<int:amount>', methods=['POST', 'GET'])
def success(uid, amount):
    # Для GET запроса
    if request.method == 'GET':
        # Собираем все доступные данные из запроса
        get_data = {
            'method': 'GET',
            'uid': uid,
            'amount': amount,
            'query_params': request.args.to_dict(),
            'headers': dict(request.headers)
        }

        # Логируем и выводим в консоль
        logger.info(f'Получен GET запрос: {get_data}')
        print(f'\n--- GET запрос на /success ---')
        print(f'Параметры пути: uid={uid}, amount={amount}')
        print(f'Query параметры: {request.args.to_dict()}')
        print(f'Заголовки:')
        for header, value in request.headers.items():
            print(f'  {header}: {value}')

        # Возвращаем простой ответ
        return f'GET запрос получен. UID: {uid}, Amount: {amount}', 200

    if request.method == 'POST':
        plategid = request.form['OrderId']
        comment = f'orderId-{request.form['OrderId']}_paymentId-{request.form["PaymentId"]}_amount-{request.form["Amount"]}_cardId-{request.form["CardId"]}'
        what = 'tBank_payment'
        what_id = request.form['OrderId']
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
        if status_pay != 'AUTHORIZED':
            logger.info(f'Статус платежа не AUTHORIZED, status = {status_pay}')
            return "Статус платежа не AUTHORIZED"

        # Обновление баланса с повторными попытками
        retry_count = 0
        retry_delay = RETRY_DELAY

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
                        Values (:plategid, :comment, :what, :what_id)
                    """)
                    result = session.execute(
                        query2,{'plategid': plategid, 'comment': comment, 'what': what, 'what_id': what_id}
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

        return render_template('success.html', uid=uid, amount=amount)


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()
    logger.info(f"Закрытие сессии БД")

if __name__ == '__main__':
    logger.info(f"Запуск приложения")
    app.run(debug=True)