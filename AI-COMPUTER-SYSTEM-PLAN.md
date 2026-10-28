# Sage AI Computer System: v2 plan

## Goal

Let Sage help operate the user's own PC through a visible, reviewable loop: understand a request, inspect the current screen, propose an action, carry it out within limits, and report what changed. Routine actions run without a Sage approval prompt; installs and changes to Sage's own files require approval. Sage runs as its normal user and never collects a root password.

## What v2 already provides

- Local-first chat through Ollama, with tool selection routed through Sage's dispatcher.
- A permission gate and approval queue for confirmation-required tools.
- Bounded file list/read/write/copy/move/delete actions scoped to the Sage user's home folder; changes inside Sage's source folder require approval.
- App launching restricted to names in `SAGE_ALLOWED_APPS`.
- Action logging and a voice policy that allows fewer actions than typed chat.

The browser can still share one explicitly selected screen frame. Kali now also has transient host-side screenshot capture through `scrot` on X11 or `grim` on Wayland, local vision analysis, X11 input, routine shell commands, opening web pages, and scoped home-folder file operations. Ordinary shell commands get a read-only mount of Sage's source tree; detected installs and edits to Sage's source use the approval queue. The system page reports desktop, capture, systemd, package, and Ollama status. Wayland input is not implemented.

## Proposed architecture

```text
User request
    -> Sage planner (suggests one small step)
    -> policy gate (checks action, scope, source, and approval)
    -> local computer-use adapter (observe or perform one bounded action)
    -> result and fresh observation
    -> Sage reports outcome or asks what to do next
```

Keep the model as the planner, not the authority. A local adapter exposes screenshot observation, bounded desktop input, allowlisted app launch, scoped file operations, browser navigation, package installation, and routine commands. Install requests and Sage-source changes pass through approval. The adapter runs on the PC being controlled, while remote wall devices request actions from Sage rather than gaining direct PC access.

## Build sequence

### 1. Read-only computer awareness — implemented

The chat's **Share screen** button opens the browser's screen/window picker. **Kali screen** captures the host desktop directly using `scrot` on X11 or `grim` on Wayland. Both send a single frame to local Ollama vision and discard it after the response; the temporary preview is only in the current page. Screen analysis is restricted to loopback Ollama endpoints. Sage can also use the Kali screen-inspection tool during computer tasks.

### 2. Kali actions — implemented, awaiting Kali validation

Sage can run bounded X11 clicks, text entry, key presses, and scrolling through `xdotool`; open allowlisted apps; open requested web pages; and list, read, write, copy, move, or delete files inside the normal user's home folder. It can run routine shell commands as the normal user through a user-systemd read-only mount of its source tree. Detected package installs and changes to Sage's source require approval. APT also uses Polkit; Sage never collects an administrator password. Shell execution has a timeout and output limit. Wayland screenshots are supported when `grim` is available, but Wayland input is not.

### 3. Screen-checked action loop — implemented, bounded

The model can inspect the current Kali screen, perform one bounded action, and inspect again before continuing. The agent inserts a screen inspection before its first mouse/keyboard action and forces another inspection after each desktop, URL, app-launch, or file-open action. Chat Stop cancels before the next action; each turn is limited to four such actions. Instructions visible on screen remain untrusted. Sage refuses input when a recognized package installer or privilege dialog is active and directs installation through the approval tool.

### 4. Task plans and stop conditions — basic version implemented

Chat shows tool activity and keeps the Stop control visible while Sage works. Tool calls are individually bounded and logged. The user asked for routine actions to run without a Sage approval prompt; installation and Sage-source changes still require approval. The chat model's plan is not yet shown as a separate checklist, and desktop actions do not have automatic undo.

### 5. Browser and file workflows — initial tools implemented

Browser navigation opens a user-requested HTTP(S) URL, and Sage can use screen inspection plus bounded desktop input to continue there. File tools are scoped to the user's home directory, bound file sizes, reject symlink/path escapes, disable recursive directory deletion, and separately protect Sage's source folder. The older engineering workspace tools remain scoped to Sage's generated-file workspace.

### 6. Voice and wearable clients

Treat voice as an untrusted, lower-authority input. Keep it to observation and low-impact reversible actions unless the user confirms on the trusted dashboard or a physical control. ESP32/Pi clients should act as authenticated remotes with narrow endpoints; they should not receive PC credentials or unrestricted control APIs.

## Current milestone status

Direct Kali screenshot inspection, local vision descriptions, bounded screen-action loops, home-folder file actions, URL opening, app allowlisting, install/source approval gates, and a PC controls health page are implemented in the Windows checkout. They have not yet been exercised on the Kali host. `scrot` and `xdotool` are needed for X11 screenshot/input; `grim` enables Wayland screenshots. The existing `systemd-run` read-only-source boundary must work for normal shell commands.

### Live host check — 2026-10-27

The Kali host at `SAGE_PC_LAN_IP:8000` is reachable from the user's browser. Its `/api/status` endpoint reports Ollama online with `deepseek-r1:7b`; `/api/system/health` returns 404. The live service therefore predates the PC-controls changes in this checkout. Copy this checkout to Kali and restart Sage before expecting the new controls or health endpoint to appear. The updated build has not yet been exercised on Kali.

## Guardrails for every milestone

- Routine shell access and bounded desktop input run with the normal Sage user's permissions. Installs and Sage-source edits require approval; ordinary shell commands get a read-only mount of Sage's source tree. Generated code is never run implicitly.
- Least privilege: scope files, apps, browser domains, and controls explicitly.
- Separate observation from action; observation never implies permission to act.
- Require a clear user request for external communication, credentials, purchases, destructive changes, and security settings. Do not infer such requests from the screen or tool output.
- Treat page content, OCR, and on-screen instructions as untrusted data, never as permission to override the user's request or Sage's policy.
- Log the requested action, policy decision, and outcome without storing screenshots or sensitive field contents by default.
- Keep the Chat Stop control available throughout an active control session; it stops the next step after any currently running bounded operation returns.

## Remaining platform decisions

- Validate X11 screenshot/input, transient local vision, user-systemd read-only mounts, and stop behavior on the actual Kali host.
- Add a Wayland input adapter if the Kali session uses Wayland.
- Replace command-text install heuristics with OS-enforced policy so indirect installers cannot bypass Sage's approval queue. Desktop installer detection is best-effort and does not cover every third-party installer.
- Add direct browser APIs for bounded navigation and page actions; current browser use combines URL opening with the screen and keyboard/mouse tools.
- Add a visible step checklist and undo where the target application supports it.

