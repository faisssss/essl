"""Door control: remote open/lock, and keeping the door unlocked around classes.

How the schedule works (Aerowis will feed in its real calendar/timetable; the
test app takes today's classes typed by hand):

  1. Classes with a gap of `merge_gap` minutes or less between them are joined.
  2. Each block is widened: unlock `before` minutes early, lock `after` minutes late.
  3. Overlapping windows are joined; if there are still more than `max_windows`,
     the closest ones are joined (devices usually allow ~3 windows per day).
  4. No classes / holiday = no windows = locked all day (finger/face still work).

While inside a window, the server keeps re-sending "unlock for `pulse_seconds`"
every `pulse_every` seconds. If the server or internet goes down, the device
relocks on its own within `pulse_seconds`, so the door fails LOCKED, never open.
"""

import datetime as dt
import re
import threading
import time

from . import adms

DEFAULTS = {
    "sn": None,               # device the schedule drives
    "enabled": False,
    "classes": "",            # one class per line: 09:00-10:00
    "no_classes": False,      # holiday / no classes today
    "before": 15,
    "after": 15,
    "merge_gap": 30,
    "max_windows": 3,
    "pulse_seconds": 120,
    "pulse_every": 60,
    "style": "hex",
    "override": None,         # None | 'open' (kept open by an admin) | 'locked' (locked by an admin)
    "override_until": None,   # for 'locked': epoch seconds when the window it cut short ends
}

TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–to ]+\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_classes(text):
    """'09:00-10:00\\n10:15 - 11:15' -> [(540, 600), (615, 675)] in minutes since midnight."""
    out = []
    for n, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        m = TIME_RE.match(line)
        if not m:
            raise adms.ValidationError(f"Line {n}: write classes like 09:00-10:00")
        h1, m1, h2, m2 = map(int, m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            raise adms.ValidationError(f"Line {n}: not a valid time")
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            raise adms.ValidationError(f"Line {n}: class must end after it starts")
        out.append((start, end))
    return sorted(out)


def compute_windows(classes, before=15, after=15, merge_gap=30, max_windows=3):
    """Turn class times into door-open windows (minutes since midnight)."""
    blocks = []
    for start, end in sorted(classes):
        if blocks and start - blocks[-1][1] <= merge_gap:
            blocks[-1][1] = max(blocks[-1][1], end)
        else:
            blocks.append([start, end])

    windows = []
    for start, end in blocks:
        start, end = max(0, start - before), min(24 * 60, end + after)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    while max_windows and len(windows) > max_windows:
        gaps = [windows[i + 1][0] - windows[i][1] for i in range(len(windows) - 1)]
        i = gaps.index(min(gaps))
        windows[i:i + 2] = [[windows[i][0], windows[i + 1][1]]]
    return [tuple(w) for w in windows]


def fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minute_of_day(now):
    t = dt.datetime.fromtimestamp(now)
    return t.hour * 60 + t.minute


class DoorController:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self.lock = threading.Lock()
        self.last_pulse = 0
        self.was_inside = False

    # ------------------------------------------------------------ settings

    def settings(self):
        s = dict(DEFAULTS)
        s.update(self.store.get_setting("door", {}))
        return s

    def save_settings(self, data, sn, actor="admin"):
        s = self.settings()
        classes = data.get("classes", s["classes"])
        parse_classes(classes)  # validate before saving
        s.update({
            "sn": data.get("sn") or sn or s["sn"],
            "enabled": bool(data.get("enabled", s["enabled"])),
            "classes": classes,
            "no_classes": bool(data.get("no_classes", s["no_classes"])),
            "style": data.get("style") if data.get("style") in adms.DOOR_STYLES else s["style"],
        })
        for key, lo, hi in (("before", 0, 180), ("after", 0, 180), ("merge_gap", 0, 600),
                            ("max_windows", 1, 10), ("pulse_seconds", 30, 250), ("pulse_every", 15, 240)):
            if key in data:
                try:
                    v = int(data[key])
                except (TypeError, ValueError):
                    raise adms.ValidationError(f"{key} must be a number")
                if not lo <= v <= hi:
                    raise adms.ValidationError(f"{key} must be between {lo} and {hi}")
                s[key] = v
        if s["pulse_every"] >= s["pulse_seconds"]:
            raise adms.ValidationError("Re-send interval must be shorter than the unlock time, or the door will click shut between pulses.")
        self.store.set_setting("door", s)
        self.store.add_door_event(s["sn"], "schedule saved",
                                  "on" if s["enabled"] else "off", actor)
        return s

    def windows(self, s=None):
        s = s or self.settings()
        if s["no_classes"]:
            return []
        return compute_windows(parse_classes(s["classes"]), s["before"], s["after"],
                               s["merge_gap"], s["max_windows"])

    def status(self, now=None):
        now = now or self.clock()
        s = self.settings()
        wins = self.windows(s)
        minute = minute_of_day(now)
        inside = any(a <= minute < b for a, b in wins)
        if s["override"] == "open":
            state = "Kept open by an admin (until someone presses Lock now or Resume schedule)"
        elif s["override"] == "locked":
            state = "Locked by an admin (schedule resumes after the current window)"
        elif not s["enabled"]:
            state = "Schedule off: door works by finger/face only"
        elif inside:
            state = "Unlocked by schedule"
        else:
            nxt = next((a for a, b in wins if a > minute), None)
            state = "Locked by schedule" + (f" (next unlock {fmt(nxt)})" if nxt is not None else " for the rest of the day")
        return {
            "settings": s,
            "windows": [{"start": fmt(a), "end": fmt(b)} for a, b in wins],
            "inside": inside,
            "state": state,
            "events": self.store.door_events(),
        }

    # ------------------------------------------------------------ manual actions

    def action(self, action, sn, seconds=None, actor="admin"):
        """open / keep_open / lock / resume. Returns the queued command id (or None)."""
        with self.lock:
            s = self.settings()
            sn = sn or s["sn"]
            if not sn:
                raise adms.ValidationError("No device has connected yet.")
            cid = None
            if action == "open":
                secs = int(seconds or 10)
                cid = self._queue(sn, adms.cmd_door(secs, s["style"]))
                self.store.add_door_event(sn, "open", f"{secs} s", actor)
            elif action == "keep_open":
                cid = self._queue(sn, adms.cmd_door(adms.DOOR_KEEP_OPEN, s["style"]))
                s["override"], s["override_until"] = "open", None
                self.store.add_door_event(sn, "keep open", "until locked", actor)
            elif action == "lock":
                cid = self._queue(sn, adms.cmd_door(0, s["style"]))
                end = self._window_end(s)
                s["override"], s["override_until"] = ("locked", end) if end else (None, None)
                self.store.add_door_event(sn, "lock", "", actor)
            elif action == "resume":
                s["override"], s["override_until"] = None, None
                self.last_pulse = 0
                self.store.add_door_event(sn, "resume schedule", "", actor)
            else:
                raise adms.ValidationError("Unknown door action.")
            self.store.set_setting("door", s)
            return cid

    def _queue(self, sn, text):
        return self.store.queue(sn, "door", text)

    def _inside(self, s, now=None):
        return self._window_end(s, now) is not None

    def _window_end(self, s, now=None):
        """Epoch seconds when the window we're in ends, or None if outside all windows."""
        now = now or self.clock()
        minute = minute_of_day(now)
        for a, b in self.windows(s):
            if a <= minute < b:
                midnight = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
                return (midnight + dt.timedelta(minutes=b)).timestamp()
        return None

    # ------------------------------------------------------------ schedule loop

    def tick(self, now=None):
        """Called every few seconds. Keeps the door unlocked inside windows."""
        now = now or self.clock()
        with self.lock:
            s = self.settings()
            if not s["enabled"] or not s["sn"]:
                self.was_inside = False
                return
            inside = self._inside(s, now)

            if s["override"] == "open":
                return
            if s["override"] == "locked":
                if now < (s["override_until"] or 0):
                    self.was_inside = False  # already locked; no extra lock at window end
                    return
                s["override"], s["override_until"] = None, None  # that window is over
                self.store.set_setting("door", s)

            if inside:
                # Don't pile up pulses while the device is offline.
                if now - self.last_pulse >= s["pulse_every"] and not self.store.has_pending(s["sn"], "door"):
                    self._queue(s["sn"], adms.cmd_door(s["pulse_seconds"], s["style"]))
                    if not self.was_inside:
                        self.store.add_door_event(s["sn"], "unlock", "schedule window started", "schedule")
                    self.last_pulse = now
                self.was_inside = True
            elif self.was_inside:
                self._queue(s["sn"], adms.cmd_door(0, s["style"]))
                self.store.add_door_event(s["sn"], "lock", "schedule window ended", "schedule")
                self.was_inside = False
                self.last_pulse = 0

    def run_forever(self, interval=10):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:  # keep the loop alive; show the problem in the console
                    print("door schedule error:", e)
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()
