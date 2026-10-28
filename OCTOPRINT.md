# OctoPrint printer integration

Sage connects to OctoPrint's HTTP API. Klipper remains behind OctoPrint; Sage sends the selected library file to OctoPrint, which controls the printer.

## Configure on Kali

1. In OctoPrint, create or copy an API key from **Settings → Application Keys** (or the API key section shown by your OctoPrint version). Use a dedicated Sage account/key if your setup supports it.
2. On the Kali Sage computer, create a private environment file:

   ```sh
   install -d -m 700 ~/.config/sage
   nano ~/.config/sage/octoprint.env
   ```

   Add the printer's reachable OctoPrint base URL and API key:

   ```ini
   SAGE_OCTOPRINT_URL=http://octoprint-host/
   SAGE_OCTOPRINT_API_KEY=replace-with-your-key
   SAGE_OCTOPRINT_STORAGE=local
   ```

   Replace the example URL with the actual OctoPrint address reachable from Kali. `SAGE_OCTOPRINT_STORAGE` can be `local` or `sdcard`. Keep this file private; do not put the key in chat, source files, or Git.
3. Restrict the file and add it to Sage's user service. Run `chmod 600 ~/.config/sage/octoprint.env`, then `systemctl --user edit sage` and add:

   ```ini
   [Service]
   EnvironmentFile=%h/.config/sage/octoprint.env
   ```

   Save, then run `systemctl --user daemon-reload` and `systemctl --user restart sage`.

The API key is sent in OctoPrint's `X-Api-Key` header. Prefer a trusted local network; use HTTPS when the connection crosses an untrusted network. Do not expose OctoPrint's API directly to the public internet.

## Use it

- “Check the printer status.”
- “List the OctoPrint printer library.”
- “Print `folder/part-name.gcode`.” Use the exact relative path shown in the library (or give the exact listed filename if it is at the library root).

Sage only starts an existing `.gcode`, `.gco`, or `.gc` file from the selected OctoPrint storage. It checks the exact file through OctoPrint and checks that the printer is operational and neither printing nor paused. OctoPrint can still reject a job; Sage reports the API result. To avoid accidentally selecting an identically named file from another storage, specify `local` or `sdcard` when needed.

This integration does not inspect the build plate, verify that the correct filament is loaded, slice STL/3MF files, upload files, or supervise the physical print. Check the printer and the first layer before leaving it running. Printing starts immediately when OctoPrint accepts the direct command.

## Troubleshooting

- If Sage says it is not configured, check the environment file path, service override, then restart the service.
- If it cannot reach OctoPrint, verify the URL from the Kali computer and that OctoPrint is listening on the LAN interface.
- HTTP 401/403 usually means the API key is missing, incorrect, or lacks file-selection permission.
- If the printer is not operational, resolve the connection or printer error in OctoPrint first. Sage will not start a new job while the printer is busy or paused.

API reference: [OctoPrint Files API](https://docs.octoprint.org/en/main/api/files.html) and [Printer API](https://docs.octoprint.org/en/main/api/printer.html).
