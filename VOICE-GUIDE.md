# Sage voice: the ESP32-S3 speaker

Talk to Sage and hear it answer through the Waveshare ESP32-S3-AUDIO-Board. The morning brief, timers, reminders and
alarms are spoken on it too. The board listens and plays; all the thinking stays on the Sage PC. The wall display
shows the speaker's status.

**Setting up the board?** Follow the dedicated [ESP32 speaker firmware installation guide](firmware/esp32/README.md) for Sage's LAN settings, ESP-IDF setup, build, flash, and troubleshooting. After setup, change the Sage PC IP from the speaker's password-protected web page without reflashing.

```
speaker board  --Wi-Fi-->  Sage PC (port 8002)
 wake word + VAD      -->  speech-to-text (faster-whisper)
                         spoken commands (sleep, status) or the normal Sage chat (same history, tools, reminders)
 plays the reply      <--  text-to-speech (Piper / espeak-ng / Windows voices)
                         wall display shows speaker status
```

## What you can say
| Say | Sage does |
|---|---|
| "Goodnight" / "Goedenacht" / "sleep mode" | Sleep mode: speaker only rings alarms and timers, light ring and screens go dark, the wall display shows only the clock. |
| "Good morning" / "Goedemorgen" | Wakes everything, says good morning, reads the brief (once per 3 hours) and then speaks what was held back. An alarm also wakes it by itself. |
| "What needs my attention?" / "status update" | Approvals waiting, overdue tasks, what is ringing, what the planner needs from you. |
| "What timers do I have?" / "Welke timers heb ik?" | Active timers and reminders with the time left. |
| "What happened while I was away?" | Notable events from the last 24 hours. |
| "When was the last backup?" | Latest backup event. |
| "Show me the voice log" | Last 24 hours: requests, held for approval, refused logins. |
| anything else | Normal Sage chat, with the limits below. |

While asleep: reminders, the brief and anything Sage says by itself wait (up to 10 hours) and play after "good morning".

**Wake-up from your phone:** when the iPhone Shortcut sends the wake signal, the wall display wakes up and shows the whole brief
full screen until you tap anywhere on it (or press "Got it"). The speaker wakes from sleep mode too and reads the brief.
The 07:30 scheduled brief does not take over the wall screen; it is only spoken. A ringing alarm or timer still shows on top
of the brief, and the brief is waiting when you dismiss it. (If the wall display is off or restarting at that moment it misses the brief.)

## Security
1. **Only your speaker can talk to Sage.** The address must be in `SAGE_ALLOWED_CLIENT_IPS`. With `SAGE_VOICE_SECRET` set,
   every request must also be signed (HMAC-SHA256 over time, method, path and body, see `voice_util.sign`), so a captured
   request cannot be replayed (90 s window, each signature once) or edited. Without a secret, `SAGE_VOICE_TOKEN` is checked.
   The secret is compiled into the firmware, so do not share a preconfigured firmware image; rotate the secret if the
   board or image is lost. To revoke the speaker, change the secret and restart Sage. Programs on the Sage PC itself
   are not asked for the secret. The gateway uses plain HTTP: HMAC authenticates requests but does not encrypt audio.
   Keep port 8002 on a trusted private LAN and never forward it to the public internet.
2. **Lockout:** 5 refused requests within 10 minutes block that address for 10 minutes and show a warning on the wall display.
   Slow or stalled connections are dropped after 30 seconds.
3. **Voice has less power than the keyboard.** Anyone in earshot can trigger it, so by voice Sage can only read, search,
   calculate, set/list/snooze/cancel timers and reminders, show the plan, move or pause tasks, and add a workshop note
   (the list is `VOICE_ALLOWED` in `sage/trust.py`). Everything else is held as an approval:
   - tools that are normally allowed (for example saving a design version) can be approved with the **physical button** on the speaker
     (short press = allow, long press = cancel, within 5 minutes);
   - tools that already needed approval (running tests, plugins that run code) need the dashboard or the wall display;
   - voice never forgets memories, never saves memories on its own, never creates schedules or starts autonomous tasks,
     and never runs `/tool` commands.
4. **Audit trail:** every voice event is stored in the `voice_audit` table (time, address, what was heard, what Sage did,
   what was refused). See it at `GET /api/voice/audit` on the main Sage port, or ask "show me the voice log".
   The last 3000 rows are kept. Audio itself is never stored.
5. The wake word runs on the board; audio is sent only after it hears the wake word. This firmware does not implement a
   hardware microphone mute or a physical light ring; add a hardware mute if you need a physical microphone disconnect.

## Light ring
The server tells the wall display (and the board can mirror it) what the ring should show:
idle (dim blue), listening (blue), thinking (amber, circling), speaking (green), alert (amber flashing while a timer or
reminder is ringing), muted (red), error (red), sleeping (almost off). The wall display shows it next to the sleep button
when the speaker is online. The current ESP firmware has no RGB control and only posts `idle` to the gateway; it does not
provide physical light-ring feedback.

