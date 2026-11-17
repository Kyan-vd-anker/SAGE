# Sage for VS Code

Sage exposes its three models to VS Code through an OpenAI-compatible API, plus an inline-suggestion endpoint on the fast model.
Any extension that can talk to an "OpenAI-compatible" server (Continue, Cline, ...) can use it, and a custom extension can call the
inline endpoint directly.

## Turn it on

Set a long random token where you set the other Sage secrets (`start-sage.local.bat` on Windows) and restart Sage:

    SAGE_VSCODE_TOKEN=<long random string>

Until it is set, every endpoint below answers `503 disabled`. The token is sent as `Authorization: Bearer <token>`. The normal browser
login is not used for these paths, so keep the token private; Sage's client-IP allow-list still applies. Reach Sage the way you
already do (Tailscale Serve, for example) and use `https://<your-sage-host>/v1` as the API base.

## Models

| id            | runs on                          | thinking | use                        |
|---------------|----------------------------------|----------|----------------------------|
| `sage-fast`   | `SAGE_FAST_MODEL`                | off      | inline suggestions         |
| `sage-coder`  | `SAGE_DEEP_MODEL`                | off      | code chat, edits           |
| `sage-reason` | `SAGE_REASONING_MODEL`           | on       | hard problems and planning |

Only these three are reachable; an unknown model id falls back to `sage-coder` (chat) or `sage-fast` (completions). Thinking can be
overridden with `SAGE_THINK_FAST`, `SAGE_THINK_DEEP`, `SAGE_THINK_REASONING` (`true` / `false`). The reasoning lane returns its thinking
in `reasoning_content`, apart from the answer.

## Endpoints

| method | path                     | what                                                                 |
|--------|--------------------------|----------------------------------------------------------------------|
| GET    | `/v1/models`             | the three lanes                                                      |
| POST   | `/v1/chat/completions`   | chat; `stream: true` gives server-sent events; tool calls supported   |
| POST   | `/v1/completions`        | inline completion: `prompt` = code before the cursor, `suffix` = after |
| POST   | `/v1/embeddings`         | embeddings (`SAGE_EMBED_MODEL`) for codebase indexing                |
| POST   | `/api/vscode/inline`     | inline completion with editor fields (below)                         |
| GET    | `/api/vscode/status`     | lanes, thinking, and which inline method is in use                   |

### Inline suggestions

    POST /api/vscode/inline
    { "prefix": "...code before the cursor...", "suffix": "...code after...", "language": "python", "path": "src/a.py",
      "max_tokens": 128, "temperature": 0.2 }
    -> { "completion": "a + b", "model": "sage-fast", "method": "native" | "chat" | "cache", "superseded": false, "ms": 310 }

- If the fast model supports fill-in-the-middle (Ollama reports the `insert` capability) it is used directly. Otherwise a short chat prompt with a
  `<CURSOR>` marker is used, with thinking off, and the answer is cleaned (no markdown fences, no repeated code before or after the cursor).
- When code follows the cursor on the same line only the rest of that line is completed; otherwise up to 12 lines.
- Send an `X-Client-Id` header (one value per editor window). A newer request with the same id cancels the older one still running, and the cancelled one
  returns `"superseded": true` with an empty completion: ignore it. Identical requests within two minutes are answered from a cache.
- Only the last `SAGE_VSCODE_PREFIX_CHARS` (6000) characters before and the first `SAGE_VSCODE_SUFFIX_CHARS` (1500) after the cursor are used.
- `SAGE_VSCODE_FIM=off` forces the chat prompt, `on` forces native fill-in-the-middle, `auto` (default) asks Ollama.

### Chat

Requests are passed to Ollama with a fixed context window (`SAGE_VSCODE_NUM_CTX`, default 8192, the same as the built models so nothing is reloaded). A
request that would not fit is refused with `400 context_length_exceeded` instead of letting Ollama silently drop the start of the prompt: send less
code or history. At most `SAGE_VSCODE_MAX_CONCURRENT` (4) requests run at once; more get `429`.

## Example: Continue

The key names change between Continue versions, so treat this as a guide and check Continue's current documentation.

```yaml
models:
  - name: Sage coder
    provider: openai
    model: sage-coder
    apiBase: https://<your-sage-host>/v1
    apiKey: <SAGE_VSCODE_TOKEN>
    roles: [chat, edit, apply]
  - name: Sage reason
    provider: openai
    model: sage-reason
    apiBase: https://<your-sage-host>/v1
    apiKey: <SAGE_VSCODE_TOKEN>
    roles: [chat]
  - name: Sage fast
    provider: openai
    model: sage-fast
    apiBase: https://<your-sage-host>/v1
    apiKey: <SAGE_VSCODE_TOKEN>
    roles: [autocomplete]
    useLegacyCompletionsEndpoint: true
  - name: Sage embeddings
    provider: openai
    model: nomic-embed-text
    apiBase: https://<your-sage-host>/v1
    apiKey: <SAGE_VSCODE_TOKEN>
    roles: [embed]
```

## Tips

- After building the models, run the models once so they are loaded: the first request after a model has been unloaded takes several seconds.
- By default Sage keeps models loaded for 30 minutes. Set `SAGE_OLLAMA_KEEP_ALIVE=0` before starting Sage to unload each model after its request and reduce Ollama's RAM/VRAM use; requests will be slower because models must be loaded again.
- Ollama does not expose a way to save/restore its KV tensors to an SSD or vector database. Sage already persists conversation messages and embedded memories in its local SQLite database, retrieves relevant saved context, and rebuilds the prompt after a model reload; the model has to process that prompt again.
- If memory allows, run Ollama with `OLLAMA_MAX_LOADED_MODELS=2` so inline suggestions are not
  waiting for the coder or reasoning model to be swapped out.
