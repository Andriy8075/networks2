# _show_http.py
from pathlib import Path

import requests


def load_env(path):
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require(env, key):
    if key not in env or not env[key]:
        raise SystemExit(f"У файлі .env немає значення {key}")
    return env[key]


env = load_env(Path(__file__).resolve().parent / ".env")
host = require(env, "PROXY_HOST")
port = require(env, "PROXY_PORT")
url = f"http://{host}:{port}/"

for number in (1, 2):
    response = requests.get(url, timeout=5)
    print(
        f"запит {number}: status={response.status_code} "
        f"elapsed={response.elapsed.total_seconds()} body={response.text.strip()}"
    )
