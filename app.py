import os
import hashlib
import requests
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv
import time
import logging
import logging.handlers
from sqlalchemy import text, exc
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import asyncio
import backoff


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
ASYNC_DB_URI  = f'mysql+aiomysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}?charset=utf8mb4'
async_engine = create_async_engine(
    ASYNC_DB_URI,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=True
)

# Создаем асинхронную фабрику сессий
AsyncSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

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
async def success(uid, amount):
    logger.info(f'Получен ответ об операции: {request.json}')

    # Проверка JSON и TerminalKey
    if not request.is_json:
        logger.error("Отсутствует JSON в запросе")
        return "Некорректный запрос", 400

    data = request.get_json()
    t_key = data.get('TerminalKey')
    if TERMINAL_KEY != t_key:
        logger.error(f'ID Терминала не совпадают, присланый ID {t_key}')
        return "ID Терминала не совпадают", 403

    # Обновление баланса с обработкой ошибок
    try:
        await update_balance(uid, amount)
        logger.info(f"Баланс пользователя {uid} успешно обновлен на +{amount}")
        return render_template('success.html', uid=uid, amount=amount)

    except ValueError as e:
        logger.error(f"Ошибка валидации: {e}")
        return str(e), 400
    except exc.NoResultFound:
        logger.error(f"Пользователь с uid={uid} не найден")
        return "Пользователь не найден", 404
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return "Ошибка сервера", 500

    return render_template('success.html', uid=uid, amount=amount)

@backoff.on_exception(backoff.expo,
                      (exc.OperationalError, exc.DBAPIError),
                      max_tries=5,
                      jitter=backoff.full_jitter)


async def update_balance(uid: int, amount: int):
    """Асинхронное обновление баланса с повторными попытками"""
    if amount <= 0:
        raise ValueError("Некорректная сумма платежа")

    async with AsyncSession(async_engine) as session:
        async with session.begin():
            # Атомарное обновление баланса
            update_query = text("""
                UPDATE users 
                SET deposit = deposit + :amount 
                WHERE uid = :uid
            """)
            result = await session.execute(
                update_query,
                {"amount": amount, "uid": uid}
            )

            # Проверка, что пользователь существует
            if result.rowcount == 0:
                # Дополнительная проверка существования пользователя
                exists_query = text("SELECT 1 FROM accounts WHERE uid = :uid")
                exists = await session.scalar(exists_query, {"uid": uid})
                if not exists:
                    raise exc.NoResultFound("Пользователь не существует")
                else:
                    logger.warning(f"Обновление баланса не затронуло строки для uid={uid}")


@app.route('/health')
async def health():
    try:
        async with AsyncSession(async_engine) as session:
            await session.scalar(text("SELECT 1"))
        return "OK", 200
    except Exception:
        return "Database unavailable", 500

if __name__ == '__main__':
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = ["127.0.0.1:5000"]
    config.worker_class = "asyncio"

    asyncio.run(serve(app, config))