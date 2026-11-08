# Install guide: Sage ESP32-C3 beacon and room receiver

Two ESP32-C3 boards run the **same project** in two different roles:

| Board | Role | Needs Wi-Fi | Where it lives |
|---|---|---|---|
| C3 #1 | **Carried beacon** | No | In your pocket or bag |
| C3 #2 | **Engineering-room receiver** (`engineering-c3`) | Yes (2.4 GHz) | Fixed in the engineering room |

The bedroom receiver is the ESP32-S3 speaker, which is already installed. Sage greets you at the bedroom only after both receivers have stayed online and the beacon has been away for 10 minutes.

## What you need

- Two ESP32-C3 boards and two USB **data** cables (charge-only cables will not work)
- Windows PC with **ESP-IDF 5.5.x** (Espressif's Windows installer; open the "ESP-IDF 5.5 PowerShell" shortcut it creates)
- The Sage server's LAN IP address (the Kali machine) and the **voice secret** the speaker already uses
- Your 2.4 GHz Wi-Fi name and password
- The BLE UUID the speaker uses (default `65520`; all three devices must match)

## Step 1: Prepare the Sage server (Kali)

The speaker is already working, so most of this exists. Check it, and add the new receiver.

1. Give the new receiver a fixed IP: add a DHCP reservation for it in your router once it has connected the first time (Step 3).
2. Make sure the voice gateway listens on the network and knows both receivers. In the Sage service environment (the same place you set `SAGE_PASSWORD`):

   ```
   SAGE_VOICE_HOST=0.0.0.0
   SAGE_VOICE_PORT=8002
   SAGE_VOICE_SECRET=<same secret as the speaker>
   SAGE_BLE_PRESENCE=1
   SAGE_BLE_NODE_ROOMS=bedroom-speaker:bedroom,engineering-c3:engineering
   SAGE_BLE_NEAR_RSSI=-80
   SAGE_BLE_AWAY_SECONDS=600
   SAGE_ALLOWED_CLIENT_IPS=<speaker IP>,<engineering receiver IP>
   ```

3. If Kali uses a firewall, allow only the two devices to reach port 8002, and never port-forward it:

   ```bash
   sudo ufw allow from <speaker IP> to any port 8002 proto tcp
   sudo ufw allow from <receiver IP> to any port 8002 proto tcp
   ```

4. Restart Sage: `systemctl --user restart sage`

## Step 2: Flash the carried beacon

Do the beacon first. It holds no Wi-Fi password or secret.

```powershell
cd "C:\Github Sage\SAGE\firmware\esp32-c3"
idf.py set-target esp32c3
idf.py menuconfig
```

In **Sage BLE presence node**:
- **BLE node role** → **Carried beacon**
- **BLE beacon service UUID** → same as the speaker (default `65520`)

Save and quit (`S`, then `Q`), then plug in board #1 and flash:

```powershell
idf.py build
idf.py -p COM5 flash monitor
```

Replace `COM5` with the port shown in Device Manager under *Ports (COM & LPT)*. You should see `Sage presence beacon is advertising`. Press `Ctrl+]` to leave the monitor. Unplug it and power it from a USB power bank or battery.

## Step 3: Flash the engineering receiver

Plug in board #2, then in the same folder:

```powershell
idf.py menuconfig
```

In **Sage BLE presence node**:
- **BLE node role** → **Stationary room receiver**
- **Receiver node ID** → `engineering-c3` (must match `SAGE_BLE_NODE_ROOMS`)
- **Wi-Fi SSID** and **Wi-Fi password** → your 2.4 GHz network
- **Sage PC IPv4 address** → the Kali server's LAN IP
- **Sage voice gateway port** → `8002`
- **Sage voice HMAC secret** → the same secret as the speaker and Sage
- **BLE beacon service UUID** → same as the beacon and speaker

Then:

```powershell
idf.py build
idf.py -p COM6 flash monitor
```

You should see `Sage BLE room receiver is ready`. Now add the router reservation for this board's IP (Step 1.1) and put that IP in `SAGE_ALLOWED_CLIENT_IPS` if you did not already, then restart Sage.

> **Keep `sdkconfig` private.** After Step 3 it contains your Wi-Fi password and the voice secret. It is now listed in `.gitignore`; do not share the file or the `build` folder. If either was ever committed or shared, change the Wi-Fi password and the voice secret.

## Step 4: Verify

1. On Kali, check both receivers are online:

   ```bash
   curl http://127.0.0.1:8002/api/voice/presence
   ```

   `bedroom-speaker` and `engineering-c3` should both be listed. A receiver sends a signed heartbeat every 20 seconds.
2. Hold the beacon near a receiver for a moment so Sage sees it.
3. Take the beacon out of range of both receivers for at least 10 minutes while both stay online.
4. Walk to the bedroom. Sage says **"Hello sir."** once when the bedroom signal reaches the threshold. A first-ever sighting does not trigger a greeting.

BLE signal strength depends on walls, orientation and interference. If the greeting fires too early or late, tune `SAGE_BLE_NEAR_RSSI` (more negative = farther away).

## Troubleshooting

| Problem | Fix |
|---|---|
| No COM port appears | Use a data cable. Hold **BOOT**, tap **RESET**, release BOOT, then flash again. |
| `idf.py` not found | Use the ESP-IDF PowerShell, not a normal terminal. |
| Receiver never shows online | Wi-Fi must be 2.4 GHz. Check the server IP, port 8002, the firewall rule, and that `SAGE_VOICE_HOST` is `0.0.0.0`. |
| Receiver connects but is rejected | The HMAC secret differs from the one Sage uses. Re-enter it in `menuconfig` and reflash. |
| Beacon is never seen | UUID differs between beacon and receivers. All must match. |
| Greeting never fires | Both receivers must stay online for the full 10 minutes away. Check `/api/voice/presence`. |
| Wrong role flashed | Run `idf.py menuconfig`, change the role, then build and flash again. |
| Link error `undefined reference to esp_ble_gap_start_scanning` (or `start_advertising`, `set_scan_params`) | The project needs BLE 4.2 features, which IDF 5.5 turns off by default on the C3. `sdkconfig.defaults` now sets them. If an old `sdkconfig` still has `CONFIG_BT_BLE_42_FEATURES_SUPPORTED` unset, delete `sdkconfig`, run `idf.py set-target esp32c3`, then redo `menuconfig` and build. |

The dashboard password does not affect any of this. Receivers talk to the voice gateway on port 8002, which uses its own HMAC secret.
