# Sage

Sage is a local-first personal assistant built around the Ollama installation on this machine. It uses `deepseek-r1:7b` for ordinary chat and `qwen2.5-coder:7b` for coding requests by default, stores state in SQLite, and serves a small browser dashboard without requiring a JavaScript build step or Python packages.

## Run it

1. Make sure Ollama is running (`http://127.0.0.1:11434`) and `ollama list` includes `deepseek-r1:7b` and `qwen2.5-coder:7b`. Install either with `ollama pull deepseek-r1:7b` and `ollama pull qwen2.5-coder:7b`.
2. From this folder, run `python -m sage`.
3. Open <http://127.0.0.1:8000>.

For the Sage dashboard currently configured on port 8001, double-click [`start-sage.bat`](start-sage.bat) and keep its window open while using Sage, then open <http://127.0.0.1:8001>. If Sage is already running, use that existing server rather than starting a second copy.

The first run creates `data/sage.db`, `data/vault/`, and `data/workspace/`. Set `SAGE_HOST`, `SAGE_PORT`, `OLLAMA_URL`, `SAGE_MODEL`, `SAGE_CODING_MODEL`, `SAGE_DB`, `SAGE_VAULT`, `SAGE_WORKSPACE`, `SAGE_WAKE_WORD`, and `SAGE_WEB_SEARCH_ENABLED` to override defaults. `SAGE_MODEL` selects the ordinary-chat model; `SAGE_CODING_MODEL` selects the coding model. The UI binds to localhost by default. Do not expose it to a network without adding authentication.

## Trigger a briefing from an iPhone Shortcut

Sage can record that you are awake and immediately create the morning brief when an iPhone Shortcut calls `POST /api/shortcuts/awake`. The response contains the brief in its `answer` field; the Shortcut can speak it aloud. This endpoint is disabled until `SAGE_SHORTCUT_TOKEN` is configured and requires that bearer token. Keep Sage on localhost and access it through Tailscale Serve; do not expose the unauthenticated dashboard or API to the public internet.

On the Sage computer, generate a private token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. For the Kali systemd user service, run `systemctl --user edit sage` and add:

```
[Service]
Environment="SAGE_SHORTCUT_TOKEN=your-generated-token"
```

Save the override, then run `systemctl --user restart sage`. Keep the token private. In the iPhone Shortcuts app, create a shortcut with these actions:

1. **Get Contents of URL**: use the HTTPS Tailscale Serve address for your Sage computer, followed by `/api/shortcuts/awake`.
2. Set the method to **POST**, add the header `Authorization` with value `Bearer your-generated-token`, and set the request body to **JSON** containing `{}`.
3. Add **Get Dictionary Value**, select the `answer` value from the response, then add **Speak Text**.

Sign in to Tailscale on the iPhone and keep Sage's Tailscale Serve route active. Each successful call records a `user.awake` event, adds the generated morning brief to Sage chat, and returns that same brief to Shortcuts. The morning brief uses Sage's calendar, reminders, tasks, and work plan data.

## Install on Kali Linux and start at boot

Sage's core runs on Kali with Python 3 and systemd without extra Python packages. Phone speech recognition is optional and uses the separate `faster-whisper` environment described below. Ollama provides the local language models.

1. Install Python and Git, then install Ollama using its official Linux instructions:
   ```
   sudo apt update
   sudo apt install -y python3 git curl xdotool scrot
   curl -fsSL https://ollama.com/install.sh | sh
   ```
   Review the Ollama installation instructions at <https://ollama.com/download/linux> if you prefer not to use its install script.
2. Download Sage or copy this project folder to the Kali PC. In a terminal, change into the project directory.
3. Start Ollama and download Sage's default chat models:
   ```
   sudo systemctl enable --now ollama
   ollama pull deepseek-r1:7b
   ollama pull qwen2.5-coder:7b
   ollama pull llava:7b
   ```
   For semantic memory search, also run `ollama pull nomic-embed-text`.
