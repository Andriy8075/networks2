"""Інвентаризація Compose-проєкту ЛР1 через SSH.

Скрипт працює на машині керування. Docker CLI викликається на Linux-хості
через Paramiko, а Docker Engine API — через Docker SDK поверх SSH.
"""

import argparse
import csv
import json
import re
import shlex
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
import paramiko
import requests

COMMAND_TIMEOUT = 10
HTTP_TIMEOUT = 5
PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VISIT_COUNT = re.compile(r"Visit count:\s*(\d+)")


def configure_stdio():
    """Друкувати український текст у консоль Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Інвентаризація та моніторинг Compose-проєкту ЛР1 через SSH."
    )
    parser.add_argument("--host", default="127.0.0.1", help="SSH-хост Linux VM")
    parser.add_argument("--port", type=int, default=22, help="SSH-порт")
    parser.add_argument("--user", default="andrii", help="SSH-користувач")
    parser.add_argument(
        "--key",
        default=None,
        help="Шлях до приватного SSH-ключа. Якщо не задано, Paramiko шукає ключі у ~/.ssh",
    )
    parser.add_argument("--project", default="code", help="Назва Compose-проєкту")
    parser.add_argument(
        "--proxy-url",
        default="http://127.0.0.1:8080/",
        help="URL веб-проксі для HTTP-перевірки",
    )
    parser.add_argument(
        "--compose-dir",
        default="/mnt/c/Users/Admin/projects/NetworkLabs/lab1/code",
        help="Каталог Compose-проєкту на Linux-хості",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Каталог для inventory.json, report.csv і monitoring.csv",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Періодичний моніторинг замість разової інвентаризації",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10,
        help="Інтервал моніторингу в секундах",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Скільки циклів моніторингу виконати. Без параметра цикл триває до Ctrl+C",
    )
    return parser.parse_args()


def output_dir(args):
    if args.output_dir:
        path = Path(args.output_dir)
    else:
        path = Path(__file__).resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_ssh(host, port, user, key_path, timeout=COMMAND_TIMEOUT):
    """Відкрити SSH-сесію. Ключ береться зі шляху, агента або ~/.ssh."""
    if not PROJECT_NAME.match(user):
        raise ValueError(f"Некоректне ім'я користувача SSH: {user}")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        "look_for_keys": True,
        "allow_agent": True,
    }
    if key_path:
        key_file = Path(key_path)
        if not key_file.is_file():
            raise FileNotFoundError(f"SSH-ключ не знайдено: {key_file}")
        connect_kwargs["key_filename"] = str(key_file)
    client.connect(**connect_kwargs)
    return client


def run_remote(ssh, command, timeout=COMMAND_TIMEOUT):
    """Виконати команду на Linux-хості і повернути код, stdout та stderr."""
    _stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def require_ok(code, out, err, command):
    if code != 0:
        message = err.strip() or out.strip() or f"код виходу {code}"
        raise RuntimeError(f"Команда завершилась з помилкою: {command}\n{message}")
    return out


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def empty_container(service, container_id, name, image, state, started_at):
    return {
        "service": service,
        "id": container_id,
        "name": name,
        "image": image,
        "state": state,
        "started_at": started_at,
        "networks": [],
        "published_ports": [],
    }


def parse_ps(text):
    """Розібрати вивід docker ps: повний ID, образ і стан."""
    containers = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            raise RuntimeError(f"Не вдалося розібрати рядок docker ps: {line}")
        containers.append(
            {
                "id": parts[0],
                "image": " ".join(parts[1:-1]),
                "state": parts[-1],
            }
        )
    return containers


def parse_service_and_start(text, container_id):
    line = text.strip()
    if not line or " " not in line:
        raise RuntimeError(
            f"Не вдалося розібрати сервіс і час запуску контейнера {container_id}: {line!r}"
        )
    service, started_at = line.split(None, 1)
    return service, started_at.strip()


def parse_networks(text):
    networks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, separator, ip_address = line.partition(" ")
        networks.append({"name": name, "ip": ip_address.strip() if separator else ""})
    networks.sort(key=lambda item: item["name"])
    return networks


def parse_published_ports(text):
    ports = [line.strip() for line in text.splitlines() if line.strip()]
    return sorted(ports)


def format_api_ports(ports):
    """Лише прив'язки до хоста. Порт без HostPort не є публікацією."""
    published = []
    for container_port, bindings in (ports or {}).items():
        if not bindings:
            continue
        for binding in bindings:
            host_ip = binding.get("HostIp") or "0.0.0.0"
            host_port = binding.get("HostPort")
            if not host_port:
                continue
            if host_ip == "::":
                host_ip = "[::]"
            published.append(f"{container_port} -> {host_ip}:{host_port}")
    return sorted(published)


