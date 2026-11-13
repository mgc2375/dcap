"""Run Codex on a task with DCAP listening from outside, through Codex's own event stream.

The AI gets no tool or instruction from this. As soon as it sees something, anything DCAP wants to say
(an offer, or the pasted code after a yes) pauses the turn and becomes the next message in the same session.

Usage: drive.py --codex PATH --workspace DIR [--definitions DIR] [--log FILE] "task"
"""
import argparse
import json
from pathlib import Path
import subprocess
import threading

from dcap import Dcap
from dcap.listener import Listener

MAX_TURNS = 6
TURN_SECONDS = 180


def observed(event, workspace):
    """What one Codex event shows the AI saying or writing: (said, wrote)."""
    item = event.get("item") or {}
    if event.get("type") != "item.completed":
        return [], []
    if item.get("type") == "agent_message" and item.get("text"):
        return [item["text"]], []
    if item.get("type") == "file_change":
        wrote = []
        for change in item.get("changes") or []:
            path = Path(workspace, change.get("path", ""))
            if path.suffix == ".py" and path.is_file():
                wrote.append(path.read_text(encoding="utf-8", errors="replace"))
        return [], wrote
    return [], []


def run_turn(codex, workspace, shared, prompt, thread, listener, log):
    """Follow one turn live. Pause it as soon as DCAP has something to say.

    Returns (thread, what DCAP wants to say next).
    """
    command = [codex, "exec", *shared, *(["resume", thread, prompt] if thread else [prompt])]
    proc = subprocess.Popen(
        command, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True
    )
    timer = threading.Timer(TURN_SECONDS, proc.kill)
    timer.start()
    message = None
    handle = open(log, "a") if log else None
    try:
        if handle:
            handle.write(json.dumps({"prompt": prompt}) + "\n")
        for line in proc.stdout:
            if handle:
                handle.write(line)
                handle.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                thread = event.get("thread_id")
            said, wrote = observed(event, workspace)
            if said or wrote:
                message = listener.observe(said, wrote)
                listener.save()
                if message:
                    proc.terminate()  # at a message boundary, before the AI's next action
                    break
    finally:
        timer.cancel()
        proc.wait()
        if handle:
            handle.close()
    return thread, message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--definitions", action="append", default=[])
    parser.add_argument("--log")
    parser.add_argument("--option", action="append", default=[], help="passed to codex exec")
    parser.add_argument("task")
    args = parser.parse_args()
    shared = ["--json", "--skip-git-repo-check", *args.option]

    listener = Listener(Dcap(args.workspace, args.definitions))
    thread, prompt = None, args.task
    for _ in range(MAX_TURNS):
        thread, message = run_turn(args.codex, args.workspace, shared, prompt, thread, listener, args.log)
        if not message:
            break
        prompt = message


if __name__ == "__main__":
    main()
