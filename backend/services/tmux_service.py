import subprocess

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

def send_keys(target: str, text: str, press_enter: bool = True) -> None:
    cmd = ["tmux", "send-keys", "-t", target, text]
    if press_enter:
        cmd.append("Enter")
    subprocess.run(cmd, check=True, capture_output=True)

def send_continue(target: str) -> None:
    send_keys(target, "continue")

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
        ["claude", "-p", "--model", model],
        input=prompt,
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
