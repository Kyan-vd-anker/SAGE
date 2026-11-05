# Install Sage firmware on the Waveshare ESP32-S3 speaker

This guide walks through configuring Sage on your PC, building the firmware, flashing the board, and checking that it works.
It is for the Waveshare ESP32-S3-AUDIO-Board. The firmware is built on your PC; there is no prebuilt image to download.

## What you need

- The Waveshare ESP32-S3-AUDIO-Board and a USB-C **data** cable.
- A 4–8 Ω speaker (about 3 W) connected to the board's speaker output.
- A Windows PC with this Sage repository checked out.
- The PC and speaker on the same trusted 2.4 GHz Wi-Fi network. The ESP32-S3 does not connect to 5 GHz Wi-Fi.
- Espressif ESP-IDF 5.5.x for ESP32-S3.
- Router access to reserve stable local IP addresses for the PC and speaker.

Do not connect a speaker with an impedance below 4 Ω. The speaker connection uses HTTP: HMAC authenticates requests but
does not encrypt audio. Use a trusted private network and do not expose the Sage voice port to the public internet.

## 1. Prepare Sage on the PC

The speaker calls Sage over the local network. First configure Sage's voice gateway and the network access it needs.

1. Reserve stable IPv4 addresses for the Sage PC, speaker, and engineering-room receiver in your router. Write them down. For example:
   - Sage PC: `192.168.1.20`
   - ESP32-S3 speaker: `192.168.1.50`
   - Engineering-room ESP32-C3 receiver: `192.168.1.51`
2. In the Sage repository folder, run `notepad .\start-sage.local.bat` to create or edit the ignored local settings file. Keep the existing `SAGE_HOST` value at `127.0.0.1`; only the voice gateway should listen on the LAN:

   ```bat
   set "SAGE_HOST=127.0.0.1"
   set "SAGE_VOICE_HOST=0.0.0.0"
   set "SAGE_VOICE_PORT=8002"
   set "SAGE_ALLOWED_CLIENT_IPS=192.168.1.50,192.168.1.51"
   set "SAGE_VOICE_SECRET=paste-a-private-random-secret-here"
   set "SAGE_BLE_PRESENCE=1"
   set "SAGE_BLE_NODE_ROOMS=bedroom-speaker:bedroom,engineering-c3:engineering"
   set "SAGE_BLE_NEAR_RSSI=-65"
   ```

   Replace both example device addresses with their reserved addresses. The carried beacon needs no Wi-Fi address. Generate a secret in PowerShell with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Paste the generated value into `SAGE_VOICE_SECRET`. Use that exact value later in the firmware settings. Do not put secrets in `start-sage.bat`, source control, or messages.
3. In an Administrator PowerShell, allow only the speaker and engineering receiver addresses through the Windows Private firewall profile. Replace the example addresses:

   ```powershell
   New-NetFirewallRule -DisplayName "Sage voice from ESP32" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8002 -RemoteAddress 192.168.1.50,192.168.1.51 -Profile Private
   ```

   Do not create a router port-forward for port 8002.