def format_api_networks(networks):
    items = []
    for name, data in (networks or {}).items():
        items.append({"name": name, "ip": (data or {}).get("IPAddress") or ""})
    items.sort(key=lambda item: item["name"])
    return items


def collect_cli(ssh, project):
    """Зібрати інвентаризацію текстовими командами Docker CLI."""
    collected_at = utc_now()
    hostname = require_ok(*run_remote(ssh, "hostname"), "hostname").strip()
    ps_command = (
        "docker ps -a --no-trunc "
        f"--filter label=com.docker.compose.project={shlex.quote(project)} "
        "--format '{{.ID}} {{.Image}} {{.State}}'"
    )
    ps_text = require_ok(*run_remote(ssh, ps_command), ps_command)
    containers = []
    for row in parse_ps(ps_text):
        container_id = row["id"]
        service_command = (
            "docker inspect --format "
            "'{{index .Config.Labels \"com.docker.compose.service\"}} {{.State.StartedAt}}' "
            + container_id
        )
        service, started_at = parse_service_and_start(
            require_ok(*run_remote(ssh, service_command), service_command),
            container_id,
        )
        name_command = "docker inspect --format '{{.Name}}' " + container_id
        name = require_ok(*run_remote(ssh, name_command), name_command).strip().lstrip("/")
        network_command = (
            "docker inspect --format "
            "'{{range $name, $network := .NetworkSettings.Networks}}"
            "{{println $name $network.IPAddress}}{{end}}' "
            + container_id
        )
        networks = parse_networks(
            require_ok(*run_remote(ssh, network_command), network_command)
        )
        port_command = f"docker port {container_id}"
        code, out, err = run_remote(ssh, port_command)
        if code != 0 and err.strip():
            raise RuntimeError(f"docker port не виконано для {container_id}: {err.strip()}")
        record = empty_container(service, container_id, name, row["image"], row["state"], started_at)
        record["networks"] = networks
        record["published_ports"] = parse_published_ports(out)
        containers.append(record)
    containers.sort(key=lambda item: (item["service"], item["name"], item["id"]))
    return {
        "hostname": hostname,
        "project": project,
        "collected_at": collected_at,
        "containers": containers,
    }


def collect_api(host, port, user, project):
    """Зібрати ту саму інвентаризацію через Docker SDK і SSH."""
    collected_at = utc_now()
    base_url = f"ssh://{user}@{host}:{port}"
    client = docker.DockerClient(base_url=base_url)
    try:
        info = client.info()
        hostname = info.get("Name") or ""
        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={project}"},
        )
        records = []
        for container in containers:
            container.reload()
            attrs = container.attrs
            record = empty_container(
                container.labels.get("com.docker.compose.service", ""),
                container.id,
                container.name,
                attrs["Config"]["Image"],
                attrs["State"]["Status"],
                attrs["State"]["StartedAt"],
            )
            record["networks"] = format_api_networks(attrs["NetworkSettings"]["Networks"])
            record["published_ports"] = format_api_ports(attrs["NetworkSettings"]["Ports"])
            records.append(record)
        records.sort(key=lambda item: (item["service"], item["name"], item["id"]))
        return {
            "hostname": hostname,
            "project": project,
            "collected_at": collected_at,
            "containers": records,
        }
    finally:
        client.close()


def network_table(containers):
    table = {}
    for container in containers:
        for network in container["networks"]:
            table.setdefault(network["name"], []).append(container["name"])
    return {
        name: sorted(set(names))
        for name, names in sorted(table.items())
    }


def check_published_ports(containers):
    """Лише сервіс proxy може мати прив'язку порту до хоста."""
    violations = []
    proxy_found = False
    for container in containers:
        published = container["published_ports"]
        if container["service"] == "proxy":
            proxy_found = True
            if not published:
                violations.append(f"{container['name']} (proxy): немає прив'язки порту до хоста")
        elif published:
            ports = ", ".join(published)
            violations.append(f"{container['name']} ({container['service']}): {ports}")
    if not proxy_found:
        violations.append("у проєкті немає контейнера сервісу proxy")
    if violations:
        return "порушення", violations
    return "відповідає вимогам", []


def http_check(url):
    started = time.perf_counter()
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    elapsed = response.elapsed.total_seconds()
    if elapsed == 0:
        elapsed = time.perf_counter() - started
    return {
        "url": url,
        "status_code": response.status_code,
        "elapsed_seconds": round(elapsed, 6),
        "body": response.text.strip(),
    }


def visit_count(body):
    match = VISIT_COUNT.search(body or "")
    if not match:
        return None
    return int(match.group(1))