## Phone as a second speaker (iPhone)
Talk to the same Sage from your phone, with the same history, status commands, sleep mode and the same limits as the speaker.
Two ways, use either or both:

**A. The Talk page (voice in, voice out, local speech models).** Open `https://<your-pc>.<your-tailnet>.ts.net:8443/phone` in Safari.
Tap the microphone and speak; Sage answers aloud and, with "Keep listening" on, you can just keep talking. Add it to the Home Screen for a full-screen app.
When a request needs approval the page shows Approve / Cancel buttons (only for requests made from that phone).
Limits: the page only listens while it is open and the screen is on (iOS pauses web pages in the background), and Safari only allows the
microphone over HTTPS, which is why it goes through Tailscale.

**B. A Shortcut (works from the lock screen, with Siri, AirPods, CarPlay).** Uses the iPhone's own dictation and voice:
1. **Dictate Text** (stop listening: after pause).
2. **Get Contents of URL**: `https://<your-pc>.<your-tailnet>.ts.net:8443/api/voice/phone/text`, method POST, header
   `Authorization` = `Bearer <your phone token>`, JSON body `{"text": <Dictated Text>}`.
3. **Get Dictionary Value** `reply`, then **Speak Text**.
4. To keep talking, wrap steps 1-3 in **Repeat** and use **If** on the dictionary value `listen` (stop when it is false), or say "Hey Siri, talk to Sage" each time.
   The response also contains `asleep`, `button` and `outcome`.

### Kali/Linux setup

1. Install `python3-venv` and `espeak-ng` from Kali's repositories, then create an isolated Python environment and install the speech recognizer:
   ```
   sudo apt install -y python3-venv espeak-ng
   python3 -m venv "$HOME/.local/share/sage-voice"
   "$HOME/.local/share/sage-voice/bin/pip" install faster-whisper
   ```
   The first speech-recognition request downloads the Whisper model selected by `SAGE_STT_MODEL` (default `small`).
2. Generate a private phone token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Add it to Sage's user service without putting it in chat or a public file:
   ```
   systemctl --user edit sage
   ```
   Add this override, replacing the example token:
   ```ini
   [Service]
   ExecStart=
   ExecStart=%h/.local/share/sage-voice/bin/python -m sage
   Environment="SAGE_PHONE_TOKEN=replace-with-your-generated-token"
   ```
   The venv Python runs Sage from the service's existing project directory. Keep the phone token different from the speaker and Shortcut tokens.
3. Reload and restart Sage, then check `systemctl --user status sage`. For local diagnostics, run `curl http://127.0.0.1:8002/api/voice/status`; expect `stt: true` and `espeak` or `piper` in `tts_engines`:
   ```
   systemctl --user daemon-reload
   systemctl --user restart sage
   curl http://127.0.0.1:8002/api/voice/status
   ```
4. On Kali, serve only the local voice gateway over HTTPS to your tailnet:
   ```
   tailscale serve --bg --https=8443 http://127.0.0.1:8002
   ```
   Sign in to Tailscale on the phone, then open `https://<your-kali-device>.<your-tailnet>.ts.net:8443/phone`. Tap **Access**, enter the phone token, allow microphone access, and tap the microphone. Keep the page open in the foreground. Add it to the Home Screen for an app-like shortcut. Never use Tailscale Funnel or expose port 8002 to the public internet.

### Windows Sage host

Set `SAGE_PHONE_TOKEN` in `start-sage.bat` to a long random value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Install `faster-whisper` in Sage's Python environment and configure Piper or use an available Windows speech voice. Restart Sage. Share the voice port over Tailscale HTTPS with `tailscale serve --bg --https=8443 http://127.0.0.1:8002`, then open `https://<your-pc>.<your-tailnet>.ts.net:8443/phone` on the phone and enter the token. Never use Tailscale Funnel.

Security for the phone: every phone request needs the token, even when it arrives through Tailscale on this PC (so the tailnet alone
is not enough). 5 wrong tries lock that device for 10 minutes. Requests that come through the proxy never get the "this is the local PC"
trust, so the speaker's own endpoints cannot be reached from the phone path. Phone requests are in the audit trail as `phone:<name>`.
The phone does not receive timers, reminders or the brief by voice (those stay on the speaker); the iOS Shortcut that triggers your wake-up
brief is unchanged.

