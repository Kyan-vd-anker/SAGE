# Outlook email, calendar, and local file search

Sage extends its existing Microsoft Graph Outlook connection with email search, unread inbox briefs, and reply drafts. Calendar ICS feeds remain read-only and can only be used for calendar data; email needs the Graph connection. Sage does not have an email-send tool.

## Outlook email access

1. Open Sage's **Daily briefing** page and connect Microsoft Graph. The app registration must allow public client/device-code sign-in and have the delegated Microsoft Graph permissions `User.Read`, `Calendars.ReadWrite`, and `Mail.ReadWrite`.
2. If Graph is already connected for calendar use, choose **Grant email access** in the Outlook panel. Sage starts a new Microsoft sign-in flow so you can consent to the additional mail permission; it does not silently expand the old token. If the button is unavailable, disconnect and reconnect Graph from that page.
3. After sign-in, the panel reports whether email access is present. An ICS-only calendar link does not grant mailbox access.

Microsoft lists `Mail.ReadWrite` as the least-privileged permission for creating a reply draft. That permission allows Sage to read and modify mail in the connected mailbox, but does not include sending mail. Sage uses Graph's `createReply` endpoint and saves the text in Drafts. Sage never sends it. See Microsoft's [createReply API](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0) and [mail permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0).

Example requests:

- “Search my email for the message from Alex about the printer.”
- “Give me a brief of my unread inbox.”
- “Summarize my latest email.”
- “Summarize the latest email about the delivery.”
- “Draft a reply to that email saying I can collect it Friday.”
- “Find my next dentist appointment.”
- “What is on my calendar this week?”

Search and inbox tools return sender, date, subject, a short preview, and the opaque message id needed for a follow-up draft. Sage logs only counts for mail search and omits queries, message text, and reply text from its action log. Email and calendar requests are excluded from automatic conversation reflection so their contents are not proposed as saved memories. The local model still receives the previews needed to answer your request.

## Local document search

By default Sage searches existing `Documents`, `Desktop`, and `Downloads` folders in the Kali Sage user's home directory. To select different folders, add `SAGE_FILE_SEARCH_ROOTS` to Sage's systemd user-service override as a path-separated list. Example for Linux:

```ini
[Service]
Environment="SAGE_FILE_SEARCH_ROOTS=%h/Documents:/mnt/shared/manuals"
```

Then run `systemctl --user daemon-reload` and `systemctl --user restart sage`. Paths with spaces are supported. On Windows, separate multiple roots with semicolons, for example `C:\Users\Kyan\Documents;D:\Manuals`.

Ask “search my documents for the stepper motor wiring” or “find the file named calibration notes.” Search is read-only and scans only the configured roots. It returns matching paths and short snippets from readable text formats such as TXT, Markdown, CSV, JSON, YAML, INI, source code, HTML, and logs. Binary formats such as PDF and DOCX are not searched by this feature. Hidden folders, common dependency/cache folders, files larger than 512 KB, and credential-like filenames are skipped. A scan is bounded to 12,000 files and Sage reports when that limit is reached.

## How Sage selects tools

Sage maps common action and subject phrases to a small set of relevant tools before asking its local model to choose the exact call. For example, “unread inbox” surfaces the inbox brief; a sender or subject search surfaces email search; “draft a reply” surfaces the draft action; and “find an appointment” surfaces calendar search. A direct-request check blocks draft creation when the latest user message is only discussing the feature. The model must use a message id returned by a mail search or inbox result; if the target is unclear, it should ask which message you mean.

The selector is intentionally a useful first pass rather than a guarantee that every phrasing is understood. If Sage picks the wrong capability, name the intended action (search, summarize unread mail, find an event, or draft a reply) and the target. No email send capability is registered.