4. Install and start Sage's per-user systemd service:
   ```
   bash scripts/install-kali.sh
   ```
   The installer enables systemd lingering for your user so Sage starts after boot even before you log in. It starts on `127.0.0.1:8000`; open <http://127.0.0.1:8000> on the Kali PC.

### Updating without losing data

For the one-time move from a copied folder or ZIP into a Git checkout, and the safe update workflow after that, follow [UPDATE.md](UPDATE.md). On Kali, later releases are installed with `bash scripts/update-kali.sh`. It stops the Sage service, archives the local `data/` folder, updates source using a fast-forward-only Git pull, checks Python syntax, and brings the service back up. The SQLite database, memories, projects, notes, and workspace files are not replaced by a code update. Data and credentials are excluded from releases.

Windows release publishing is documented in [UPDATE.md](UPDATE.md). Run `scripts/publish-release.ps1` after changes to update the version and changelog, create a tagged release commit, and push it to the SAGE GitHub repository. It only supports patch and minor bumps; the major version stays unchanged.

## Telegram messages and notifications

Sage can send important reminders and work updates to one privately paired Telegram chat, and you can send text messages back to Sage. Sage polls Telegram over outbound HTTPS, so it does not need a public inbound port. Telegram receives the messages you send and the event titles/details Sage notifies you about; calendar events, voice events, and routine informational events are excluded. Messages from unpaired accounts are ignored. If Sage requests an action that requires approval, approve it in Sage's dashboard.

The bot token must be a replacement token from BotFather: revoke any token previously pasted into chat. From the Sage project folder on Kali, run `bash scripts/configure-telegram.sh` and paste the replacement token at the hidden prompt. The helper stores it in `~/.config/sage/telegram.env` with private permissions and restarts the Sage user service. It is not saved in the repository or Sage database.

Then open Sage's **System** page, generate a pairing code, and send `/pair CODE` to your bot in a private Telegram chat. The code expires after ten minutes and works once; five wrong attempts invalidate it. The paired chat can send ordinary text, voice notes, photos, and documents (TXT, Markdown, CSV, JSON, DOCX, and PDF when `pdftotext` is available). Voice notes need Sage's optional `faster-whisper` dependency; spoken replies additionally need `ffmpeg` and a configured local TTS engine. Photo analysis uses Sage's local vision model. Attachments are size-limited to 8 MB, processed locally after Telegram delivery, and not saved as Sage memories.

Quick commands include `/briefing`, `/tasks`, `/next`, `/approvals`, and `/stop`. Approval cards have Approve and Decline buttons. Package installs, Linux commands, and destructive file changes require a second short-lived phrase: approve the card, then send the exact `/confirm ID PHRASE` shown by Sage. Other approvals run on the first explicit approval. Notification controls are `/notifications on|off`, `/notify tasks|reminders|jobs|approvals on|off`, `/quiet 22:00 07:00`, and `/quiet off`; quiet hours use the Sage server's local time. Use `/memory` to inspect active memories, `/memory off` to disable automatic capture from Telegram, `/memory on` to re-enable it, `/remember TEXT` to save one deliberately, and `/forget ID` to archive one. Automatic capture now saves at most one memory per message and ignores transient wants and project plans; saved memory context is limited to strongly relevant matches. The paired chat can also use `/status` and `/unlink`. Only one Telegram chat can be paired at a time. Allow outbound HTTPS access from Kali to `api.telegram.org`.

## Talk to Sage from your phone

Sage includes a phone Talk page: speak into the phone microphone and Sage replies with streamed speech from the Kali PC. Speech recognition uses `faster-whisper`; speech output uses Piper when configured, otherwise `espeak-ng`. The models run on the Sage computer. The phone page needs HTTPS for microphone access, so use Tailscale Serve and keep Sage bound to localhost.

