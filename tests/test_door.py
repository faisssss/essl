import datetime as dt
import os
import tempfile
import unittest

from essl_test_server import adms
from essl_test_server.door import DoorController, compute_windows, fmt, parse_classes
from essl_test_server.store import Store


def at(hhmm):
    """Epoch seconds for today at HH:MM local time."""
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def windows(text, **kw):
    return [(fmt(a), fmt(b)) for a, b in compute_windows(parse_classes(text), **kw)]


class ScheduleTests(unittest.TestCase):
    def test_example_day(self):
        # 15 min gap merges; 2h45 lunch gap doesn't.
        self.assertEqual(windows("09:00-10:00\n10:15-11:15\n14:00-15:00"),
                         [("08:45", "11:30"), ("13:45", "15:15")])

    def test_gap_rule_boundary(self):
        self.assertEqual(windows("09:00-10:00\n10:30-11:00"), [("08:45", "11:15")])  # 30 min: stays open
        # 45 min gap: separate classes, but the 15+15 widening still leaves only 15 min locked
        self.assertEqual(windows("09:00-10:00\n10:45-11:00"), [("08:45", "10:15"), ("10:30", "11:15")])

    def test_caps_at_three_windows_by_joining_closest(self):
        text = "08:00-08:30\n10:00-10:30\n12:00-12:30\n13:30-14:00\n17:00-17:30"
        self.assertEqual(windows(text), [("07:45", "10:45"), ("11:45", "14:15"), ("16:45", "17:45")])

    def test_no_classes(self):
        self.assertEqual(windows(""), [])

    def test_clamps_to_day(self):
        self.assertEqual(windows("00:05-23:55"), [("00:00", "24:00")])

    def test_parse_errors(self):
        for bad in ["9-10", "10:00-09:00", "25:00-26:00", "hello"]:
            with self.assertRaises(adms.ValidationError):
                parse_classes(bad)
        self.assertEqual(parse_classes("9:00 - 10:30\n\n 13.00-14.00 "), [(540, 630), (780, 840)])


class DoorCommandTests(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(adms.cmd_door(5), "CONTROL DEVICE 01010105")
        self.assertEqual(adms.cmd_door(120), "CONTROL DEVICE 01010178")
        self.assertEqual(adms.cmd_door(255), "CONTROL DEVICE 010101FF")
        self.assertEqual(adms.cmd_door(0), "CONTROL DEVICE 01010100")
        self.assertEqual(adms.cmd_door(10, "decimal"), "CONTROL DEVICE 01010110")
        self.assertEqual(adms.cmd_door(10, "ac_unlock"), "AC_UNLOCK")
        for bad in (-1, 256, "x"):
            with self.assertRaises(adms.ValidationError):
                adms.cmd_door(bad)


class ControllerTests(unittest.TestCase):
    SN = "TDBD254500943"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.door = DoorController(self.store)
        self.door.save_settings({"enabled": True, "style": "hex", "classes": "09:00-10:00\n14:00-15:00"}, self.SN)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def sent(self):
        """Queued door commands, oldest first; marks them done like a device would."""
        cmds = [c for c in reversed(self.store.commands(self.SN)) if c["kind"] == "door" and c["status"] == "queued"]
        for c in cmds:
            self.store.finish_command(self.SN, c["id"], 0)
        return [c["text"] for c in cmds]

    def test_locked_before_window(self):
        self.door.tick(at("08:30"))
        self.assertEqual(self.sent(), [])

    def test_pulses_inside_window_then_locks(self):
        self.door.tick(at("08:45"))
        self.assertEqual(self.sent(), ["CONTROL DEVICE 01010178"])  # unlock 120 s
        self.door.tick(at("08:45") + 30)
        self.assertEqual(self.sent(), [])                           # not yet time to re-send
        self.door.tick(at("08:46"))
        self.assertEqual(self.sent(), ["CONTROL DEVICE 01010178"])  # re-sent after 60 s
        self.door.tick(at("10:15"))
        self.assertEqual(self.sent(), ["CONTROL DEVICE 01010100"])  # window over: lock
        self.door.tick(at("10:16"))
        self.assertEqual(self.sent(), [])

    def test_offline_device_does_not_pile_up_commands(self):
        self.door.tick(at("09:00"))
        self.door.tick(at("09:05"))  # first pulse never picked up by the device
        self.assertEqual(len(self.sent()), 1)

    def test_admin_lock_holds_until_window_ends(self):
        self.door.clock = lambda: at("09:30")
        self.door.tick(at("09:30"))
        self.sent()
        self.door.action("lock", self.SN, actor="Boss")
        self.assertEqual(self.sent(), ["CONTROL DEVICE 01010100"])
        self.door.tick(at("09:40"))
        self.assertEqual(self.sent(), [])                           # stays locked
        self.door.tick(at("13:50"))                                 # next window: schedule resumes
        self.assertEqual(self.sent(), ["CONTROL DEVICE 01010178"])
        self.assertEqual(self.store.door_events()[-1]["actor"], "admin")  # schedule saved in setUp

    def test_keep_open_overrides_schedule_until_resume(self):
        self.door.clock = lambda: at("11:00")
        self.door.action("keep_open", self.SN)
        self.assertEqual(self.sent(), ["CONTROL DEVICE 010101FF"])
        self.door.tick(at("16:00"))
        self.assertEqual(self.sent(), [])
        self.door.action("resume", self.SN)
        self.door.tick(at("16:01"))
        self.assertEqual(self.sent(), [])                           # outside windows: nothing to do

    def test_holiday_means_no_unlocks(self):
        self.door.save_settings({"no_classes": True}, self.SN)
        self.door.tick(at("09:30"))
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.door.status(at("09:30"))["windows"], [])

    def test_disabled_schedule_does_nothing(self):
        self.door.save_settings({"enabled": False}, self.SN)
        self.door.tick(at("09:30"))
        self.assertEqual(self.sent(), [])

    def test_ac_unlock_open_and_guards(self):
        self.door.save_settings({"enabled": False, "style": "ac_unlock"}, self.SN)
        self.door.action("open", self.SN, seconds=5, actor="Boss")
        self.assertEqual(self.sent(), ["AC_UNLOCK"])
        for action in ("keep_open", "lock"):
            with self.assertRaises(adms.ValidationError):
                self.door.action(action, self.SN)
        with self.assertRaises(adms.ValidationError):
            self.door.save_settings({"enabled": True}, self.SN)  # schedule needs another mechanism

    def test_old_saved_default_style_is_replaced(self):
        self.store.set_setting("door", {"style": "hex", "sn": self.SN})  # saved by the first version
        self.assertEqual(self.door.settings()["style"], "ac_unlock")

    def test_pulse_settings_validated(self):
        with self.assertRaises(adms.ValidationError):
            self.door.save_settings({"pulse_every": 120, "pulse_seconds": 120}, self.SN)


if __name__ == "__main__":
    unittest.main()
