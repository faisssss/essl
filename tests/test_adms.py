import unittest

from essl_test_server import adms


class EndpointTests(unittest.TestCase):
    def test_strips_server_prefix(self):
        self.assertEqual(adms.endpoint_of("/essl/push/iclock/cdata"), "cdata")
        self.assertEqual(adms.endpoint_of("/iclock/getrequest"), "getrequest")
        self.assertEqual(adms.endpoint_of("/iclock/cdata.aspx"), "cdata")
        self.assertIsNone(adms.endpoint_of("/api/state"))


class CommandTests(unittest.TestCase):
    def test_add_user(self):
        self.assertEqual(adms.cmd_add_user("1001", "Rahul Kumar"),
                         "DATA UPDATE USERINFO PIN=1001\tName=Rahul Kumar\tPri=0")

    def test_name_cannot_inject_fields_or_commands(self):
        cmd = adms.cmd_add_user("1001", "Evil\tPri=14\nC:9:CLEAR DATA")
        self.assertNotIn("\n", cmd)
        self.assertEqual(cmd.count("\t"), 2)

    def test_bad_pin_rejected(self):
        for pin in ["", "10 01", "10\t01", "a" * 15, "x;y"]:
            with self.assertRaises(adms.ValidationError):
                adms.cmd_add_user(pin, "A")

    def test_enroll_styles(self):
        self.assertEqual(adms.cmd_enroll("1001", 6), "ENROLL_FP PIN=1001\tFID=6\tRETRY=3\tOVERWRITE=1")
        self.assertEqual(adms.cmd_enroll("1001", "3", "ENROLL_BIO"),
                         "ENROLL_BIO TYPE=1\tPIN=1001\tNO=3\tRETRY=3\tOVERWRITE=1")
        with self.assertRaises(adms.ValidationError):
            adms.cmd_enroll("1001", 10)

    def test_commands_response(self):
        self.assertEqual(adms.commands_response([]), "OK")
        self.assertEqual(adms.commands_response([(1, "INFO"), (2, "CHECK")]), "C:1:INFO\nC:2:CHECK\n")


class ParseTests(unittest.TestCase):
    def test_attlog(self):
        rows = adms.parse_attlog("1001\t2026-10-15 09:01:02\t0\t1\t0\t0\t0\n\n1002\t2026-10-15 09:05:00\t1\t1\n")
        self.assertEqual([r["pin"] for r in rows], ["1001", "1002"])
        self.assertEqual(rows[0]["time"], "2026-10-15 09:01:02")
        self.assertEqual(rows[1]["status"], "1")

    def test_operlog_users_and_fps(self):
        body = ("OPLOG 4\t0\t2026-10-15 09:00:00\t0\t0\t0\t0\n"
                "USER PIN=1001\tName=Rahul\tPri=0\tPasswd=\tCard=\tGrp=1\n"
                "FP PIN=1001\tFID=6\tSize=1184\tValid=1\tTMP=QUJD\n"
                "BIODATA Pin=1002\tNo=0\tIndex=0\tValid=1\tDuress=0\tType=1\tMajorVer=10\tTmp=WFla\n"
                "BIODATA Pin=1003\tNo=0\tType=9\tTmp=face\n")
        users, fps, other = adms.parse_operlog(body)
        self.assertEqual(users, [{"pin": "1001", "name": "Rahul"}])
        self.assertEqual([(f["pin"], f["finger"]) for f in fps], [("1001", 6), ("1002", 0)])
        self.assertEqual(other, 1)

    def test_devicecmd(self):
        res = adms.parse_devicecmd("ID=5&Return=0&CMD=DATA\nID=6&Return=-1&CMD=ENROLL_FP\ngarbage\n")
        self.assertEqual(res, [{"id": 5, "return": 0, "cmd": "DATA"}, {"id": 6, "return": -1, "cmd": "ENROLL_FP"}])

    def test_redacts_templates(self):
        self.assertEqual(adms.redact_templates("FP PIN=1\tTMP=secret\tX=1"), "FP PIN=1\tTMP=<redacted>\tX=1")


if __name__ == "__main__":
    unittest.main()
