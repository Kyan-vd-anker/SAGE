# Sage ESP32-WROOM wearable

This is the first-stage firmware for the ESP32-WROOM-32 belt wearable. It
leaves the existing C3 beacon unchanged.

## Current stage

Implemented:

- Wi-Fi setup access point: `Sage-Wearable-Setup`
- Browser setup at `http://192.168.4.1/`
- Saved Wi-Fi SSID/password, Sage host, and voice secret
- SH1106 OLED initialization and status text rendering
- GPIO0 active-low push-to-talk button
- I2C OLED initialization at address `0x3c`
- Status logging for setup, Wi-Fi, and button state

Not implemented yet:

- Bluetooth Classic discovery and profile connection for AirPods Pro 2.
- Microphone capture and PCM upload to `/api/voice/ask`.
- PCM response playback through AirPods.

Bluetooth now scans for a device whose name contains `AirPods`, connects the
A2DP source and HFP Audio Gateway profiles, and maps AVRCP play/pause events
to push-to-talk. The current callback plumbing sends silence and logs HFP
microphone frames; it does not yet upload microphone PCM to Sage or stream
Sage responses because those paths require an audio queue and the signed HTTP
request/response implementation. Pair the AirPods with the wearable during
testing and use the serial log to verify profile connections first.

## Build and flash

Use an ESP-IDF 5.5.x PowerShell or Command Prompt:

```powershell
cd "C:\Github Sage\SAGE\firmware\esp32-wearable"
idf.py set-target esp32
idf.py menuconfig
idf.py build
idf.py -p COMx flash monitor
```

In `menuconfig`, verify:

- Push-to-talk GPIO: `0` for the normal BOOT button
- OLED SDA: `21`
- OLED SCL: `22`
- OLED address: `0x3c`
- Setup AP password

If GPIO0 is held while resetting, the ESP32 enters download mode. Release
BOOT after reset and flash again.

## First setup

1. Flash the firmware.
2. Connect to Wi-Fi network `Sage-Wearable-Setup`.
3. Open `http://192.168.4.1/`.
4. Enter the home Wi-Fi credentials, Sage host address, and the same
   `SAGE_VOICE_SECRET` used by the existing speaker.
5. Use **Scan and connect AirPods** on the same page. Put the AirPods Pro 2
   in their case, open the lid, and hold the case button until the status
   light flashes white. Keep the case near the wearable during discovery.
6. Save and let the device reboot.

The setup page shows Bluetooth status, lists all discovered Classic Bluetooth
devices with their address and signal strength, and starts discovery only when
you click **Scan Bluetooth devices**. This avoids scanning while Wi-Fi is
initializing.

If the serial log reports repeated Wi-Fi `SA Query` failures, the access point
is disconnecting the wearable during WPA3/PMF handling. Use a WPA2-Personal
2.4 GHz network or disable WPA3 transition mode for the wearable network.
Select **Connect** next to the AirPods entry to start the A2DP and HFP
connections. The wearable also automatically connects to a nearby device
whose Bluetooth name contains `AirPods` after a scan.

After setup, the same web UI is available on the wearable's normal Wi-Fi
address. Find that address in the serial monitor from the line
`Web UI available at http://.../`, or in your router's connected-device list,
then open that address in a browser. The wearable and browser must be on the
same Wi-Fi network. The page uses HTTP and is not protected by a login, so do
not expose it outside your trusted local network.
