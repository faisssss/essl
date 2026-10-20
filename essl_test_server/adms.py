"""Pure helpers for the ZKTeco / eSSL ADMS ("PUSH" / iclock) protocol.

Nothing in here touches the network or the database, so this is the part the
Aerowis team can port almost line-for-line into their own backend.

Protocol summary (device = client, our server = HTTP server):

  GET  .../iclock/cdata?SN=..&options=all      handshake, we reply with options
  POST .../iclock/cdata?SN=..&table=ATTLOG     attendance punches
  POST .../iclock/cdata?SN=..&table=OPERLOG    users / fingerprints / op logs
  POST .../iclock/cdata?SN=..&table=BIODATA    biometric templates (newer fw)
  GET  .../iclock/getrequest?SN=..             "any commands for me?"
  POST .../iclock/devicecmd?SN=..              results of commands we sent
"""

import re

# Fingerprint index the device uses: 0-4 left little..thumb, 5-9 right thumb..little.
FINGER_NAMES = {
    0: "Left little", 1: "Left ring", 2: "Left middle", 3: "Left index", 4: "Left thumb",
    5: "Right thumb", 6: "Right index", 7: "Right middle", 8: "Right ring", 9: "Right little",
}

PIN_RE = re.compile(r"^[A-Za-z0-9]{1,14}$")
NAME_MAX = 24


class ValidationError(ValueError):
    pass


def clean_pin(pin):
    pin = (pin or "").strip()
    if not PIN_RE.match(pin):
        raise ValidationError("User ID must be 1-14 letters/digits (no spaces or symbols).")
    return pin


def clean_name(name):
    # Tabs/newlines would let a name inject extra fields or commands into the protocol.
    name = re.sub(r"[\t\r\n]", " ", name or "").strip()
    if not name:
        raise ValidationError("Name is required.")
    return name[:NAME_MAX]


def clean_finger(finger):
    try:
        finger = int(finger)
    except (TypeError, ValueError):
        raise ValidationError("Finger must be a number 0-9.")
    if finger not in FINGER_NAMES:
        raise ValidationError("Finger must be a number 0-9.")
    return finger


# ---------------------------------------------------------------- endpoints

def endpoint_of(path):
    """Map any '.../iclock/<name>' path to '<name>'.

    The device prepends whatever path the Server Address has (Aerowis uses
    '/essl/push'), so we only look at what follows '/iclock/'.
    """
    idx = path.find("/iclock/")
    if idx < 0:
        return None
    name = path[idx + len("/iclock/"):].strip("/")
    if name.endswith(".aspx"):
        name = name[:-5]
    return name or None


# ---------------------------------------------------------------- responses

def handshake_response(sn, delay=5):
    """Options sent back on the first GET /iclock/cdata.

    Deliberately leaves out TimeZone and anything that changes device
    settings: a test server should not alter the clock or config.
    """
    lines = [
        f"GET OPTION FROM: {sn}",
        "ATTLOGStamp=None",
        "OPERLOGStamp=9999",
        "ATTPHOTOStamp=None",
        "ErrorDelay=30",
        f"Delay={delay}",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=TransData AttLog\tOpLog\tEnrollUser\tChgUser\tEnrollFP\tChgFP",
        "Realtime=1",
        "Encrypt=None",
        "ServerVer=2.4.1",
    ]
    return "\n".join(lines) + "\n"


def commands_response(commands):
    """Body for GET /iclock/getrequest. commands = [(id, text), ...]."""
    if not commands:
        return "OK"
    return "".join(f"C:{cid}:{text}\n" for cid, text in commands)


# ---------------------------------------------------------------- commands

def cmd_add_user(pin, name):
    return f"DATA UPDATE USERINFO PIN={clean_pin(pin)}\tName={clean_name(name)}\tPri=0"


def cmd_delete_user(pin):
    return f"DATA DELETE USERINFO PIN={clean_pin(pin)}"


def cmd_enroll(pin, finger, style="ENROLL_FP"):
    """Ask the device to show its 'place your finger' screen for this user.

    Firmwares differ in which spelling they accept, so the test app lets you
    try both: ENROLL_FP (older push) and ENROLL_BIO (newer push).
    """
    pin, finger = clean_pin(pin), clean_finger(finger)
    if style == "ENROLL_BIO":
        return f"ENROLL_BIO TYPE=1\tPIN={pin}\tNO={finger}\tRETRY=3\tOVERWRITE=1"
    return f"ENROLL_FP PIN={pin}\tFID={finger}\tRETRY=3\tOVERWRITE=1"