1. Install the optional voice packages (these are software installs):
   ```
   sudo apt install -y python3-venv espeak-ng
   python3 -m venv "$HOME/.local/share/sage-voice"
   "$HOME/.local/share/sage-voice/bin/pip" install faster-whisper
   ```
   `faster-whisper` is the speech-to-text engine; its first run downloads the selected model. See the [upstream install guide](https://github.com/SYSTRAN/faster-whisper#installation).
2. Create a private phone token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Run `systemctl --user edit sage` and add this override, replacing the example token with the generated one:
   ```
   [Service]
   ExecStart=
   ExecStart=%h/.local/share/sage-voice/bin/python -m sage
   Environment="SAGE_PHONE_TOKEN=replace-with-your-generated-token"
   ```
   This makes the service use the voice-enabled Python environment. Keep the token private; the phone page stores it in that phone's browser.
3. Restart Sage and confirm voice is ready:
   ```
   systemctl --user daemon-reload
   systemctl --user restart sage
   curl http://127.0.0.1:8002/api/voice/status
   ```
   Check that `stt` is `true` and `tts_engines` includes `espeak` or `piper`.
4. On Kali, expose the voice service to your tailnet over HTTPS:
   ```
   tailscale serve --bg --https=8443 http://127.0.0.1:8002
   ```
   Tailscale Serve keeps this reachable only inside your tailnet and provides HTTPS. Do not use Funnel or open port 8002 on your router. See [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve).
5. Sign in to Tailscale on your phone and open `https://<your-kali-device>.<your-tailnet>.ts.net:8443/phone`. Tap **Access** and enter the token, allow microphone access, then tap the microphone. Keep the page open and the screen awake while talking; choose **Keep listening** for back-and-forth conversation. Add the page to your Home Screen if you want an app-like shortcut.

The Talk page uses the same Sage conversation system. Voice requests have a narrower tool policy than chat; requests that need approval are shown with Approve/Cancel controls. The page works while open in the foreground; iOS may pause microphone access when it is backgrounded or the screen is locked.

Check the service with `systemctl --user status sage` and follow logs with `journalctl --user -u sage -f`. Use `systemctl --user restart sage` after changing the installation or configuration, and `systemctl --user disable --now sage` to remove it from startup. Ollama also needs to be enabled at boot as shown above. Keep Sage bound to localhost; for remote access, use the Tailscale Serve setup below rather than exposing the unauthenticated dashboard directly.

## Architecture

```text
Browser UI → local HTTP API → chat coordinator → Ollama
                            ├→ plugin registry + permission gate + action log
                            ├→ SQLite (chat, memory, projects, jobs, logs)
                            ├→ background job worker
                            └→ Obsidian-compatible Markdown vault
```

The backend is split into small modules: `config`, `db`, `llm`, `plugins`, `agent`, `jobs`, `server`, and `static`. Python's built-in HTTP server keeps the core runnable without installing dependencies; the optional voice service uses local speech-recognition and speech-output engines. The Ollama adapter uses its local HTTP API.

## What works

- Chat saves both sides of each exchange, but sends only up to three relevant prior messages or saved memories to Ollama; unrelated history and memories are omitted. Retrieved conversation, memory, and project records are explicitly labeled as optional context, not requests or instructions. Only the latest user message is sent as the request to answer; Sage should use context only when it directly helps with that message. The chat's Context controls panel shows what Sage used for its latest reply and lets you pin up to three messages or memories for future context.
- Chat routes programming requests and tool selection/calling to `qwen2.5-coder:7b`; ordinary conversation, equations, and problem-solving discussion use `deepseek-r1:7b`. Explicit math and discussion intent takes precedence over tool routing. Set `SAGE_MODEL` and `SAGE_CODING_MODEL` to use different Ollama models.
- Direct requests to “search online”, “look up”, or find current information route to the web search tool and return its cited sources; Sage does not rely on the model to pretend it searched.
- After each chat turn, an idle reflection job asks the local model whether the conversation contains a durable memory, a clearly requested project, or an explicit learning request. It skips secrets and fleeting details, writes accepted memories to SQLite and linked Markdown notes, and creates project tasks without requiring manual entry.
- Explicit chat tool calls use `/tool PLUGIN_NAME {"key":"value"}`. Sage never executes a tool based only on the model's untrusted text.
- During normal conversation, Sage offers enabled plugin definitions and built-in project/workshop tools to the model. Ollama's native tool calling is used when supported; models that reject Ollama's `tools` parameter use a compact JSON-constrained fallback protocol with the same selected model. This keeps the configured model and routes every proposed call through Sage's dispatcher. Sage validates generated tool arguments against each tool's schema before execution; malformed calls and placeholder values such as `<project_id>` are never executed or shown as raw chat output. Sage retries once without tools to answer the original request or ask for missing information. The model can search memories/notebooks, plan projects/tasks, record workshop notes, calculate engineering values, manage inventory, save lessons/knowledge notes, queue research/reviews/code projects, create BOMs and handoffs, browse the approved workspace, search the web, and more. Safe actions run and their results are sent back to the model for a truthful answer. Confirmation-required tools (such as creating calendar events, changing Sage source files, saving design snapshots, or running generated tests) pause in chat until the user approves or declines them. Sage can also generate and immediately integrate a plugin from a chat request; each generated plugin is marked confirmation-required, so running its code still requires explicit approval. Plugin code runs with Sage's local account permissions and is not sandboxed. Blocked plugins are not exposed to the model. The Tools & plugins page shows the live callable catalog. Tool calls are limited to one per model round and at most ten rounds per chat turn.
- Memory can be explicitly saved, searched, corrected, and removed. Chat is not automatically promoted to long-term memory. The Context controls panel can search memories semantically using Ollama's local embedding API; install the default embedding model with `ollama pull nomic-embed-text` (or set `SAGE_EMBED_MODEL`). Embeddings are cached in SQLite, and ordinary chats do not incur embedding-search latency.
- Projects and tasks support status, priority, due date, dependencies, and next action fields.
- Planner includes an explicitly confirmed action to mark all blocked tasks done after reviewing and approving their completed work.
- Background jobs persist across restarts and can be paused, resumed, or cancelled. During idle time, Sage automatically takes the next dependency-ready task from an active project, creates a Markdown deliverable in `data/workspace/projects/`, and records its result on the task. Project planning, task, and code-generation model calls can run for up to 10 minutes; active chat still interrupts background work immediately. Digital tasks with a usable deliverable are marked done; tasks needing physical work, external access, or user input are marked blocked with the reason. Project results appear in the Projects page and can be downloaded.
- Active chat takes priority over background model work. A new user request interrupts an in-flight background Ollama call, requeues that job at its checkpoint, and resumes background work after the idle window.
- Plugins are discoverable from `plugins/`, have JSON schemas and permission metadata, and run through a permission gate. The included calculator is safe to execute. Install commands and Sage-source changes require approval; routine system commands and bounded desktop actions are enabled.
- The local computer tools can list/read/open/write/copy/move/delete bounded files inside the Sage user's home folder, launch only named apps in `SAGE_ALLOWED_APPS` (a JSON object mapping labels to argument arrays), and open a user-requested HTTP(S) URL. Sage's source tree has a separate approval boundary.
- On Kali, Sage can inspect its host desktop with a transient screenshot (`scrot` on X11 or `grim` on Wayland) and local Ollama vision, run routine Linux shell commands, and perform bounded X11 mouse/keyboard actions without a Sage approval prompt. The image preview is kept only in the current page and is not stored by Sage. Before mouse/keyboard input Sage inspects the screen; after each desktop, URL, app-launch, or file-open action it inspects again. A turn is limited to four such actions and Chat's Stop button stops the next step. Install commands and changes to Sage's own source files require approval. Ordinary shell commands run through the user systemd manager with Sage's source directory mounted read-only, and fail closed if that protection cannot be set up. Package installation uses the desktop Polkit prompt; Sage does not collect administrator passwords. Screen text and command output are treated as untrusted input. Command text is removed from approval records after the decision; command output is not kept in the action log. Do not include passwords or tokens in commands. Desktop input currently supports X11 with `xdotool`, not Wayland. Sage refuses input when it recognizes an installer or privilege dialog, but installer detection is best-effort; command-text checks cannot prevent every indirect installer from being launched.

Use **PC controls** to inspect Sage's live Kali screen and check system readiness. In Chat, ask Sage for a computer task; it should inspect the screen before acting and again to verify changes. Press Stop to end the task. Install requests appear in the chat approval card. The control depends on a working graphical session and local Ollama vision model. The PC controls page shows whether Sage can capture the screen, send input, use the safe shell boundary, and reach APT/Polkit and Ollama.
- A visible action log and dry-run preview are available in the dashboard.
- Obsidian-compatible notes are stored under `data/vault/`; accepted memories and generated projects/tasks are linked with wiki links, and the dashboard graph displays those relationships.
- The `web_search` plugin uses Bing's public RSS search endpoint and summarizes fetched source excerpts with citations and a source list. An explicit “learn about…” request queues this work for idle time and stores the findings in memory and the vault. The search query is sent to Bing; this feed endpoint is for personal, non-commercial use. Turn online search off with `SAGE_WEB_SEARCH_ENABLED=false`.
- The chat page has an opt-in wake-word listener (default “Sage”). Click “Wake word off” to enable it and grant microphone access. Browser speech recognition support is required and may use the browser's speech service; read-aloud uses browser speech synthesis.
- The phone's **Talk to Sage** page supports microphone input and spoken replies through the Kali voice gateway. It uses local `faster-whisper` and Piper or `espeak-ng`; setup is in the [phone voice guide](VOICE-GUIDE.md).
- The chat has two screen checks: **Kali screen** captures Sage's active Linux desktop on the host; **Share screen** uses the browser's explicit screen/window picker. Each sends one frame to local Ollama vision and discards the source image after the answer. `llava:7b` is the default vision model (set `SAGE_VISION_MODEL` to change it); screen analysis refuses non-local `OLLAMA_URL` endpoints. Browser sharing requires localhost or HTTPS. Host-side capture needs `scrot` on X11 or `grim` on Wayland.
- Voice controls now show microphone/API errors in the page, support stopping dictation cleanly, and restart the wake listener after browser timeouts. Browser speech recognition still depends on support in the browser and its speech service.
- A token-protected Home Assistant bridge accepts only the named `briefing`, `next_action`, and `project_status` requests, then returns Sage's answer for Home Assistant TTS playback. Configure `SAGE_HA_TOKEN` and `SAGE_ALLOWED_CLIENT_IPS`; the allowlist applies to all remote Sage requests. See the [Google Home Mini setup guide](outputs/google-home-mini-setup.md) for Google Assistant, Cast, and automation setup.
- Engineering code projects stay in `data/workspace/engineering/code/`. Generated code is not run automatically; the separate Kali command tool runs with the Sage user's operating-system permissions and protects Sage's own source tree from ordinary commands.
- When Sage replies with fenced code, chat renders it in a code card with language, copy, and download controls. Each block is automatically saved with a unique filename under `data/workspace/generated/`; Sage adds the saved path to the chat history. Generated code is not executed automatically.
- The dashboard includes a daily briefing and project check-ins for overdue work, completed task lists, or missing next actions.
- Optional browser notifications can announce completed jobs and a daily 9:00 local-time briefing while the dashboard is open. Grant permission with “Enable notifications”; browser notifications cannot run while the browser is closed.
- Outlook calendar can be connected from the Daily briefing page with a read-only published ICS link (no app registration) or optional Microsoft Graph. Graph event creation is confirmation-gated and checks for conflicts first. See the [Outlook calendar setup guide](outputs/outlook-calendar-setup.md).
- Focus mode tracks a goal, optional project, and a 1–8 hour session.
- The Research desk queues sourced reports and writes Markdown reports into the vault. The Learner tab queues explicit topics and can detect clear factual corrections or Sage uncertainty, then queue one deduplicated, evidence-backed research task during idle time. Learned notes preserve citations. Conversation-inferred memory suggestions wait for review, and active memory context is selected by semantic relevance with a keyword fallback.
- Research watchlists schedule searches at the chosen interval when Sage is idle. Search queries are sent to Bing as described above.
- Import selected TXT, Markdown, CSV, and JSON files into `data/imports/` and create matching notes in the Obsidian vault. Files stay local.
- The plugin area can surface capability requests Sage recognizes in conversations and create a safe starter plugin draft. Generated plugin code remains a placeholder with confirmation permission; review and implement it before relying on it.
- The knowledge graph is an SVG network: notes are circles, links are visible edges, and more links increase a circle's size.
- Workshop log entries accept notes, dictated text, measurements, and JPG/PNG/WEBP photos. Entries persist in SQLite and as Obsidian Markdown notes.
- Engineering calculators include unit conversion, Ohm's law, electrical power, force/moment, pressure force, and density. Each result records its formula, assumptions, and a Markdown artifact; these tools do not replace a reviewed design calculation.
- Sage can queue and generate Markdown test plans and design-review checklists using Ollama. Attach imported project files for metadata context, record real test outcomes, and download the saved report.
- CAD/BOM imports accept STEP/STP, STL, DXF, KiCad PCB/schematic, CSV/BOM, JSON, and workshop photos. The MVP extracts basic structural metadata, part-table headers, file hashes, and simple STL bounds; it does not validate CAD geometry or electrical/manufacturing rules.
- The Engineering Code Lab can queue starter projects, including Python code/tests, CSV utilities, and OpenSCAD source. Files are written under `data/workspace/engineering/code/` and can be inspected and edited before running tests. Test execution is explicitly approved, limited to Python `unittest` discovery, uses no shell, and has a timeout. It runs with the Sage account's operating-system permissions, so it is not a true OS sandbox; review project files and disconnect workshop hardware before running.
- The BOM Builder creates project-linked CSV part lists and saves them in the engineering library. Quantities, units, references, supplier, and notes are editable before export.
- Parts inventory tracks part numbers, quantities, units, storage locations, suppliers, project links, reorder thresholds, and a stock adjustment history. Low-stock items are highlighted in the inventory view.
- Workshop notebook search searches workshop entries and measurements, engineering reports, failure lessons, calculations, projects, and saved memories together.
- Troubleshooting guides save a symptom and ordered diagnostic steps; guided sessions keep the current step and observations so checks are repeatable.
- Design version snapshots copy an imported CAD/electronics/BOM file into immutable local storage and record its SHA-256, version number, and change notes. Import a changed file and snapshot it again to keep revisions. This versions files; it does not calculate a CAD geometry diff.
- Chat has an active-project selector; when selected, Sage receives that project's tasks, workshop notes, test results, versions, parts, and decisions as context.
- Design snapshots and test runs have comparison views. Text design files show a unified diff; binary CAD files show whether hashes changed. Test comparisons show differences between the two latest recorded runs.
- Measurement templates define repeatable named fields with units, targets, and tolerances. Each run is saved and can be attached to a project.
- Project pages can compare imported BOM quantities against project-linked and general inventory, and list shortages.
- The decision log records an engineering choice, alternatives, rationale, source, and project link as SQLite data and an Obsidian note.
- Bench mode keeps one project selected, shows its tasks and BOM shortage count, supports continuous browser speech dictation, and saves notes to the workshop log.
- Session review queues an evidence-based summary as background work. Drafts stay in a review state until explicitly approved for the Obsidian vault or discarded.
- Backups create a ZIP containing a SQLite-consistent database snapshot plus the vault, workspace, engineering files, and imports. Restore requires an explicit UI confirmation, validates archive paths and the database, and retains replaced folders under `data/.restore-previous/`. Restart Sage after restoring so all in-memory state reloads.
- Sage's AI-callable tools also include ranked local knowledge retrieval across memories, workshop records, failures, decisions, engineering reports, and Obsidian notes; project briefs and chronological timelines; dependency/readiness graph checks; bounded engineering-file previews; BOM audits and revision comparisons; measurement target/tolerance analysis and trends; and memory provenance. These tools report recorded evidence and limits without inferring causes or certifying designs.
- Project handoff sheets summarize the goal, open and completed tasks, recent workshop notes, relevant inventory, design snapshots, and recorded failures/lessons. They are saved as Markdown in the workspace and Obsidian vault.
- Failure and lesson tracking records symptoms, suspected/root causes, fixes, and lessons in SQLite and as Obsidian notes, making them available to notebook search.
- Tailscale remote access works through Tailscale Serve: keep Sage bound to `127.0.0.1`, install/sign into Tailscale on the Sage computer, and run `tailscale serve localhost:8000` (substitute the configured Sage port). Open the HTTPS tailnet URL shown by Tailscale on an allowed device. The dashboard includes Tailscale CLI/Serve status and a copyable command. Use tailnet access policies to restrict who can reach it; Sage has no separate login screen. Never use Tailscale Funnel for this dashboard. To remove the Serve route, run `tailscale serve reset`.

## Plugins

Ask Sage in chat to create a plugin and describe its purpose and inputs. Sage generates the Python module and JSON manifest under `plugins/`, and the new plugin becomes available to the tool catalog immediately. Generated plugins use the `confirm` permission and are not executed unless the user approves the action in the UI. Plugin code runs with the local account's permissions and is not sandboxed; review generated code before approving its execution. For manual plugins, create a JSON file under `plugins/` with `name`, `description`, `version`, `input_schema`, `permission` (`safe`, `confirm`, or `blocked`) and an `entrypoint` of `module:function`. Put the Python module alongside it. Implement `run(arguments, context)` and return a JSON-serializable result.

Example: `plugins/calculator.json` and `plugins/calculator.py`.

## Boundaries and next steps

An always-listening local wake-word engine is not included. The dashboard wake-word listener relies on browser speech recognition and may use the browser's speech service; the phone Talk page instead sends audio to local `faster-whisper` and receives streamed speech from Piper or `espeak-ng`. The Google Home Mini microphone is not available as a general-purpose Sage microphone; its Google wake word can trigger the fixed Home Assistant actions documented in the setup guide. Bing search requires network access; the query leaves the machine and source pages may be unavailable or change. Sage records the links it actually received and reports failures instead of inventing citations. Tailscale Serve provides HTTPS on the tailnet while Sage remains on localhost, but the dashboard does not add its own account/password layer; anyone allowed by the tailnet policy can use it with Sage's local permissions. Daily notifications run in the open dashboard; a closed browser cannot deliver them.

## Backup

Stop Sage and copy `data/sage.db` and `data/vault/`. Markdown notes are portable and can be opened directly as an Obsidian vault. Database export/restore is a normal SQLite backup. Avoid copying the database while a backup-sensitive migration is in progress.

## Examples

- “Help me plan a website redesign.” Sage can draft a plan in chat; add and track concrete tasks in Projects.
- Save a durable preference with the Memory form so it can be recalled later.
- Queue a research or planning assignment from Background work, then pause/cancel it or inspect its checkpoint.
- Add a trusted plugin with its own schema and declared permission level.
