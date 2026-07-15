"""
download_model.py — fetch a small GGUF model for the EMBEDDED (serverless) LLM.

The analysis package can run its local model two ways:

  * an Ollama server (http://127.0.0.1:11434), or
  * EMBEDDED — llama-cpp-python loads a .gguf weights file from this folder
    directly into the Python process: no server, no daemon, nothing to
    connect to.

This script downloads one of the vetted small instruct models below from
Hugging Face into ``analysis/models/``.  The embedded backend auto-discovers
any ``*.gguf`` here (or set LLM_GGUF_PATH to an explicit file).

Usage
-----
    python analysis/models/download_model.py                 # default: gemma2-2b
    python analysis/models/download_model.py --model qwen2.5-1.5b
    python analysis/models/download_model.py --list

Downloads resume if interrupted (HTTP Range).
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

DEST_DIR = os.path.dirname(os.path.abspath(__file__))

# Vetted small instruct models (Q4_K_M quantisation — the same class Ollama
# serves).  gemma2-2b is the default because it won the extraction benchmark
# (see LOGIC.md section 7).  Sizes are approximate.
MODELS = {
    "gemma2-2b": {
        "repo": "bartowski/gemma-2-2b-it-GGUF",
        "file": "gemma-2-2b-it-Q4_K_M.gguf",
        "size": "1.7 GB", "license": "Gemma terms",
        "note": "benchmark winner for extraction (default)",
    },
    "qwen2.5-1.5b": {
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size": "1.0 GB", "license": "Apache-2.0",
        "note": "smallest with acceptable quality",
    },
    "phi3.5-mini": {
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "file": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "size": "2.3 GB", "license": "MIT",
        "note": "3.8B - a bit stronger, a bit slower",
    },
    "llama3.2-1b": {
        "repo": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "file": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "size": "0.8 GB", "license": "Llama 3.2 license",
        "note": "tiny/fast; weakest on our tasks",
    },
}


def hf_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


def download(name: str, dest_dir: str = DEST_DIR) -> str:
    spec = MODELS[name]
    url = hf_url(spec["repo"], spec["file"])
    dest = os.path.join(dest_dir, spec["file"])
    part = dest + ".part"

    if os.path.exists(dest):
        print(f"already downloaded: {dest}")
        return dest

    resume_from = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "sec-analysis-model-fetch"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        print(f"resuming at {resume_from / 1e6:.0f} MB")

    print(f"downloading {spec['file']}  (~{spec['size']}, {spec['license']})")
    print(f"  from {url}")
    with requests.get(url, headers=headers, stream=True, timeout=60,
                      allow_redirects=True) as r:
        if r.status_code == 416:            # .part is already the full file
            os.replace(part, dest)
            print(f"done: {dest}")
            return dest
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) + resume_from
        mode = "ab" if resume_from and r.status_code == 206 else "wb"
        got = resume_from if mode == "ab" else 0
        last_step = -1
        with open(part, mode) as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                got += len(chunk)
                if total:
                    step = int(100 * got / total) // 2   # report every 2%
                    if step > last_step:
                        last_step = step
                        print(f"  {got / 1e6:7.0f} / {total / 1e6:.0f} MB "
                              f"({100 * got / total:4.1f}%)", flush=True)
    os.replace(part, dest)
    print(f"done: {dest}")
    print("The embedded backend will now pick this up automatically "
          "(no Ollama needed).")
    return dest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--model", default="gemma2-2b", choices=sorted(MODELS),
                   help="which model to download (default: gemma2-2b)")
    p.add_argument("--list", action="store_true", help="list vetted models")
    args = p.parse_args()

    if args.list:
        for k, v in MODELS.items():
            print(f"{k:<14} {v['size']:>7}  {v['license']:<18} {v['note']}")
        return 0
    try:
        download(args.model)
    except requests.HTTPError as exc:
        print(f"download failed: {exc}")
        print("Check the repo/file still exists on huggingface.co, or pick "
              "another model (--list).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
