"""eSSL / ZKTeco ADMS test server.

Run:  python -m essl_test_server            (from the repo folder)
Then: point the device's Cloud Server address at http://<this-pc-ip>:8080
      and open http://localhost:8080 in a browser on this PC.
"""

import argparse
import collections
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import adms
from .door import DoorController
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOG_BODY_MAX = 1500


class ProtocolLog:
    """Recent device traffic, kept in memory for the UI and appended to a file."""

    def __init__(self, path, maxlen=300):
        self.items = collections.deque(maxlen=maxlen)
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0

    def add(self, **entry):
        entry["body"] = _trim(adms.redact_templates(entry.get("body", "")))
        entry["response"] = _trim(entry.get("response", ""))
        with self.lock:
            self.seq += 1
            entry["seq"] = self.seq
            entry["t"] = time.time()
            self.items.append(entry)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, n=100):
        with self.lock:
            return list(self.items)[-n:][::-1]


def _trim(s):
    return s if len(s) <= LOG_BODY_MAX else s[:LOG_BODY_MAX] + f"... ({len(s)} chars)"


class App:
    def __init__(self, store, log, delay=5, allowed_sns=None, allow_remote_admin=False):
        self.store = store
        self.log = log
        self.delay = delay
        self.allowed_sns = set(allowed_sns or [])
        self.allow_remote_admin = allow_remote_admin
        self.door = DoorController(store)

    def default_sn(self):
        devices = self.store.devices()
        return devices[0]["sn"] if devices else None

    # ------------------------------------------------------------ device side

    def handle_device(self, method, endpoint, query, body, ip):
        """Returns (status, text). Everything the device sees comes from here."""
        sn = query.get("SN", "")
        if not sn:
            return 400, "Missing SN"
        if self.allowed_sns and sn not in self.allowed_sns:
            return 403, "Unknown device"
        self.store.touch_device(sn, ip, info=query.get("INFO"))

        if endpoint == "cdata" and method == "GET":
            return 200, adms.handshake_response(sn, self.delay)

        if endpoint == "cdata" and method == "POST":
            table = query.get("table", "")
            if table == "ATTLOG":
                rows = adms.parse_attlog(body)
                self.store.add_attlogs(sn, rows)
                return 200, f"OK: {len(rows)}"
            if table in ("OPERLOG", "BIODATA", "USERINFO"):
                users, fps, other = adms.parse_operlog(body)
                for u in users:
                    self.store.upsert_user(sn, u["pin"], u["name"], "device")
                for fp in fps:
                    self.store.add_fingerprint(sn, fp)
                return 200, f"OK: {len(users) + len(fps) + other}"
            if table == "options":
                self.store.set_device_options(sn, body)
                return 200, "OK"
            # ATTPHOTO and anything else: accept so the device doesn't keep retrying.
            return 200, "OK"

        if endpoint == "getrequest":
            return 200, adms.commands_response(self.store.next_commands(sn))

        if endpoint == "devicecmd":
            for r in adms.parse_devicecmd(body):
                cmd = self.store.finish_command(sn, r["id"], r["return"])
                if cmd and cmd["kind"] == "delete_user" and r["return"] == 0:
                    self.store.delete_user(sn, cmd["pin"])
            return 200, "OK"

        if endpoint == "registry":
            # Newer push protocol (3.x) registers first; harmless to support.
            return 200, "RegistryCode=essltest"

        if endpoint == "push":
            return 200, adms.handshake_response(sn, self.delay)

        if endpoint == "ping":
            return 200, "OK"

        return 200, "OK"

    # ------------------------------------------------------------ admin API

    def api_state(self, query):
        sn = query.get("sn") or self.default_sn()
        now = time.time()
        devices = self.store.devices()
        for d in devices:
            d["online"] = now - (d["last_seen"] or 0) < max(30, self.delay * 4)
        return {
            "now": now,
            "sn": sn,
            "devices": devices,
            "users": self.store.users(sn) if sn else [],
            "commands": self.store.commands(sn) if sn else [],
            "attlogs": self.store.attlogs(sn) if sn else [],
            "log": self.log.recent(),
            "door": self.door.status(),
            "fingers": adms.FINGER_NAMES,
        }

    def api_register(self, data):
        sn = data.get("sn") or self.default_sn()
        if not sn:
            raise adms.ValidationError("No device has connected yet. Check the device's server address.")
        pin = adms.clean_pin(data.get("pin"))
        name = adms.clean_name(data.get("name"))
        add_id = self.store.queue(sn, "add_user", adms.cmd_add_user(pin, name), pin)
        self.store.upsert_user(sn, pin, name, "app")
        enroll_id = None
        if data.get("enroll", True):
            enroll_id = self._queue_enroll(sn, pin, data)
        return {"sn": sn, "pin": pin, "add_command": add_id, "enroll_command": enroll_id}

    def api_enroll(self, data):
        sn = data.get("sn") or self.default_sn()
        if not sn:
            raise adms.ValidationError("No device has connected yet.")
        pin = adms.clean_pin(data.get("pin"))
        return {"sn": sn, "pin": pin, "enroll_command": self._queue_enroll(sn, pin, data)}

    def _queue_enroll(self, sn, pin, data):
        finger = adms.clean_finger(data.get("finger", 6))
        style = "ENROLL_BIO" if data.get("style") == "ENROLL_BIO" else "ENROLL_FP"
        return self.store.queue(sn, "enroll", adms.cmd_enroll(pin, finger, style), pin)

    def api_delete_user(self, data):
        sn = data.get("sn") or self.default_sn()
        if not sn:
            raise adms.ValidationError("No device has connected yet.")
        pin = adms.clean_pin(data.get("pin"))
        return {"command": self.store.queue(sn, "delete_user", adms.cmd_delete_user(pin), pin)}

    def api_raw(self, data):
        sn = data.get("sn") or self.default_sn()
        if not sn:
            raise adms.ValidationError("No device has connected yet.")
        text = (data.get("text") or "").replace("\r", "").replace("\n", "").strip()
        if not text:
            raise adms.ValidationError("Command is empty.")
        # Allow \t to be typed literally in the browser box.
        text = text.replace("\\t", "\t")
        return {"command": self.store.queue(sn, "raw", text)}

    def api_door_action(self, data):
        sn = data.get("sn") or self.door.settings()["sn"] or self.default_sn()
        return {"command": self.door.action(data.get("action"), sn, data.get("seconds"),
                                            actor=(data.get("actor") or "admin")[:40])}

    def api_door_schedule(self, data):
        self.door.save_settings(data, data.get("sn") or self.default_sn(),
                                actor=(data.get("actor") or "admin")[:40])
        return self.door.status()

    def api_cancel(self, data):
        return {"cancelled": self.store.cancel(int(data.get("id", 0)))}

    def api_registration(self, query):
        """Where is this student in the register -> finger flow? Drives the big status box."""
        sn = query.get("sn") or self.default_sn()
        pin = query.get("pin", "")
        if not sn or not pin:
            return {"stage": "idle"}
        cmds = self.store.commands_for_pin(sn, pin)
        add = next((c for c in reversed(cmds) if c["kind"] == "add_user"), None)
        enroll = next((c for c in reversed(cmds) if c["kind"] == "enroll"), None)
        fps = self.store.fingerprints_for(sn, pin)
        out = {"pin": pin, "add": add, "enroll": enroll, "fingerprints": fps}

        if add and add["status"] in ("failed", "timeout"):
            out["stage"] = "user_failed"
        elif add and add["status"] in ("queued", "sent"):
            out["stage"] = "sending_user"
        elif enroll is None:
            out["stage"] = "user_created"
        elif any(f["received"] >= enroll["created"] for f in fps):
            out["stage"] = "enrolled"
        elif enroll["status"] == "done":
            out["stage"] = "enrolled"
        elif enroll["status"] == "queued":
            out["stage"] = "sending_enroll"
        elif enroll["status"] == "sent":
            out["stage"] = "waiting_finger"
        elif enroll["status"] == "cancelled":
            out["stage"] = "user_created"
        else:
            out["stage"] = "enroll_" + enroll["status"]  # enroll_failed / enroll_timeout
        return out


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "essl-test"

        def log_message(self, fmt, *args):
            pass  # traffic goes to ProtocolLog instead

        def _read_body(self):
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
                raw = b"".join(chunks)
            else:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return raw

        def _send(self, status, body, ctype="text/plain; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str), "application/json")

        def _route(self, method):
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            raw = self._read_body() if method == "POST" else b""
            endpoint = adms.endpoint_of(parts.path)

            if endpoint:
                body = raw.decode("utf-8", errors="replace")
                try:
                    status, text = app.handle_device(method, endpoint, query, body, self.client_address[0])
                except Exception as e:  # never crash on odd device input
                    status, text = 500, f"ERROR: {e}"
                # Idle "any commands?" polls arrive every few seconds; logging them buries everything else.
                if not (endpoint == "getrequest" and text == "OK"):
                    app.log.add(ip=self.client_address[0], method=method, path=parts.path,
                                query=parts.query, body=body if body else "", status=status, response=text)
                return self._send(status, text)

            if not app.allow_remote_admin and self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._send(403, "Admin page is only available on this PC (http://localhost).")

            if parts.path in ("/", "/index.html") and method == "GET":
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if parts.path.startswith("/api/"):
                name = parts.path[len("/api/"):]
                try:
                    if method == "GET" and name == "state":
                        return self._json(200, app.api_state(query))
                    if method == "GET" and name == "registration":
                        return self._json(200, app.api_registration(query))
                    if method == "POST":
                        data = json.loads(raw or b"{}")
                        fn = {
                            "register": app.api_register, "enroll": app.api_enroll,
                            "delete-user": app.api_delete_user, "raw": app.api_raw,
                            "cancel": app.api_cancel,
                            "door/action": app.api_door_action, "door/schedule": app.api_door_schedule,
                        }.get(name)
                        if fn:
                            return self._json(200, fn(data))
                except adms.ValidationError as e:
                    return self._json(400, {"error": str(e)})
                except (ValueError, json.JSONDecodeError) as e:
                    return self._json(400, {"error": f"Bad request: {e}"})

            return self._send(404, "Not found")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def build_server(host, port, db_path, log_path, delay=5, allowed_sns=None,
                 allow_remote_admin=False, store_templates=False):
    app = App(Store(db_path, store_templates), ProtocolLog(log_path), delay,
              allowed_sns, allow_remote_admin)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server, app


def network_check(device_ip, port):
    """Print which IP the device should use, and whether this PC can see the device."""
    print()
    print("Network check")
    print("-------------")
    my_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((device_ip, 4370))
        my_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    if my_ip:
        print(f"  This PC's IP (towards the device): {my_ip}")
        same = my_ip.rsplit(".", 1)[0] == device_ip.rsplit(".", 1)[0]
        if same:
            print(f"  OK: same network as the device ({device_ip}).")
        else:
            print(f"  WARNING: this PC is on {my_ip.rsplit('.', 1)[0]}.x but the device is on "
                  f"{device_ip.rsplit('.', 1)[0]}.x.")
            print("  The device probably cannot reach this PC. Connect this PC to the same")
            print("  router/switch as the device (see README, 'Network check').")
    try:
        with socket.create_connection((device_ip, 4370), timeout=2):
            print(f"  OK: device answered on {device_ip}:4370.")
    except OSError:
        print(f"  Could not reach {device_ip}:4370 from this PC (not always a problem,")
        print("  but if the device never shows up below, check the network).")
    print()
    if my_ip:
        print("On the device: Menu > COMM. > Cloud Server Setting > Server Address:")
        print(f"      http://{my_ip}:{port}")
    print()


def main(argv=None):
    p = argparse.ArgumentParser(description="eSSL/ZKTeco ADMS test server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default="essl_test.db")
    p.add_argument("--log", default="protocol.log", help="device traffic log file ('' to disable)")
    p.add_argument("--delay", type=int, default=5, help="seconds between device polls")
    p.add_argument("--device-ip", default="192.168.29.48", help="for the startup network check")
    p.add_argument("--allowed-sn", action="append", help="only accept these device serials")
    p.add_argument("--allow-remote-admin", action="store_true",
                   help="let other PCs open the admin page (off by default)")
    p.add_argument("--store-templates", action="store_true",
                   help="keep fingerprint templates in the database (off by default)")
    args = p.parse_args(argv)

    server, app = build_server(args.host, args.port, args.db, args.log or None, args.delay,
                             args.allowed_sn, args.allow_remote_admin, args.store_templates)
    print(f"eSSL test server listening on port {args.port}")
    network_check(args.device_ip, args.port)
    app.door.run_forever()
    print(f"Open the test page:  http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Remember to set the device's server address back to Aerowis.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
