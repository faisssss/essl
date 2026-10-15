"""A pretend eSSL device that speaks ADMS, for trying the test server without hardware.

    python tools/fake_device.py --server http://localhost:8080

It handshakes, polls for commands, "enrolls" a fingerprint a couple of seconds
after an ENROLL command, and sends a punch for each enrolled user.
Use --reject-enroll to simulate firmware that refuses remote enrollment.
"""

import argparse
import re
import time
import urllib.request


class FakeDevice:
    def __init__(self, server, sn="FAKE0000001", path="/iclock", reject_enroll=False, enroll_delay=2.0):
        self.base = server.rstrip("/") + path
        self.sn = sn
        self.reject_enroll = reject_enroll
        self.enroll_delay = enroll_delay
        self.users = {}

    def _req(self, endpoint, query="", body=None):
        url = f"{self.base}/{endpoint}?SN={self.sn}" + (f"&{query}" if query else "")
        data = body.encode("utf-8") if body is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return r.read().decode("utf-8")

    def handshake(self):
        return self._req("cdata", "options=all&pushver=2.0.33S&language=69")

    def punch(self, pin, when=None):
        when = when or time.strftime("%Y-%m-%d %H:%M:%S")
        return self._req("cdata", "table=ATTLOG&Stamp=1", f"{pin}\t{when}\t0\t1\t0\t0\t0\n")

    def poll_once(self):
        """One getrequest round. Returns the commands it executed."""
        body = self._req("getrequest", "INFO=Ver%208.0.4.3,0,0,0,192.168.29.48,10,0,0,0,111")
        if body.strip() == "OK":
            return []
        done = []
        for line in body.strip().splitlines():
            _, cid, text = line.split(":", 2)
            ret = self._execute(text)
            self._req("devicecmd", "", f"ID={cid}&Return={ret}&CMD={text.split(' ')[0]}")
            done.append((int(cid), text, ret))
        return done

    def _execute(self, text):
        fields = dict(re.findall(r"(\w+)=([^\t]*)", text))
        if text.startswith("DATA UPDATE USERINFO"):
            self.users[fields["PIN"]] = fields.get("Name", "")
            self._req("cdata", "table=OPERLOG&Stamp=1", f"USER PIN={fields['PIN']}\tName={fields.get('Name', '')}\tPri=0\n")
            return 0
        if text.startswith("DATA DELETE USERINFO"):
            return 0 if self.users.pop(fields.get("PIN"), None) is not None else -1
        if text.startswith("ENROLL_FP") or text.startswith("ENROLL_BIO"):
            if self.reject_enroll:
                return -1
            pin = fields.get("PIN")
            finger = fields.get("FID", fields.get("NO", "6"))
            if pin not in self.users:
                return -1
            time.sleep(self.enroll_delay)  # the student pressing their finger 3 times
            self._req("cdata", "table=OPERLOG&Stamp=1",
                      f"FP PIN={pin}\tFID={finger}\tSize=8\tValid=1\tTMP=RkFLRVRNUEw=\n")
            return 0
        return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://localhost:8080")
    p.add_argument("--sn", default="FAKE0000001")
    p.add_argument("--reject-enroll", action="store_true")
    p.add_argument("--interval", type=float, default=3)
    args = p.parse_args()

    dev = FakeDevice(args.server, args.sn, reject_enroll=args.reject_enroll)
    print(dev.handshake())
    while True:
        for cid, text, ret in dev.poll_once():
            print(f"ran #{cid}: {text!r} -> {ret}")
            if text.startswith("ENROLL") and ret == 0:
                pin = text.split("PIN=")[1].split("\t")[0]
                dev.punch(pin)
                print(f"punched {pin}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
