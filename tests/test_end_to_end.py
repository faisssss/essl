"""Runs the real test server against the fake device over HTTP."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from essl_test_server import store as store_mod  # noqa: E402
from essl_test_server.server import build_server  # noqa: E402
from fake_device import FakeDevice  # noqa: E402


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server, self.app = build_server("127.0.0.1", 0, os.path.join(self.tmp.name, "t.db"), None)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.store.db.close()
        self.tmp.cleanup()

    def api(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + "/api/" + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    def device(self, **kw):
        # Same URL shape Aerowis uses, to prove the prefix doesn't matter.
        return FakeDevice(self.url, path="/essl/push/iclock", enroll_delay=0, **kw)

    def test_register_and_enroll_flow(self):
        dev = self.device()
        self.assertIn("GET OPTION FROM: FAKE0000001", dev.handshake())

        r = self.api("register", {"pin": "1001", "name": "Rahul Kumar", "finger": 6})
        self.assertEqual(self.api(f"registration?pin=1001")["stage"], "sending_user")

        # Poll 1: only the add-user command goes out; enroll waits its turn.
        ran = dev.poll_once()
        self.assertEqual([c[0] for c in ran], [r["add_command"]])
        self.assertEqual(dev.users, {"1001": "Rahul Kumar"})
        self.assertEqual(self.api("registration?pin=1001")["stage"], "sending_enroll")

        # Poll 2: the enroll command goes out alone; fake device scans the finger.
        ran = dev.poll_once()
        self.assertEqual([c[1].split(" ")[0] for c in ran], ["ENROLL_FP"])
        reg = self.api("registration?pin=1001")
        self.assertEqual(reg["stage"], "enrolled")
        self.assertEqual(len(reg["fingerprints"]), 1)

        dev.punch("1001", "2026-10-15 09:00:00")
        state = self.api("state")
        self.assertEqual(state["attlogs"][0]["name"], "Rahul Kumar")
        self.assertEqual(state["users"][0]["fingers"], 1)
        self.assertNotIn("RkFLRVRNUEw=", json.dumps(state["log"]))  # templates never logged

    def test_rejected_enroll_reports_failure(self):
        dev = self.device(reject_enroll=True)
        dev.handshake()
        self.api("register", {"pin": "1002", "name": "Asha"})
        dev.poll_once()
        dev.poll_once()
        reg = self.api("registration?pin=1002")
        self.assertEqual(reg["stage"], "enroll_failed")
        self.assertEqual(reg["enroll"]["return_code"], -1)

    def test_register_without_auto_enroll(self):
        dev = self.device()
        dev.handshake()
        self.api("register", {"pin": "1003", "name": "Manual", "enroll": False})
        dev.poll_once()
        self.assertEqual(self.api("registration?pin=1003")["stage"], "user_created")

    def test_unanswered_command_times_out(self):
        dev = self.device()
        dev.handshake()
        self.api("register", {"pin": "1004", "name": "Slow", "enroll": False})
        sent = self.app.store.next_commands("FAKE0000001")  # hand it out, never answer
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.app.store.next_commands("FAKE0000001"), [])  # still waiting
        self.app.store._q("UPDATE commands SET sent = sent - ?", (store_mod.DEFAULT_SENT_TIMEOUT + 1,))
        self.app.store.next_commands("FAKE0000001")
        self.assertEqual(self.api("registration?pin=1004")["stage"], "user_failed")

    def test_delete_user(self):
        dev = self.device()
        dev.handshake()
        self.api("register", {"pin": "1005", "name": "Gone", "enroll": False})
        dev.poll_once()
        self.api("delete-user", {"pin": "1005"})
        dev.poll_once()
        self.assertEqual(dev.users, {})
        self.assertEqual(self.api("state")["users"], [])

    def test_validation_and_no_device(self):
        self.assertEqual(self.api("register", {"pin": "1", "name": "x"})["status"], 400)  # no device yet
        self.device().handshake()
        self.assertEqual(self.api("register", {"pin": "bad id", "name": "x"})["status"], 400)

    def test_remote_door_open(self):
        dev = self.device()
        dev.handshake()
        r = self.api("door/action", {"action": "open", "seconds": 5, "actor": "Boss"})
        dev.poll_once()
        self.assertEqual(dev.door_log, ["AC_UNLOCK"])
        cmd = next(c for c in self.api("state")["commands"] if c["id"] == r["command"])
        self.assertEqual(cmd["status"], "done")
        event = self.api("state")["door"]["events"][0]
        self.assertEqual((event["action"], event["actor"]), ("open", "Boss"))

    def test_door_schedule_validation(self):
        self.device().handshake()
        self.assertEqual(self.api("door/schedule", {"classes": "9 to ten"})["status"], 400)
        ok = self.api("door/schedule", {"classes": "09:00-10:00", "enabled": True, "style": "hex"})
        self.assertEqual(ok["windows"], [{"start": "08:45", "end": "10:15"}])

    def test_allowed_sn(self):
        self.app.allowed_sns = {"NFZ8250601118"}
        with self.assertRaises(urllib.error.HTTPError):
            self.device().handshake()


if __name__ == "__main__":
    unittest.main()