def compare_inventories(cli_snapshot, api_snapshot):
    """Порівняти склад, стани, мережі та порти. Порядок списків не враховується."""
    cli_by_id = {item["id"]: item for item in cli_snapshot["containers"]}
    api_by_id = {item["id"]: item for item in api_snapshot["containers"]}
    differences = []
    for container_id in sorted(set(cli_by_id) - set(api_by_id)):
        differences.append(f"{container_id}: є у CLI, немає в API")
    for container_id in sorted(set(api_by_id) - set(cli_by_id)):
        differences.append(f"{container_id}: є в API, немає у CLI")
    for container_id in sorted(set(cli_by_id) & set(api_by_id)):
        cli_item = cli_by_id[container_id]
        api_item = api_by_id[container_id]
        label = cli_item["name"] or container_id
        if cli_item["state"] != api_item["state"]:
            differences.append(
                f"{label}: стан CLI={cli_item['state']}, API={api_item['state']}"
            )
        if cli_item["networks"] != api_item["networks"]:
            differences.append(
                f"{label}: мережі CLI={cli_item['networks']}, API={api_item['networks']}"
            )
        if cli_item["published_ports"] != api_item["published_ports"]:
            differences.append(
                f"{label}: порти CLI={cli_item['published_ports']}, API={api_item['published_ports']}"
            )
    return differences


def probe_command_error(ssh):
    command = "docker inspect --format '{{.Id}}' container-does-not-exist"
    code, out, err = run_remote(ssh, command)
    return {
        "command": command,
        "exit_code": code,
        "stdout": out.strip(),
        "stderr": err.strip(),
        "handled": code != 0,
    }


def probe_unreachable(user, key_path):
    """Спроба SSH на закритий порт. З'єднання має завершитись помилкою."""
    closed_port = 19991
    try:
        ssh = connect_ssh("127.0.0.1", closed_port, user, key_path, timeout=5)
        ssh.close()
        return {
            "target": f"127.0.0.1:{closed_port}",
            "handled": False,
            "error": "з'єднання неочікувано встановлено",
        }
    except Exception as exc:
        return {
            "target": f"127.0.0.1:{closed_port}",
            "handled": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def print_snapshot(title, snapshot):
    print(f"\n=== {title} ===")
    print(
        f"хост={snapshot['hostname']} проєкт={snapshot['project']} "
        f"час={snapshot['collected_at']} контейнерів={len(snapshot['containers'])}"
    )
    for container in snapshot["containers"]:
        networks = ", ".join(
            f"{item['name']} {item['ip']}" for item in container["networks"]
        ) or "-"
        ports = ", ".join(container["published_ports"]) or "-"
        print(
            f"{container['service']:8} {container['name']:16} {container['state']:10} "
            f"{container['image']:16} мережі: {networks} порти: {ports}"
        )


def write_inventory(path, cli_snapshot, api_snapshot):
    payload = {"cli": cli_snapshot, "api": api_snapshot}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_report_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["check", "result", "details"])
        writer.writerows(rows)


def one_line(text):
    return " ".join((text or "").split())