# Remote door control. The ZKTeco push command is CONTROL DEVICE AABBCCDD:
#   AA=01 (output), BB=01 (door 1), CC=01 (lock relay), DD=seconds to stay unlocked,
#   where 00 = lock now and FF (255) = stay unlocked until told otherwise.
# Whether DD is hex or decimal varies by firmware, and some firmware uses AC_UNLOCK
# instead, so the test app lets you pick. Test with 5 seconds first: '05' is the
# same in both.
DOOR_STYLES = ("hex", "decimal", "ac_unlock")
DOOR_KEEP_OPEN = 255


def cmd_door(seconds, style="hex"):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        raise ValidationError("Seconds must be a number.")
    if not 0 <= seconds <= 255:
        raise ValidationError("Seconds must be between 0 and 255.")
    if style == "ac_unlock":
        if seconds == 0:
            raise ValidationError("AC_UNLOCK cannot lock the door; use CONTROL DEVICE for that.")
        return "AC_UNLOCK"
    dd = f"{seconds:02d}" if style == "decimal" else f"{seconds:02X}"
    return f"CONTROL DEVICE 010101{dd}"


def is_enroll_command(text):
    return text.startswith("ENROLL_")


# ---------------------------------------------------------------- parsing

def parse_kv_fields(text):
    """'PIN=1\tName=Abc\tPri=0' -> {'PIN': '1', 'Name': 'Abc', 'Pri': '0'}."""
    out = {}
    for part in text.split("\t"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v
    return out


def parse_attlog(body):
    """ATTLOG lines: PIN \t 'YYYY-MM-DD HH:MM:SS' \t status \t verify \t workcode ..."""
    rows = []
    for line in body.splitlines():
        cols = line.split("\t")
        if len(cols) < 2 or not cols[0].strip():
            continue
        rows.append({
            "pin": cols[0].strip(),
            "time": cols[1].strip(),
            "status": cols[2].strip() if len(cols) > 2 else "",
            "verify": cols[3].strip() if len(cols) > 3 else "",
        })
    return rows


def parse_operlog(body):
    """OPERLOG / BIODATA / USERINFO bodies. Returns (users, fingerprints, other_count).

    Users:        'USER PIN=1\tName=..\tPri=0\t...'
    Fingerprints: 'FP PIN=1\tFID=6\tSize=1184\tValid=1\tTMP=<base64>'
                  'BIODATA Pin=1\tNo=0\tIndex=6\tValid=1\tType=1\t...\tTmp=<base64>'
    """
    users, fps, other = [], [], 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        head = head.upper()
        if head == "USER":
            f = parse_kv_fields(rest)
            if f.get("PIN"):
                users.append({"pin": f["PIN"], "name": f.get("Name", "")})
        elif head == "FP":
            f = parse_kv_fields(rest)
            if f.get("PIN"):
                fps.append({
                    "pin": f["PIN"], "finger": _int(f.get("FID")),
                    "size": _int(f.get("Size")), "valid": f.get("Valid", ""),
                    "template": f.get("TMP", ""),
                })
        elif head == "BIODATA":
            f = parse_kv_fields(rest)
            # Type 1 = fingerprint; other types are face/palm/etc.
            if f.get("Pin") and f.get("Type", "1") == "1":
                tmp = f.get("Tmp", "")
                fps.append({
                    "pin": f["Pin"], "finger": _int(f.get("No")),
                    "size": len(tmp), "valid": f.get("Valid", ""), "template": tmp,
                })
        else:
            other += 1
    return users, fps, other


def parse_devicecmd(body):
    """'ID=12&Return=0&CMD=DATA' (one per line) -> [{'id': 12, 'return': 0, 'cmd': 'DATA'}]."""
    results = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = {}
        for part in line.split("&"):
            k, _, v = part.partition("=")
            fields[k.strip()] = v.strip()
        cid = _int(fields.get("ID"))
        if cid is None:
            continue
        results.append({"id": cid, "return": _int(fields.get("Return")), "cmd": fields.get("CMD", "")})
    return results


def redact_templates(text):
    """Strip fingerprint templates out of anything we log."""
    return re.sub(r"((?:TMP|Tmp)=)[^\t\r\n]*", r"\1<redacted>", text)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