## 1. Install on the Sage PC
1. `pip install faster-whisper` (first start downloads the model; `SAGE_STT_MODEL=small`, try `base` if slow, `medium` for better Dutch).
2. Speech output, pick one: **Piper** (best; set `SAGE_PIPER_CMD`, `SAGE_PIPER_VOICE_NL`, `SAGE_PIPER_VOICE_EN`, each voice needs its
   `.onnx` and `.onnx.json`), **Windows voices** (no setup; Dutch only if a Dutch voice is installed), or **espeak-ng** (Linux/Kali).
3. Check: `python -m sage.voice --check`, then `python -m sage.voice --say "Goedemorgen" --lang nl` and play `data\voice-test.wav`.
4. Give the Sage PC and speaker stable addresses on the same trusted LAN. In the ignored `start-sage.local.bat`, keep the dashboard on localhost and expose only the voice gateway:
   ```
   set "SAGE_HOST=127.0.0.1"
   set "SAGE_VOICE_HOST=0.0.0.0"
   set "SAGE_ALLOWED_CLIENT_IPS=192.168.1.50"
   set "SAGE_VOICE_SECRET=replace-with-a-long-random-secret"
   ```
   Replace the IP with the speaker's reserved address. Generate a secret with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it private. Do not put these values in `start-sage.bat`.
   `SAGE_VOICE_HOST` is separate from `SAGE_HOST`, so this does not expose the dashboard.
5. Open port 8002 on the Windows firewall for the **Private** profile only (admin prompt):
   `netsh advfirewall firewall add rule name="Sage voice" dir=in action=allow protocol=TCP localport=8002 profile=private remoteip=192.168.1.50`
   Do not forward the port from your router or expose it to the public internet.
6. Restart Sage and check `http://127.0.0.1:8002/api/voice/status`. Confirm speech recognition and at least one TTS engine are available. Restart the wall display if needed.

## 2. The board hardware
- The speaker header needs an external 4–8 Ω speaker (about 3 W).
- Pins: I2C SDA 11 / SCL 10; I2S MCLK 12, SCLK 13, LRCK 14; speaker data out (ES8311) 16; microphone data in (ES7210) 15.
  The firmware enables the speaker amplifier using the TCA9555 expander. The BOOT button maps to short/long approval presses.
- “Hi ESP” is detected locally by WakeNet. Audio is sent to Sage only after the wake word. The initial firmware uses the first microphone channel and does not yet include echo cancellation or RGB ring control.

## 3. Protocol for the firmware (port 8002)
Audio both ways: 16 kHz, mono, signed 16-bit little-endian, no header. HTTP client header buffer at least 2048 bytes.
Every request carries `X-Sage-Time` (unix seconds) and `X-Sage-Sig` = hex HMAC-SHA256(secret, `"{time}.{METHOD}.{path+query}.{sha256hex(body)}"`).

| Call | Meaning |
|---|---|
| `POST /api/voice/ask` | Body = one utterance (stop on silence, max 28 s in this firmware, with `Content-Length`). `204` = nothing understood. `200` = chunked PCM reply. Headers: `X-Sage-Heard`, `X-Sage-Reply`, `X-Sage-Lang`, `X-Sage-Listen`, `X-Sage-Asleep`. |
| `GET /api/voice/poll?wait=25` | Run in its own task. `204` = nothing (still has `X-Sage-Asleep`), `200` = chunked PCM to play (brief, timer, reminder, alarm), plus `X-Sage-Kind`, `X-Sage-Id`. |
| `POST /api/voice/button` | `{"press":"short"}` approves / `{"press":"long"}` cancels the one request waiting for the button. `204` = nothing waiting; `200` = spoken result. Send it on every button press that is not used for something else (alarm stop, mute). |
| `POST /api/voice/ring` | `{"state":"listening"|"idle"|"muted"|"error"}` describes device status. This firmware currently posts `idle` only. |
| `X-Sage-Listen: 1` | After playing, open the microphone for about 6 s without a wake word (to answer "done"/"snooze", or a question). |
| `X-Sage-Asleep: 1` | Switch the ring and any attached screen off; keep listening for the wake word. |
| `POST /api/voice/say`, `GET /api/voice/status` | Make the speaker say something; engines and `device_online`. |

## Test without the board
From the Sage PC (no signature needed on this PC):
```
curl http://localhost:8002/api/voice/status
curl -X POST -d "{\"text\":\"Goedemorgen Kyan\",\"lang\":\"nl\"}" http://localhost:8002/api/voice/say
```
Tests: `python -m unittest tests.test_voice` (helpers, intents, signing, trust limits).

## Firmware validation status
- Firmware source and the install steps are included, but this release has **not been compiled or tested on a physical board** in this environment. First-time setup requires ESP-IDF to build the model image and verify the board-specific audio path.
- Echo cancellation and RGB ring control are not included yet.
- Speaking replies while the model is still writing (now: whole answer first, then sentence by sentence).
- Dashboard page for the audit trail (the data and API exist).
- The real speech models and physical speaker still need end-to-end testing.
