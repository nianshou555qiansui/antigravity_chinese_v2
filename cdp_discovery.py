"""Find local CDP listeners owned by Antigravity without scanning other apps."""

import csv
from dataclasses import dataclass
import http.client
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


@dataclass
class Instance:
    pids: set
    endpoint: Endpoint | None = None
    page: dict | None = None


def _run(args):
    options = {"capture_output": True, "timeout": 5}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(args, **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoveryError(f"Cannot inspect Antigravity ({type(exc).__name__}).") from exc


def antigravity_pids():
    if sys.platform == "win32":
        result = _run(["tasklist", "/FO", "CSV", "/NH"])
        if result.returncode != 0:
            raise DiscoveryError("tasklist failed; process state is unknown.")
        rows = list(csv.reader(result.stdout.decode("utf-8", "replace").splitlines()))
        processes = [row for row in rows if len(row) >= 2 and row[1].isdigit()]
        if not processes:
            raise DiscoveryError("tasklist returned no readable process table.")
        return {int(row[1]) for row in processes if row[0].lower() == "antigravity.exe"}
    if sys.platform == "darwin":
        result = _run(["pgrep", "-x", "Antigravity"])
        if result.returncode == 1:
            return set()
        if result.returncode != 0:
            raise DiscoveryError("pgrep failed; process state is unknown.")
        try:
            pids = {int(pid) for pid in result.stdout.split()}
            if not pids or any(pid <= 0 for pid in pids):
                raise ValueError
            return pids
        except ValueError as exc:
            raise DiscoveryError("pgrep returned invalid process IDs.") from exc
    raise DiscoveryError("Automatic discovery supports Windows and macOS only.")


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint(address):
    try:
        host, port = address.rsplit(":", 1)
        host = host.strip("[]")
        port = int(port)
        if _loopback(host) and 0 < port <= 65535:
            return Endpoint(host, port)
    except ValueError:
        pass
    return None


def listening_endpoints(pids):
    if not pids:
        return []
    endpoints = set()
    if sys.platform == "win32":
        result = _run(["netstat", "-ano"])
        if result.returncode != 0:
            raise DiscoveryError("netstat failed; listener ownership is unknown.")
        for line in result.stdout.decode("ascii", "replace").splitlines():
            fields = line.split()
            if (len(fields) == 5 and fields[0] == "TCP"
                    and fields[3] == "LISTENING" and fields[4].isdigit()
                    and int(fields[4]) in pids):
                endpoint = _endpoint(fields[1])
                if endpoint:
                    endpoints.add(endpoint)
    elif sys.platform == "darwin":
        result = _run(["lsof", "-nP", "-a", "-p", ",".join(map(str, sorted(pids))),
                       "-iTCP", "-sTCP:LISTEN", "-Fpn"])
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return []
        if result.returncode != 0 or result.stderr:
            raise DiscoveryError("lsof failed; listener ownership is unknown.")
        owner = None
        for line in result.stdout.decode("ascii", "replace").splitlines():
            if line.startswith("p"):
                owner = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n") and owner in pids:
                endpoint = _endpoint(line[1:])
                if endpoint:
                    endpoints.add(endpoint)
    else:
        raise DiscoveryError("Unsupported platform for listener discovery.")
    return sorted(endpoints, key=lambda e: (e.port, e.host != "127.0.0.1", e.host))


def _http_json(endpoint, path):
    connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=0.5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None
        return json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _local_url(url, schemes):
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme in schemes and not parts.username and not parts.password
                and (_loopback(parts.hostname) or parts.hostname == "localhost")
                and parts.port is not None)
    except (ValueError, TypeError):
        return False


def _select_page(endpoint, pages):
    if not isinstance(pages, list):
        return None
    candidates = []
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        if not _local_url(page.get("url"), ("http", "https")):
            continue
        if str(page.get("title", "")).startswith("Loading"):
            continue
        websocket = page.get("webSocketDebuggerUrl")
        if not _local_url(websocket, ("ws",)):
            continue
        parts = urlsplit(websocket)
        if parts.port != endpoint.port or not parts.path.startswith("/devtools/page/"):
            continue
        # Pin the returned URL to the listener we verified, not a DNS name.
        host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
        page = dict(page)
        page["webSocketDebuggerUrl"] = urlunsplit(
            ("ws", f"{host}:{endpoint.port}", parts.path, parts.query, ""))
        candidates.append(page)
    named = [page for page in candidates if page.get("title") == "Antigravity"]
    if named:
        return named[0]
    if len(candidates) > 1:
        raise DiscoveryError("Multiple local pages; cannot safely identify the Antigravity window.")
    if candidates:
        return candidates[0]
    return None


def find_running_instance(port=None):
    pids = antigravity_pids()
    candidates = listening_endpoints(pids)
    if port is not None:
        candidates = [endpoint for endpoint in candidates if endpoint.port == port]
    matches = []
    for endpoint in candidates:
        version = _http_json(endpoint, "/json/version")
        if not isinstance(version, dict):
            continue
        agent = version.get("User-Agent", "")
        if not isinstance(agent, str) or not re.search(r"(?:^|\s)Antigravity/[0-9]+(?:[.][0-9]+)+(?:\s|$)", agent):
            continue
        if not any(old.port == endpoint.port for old in matches):
            matches.append(endpoint)
    if len(matches) > 1:
        ports = ", ".join(str(endpoint.port) for endpoint in matches)
        raise DiscoveryError(f"Multiple Antigravity CDP ports ({ports}); select one with --port.")
    if not matches:
        return Instance(pids)
    endpoint = matches[0]
    return Instance(pids, endpoint, _select_page(endpoint, _http_json(endpoint, "/json")))


def require_free_port(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise DiscoveryError(f"Port {port} is occupied; choose another with --port.") from exc


def stop_confirmed_instance(confirmed_pids):
    current = antigravity_pids()
    if current - confirmed_pids:
        raise DiscoveryError("Antigravity processes changed; restart cancelled. Run again to confirm.")
    targets = current & confirmed_pids
    if not targets:
        return
    if sys.platform == "win32":
        command = ["taskkill", "/F"]
        for pid in sorted(targets):
            command.extend(["/PID", str(pid)])
    elif sys.platform == "darwin":
        command = ["kill", "-TERM", *map(str, sorted(targets))]
    else:
        raise DiscoveryError("Unsupported platform for restart.")
    _run(command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not antigravity_pids():
            return
        time.sleep(0.2)
    raise DiscoveryError("Antigravity is still running; no new instance was started.")
