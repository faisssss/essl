# Adding device registration and fingerprint enrollment to Aerowis

**Audience:** the team that maintains Aerowis and `api.aerowis.com/essl/push`.

**Goal:** register a student or staff member in Aerowis only, and have Aerowis
create the user on the eSSL device and start fingerprint enrollment. No eSSL
app is involved.

A working reference implementation is in this repository (`essl_test_server/`,
about 600 lines of Python, standard library only). It was tested against the
device below. This document describes what to add to Aerowis. The logic is
small and ports directly to any backend language.

## Device

| | |
|---|---|
| Model | eSSL X2008 (ZKTeco ZLM60_TFT platform) |
| Serial | NFZ8250601118 |
| Firmware | 8.0.4.3-20230515 |
| Push service | 2.0.33S-20220613 |
| Fingerprint algorithm | Finger VX10.0 |
| Settings | ADMS mode, Alphanumeric User ID = ON |

## On-site test results

*To be filled in after the test (see README, "Run the tests").*

| Test | Result | Notes / return code |
|---|---|---|
| A. Create user from server (`DATA UPDATE USERINFO`) | | |
| B. Remote enrollment `ENROLL_FP` | | |
| C. Remote enrollment `ENROLL_BIO` | | |
| D. Punch received with correct ID | | |
| E. Fingerprint uploaded after manual enrollment on device | | |
| F. Delete user (`DATA DELETE USERINFO`) | | |

## What Aerowis already has

The device already pushes attendance to Aerowis, so Aerowis already answers:

- `GET  /essl/push/iclock/cdata?SN=…&options=all` (handshake)
- `POST /essl/push/iclock/cdata?SN=…&table=ATTLOG` (punches)
- probably `GET /essl/push/iclock/getrequest?SN=…`, answered with `OK`

## What to add

### 1. A command queue (one new table)

```
device_commands
  id            auto-increment     -- sent to the device as the command ID
  device_sn     text
  user_pin      text null          -- Aerowis student/staff ID
  kind          text               -- add_user | enroll | delete_user
  command       text               -- exact text sent to the device
  status        text               -- queued | sent | done | failed | timeout | cancelled
  return_code   int null
  created_at, sent_at, finished_at
```

Reference: `essl_test_server/store.py`.

### 2. `GET …/iclock/getrequest?SN=…`: hand out commands

The device calls this every `Delay` seconds. Reply `OK` when there is
nothing to send. Otherwise reply with one line per command:

```
C:<id>:<command text>\n
```

Mark those commands `sent`.

**Sequencing rules.** These matter. Reference: `Store.next_commands`.

- If any command for this device is still `sent` and has no result, reply `OK`
  and wait.
- Send queued commands in ID order, up to 10 at a time.
- **Always send an `ENROLL_…` command on its own.** The device sits on the "place
  finger" screen until the person finishes or it times out.
- Mark a command `timeout` if there is no result after 60 s. For enroll
  commands, wait 180 s, because a person has to walk up and scan three times.

Lower `Delay` in the handshake response (we used `Delay=5`) so the device
reacts within seconds.

### 3. `POST …/iclock/devicecmd?SN=…`: command results

Body (one line per result):

```
ID=12&Return=0&CMD=DATA
```

`Return=0` means success; anything else is failure. Update the command row.
Reply `OK`.

### 4. `POST …/iclock/cdata?SN=…&table=OPERLOG` (and `table=BIODATA`): users and fingerprints

The device reports user changes and new fingerprints here. Reply `OK: <lines>`.

```
USER PIN=1001\tName=Rahul\tPri=0\t...
FP PIN=1001\tFID=6\tSize=1184\tValid=1\tTMP=<base64 template>
BIODATA Pin=1001\tNo=6\tIndex=0\tValid=1\tType=1\t...\tTmp=<base64>   (newer firmware; Type=1 is fingerprint)
```

An `FP`/`BIODATA` line for a PIN is the reliable signal that the person is
enrolled. This works whether the finger was enrolled remotely or by hand on
the device. Reference: `adms.parse_operlog`.

### 5. Commands Aerowis sends

| Action | Command text (`\t` = tab character) |
|---|---|
| Create or update user | `DATA UPDATE USERINFO PIN=1001\tName=Rahul Kumar\tPri=0` |
| Start fingerprint enrollment | `ENROLL_FP PIN=1001\tFID=6\tRETRY=3\tOVERWRITE=1` |
| (alternative, newer firmware) | `ENROLL_BIO TYPE=1\tPIN=1001\tNO=6\tRETRY=3\tOVERWRITE=1` |
| Delete user | `DATA DELETE USERINFO PIN=1001` |

`FID`/`NO` is the finger index: 0–4 are the left little finger to the left
thumb, and 5–9 are the right thumb to the right little finger. 6 is the right index.

Use whichever enroll command passed the on-site test.

**Validate before building commands.** Fields are separated by tabs and
commands by newlines. A name containing either could inject extra fields or a
whole extra command, such as `CLEAR DATA`, which wipes the device. Our
reference implementation:

- restricts the PIN to `[A-Za-z0-9]{1,14}`
- replaces tabs and newlines in names with spaces
- truncates names to 24 characters

See `adms.clean_pin` and `adms.clean_name`.

### 6. Registration screen in Aerowis

When staff save a new student:

1. Queue `add_user`, then `enroll`.
2. Show a live status from the command rows plus "fingerprint received":
   *sending user → waiting for finger → enrolled* (or *failed / timed out → Retry*).
3. A **Next** button clears the form for the next student.

`App.api_registration` in `server.py` computes this status, and
`static/index.html` shows the UI we tested.

If remote enrollment isn't supported (tests B and C fail), queue only
`add_user`. Tell the operator to enroll on the device (User Mgt. → ID →
Fingerprint), and show "enrolled" when the `FP` line arrives.

## Security and privacy checklist

- **Allow-list device serials.** Reject `cdata` and `devicecmd` from unknown
  `SN`s. Otherwise anyone who finds the URL can post fake punches.
- Keep HTTPS on the production endpoint.
- **Fingerprint templates are biometric personal data** (DPDP Act 2023).
  - Only store them if you need them, for example to copy users to the second
    office's device or to a replacement device. If you store them, encrypt
    them and restrict access.
  - Never log them. Our logs redact `TMP=`/`Tmp=`.
  - Collect consent at registration.
- Never let user-supplied text reach the device unvalidated (see section 5).

## Notes

- The handshake our test server sends is in `adms.handshake_response`. It
  deliberately sets no `TimeZone` and no other device settings.
- Multiple devices (there is a second office) work the same way: commands are
  queued per device serial.
- To trial without hardware: `python tools/fake_device.py --server <url>`
  simulates a device.
