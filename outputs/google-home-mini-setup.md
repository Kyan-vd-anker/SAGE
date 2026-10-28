# Connect a Google Home Mini to Sage through Home Assistant

This guide sets up three fixed voice requests: a daily briefing, the best next action, and a project status update. The Google Home Mini hears the wake phrase **“Hey Google”**. Home Assistant sends the selected request to Sage, then speaks Sage’s answer through the Mini.

## What this can and cannot do

Google Home does not give Sage direct access to the Mini’s microphone or arbitrary transcripts. Google’s old custom conversational Actions were discontinued. This bridge therefore uses named Home Assistant switches exposed to Google Assistant, such as “Sage briefing.” For unrestricted conversation and Sage’s own wake word, use a separate microphone connected to a PC or Raspberry Pi running Sage’s voice client.

## 1. Find the two server addresses

In Home Assistant, find its fixed LAN IP address under **Settings → System → Network** or in your router’s device list. Reserve that address in the router so it does not change.

Also reserve a LAN IP for the computer running Sage. Do not use `127.0.0.1` in Home Assistant: that would mean Home Assistant itself, not the Sage computer.

## 2. Set up Sage’s restricted Home Assistant bridge

The bridge accepts only three named commands, and it requires a secret token. Sage also restricts network access to the exact client IP addresses you list.

On the Sage computer, stop the existing Sage process first with **Ctrl+C** in the terminal that started it. In a PowerShell window opened in the Sage project folder, set these environment variables, replacing the example addresses and token with your own values:

```powershell
$env:SAGE_HOST = "0.0.0.0"
$env:SAGE_PORT = "8001"
$env:SAGE_HA_TOKEN = "PASTE_A_LONG_RANDOM_SECRET_HERE"
$env:SAGE_ALLOWED_CLIENT_IPS = "HOME_ASSISTANT_IP,TOUCHSCREEN_IP,TRUSTED_PC_IP"
python -m sage
```

Use the Home Assistant IP and any trusted computer or touchscreen IPs that need to open Sage. Loopback access on the Sage computer remains allowed. Keep the Sage and Home Assistant computers on your private network, do not forward Sage’s port through your router, and do not add untrusted devices to the allowlist. Sage’s web API is not designed to be exposed to the public internet.

Generate a strong token locally, for example with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the same token into Home Assistant’s `secrets.yaml` as described below. The commands above set variables for the current PowerShell window. To make them persistent, set them in the Sage computer’s service/startup configuration and restart Sage from that environment.

## 3. Confirm Home Assistant sees the Mini

