# analysis/models/ — the embedded LLM lives HERE

**⚑ The local LLM weights file sits in this folder:**

```
analysis/models/gemma-2-2b-it-Q4_K_M.gguf     (~1.7 GB)
```

This is the model the analysis pipeline loads **in-process** (via
`llama-cpp-python`, see `analysis/local_llm.py`) when it runs without an
Ollama server — no daemon, nothing to connect to. The backend auto-discovers
any `*.gguf` placed in this folder (newest wins); to point somewhere else set
the env var `LLM_GGUF_PATH` to an explicit file path.

## Facts to know

- **Gitignored.** `.gguf` files never get committed (see the repo
  `.gitignore`), so this folder looks almost empty on GitHub — that's normal.
- **Safe to delete.** Removing the file frees the 1.7 GB; the pipeline then
  falls back to the Ollama server if one is running, or to the deterministic
  regex/rules layers if not. Nothing breaks.
- **Re-download any time:**

  ```bash
  python analysis/models/download_model.py            # default: gemma2-2b
  python analysis/models/download_model.py --list     # other vetted models
  ```

  Downloads come from Hugging Face and resume if interrupted
  (`*.gguf.part` is a partial download — safe to delete too).
- **Backend selection:** env `LLM_BACKEND` = `auto` (default: Ollama server
  if up, else this file) | `ollama` | `embedded` (only ever this file) |
  `none`.
- Don't confuse this with Ollama's own model store (`~/.ollama/models`) —
  that one belongs to the Ollama server; this folder is the server-free copy.
