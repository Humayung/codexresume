#!/usr/bin/env python3
"""Select and resume a Codex session saved for the current directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator


def session_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / "sessions"


def iter_session_lines(path: Path) -> Iterator[bytes]:
    """Read a rollout, including zstd files when a decoder is available."""
    if not path.name.endswith(".zst"):
        with path.open("rb") as stream:
            yield from stream
        return

    try:
        import zstandard  # type: ignore[import-not-found]

        with zstandard.open(path, mode="rb") as stream:
            yield from stream
        return
    except ImportError:
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError("compressed log needs the Python zstandard package or zstd command")

    process = subprocess.Popen(
        [zstd, "-dc", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        yield from process.stdout
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()


def extract_prompt(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    else:
        return ""

    text = re.sub(r"<environment_context>.*?</environment_context>", " ", text, flags=re.S)
    return " ".join(text.split())


def local_time(value: str) -> tuple[datetime | None, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.astimezone()
        return parsed, parsed.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None, value or "unknown date"


def read_session(path: Path, target_cwd: Path) -> dict | None:
    metadata = None
    prompt = ""

    for line in iter_session_lines(path):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if metadata is None and record.get("type") == "session_meta":
            saved_cwd = payload.get("cwd") or record.get("cwd")
            if not saved_cwd:
                return None
            if Path(saved_cwd).expanduser().resolve() != target_cwd:
                return None
            metadata = payload

        if not prompt and record.get("type") == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "user":
                prompt = extract_prompt(payload)

        if metadata is not None and prompt:
            break

    if metadata is None:
        return None

    session_id = metadata.get("session_id") or metadata.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None

    timestamp = metadata.get("timestamp") or ""
    parsed_time, display_time = local_time(timestamp)
    return {
        "id": session_id,
        "cwd": str(target_cwd),
        "timestamp": parsed_time,
        "display_time": display_time,
        "prompt": prompt or "(no prompt preview)",
        "files": [path],
    }


def find_sessions(target_cwd: Path) -> tuple[list[dict], list[str]]:
    root = session_root()
    if not root.is_dir():
        return [], [f"Session directory not found: {root}"]

    sessions: dict[str, dict] = {}
    skipped: list[str] = []
    paths = sorted(root.rglob("rollout-*.jsonl*"))

    for path in paths:
        if not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
            continue
        try:
            item = read_session(path, target_cwd)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            skipped.append(f"{path.name}: {error}")
            continue
        if item is None:
            continue

        existing = sessions.get(item["id"])
        if existing is None:
            sessions[item["id"]] = item
        else:
            existing["files"].append(path)
            if existing["timestamp"] is None and item["timestamp"] is not None:
                existing["timestamp"] = item["timestamp"]
                existing["display_time"] = item["display_time"]
            if existing["prompt"] == "(no prompt preview)" and item["prompt"] != "(no prompt preview)":
                existing["prompt"] = item["prompt"]

    result = list(sessions.values())
    result.sort(key=lambda session: session["timestamp"] or datetime.min, reverse=True)
    return result, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Choose a saved Codex session for the current working directory."
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="directory to match (defaults to the directory where you run codexresume)",
    )
    args = parser.parse_args()
    target_cwd = args.cwd.expanduser().resolve()

    sessions, skipped = find_sessions(target_cwd)
    if not sessions:
        print(f"No saved sessions found for: {target_cwd}")
        if skipped:
            print(f"Some compressed logs could not be read ({len(skipped)}). Install zstandard or zstd to include them.")
        return 1

    print(f"Codex sessions for {target_cwd}\n")
    for number, session in enumerate(sessions, start=1):
        prompt = session["prompt"]
        if len(prompt) > 120:
            prompt = prompt[:117].rstrip() + "..."
        file_count = len(session["files"])
        file_note = f" ({file_count} log files)" if file_count > 1 else ""
        print(f"{number:>2}. {session['display_time']}  {session['id']}{file_note}")
        print(f"    {prompt}")

    if skipped:
        print(f"\nNote: {len(skipped)} compressed/unreadable log(s) were skipped.")
        print("Install the Python 'zstandard' package or the 'zstd' command to include compressed logs.")

    while True:
        try:
            choice = input("\nEnter a number to resume, or q to quit: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice.lower() in {"q", "quit", "exit"}:
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
            print(f"Enter a number from 1 to {len(sessions)}, or q to quit.")
            continue

        selected = sessions[int(choice) - 1]
        codex = shutil.which("codex")
        if codex is None:
            print("Could not find 'codex' on PATH.", file=sys.stderr)
            return 127

        os.execv(
            codex,
            [codex, "resume", "-C", str(target_cwd), selected["id"]],
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
