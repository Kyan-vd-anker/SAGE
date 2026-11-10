# Sage ESP32-WROOM wearable

This is the first-stage firmware for the ESP32-WROOM-32 belt wearable. It
leaves the existing C3 beacon unchanged.

## Current stage

Implemented:

- Wi-Fi setup access point: `Sage-Wearable-Setup`
- Browser setup at `http://192.168.4.1/`
- Saved Wi-Fi SSID/password, Sage host, and voice secret
- GPIO0 active-low push-to-talk button
- I2C OLED initialization at address `0x3c`
- Status logging for setup, Wi-Fi, and button state

Not implemented yet:

- OLED text rendering; the OLED controller must be identified as SSD1306 or
  SH1106.
- Bluetooth Classic A2DP/HFP connection to AirPods.
- Microphone capture and PCM upload to `/api/voice/ask`.
- PCM response playback through AirPods.

The firmware intentionally does not claim that a button press sent audio.
Those parts require validating the exact WROOM board audio path and the
ESP-IDF Bluetooth Classic audio profile support.

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
5. Save and let the device reboot.