4. Start or restart Sage by running `start-sage.bat`. Check the gateway locally:

   ```powershell
   curl.exe http://127.0.0.1:8002/api/voice/status
   ```

   Continue when this returns a status response. Speech recognition and at least one speech output engine must be installed for voice conversations and spoken replies. If needed, follow the PC speech setup in [VOICE-GUIDE.md](../../VOICE-GUIDE.md#1-install-on-the-sage-pc).

## 2. Install ESP-IDF

1. Install Espressif's **ESP-IDF v5.5.x** for Windows using the [official ESP32-S3 Windows setup guide](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/get-started/windows-setup.html). The Espressif installer sets up the compiler, Python environment, and build tools.
2. Open the **ESP-IDF 5.5 PowerShell** or **ESP-IDF 5.5 Command Prompt** shortcut installed by Espressif. Use this ESP-IDF terminal for every `idf.py` command below.
3. Check that the tool is available:

   ```powershell
   idf.py --version
   ```

## 3. Connect the speaker and configure the firmware

1. Connect the board to the PC with the USB-C data cable. Connect the external 4–8 Ω speaker to the board's speaker output.
2. In the ESP-IDF terminal, change to the firmware folder. Adjust the path if you checked out Sage somewhere else:

   ```powershell
   cd "C:\Github Sage\SAGE\firmware\esp32"
   idf.py set-target esp32s3
   idf.py menuconfig
   ```

3. If Bluetooth is not enabled yet, open **Component config > Bluetooth**, enable Bluetooth with the **Bluedroid** host and controller, and keep **Bluetooth Low Energy** enabled. Then in **Sage speaker**, enter:
   - **Wi-Fi SSID** and **Wi-Fi password** for the 2.4 GHz network.
   - **Sage PC IPv4 address**, for example `192.168.1.20`. This is the initial address; you can change it later on the speaker's setup page without rebuilding.
   - **Sage voice gateway port**: `8002`.
   - **Sage voice HMAC secret**: the exact same value as `SAGE_VOICE_SECRET` in `start-sage.local.bat`.
   - **Speaker setup page password**: use a different random password, up to 64 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   - **Enable bedroom BLE presence greetings**: on.
   - **BLE receiver node ID**: `bedroom-speaker`.
   - **BLE beacon service UUID**: leave at the shared default `65520` (`0xFFF0`) unless you change it on every node.
4. Find the **ESP Speech Recognition** options. Enable the **Hi,ESP / `wn9_hiesp`** WakeNet model. In menuconfig, press `/` to search for `wn9_hiesp` if you do not see it in the menus. Enable the matching model option, return to the menu, then save and exit.
5. The project supplies its own partition table and defaults for the board's 16 MB flash, PSRAM, and CPU. Do not replace `firmware/esp32/partitions.csv`.

The settings are saved in the local `sdkconfig` file. It contains the Wi-Fi password, HMAC secret, and setup page password: do not share it or commit it.
Build and flash the carried beacon and engineering-room receiver using the [ESP32-C3 presence-node guide](../esp32-c3/README.md).

## 4. Change the Sage server IP from a browser

After the speaker connects to Wi-Fi, its serial log prints a setup-page URL such as `http://192.168.1.50/`. Open that address
from a device on the same trusted Wi-Fi network. Sign in with username `sage` and the setup page password you configured in
menuconfig. Enter the Sage PC's current local IPv4 address and select **Save and restart speaker**. The address is saved
to the speaker and remains set after power loss; you do not need to rebuild or reflash to change it.
If you forget the setup page password, set a new one in menuconfig and reflash the firmware.

The page uses HTTP, not HTTPS. Only use it on a trusted private network; anyone who can observe that Wi-Fi traffic could
see the password. Do not expose the setup page or the Sage voice gateway to the public internet.

## 5. Build and flash

1. From the `firmware\esp32` folder, build:

   ```powershell
   idf.py build
   ```

   The first build can take several minutes while ESP-IDF downloads the required components and builds the wake-word model image. The build must finish successfully before flashing.
2. Find the board's COM port in **Windows Device Manager > Ports (COM & LPT)**. If more than one port appears, unplug the board and check which port disappears, then reconnect it.
3. Flash and open the serial log. Replace `COM5` with the port you found:

   ```powershell
   idf.py -p COM5 flash monitor
   ```

   If the board will not enter download mode, unplug USB, hold **BOOT** while reconnecting USB, then release **BOOT** and retry the flash command.
4. Wait for the serial log to show:

   ```text
   Sage speaker ready; say Hi ESP to speak
   ```

   If you need to close the serial monitor, press **Ctrl+]**.

## 6. Test it

1. Confirm Sage is still running and the PC and speaker are on the same Wi-Fi network.
2. Say **“Hi ESP”**, pause briefly, then ask a short question, for example **“What needs my attention?”**
3. Check the serial log and Sage voice status if it does not respond:

   ```powershell
   curl.exe http://127.0.0.1:8002/api/voice/status
   ```

4. Check receiver health at `http://127.0.0.1:8002/api/voice/presence`. The greeting is issued only when both configured room receivers are online, the beacon has remained unseen for at least 10 minutes, and its bedroom RSSI reaches the configured threshold. It is queued once on the transition to near. RSSI is an approximate signal-strength threshold, not a precise distance measurement.

## Troubleshooting

- **`idf.py` is not recognized:** open the ESP-IDF terminal shortcut, not a regular PowerShell window.
- **Build cannot find `wn9_hiesp` or the model partition:** confirm the model is enabled in menuconfig, then run `idf.py build` and `idf.py -p COM5 flash monitor` again. The flash target also writes the generated model image.
- **Wi-Fi does not connect:** verify the SSID and password, and use a 2.4 GHz network. The firmware waits for a successful NTP time sync before sending signed requests.
- **Sage returns HTTP 401:** the firmware's HMAC secret must exactly match `SAGE_VOICE_SECRET` on the PC. Make sure both the PC and board can get the correct time.
- **Sage returns HTTP 403 or the request times out:** verify the speaker's reserved IP matches `SAGE_ALLOWED_CLIENT_IPS` and the firewall rule's `RemoteAddress`; check that guest Wi-Fi or router client isolation is not blocking local traffic.
- **Speaker connects but Sage cannot be reached:** open the setup page using the address in the serial log and confirm it points to the PC's current LAN IPv4 address. Confirm Sage is running, `SAGE_VOICE_HOST` is `0.0.0.0`, and the port is `8002`.
- **Sage replies in the log but there is no sound:** check the external speaker connection and 4–8 Ω rating, then verify Sage has a working text-to-speech engine.

## Current limitations

This firmware targets the “Hi ESP” wake word and mirrors Sage's wall-display states on the board's seven WS2812 RGB LEDs (GPIO 38). It does not include echo cancellation or a hardware microphone mute. The firmware builds with ESP-IDF 5.5.6 but has not been tested on a physical board in this development environment, so confirm the audio and light-ring behavior on your board before relying on it.