def inventory(args):
    if not PROJECT_NAME.match(args.project):
        raise ValueError(f"Некоректна назва проєкту: {args.project}")
    print(f"SSH {args.user}@{args.host}:{args.port}, проєкт {args.project}")
    ssh = connect_ssh(args.host, args.port, args.user, args.key)
    try:
        info_code, info_out, info_err = run_remote(ssh, "docker info", timeout=20)
        print("\n=== docker info ===")
        print(info_out if info_code == 0 else info_err)
        compose_command = (
            f"cd {shlex.quote(args.compose_dir)} && docker compose ps"
        )
        compose_code, compose_out, compose_err = run_remote(ssh, compose_command, timeout=20)
        print("\n=== docker compose ps ===")
        print(compose_out if compose_code == 0 else compose_err)

        print("\n=== помилка команди ===")
        command_error = probe_command_error(ssh)
        print(
            f"exit={command_error['exit_code']} handled={command_error['handled']}\n"
            f"{command_error['stderr']}"
        )

        cli_snapshot = collect_cli(ssh, args.project)
    finally:
        ssh.close()

    print("\n=== недоступний вузол ===")
    unreachable = probe_unreachable(args.user, args.key)
    print(f"target={unreachable['target']} handled={unreachable['handled']}")
    print(unreachable["error"])

    api_snapshot = collect_api(args.host, args.port, args.user, args.project)
    print_snapshot("CLI", cli_snapshot)
    print_snapshot("API", api_snapshot)

    table = network_table(api_snapshot["containers"])
    print("\n=== мережа -> контейнери ===")
    for name, names in table.items():
        print(f"{name} -> {', '.join(names)}")

    port_result, violations = check_published_ports(api_snapshot["containers"])
    print("\n=== перевірка публікації портів ===")
    print(port_result)
    for violation in violations:
        print(violation)

    print("\n=== HTTP через веб-проксі ===")
    first = http_check(args.proxy_url)
    second = http_check(args.proxy_url)
    first_count = visit_count(first["body"])
    second_count = visit_count(second["body"])
    chain_ok = (
        first["status_code"] == 200
        and second["status_code"] == 200
        and first_count is not None
        and second_count is not None
        and second_count > first_count
    )
    for index, result in ((1, first), (2, second)):
        print(
            f"запит {index}: status={result['status_code']} "
            f"elapsed={result['elapsed_seconds']} body={result['body']}"
        )
    print("ланцюг proxy -> app -> db:", "підтверджено" if chain_ok else "не підтверджено")

    differences = compare_inventories(cli_snapshot, api_snapshot)
    print("\n=== порівняння CLI та API ===")
    if differences:
        print("виявлено відмінності:")
        for item in differences:
            print(item)
    else:
        print("відмінностей не виявлено")

    destination = output_dir(args)
    inventory_path = destination / "inventory.json"
    report_path = destination / "report.csv"
    write_inventory(inventory_path, cli_snapshot, api_snapshot)
    port_details = port_result if not violations else "; ".join(violations)
    network_details = "; ".join(f"{name}: {', '.join(names)}" for name, names in table.items())
    rows = [
        ["ssh_docker_info", "ok" if info_code == 0 else "error", one_line(info_out if info_code == 0 else info_err)[:500]],
        ["docker_compose_ps", "ok" if compose_code == 0 else "error", one_line(compose_out if compose_code == 0 else compose_err)],
        ["command_error", "handled" if command_error["handled"] else "not_handled", one_line(command_error["stderr"])],
        ["unreachable_host", "handled" if unreachable["handled"] else "not_handled", one_line(unreachable["error"])],
        ["network_table", "ok", network_details],
        ["port_policy", port_result, port_details],
        ["http_1", str(first["status_code"]), f"elapsed={first['elapsed_seconds']} body={one_line(first['body'])}"],
        ["http_2", str(second["status_code"]), f"elapsed={second['elapsed_seconds']} body={one_line(second['body'])}"],
        ["http_chain", "підтверджено" if chain_ok else "не підтверджено", f"{first_count} -> {second_count}"],
        ["cli_api_compare", "match" if not differences else "differences", "; ".join(differences) or "відмінностей не виявлено"],
    ]
    write_report_csv(report_path, rows)
    print(f"\nЗаписано {inventory_path}")
    print(f"Записано {report_path}")


def http_status(url):
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        body = " ".join(response.text.split())
        return str(response.status_code), body[:120]
    except requests.RequestException as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def monitor(args):
    if args.interval <= 0:
        raise ValueError("Інтервал має бути додатним")
    if not PROJECT_NAME.match(args.project):
        raise ValueError(f"Некоректна назва проєкту: {args.project}")
    destination = output_dir(args)
    csv_path = destination / "monitoring.csv"
    base_url = f"ssh://{args.user}@{args.host}:{args.port}"
    client = docker.DockerClient(base_url=base_url)
    print(f"Моніторинг {args.project} кожні {args.interval} с. Файл {csv_path}")
    cycle = 0
    try:
        new_file = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["checked_at", "service", "container", "state", "http_status"])
            while args.cycles is None or cycle < args.cycles:
                cycle += 1
                checked_at = local_now()
                containers = client.containers.list(
                    all=True,
                    filters={"label": f"com.docker.compose.project={args.project}"},
                )
                status, body = http_status(args.proxy_url)
                if not containers:
                    writer.writerow([checked_at, "", "", "немає контейнерів", status])
                for container in containers:
                    container.reload()
                    service = container.labels.get("com.docker.compose.service", "")
                    state = container.attrs["State"]["Status"]
                    writer.writerow([checked_at, service, container.name, state, status])
                    print(f"{checked_at} {service:8} {container.name:16} {state:10} http={status}")
                print(f"тіло відповіді: {body}")
                handle.flush()
                if args.cycles is not None and cycle >= args.cycles:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nМоніторинг зупинено")
    finally:
        client.close()


def main():
    configure_stdio()
    args = parse_args()
    try:
        if args.monitor:
            monitor(args)
        else:
            inventory(args)
    except (paramiko.SSHException, socket.error, RuntimeError, FileNotFoundError, ValueError, requests.RequestException, docker.errors.DockerException) as exc:
        print(f"Помилка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
