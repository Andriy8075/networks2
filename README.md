# ЛР2. Інвентаризація контейнерної мережі

Скрипт запускається на Windows і через SSH збирає дані з Ubuntu у WSL, де розгорнуто проєкт ЛР1.

## Встановлення

```text
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python для цього запуску: 3.13.7.

## Інвентаризація

Хост, порт, користувач SSH, шлях до ключа, назва проєкту, адреса проксі і каталог Compose читаються з файлу `.env` поруч зі скриптом. Зразок імен змінних є у `.env.example`.

```text
.venv\Scripts\python.exe inventory.py
```

Скрипт створює `inventory.json` і `report.csv` у цьому каталозі.

## Періодичний моніторинг

```text
.venv\Scripts\python.exe inventory.py --monitor --interval 10
```

Результат дописується у `monitoring.csv`.
