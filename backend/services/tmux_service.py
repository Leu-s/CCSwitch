import subprocess
import asyncio

def list_panes() -> list[dict]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}"],
            capture_output=True, text=True
        )
        panes = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            panes.append({
                "target": parts[0],
                "command": parts[1] if len(parts) > 1 else ""
            })
        return panes
    except FileNotFoundError:
        return []

def send_continue(target: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "continue", "Enter"],
        check=True, capture_output=True
    )

def capture_pane(target: str, lines: int = 20) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout

async def evaluate_with_haiku(capture: str, model: str) -> dict:
    prompt = (
        "Did the Claude Code session successfully continue after an account switch? "
        "Reply with one of: SUCCESS, FAILED, UNCERTAIN. Then one sentence of explanation.\n\n"
        f"Terminal output:\n{capture}"
    )
    result = subprocess.run(
        ["claude", "-p", "--model", model, prompt],
        capture_output=True, text=True, timeout=30
    )
    output = result.stdout.strip()
    status = "UNCERTAIN"
    for s in ("SUCCESS", "FAILED", "UNCERTAIN"):
        if s in output:
            status = s
            break
    explanation = output.replace(status, "").strip(" .\n") or "No explanation"
    return {"status": status, "explanation": explanation, "raw": output}
