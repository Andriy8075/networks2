# _show_api.py
from pathlib import Path

import docker


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
user = require(env, "SSH_USER")
host = require(env, "SSH_HOST")
port = require(env, "SSH_PORT")

client = docker.DockerClient(base_url=f"ssh://{user}@{host}:{port}")
containers = client.containers.list(
    all=True,
    filters={"label": "com.docker.compose.project=code"},
)
for container in containers:
    container.reload()
    attrs = container.attrs
    print(container.labels.get("com.docker.compose.service"), container.name, container.id)
    print(" image", attrs["Config"]["Image"])
    print(" state", attrs["State"]["Status"], attrs["State"]["StartedAt"])
    for name, net in attrs["NetworkSettings"]["Networks"].items():
        print(" network", name, net.get("IPAddress"))
    print(" ports", attrs["NetworkSettings"]["Ports"])
client.close()
