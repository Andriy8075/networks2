# ЛР2. Інвентаризація контейнерної мережі

Скрипт запускається на Windows і через SSH збирає дані з Ubuntu у WSL, де розгорнуто проєкт ЛР1.

## Встановлення

```text
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python для цього запуску: 3.13.7.

## Інвентаризація

```text
.venv\Scripts\python.exe inventory.py --host 127.0.0.1 --port 22 --user andrii --key %USERPROFILE%\.ssh\id_rsa --project code --proxy-url http://127.0.0.1:8080/ --compose-dir /mnt/c/Users/Admin/projects/NetworkLabs/lab1/code
```

Скрипт створює `inventory.json` і `report.csv` у цьому каталозі. Приватний ключ у код і звіт не входить: шлях передається параметром `--key`.

## Періодичний моніторинг

```text
.venv\Scripts\python.exe inventory.py --host 127.0.0.1 --port 22 --user andrii --key %USERPROFILE%\.ssh\id_rsa --project code --proxy-url http://127.0.0.1:8080/ --monitor --interval 10
```

Результат дописується у `monitoring.csv`. Зупинка скрипта: Ctrl+C. Після помилки HTTP-запиту цикл продовжується.
