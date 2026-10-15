# eSSL Enrollment Test

A small test app that proves we can **register students/staff and enroll their
fingerprint on the eSSL device directly from our own software**, without the
eSSL app.

It talks to the device with the same cloud (ADMS) protocol Aerowis already uses
for attendance. If the test works, the outside Aerowis team gets this code plus
[`docs/INTEGRATION.md`](docs/INTEGRATION.md) and builds the same thing into Aerowis.

What it does:

1. You type a User ID and name and press **Register**.
2. The user is created on the device.
3. The device shows its "place your finger" screen by itself.
4. The student scans their finger 3 times. The page shows **✅ Enrolled**.
5. Press **Next student** and repeat.

It also shows punches (attendance) as they happen, and a log of everything the
device sends, so we have proof of what works.

---

## Before the test

**Do the test after office hours.** While the device points at this test app,
punches go to the test app and **not** to Aerowis.

### 1. Check the laptop is on the device's network

The device is at `192.168.29.48`. The laptop must be on the same `192.168.29.x`
network. Same Wi-Fi name is not always enough, because there are two routers.

On the laptop, open **Command Prompt** and run:

```
ipconfig
```

Look at **IPv4 Address** under your Wi-Fi adapter.

- `192.168.29.something` → good.
- Anything else (for example `192.168.1.x` or `192.168.0.x`) → the laptop is behind
  the other router. Connect to the other Wi-Fi network, or plug the laptop
  into the same switch/router as the device with a cable.

The app also checks this for you when it starts.

### 2. Install Python

Download Python 3 from <https://www.python.org/downloads/> and install it.
**Tick "Add python.exe to PATH"** on the first installer screen.
Nothing else needs to be installed.

### 3. Get this code

On GitHub, open this repository, then **Code → Download ZIP**, and unzip it
(for example to `C:\essl`). Make sure you have the branch with the
`essl_test_server` folder.

---

## Running the test

### 1. Start the test app

In Command Prompt:

```
cd C:\essl
python -m essl_test_server
```

If Windows Firewall asks, click **Allow** (Private networks).

It prints a network check and the address to give the device, for example:

```
On the device: Menu > COMM. > Cloud Server Setting > Server Address:
      http://192.168.29.50:8080
```

Leave this window open. Closing it stops the app.

### 2. Point the device at the laptop

On the device: **Menu → COMM. → Cloud Server Setting**.

1. **Write down the current Server Address first:** `https://api.aerowis.com/essl/push`.
2. Change **Server Address** to the address the app printed (for example `http://192.168.29.50:8080`).
3. Leave **Server Mode = ADMS** and **Enable Proxy Server = OFF**.
4. Save and go back to the main screen.

If the device doesn't show up in the next step, set **Enable Domain Name** to
**OFF**. Then enter just the IP (`192.168.29.50`) and port (`8080`) in the
fields it shows.

### 3. Open the test page

On the laptop, open **http://localhost:8080** in a browser.

Within about 30 seconds the top-right should show `NFZ8250601118 · online`.
It might first receive the device's old attendance history. That's normal.

### 4. Run the tests

Use **test IDs that no real student has**, for example `99001`, `99002`.
Registering an existing ID overwrites that person's name on the device.

| # | What to do | What to check | Result |
|---|---|---|---|
| A | Untick "Start fingerprint scan…", register `99001` / `Test One` | Status shows "User created on device". On the device, `99001` appears under User Mgt. | |
| B | Leave it ticked, register `99002` / `Test Two` | The device shows the "place finger" screen by itself. After 3 scans the page shows ✅ Enrolled. | |
| C | Only if B failed: open **Advanced**, choose **ENROLL_BIO**, press **Retry fingerprint** | Same as B | |
| D | Scan `99002`'s finger on the device as a normal punch | It appears in the **Punches** tab with the name | |
| E | For `99001`, enroll the finger manually on the device (User Mgt.) | The **Users** tab shows Fingers = 1 for `99001` | |
| F | Users tab → **Delete from device** for `99001` and `99002` | Both disappear from the device | |

When you finish, take screenshots of the **Commands** and **Device log** tabs.
The file `protocol.log` in the same folder holds the full record. Send these to
the Aerowis team with the results.

**If B and C both fail:** the firmware doesn't allow starting enrollment
remotely. Test A plus test E is still a big win: users are created from our
software, staff only press "Enroll" on the device, and our software sees the
fingerprint arrive.

### 5. Put the device back (don't skip this)

On the device: **Menu → COMM. → Cloud Server Setting → Server Address** →
`https://api.aerowis.com/essl/push` (and **Enable Domain Name** back **ON** if
you changed it).

Then do one punch and check that it shows up in Aerowis.

---

## Troubleshooting

| Problem | Try |
|---|---|
| Device never shows "online" | Check the laptop IP is `192.168.29.x`. Check Windows Firewall allowed Python. Try **Enable Domain Name = OFF** with IP + port. |
| Firewall didn't ask | In an **administrator** Command Prompt: `netsh advfirewall firewall add rule name="eSSL test" dir=in action=allow protocol=TCP localport=8080` |
| Port 8080 is in use | `python -m essl_test_server --port 8090` and use `:8090` on the device |
| Status stuck on "Sending user…" | The device isn't polling. Look at the Device log tab. Restart the device if needed. |
| "Device refused the fingerprint scan (code -1)" | Try ENROLL_BIO (test C). If that fails too, see "If B and C both fail" above. |

## Options

```
python -m essl_test_server --help
  --port 8080              port to listen on
  --delay 5                seconds between device polls (lower = faster response)
  --allowed-sn NFZ8250601118   only accept this device
  --device-ip 192.168.29.48    device IP for the startup network check
  --store-templates        keep fingerprint templates (off: only "finger N received" is stored)
  --allow-remote-admin     let other PCs open the test page (off by default)
```

## Privacy

Fingerprints are sensitive personal data. By default this app **does not store
fingerprint templates**. It only records that finger N was received for user X.
Templates are also blanked out of all logs. Delete `essl_test.db` and
`protocol.log` when the test is finished.

## For developers

```
python -m unittest discover -s tests           # run the tests
python tools/fake_device.py --server http://localhost:8080   # pretend device, no hardware needed
```

- `essl_test_server/adms.py`: protocol parsing and command building (no I/O)
- `essl_test_server/store.py`: SQLite storage and the command queue
- `essl_test_server/server.py`: HTTP server for device and admin endpoints
- `essl_test_server/static/index.html`: the test page
