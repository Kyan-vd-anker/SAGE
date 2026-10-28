# Sage wall display (Raspberry Pi Zero W + 7" touch screen)

A small native touch client that shows your Sage assistant on a wall screen. It talks to Sage on your PC/server
over Wi-Fi. The AI itself keeps running on the PC; the Pi only draws the screen and handles touch.

What it shows
- Big clock and date, your calendar (next events), what Sage is working on, your next tasks, Sage's last message.
- **Needs you**: pending tool approvals with Approve / Deny buttons, and overdue tasks. A new approval wakes the screen.
- **Ask Sage**: on-screen keyboard, streamed replies, Stop button. Quick buttons: Brief, Next action, Status.
- Dims after a few minutes; at night (23:00-07:00 by default) it shows only a very dim clock. Tap to wake.

## Why a native app and not a browser?
The original Pi Zero W has an ARMv6 CPU. Chromium no longer runs properly on it (crashes or is unusably slow), so the
screen is drawn directly with pygame. It uses roughly 50 MB of RAM (measured on a PC; expect similar on the Pi) and no browser.

## Hardware that works
- Raspberry Pi **Zero W** (a Zero 2 W is faster and works the same way).
- A 7" screen with **HDMI video + USB touch** (for example a Waveshare 7" HDMI LCD (C), 1024x600). 
- **Not** the official Raspberry Pi Touch Display: it needs the DSI connector, which no Pi Zero has.
- Micro-HDMI to HDMI cable/adapter, micro-USB OTG adapter (the touch cable goes into the Pi's *USB* port),
  and a good 5 V 3 A power supply. Power the screen and the Pi properly (a weak supply gives flickering/undervoltage).
- Wi-Fi: the Zero W only supports 2.4 GHz.

## 1. Prepare the Sage PC
1. `start-sage.bat` already listens on the network (`SAGE_HOST=0.0.0.0`). Open it and put the Pi's IP address in
   `SAGE_ALLOWED_CLIENT_IPS` (comma separated for several). Other devices still get a 403.
2. Give the Pi a fixed address (DHCP reservation in your router) so this never changes.
3. Allow the port in Windows Firewall (admin Command Prompt, once):
   `netsh advfirewall firewall add rule name="Sage wall" dir=in action=allow protocol=TCP localport=8001 profile=private`
4. Restart Sage (stop the old process first, then run `start-sage.bat`).

Check from any browser on the Pi's network: `http://<PC-IP>:8001/api/wall` should NOT work from other devices (403) and
should work from the Pi once its IP is allowed.

## 2. Set up the Pi
1. Flash **Raspberry Pi OS Lite (32-bit)** with Raspberry Pi Imager. In the Imager settings set the hostname, user,
   Wi-Fi and enable SSH.
2. Boot, connect with SSH, and copy this `wall` folder to the Pi (for example with `scp -r wall pi@<pi-ip>:`).
3. Run the installer as your normal user:
   ```
   cd wall
   bash install.sh
   sudo nano /etc/sage-wall.conf     # set SAGE_URL=http://<PC-IP>:8001
   sudo timedatectl set-timezone Europe/Amsterdam
   sudo reboot
   ```
4. After the reboot the screen should show the wall display. Logs: `journalctl -u sage-wall -f`.

## Settings (`/etc/sage-wall.conf`)
`SAGE_URL`, `POLL_SECONDS`, `DIM_AFTER_SECONDS`, `NIGHT_START`, `NIGHT_END`, `NIGHT_WAKE_SECONDS`, `FONT`.
Restart after editing: `sudo systemctl restart sage-wall`.

## Try it without a Pi
On any PC with Python and `pip install pygame`:
```
python sage_wall.py --demo --windowed 1024x600                    # fake data
python sage_wall.py --url http://<PC-IP>:8001 --windowed 1024x600 # your real Sage
```
Esc goes back, mouse clicks act as touches.

## Troubleshooting
- **Black screen / service fails:** `journalctl -u sage-wall -n 50`. Make sure you rebooted after install (group access),
  and that `dtoverlay=vc4-kms-v3d` is enabled in `/boot/firmware/config.txt` (it is by default on current Pi OS).
- **Wrong resolution or no picture:** add `video=HDMI-A-1:1024x600@60` to the single line in `/boot/firmware/cmdline.txt`.
  Some Waveshare screens need extra settings; follow the screen's own Pi instructions for the picture, this app only
  needs a normal HDMI display.
- **Touch does nothing:** `ls /dev/input/by-id` should list the touch device; your user must be in the `input` group
  (`groups`). If taps land in the wrong place, check the screen's touch calibration notes.
- **"Cannot reach Sage" on screen:** wrong `SAGE_URL`, Sage not running, PC firewall, or the Pi's IP is not in
  `SAGE_ALLOWED_CLIENT_IPS` (the screen then says 403).
- **Starts but is slow:** normal for a Zero W when many things redraw; the app redraws only when something changes and
  runs at a few frames per second when idle.
- If KMS/DRM output does not work on your setup, run the program inside a minimal X session instead
  (`sudo apt install xserver-xorg xinit`, then `startx /usr/bin/python3 /opt/sage-wall/sage_wall.py -- -nocursor`)
  and remove the `SDL_VIDEODRIVER=kmsdrm` line from the service.

## Not included yet
No microphone or speaker: the Pi Zero W has no audio input, so wake-word and voice replies need extra hardware
(a USB audio adapter via an OTG hub, or an I2S mic HAT). Say if you want that added next.
