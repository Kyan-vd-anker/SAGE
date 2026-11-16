# SAGE changelog

All notable changes to SAGE are documented here. Version numbers follow `MAJOR.MINOR.PATCH`; the major number is only changed with Kyan's explicit instruction.

## Unreleased

- Memory upgrade. Memories now have tiers (core, working, episodic, semantic, procedural); core and pinned memories form an always-on profile block that sits at the start of the cached prompt prefix. A new knowledge graph (`entities.py`) links people, projects and devices to memories by alias with a single regex, so no model call is needed per turn. A changed fact now supersedes the older note instead of overwriting it (history is kept and `search_memory` can return it), and a shorter near-duplicate can no longer replace a richer note. After a reply, an idle-time capture job (`capture.py`) extracts facts, entities, relations and open loops from what the user said; high-confidence facts are saved, core-tier ones wait for approval. Each project gets a refreshed "state of play" (`userprofile.py`). The voice path skips the embedding call and the older-message scan, keeps replies short, and uses a compact profile. `search_memory` is now deep recall (memories, past conversation, decisions, entity cards; filter by tier, entity or age). New chat commands: "show core profile", "show memory tiers", "list entities", "state of play <project>". `SAGE_MEMORY_CAPTURE=0` and `SAGE_PROJECT_STATE=0` switch the two background jobs off.
- One context size for every request: `SAGE_NUM_CTX` (default 16384) is now sent with every chat and background call, and `SAGE_VSCODE_NUM_CTX` follows it. VS Code previously ran with 8192 and refused larger requests with `context_length_exceeded`. Rebuild the models with `python scripts/build-models.py` so the Modelfiles agree.
- Fixed: `draft_email_reply` raised a NameError because `agent.py` never imported `outlook`.

- VS Code endpoints (`sage/vscode.py`, see `VSCODE.md`): an OpenAI-compatible API (`/v1/models`, `/v1/chat/completions` with streaming and tool calls, `/v1/completions`, `/v1/embeddings`) over three lanes - `sage-fast` (inline suggestions, thinking off), `sage-coder` (thinking off) and `sage-reason` (thinking on, `reasoning_content`) - and `/api/vscode/inline` for editor-style inline completion (native fill-in-the-middle when the model supports it, otherwise a cursor-marker chat prompt with cleanup; a newer request from the same editor cancels the older one). Disabled until `SAGE_VSCODE_TOKEN` is set; requests that would overflow the context window are refused instead of silently truncated.
- Prompt caching (Ollama prefix/KV-cache reuse) for faster first tokens: the most stable context (project) now comes before what changes per question (memories, history) with the clock last; tool definitions are sent in a fixed order; models built by `scripts/build-models.py` (named `local/sage-*`) are now recognised, with the stored prompt verified against the current one through `/api/show` (a stale model falls back to sending the current prompt). Optional `SAGE_CHAT_TURNS` (default 0 = off) sends recent exchanges as real conversation messages in blocks (`SAGE_CHAT_TURN_BLOCK`) so the whole conversation prefix stays cacheable. `SAGE_PROMPT_CACHE=0` turns all of it off. Speed stats now include prompt tokens evaluated and an estimated cache reuse.
- Fix attached documents being summarised wrongly (for example a cited paper instead of the attached one). Cause: the whole text was pasted into a 4096-token prompt, Ollama silently dropped everything but the end of it (the reference list). Documents now go through `sage/docqa.py`: per-page ingestion, reference-list exclusion, document-only retrieval, token-budgeted prompts (`SAGE_DOC_NUM_CTX`), verified identification, claim/number/citation verification, and an explicit "not found" instead of guessing.
- Document answers are no longer fed back as chat history, never trigger reflection, and are logged to `data/logs/docqa.log`.
- Scientific reading upgrade: layout-aware PDF extraction (`sage/pdftext.py`: two-column pages read column by column, font sizes and bold kept, ligature/spacing repair, scanned or garbled pages reported or refused) and structure analysis (`sage/scidoc.py`: the journal banner is no longer taken for the title; title, authors, journal, DOI; running headers removed; figure/table captions, table rows, equations; section outline with roles). Summaries are now section-aware (`sage/scisummary.py`): each section is read with a question that fits it, notes are verified against their page, defined terms are collected, and the synthesis follows a fixed scientific structure with page citations. Sentences that go beyond the paper are marked "Interpretation:" and still checked; statements that mix up two materials or name one that is not in the paper are flagged. `SAGE_DOC_MAP_MAX_CALLS` now defaults to 16.
- Studio: new **Read a paper** button. Sage reads a PDF/DOCX/TXT with the source-grounded pipeline and opens a Studio document with the paper's important parts (verified identification, page-cited summary, the numbers it states, its structure). New **Assistant** tab: ask questions about the paper that was read (answers cite pages, or say the paper doesn't contain it), or about your own draft; say "I'm going to write a paper about ..." or describe a paper to get a first LaTeX/Markdown layout with what to write in each section (builds on the paper that was read, never invents numbers or citations, falls back to a standard layout if the model is unavailable). Read papers are stored locally and removed with their last document.

## 2.4.0 - 2026-10-28

- Add Outlook email search, unread inbox briefs, and reply drafts that are saved but never sent.
- Extend calendar search and add bounded, read-only search across configured local document folders.
- Improve intent-based tool selection and guard email draft creation behind a direct request.
- Document Microsoft Graph permissions, reauthorization, file roots, and private-data handling.

## 2.3.0 - 2026-10-28

- Add OctoPrint printer status, library browsing, and direct printing of existing G-code files.
- Require a direct user print command naming the file; verify library membership and printer state before starting.
- Document private API-key configuration for the Kali Sage service.

## 2.2.0 - 2026-10-28

- Improve memory and conversation-gap learning; add safe, documented updates

- Add review-first memory suggestions, semantic recall with a keyword fallback, and conversation-gap learning with cited findings.
- Add a backup-first Kali update script and a documented Windows release publisher.
- Exclude local data and machine-specific network settings from releases.

## 2.1.1

- Previous development version; formal release notes were not maintained yet.

