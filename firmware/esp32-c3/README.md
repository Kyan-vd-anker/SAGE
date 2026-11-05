# Sage ESP32-C3 BLE beacon and room receiver

This ESP-IDF project runs on generic ESP32-C3 boards. Flash one C3 as the carried BLE beacon and another as the engineering-room receiver. The existing ESP32-S3 speaker is the bedroom receiver. Both receivers must remain online during the 10-minute absence window before Sage will greet on bedroom arrival.

The beacon continuously advertises the service UUID `0xFFF0` by default. Nearby BLE scanners can observe it; the UUID is not encrypted or rotating. Keep this beacon within your trusted environment, and choose a different UUID in the `0xF000`–`0xFFFF` range if the default is already in use. Keep the selected UUID identical on the beacon, bedroom speaker, and engineering receiver.

## Configure Sage

In the ignored `start-sage.local.bat`, keep the dashboard on localhost and add the receiver configuration:

```bat
set "SAGE_HOST=127.0.0.1"
set "SAGE_VOICE_HOST=0.0.0.0"
set "SAGE_VOICE_PORT=8002"
set "SAGE_ALLOWED_CLIENT_IPS=192.168.1.50,192.168.1.51"
set "SAGE_VOICE_SECRET=use-the-same-random-secret-on-the-speaker-and-receiver"
set "SAGE_BLE_PRESENCE=1"
set "SAGE_BLE_NODE_ROOMS=bedroom-speaker:bedroom,engineering-c3:engineering"
set "SAGE_BLE_NEAR_RSSI=-65"
set "SAGE_BLE_AWAY_SECONDS=600"
```

Replace the example addresses with router reservations for the speaker and room receiver. The carried beacon does not need Wi-Fi or an allowed IP. The receiver and speaker must use the same HMAC secret as Sage. Allow only their reserved addresses to TCP port 8002 in the Windows Private firewall profile. Do not port-forward this port.

Restart Sage after changing the local settings. The receiver reports a signed heartbeat every 20 seconds to `/api/voice/presence`. Sage resets the continuous-absence timer whenever a configured receiver is offline for more than 90 seconds. Inspect receiver health at `http://127.0.0.1:8002/api/voice/presence`.

## Build and flash

Install ESP-IDF 5.5.x and use its PowerShell or Command Prompt. Connect the C3 with a USB data cable.

1. Build the carried beacon:

   ```powershell
   cd "C:\Github Sage\SAGE\firmware\esp32-c3"
   idf.py set-target esp32c3
   idf.py menuconfig
   ```

   In **Sage BLE presence node**, select **Carried beacon** and confirm the service UUID matches the S3 speaker (default `65520`). Save and exit, then build and flash:

   ```powershell
   idf.py build
   idf.py -p COM5 flash monitor
   ```

2. Connect the second C3. In the same project folder, run `idf.py menuconfig` and select **Stationary room receiver**. Set:
   - **Receiver node ID**: `engineering-c3`
   - **Wi-Fi SSID** and **Wi-Fi password** for the trusted 2.4 GHz network.
   - **Sage PC IPv4 address** and voice port (`8002`).
   - **Sage voice HMAC secret**: the same secret used by the speaker and Sage.
   - **BLE beacon service UUID**: the same value configured on the carried beacon and speaker.

   Save and exit, then build and flash the receiver:

   ```powershell
   idf.py build
   idf.py -p COM6 flash monitor
   ```

   Use the COM port shown for each board. The receiver log should show `Sage BLE room receiver is ready`; the beacon log should show `Sage presence beacon is advertising`.

The menuconfig values are saved in `sdkconfig`, which contains Wi-Fi credentials and the HMAC secret in receiver mode. Never share or commit it. The beacon itself broadcasts only the configured service UUID.

## Verify the greeting

1. Confirm `/api/voice/presence` lists both `bedroom-speaker` and `engineering-c3` as online.
2. Keep the beacon near a receiver for a moment, then take it out of range of both receivers for at least 10 minutes while both remain online.
3. Bring it near the bedroom speaker. Sage says **“Hello sir.”** once when bedroom RSSI reaches the threshold, default `-65` dBm. A first-ever beacon sighting does not trigger a greeting.

BLE signal strength varies with orientation, walls, and interference; tune `SAGE_BLE_NEAR_RSSI` based on the room rather than treating it as a guaranteed distance. A future smart speaker in the engineering room can replace the C3 receiver by posting the same signed heartbeat with node ID `engineering-c3`.
