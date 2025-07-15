#### Устанавливаем виртуальное окружение
```bash
python3 -m venv env
source env/bin/activate
```

#### Устанавливаем зависимости
```bash
pip install -r requirements.txt
```

#### Добавляем файл .env
```bash
torch .env
nano .env
```
```env
TBANK_TERMINAL_KEY=TinkoffBankTest
TBANK_PASSWORD=TinkoffBankTest
SUCCESS_URL=http://example.org
```
#### запуск Flask
```bash
python app.py
```
#### запуск через gunicorn
```bash
pip install gunicorn
```
```bash
gunicorn --workers 3 --threads 2 --bind 0.0.0.0:5000 wsgi:app
```
где \
--workers - число рабочих процессов, CPU * 2 + 1 \
--threads - число потоков на процесс

#### Запуск через службы
создаем файл 
```bash
torch /etc/systemd/system/app.service
nano /etc/systemd/system/app.service
```
```shell
[Unit]
Description=Gunicorn instance to serve myapp
After=network.target

[Service]
User=user
Group=group
WorkingDirectory=/path/to/your/app
ExecStart=/path/to/your/venv/bin/gunicorn --workers 3 --threads 2 --bind 0.0.0.0:5000 wsgi:app
Restart=on-failure
[Install]
WantedBy=multi-user.target
```
перезапускаем systemd и запускаем службу
```bash
sudo systemctl daemon-reload
sudo systemctl start app.service
```