In Home Assistant, open **Settings → Devices & services**. Add **Google Cast** if the Mini was not discovered automatically. Open the Mini’s device page and note its `media_player...` entity ID. Cast discovery requires mDNS to work on the local network. Home Assistant’s [Google Cast integration guide](https://www.home-assistant.io/integrations/cast/) has troubleshooting steps.

Make sure Home Assistant has a text-to-speech provider configured. A local provider such as Piper keeps the spoken reply on your network. In the examples below, replace `tts.piper` with the TTS entity shown in your Home Assistant instance.

## 4. Connect Home Assistant to Google Home

The shortest setup is Home Assistant Cloud:

1. In Home Assistant, open **Settings → Home Assistant Cloud** and connect Google Assistant.
2. In the Google Home app, sync devices if prompted.

Home Assistant Cloud has a 30-day trial and then requires a paid subscription. The manual Google Assistant setup is available, but it requires an externally reachable Home Assistant URL with a valid SSL certificate. See the [official Google Assistant integration setup](https://www.home-assistant.io/integrations/google_assistant/).

## 5. Create Sage command switches

In Home Assistant, create three toggle helpers under **Settings → Devices & services → Helpers → Create helper → Toggle**:

- `Sage briefing`
- `Sage next action`
- `Sage project status`

Expose only these helpers to Google Assistant. The `input_boolean` domain is supported by Home Assistant’s Google Assistant integration. Give them clear names and assign them to a room if needed.

## 6. Add the Sage token and REST commands

Add this line to Home Assistant’s `secrets.yaml`, replacing the placeholder with the exact Sage token:

```yaml
sage_authorization: "Bearer PASTE_THE_SAME_LONG_RANDOM_SECRET_HERE"
```

Add the following to `configuration.yaml`. Replace `SAGE_PC_LAN_IP` with the Sage computer’s reserved LAN IP. If `rest_command:` already exists, add these entries under it rather than creating a second `rest_command:` key.

```yaml
rest_command:
  sage_briefing:
    url: "http://SAGE_PC_LAN_IP:8001/api/ha/command"
    method: POST
    headers:
      Authorization: !secret sage_authorization
      Content-Type: application/json
    payload: '{"command":"briefing"}'
    timeout: 90
  sage_next_action:
    url: "http://SAGE_PC_LAN_IP:8001/api/ha/command"
    method: POST
    headers:
      Authorization: !secret sage_authorization
      Content-Type: application/json
    payload: '{"command":"next_action"}'
    timeout: 90
  sage_project_status:
    url: "http://SAGE_PC_LAN_IP:8001/api/ha/command"
    method: POST
    headers:
      Authorization: !secret sage_authorization
      Content-Type: application/json
    payload: '{"command":"project_status"}'
    timeout: 90
```

Check the Home Assistant configuration, then restart Home Assistant or reload REST commands.

## 7. Add an automation for each helper

Create an automation triggered when `input_boolean.sage_briefing` changes to **on**. Add these actions in order:

```yaml
alias: Sage briefing from Google Home
triggers:
  - trigger: state
    entity_id: input_boolean.sage_briefing
    to: "on"
actions:
  - action: rest_command.sage_briefing
    response_variable: sage_response
  - if:
      - condition: template
        value_template: "{{ sage_response['status'] == 200 }}"
    then:
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.google_home_mini
          message: "{{ sage_response['content']['answer'] }}"
    else:
      - action: persistent_notification.create
        data:
          title: Sage voice bridge
          message: "Sage did not respond. Check its status and the Home Assistant action log."
  - action: input_boolean.turn_off
    target:
      entity_id: input_boolean.sage_briefing
mode: single
```

Replace `tts.piper` and `media_player.google_home_mini` with the entity IDs in your Home Assistant. Duplicate this automation twice, changing the trigger helper and REST action to `sage_next_action` or `sage_project_status`. Home Assistant’s [REST command documentation](https://www.home-assistant.io/integrations/rest_command/) explains response variables; [TTS speak](https://www.home-assistant.io/actions/tts.speak/) documents speech playback.

## 8. Try it

First run the automation from Home Assistant and confirm that Sage replies through the Mini. Then try:

- “Hey Google, turn on Sage briefing.”
- “Hey Google, turn on Sage next action.”
- “Hey Google, turn on Sage project status.”

The Mini may confirm that it turned on the helper; Home Assistant then asks Sage and plays Sage’s answer. The Mini’s Google wake phrase remains in charge of starting the interaction.

## Troubleshooting

- **Sage says the bridge is disabled:** Sage was started without `SAGE_HA_TOKEN`; set it and restart Sage.
- **401 from Sage:** the Home Assistant `Authorization` secret must be `Bearer ` followed by the same token Sage uses.
- **403 from Sage:** add Home Assistant’s actual source IP to `SAGE_ALLOWED_CLIENT_IPS`, then restart Sage.
- **Connection refused or timeout:** confirm Sage is running on port 8000, both machines are on the same LAN, the URL contains the Sage IP, and the host firewall allows Home Assistant to reach Sage.
- **Sage answers but the Mini is silent:** test the Mini from Home Assistant’s Cast device page; verify the TTS entity and media-player entity IDs, volume, and Home Assistant internal URL. Cast audio URLs must be reachable by the speaker; see the [Cast documentation](https://www.home-assistant.io/integrations/cast/).
- **Google cannot find a helper:** confirm it is exposed to Google Assistant, sync devices, then check the helper’s spoken name in the Google Home app.
