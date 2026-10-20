# Remote door control and class-based unlocking for Aerowis

**Audience:** the team that maintains Aerowis and `api.aerowis.com/essl/push`.

**Goals:**
1. Admins can unlock or lock the institute door from the Aerowis app, from anywhere.
2. The door stays unlocked automatically while classes are on, using the Aerowis
   calendar and timetable. Outside those times, and on holidays, it stays locked,
   and people enter with face or finger as usual.

The reference implementation is in this repo: `essl_test_server/door.py` (schedule
and controller) and `adms.cmd_door` (commands), with tests in `tests/test_door.py`.
It ports directly to any backend language.

## Device

| | |
|---|---|
| Model | eSSL face + fingerprint access terminal (ZKTeco `ZMM510_TFT`) |
| Serial | TDBD254500943 |
| Firmware | ZMM510-NF24VB-Ver2.1.4, Push Service 3.0.6S-20240829 |
| Settings | ADMS mode, sending to `https://api.aerowis.com/essl/push`. Door Lock Delay 10 s. |

## On-site test results

*To be filled in after the door test (README, "Door test").*

| Test | Result | Notes |
|---|---|---|
| G. Remote open | ✅ with `AC_UNLOCK` | `CONTROL DEVICE 010101xx` returns `Return=0` but **does nothing** on this device, so don't rely on Return=0 alone. `AC_UNLOCK` opens the door for the device's Door Lock Delay (10 s). |
| H. Timed open (hex vs decimal) | n/a | `AC_UNLOCK` takes no duration. |
| I. Keep open / Lock now | | |
| J. Schedule unlock holds steadily | | |
| K. Server stops → door relocks by itself | | |
| L. Holiday → stays locked; face/finger still work | | |

## 1. Remote unlock and lock

The device already polls Aerowis for commands (see `docs/INTEGRATION.md`), so
remote control is one more command type in the same queue. It needs no network
changes at the institute.

| Action | Command |
|---|---|
| **Unlock (this device)** | **`AC_UNLOCK`**: opens for the device's Door Lock Delay, then relocks by itself. Tested and working. |
| Unlock / keep / lock (door controller panels only) | `CONTROL DEVICE 010101DD` (`DD` = seconds, `FF` = keep, `00` = lock). The device accepts it but ignores it. |

The fields in `AABBCCDD` are:
- `AA` = 01, output
- `BB` = 01, door 1
- `CC` = 01, lock relay
- `DD` = seconds

The device answers on `devicecmd` with `Return=0` on success.

**Permissions.** This is a physical security feature.
- Only admins with an explicit "door control" permission may use it.
- Log every action: who, when, what, and from which IP/device. The test app's
  door log is the minimum.
- Consider a confirmation step, or 2FA, for "Keep open".
- Never expose the command on an unauthenticated endpoint.

## 2. Class-based schedule

### Turning the timetable into door windows

Run this for each day. Reference: `compute_windows` in `door.py`.

1. Get that day's classes from the timetable. On a holiday or a day with no
   classes, the result is **no windows**, so the door stays locked all day.
2. Join classes whose gap is **≤ 30 min**.
3. Widen each block: unlock **15 min before**, lock **15 min after**.
4. Join windows that now overlap.
5. If more than 3 windows remain, join the closest ones.

Example:

```
Classes:  09:00–10:00, 10:15–11:15, 14:00–15:00
Windows:  08:45–11:30, 13:45–15:15
```

The settings agreed with management are: before = 15, after = 15, join gap = 30.

### Driving the door: fail locked

Inside a window, the server sends **"unlock for 120 s" every 60 s**. It doesn't
send one long "keep open". When the window ends, it sends "lock now".

The reason is safety. If the internet or Aerowis goes down, the pulses stop and
the device **relocks within 2 minutes by itself**. The door can be wrongly
*locked*, in which case people use face/finger, but it is never left wrongly
*unlocked* overnight.

Rules (reference: `DoorController.tick`):
- Don't queue a new pulse while the previous door command hasn't been picked up,
  so commands don't pile up while the device is offline.
- **Admin "Lock now" during a window:** stays locked until *that* window ends,
  then the schedule resumes. Store the window's end time, not just a flag.
  Otherwise a server restart between windows keeps the next window locked too.
- **Admin "Keep open":** overrides the schedule until an admin presses "Lock now"
  or "Resume schedule".

### Alternative: the device's own schedule

The device has a built-in **Normal Open Time Period** setting. It would keep
working offline, but it is limited to a few windows per weekday and doesn't
know about holidays. Writing it remotely is firmware-specific and was not part
of this test. The pulse approach above already fails safe, so it is the
recommendation.

## Security notes for this device

- The device has **SSH enabled** (Menu → System → Security Settings). That is
  remote shell access to the door controller. Disable it unless the installer
  needs it.
- Keep `api.aerowis.com` on HTTPS and allow-list device serials (see
  `docs/INTEGRATION.md`).
