import asyncio
import logging
import re
import subprocess

from ..ws import WebSocketManager

logger = logging.getLogger(__name__)

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
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "--model", model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=30)
        output = stdout.decode().strip()
    except Exception as e:
        return {"status": "UNCERTAIN", "explanation": str(e), "raw": ""}
    status = "UNCERTAIN"
    for s in ("SUCCESS", "FAILED", "UNCERTAIN"):
        if s in output:
            status = s
            break
    explanation = output.replace(status, "").strip(" .\n") or "No explanation"
    return {"status": status, "explanation": explanation, "raw": output}


async def notify_monitors(monitors, ws: WebSocketManager, model: str) -> None:
    """
    For each enabled monitor, find matching tmux panes, send 'continue',
    capture output, evaluate with Haiku, and broadcast the result.
    """
    all_panes = list_panes()
    for monitor in monitors:
        if monitor.pattern_type == "manual":
            matching = [p for p in all_panes if p["target"] == monitor.pattern]
        else:
            try:
                matching = [p for p in all_panes if re.search(monitor.pattern, p["target"])]
            except re.error:
                matching = []

        for pane in matching:
            try:
                send_continue(pane["target"])
                await asyncio.sleep(2)
                capture = capture_pane(pane["target"])
                eval_result = await evaluate_with_haiku(capture, model)
                try:
                    await ws.broadcast({
                        "type": "tmux_result",
                        "monitor_id": monitor.id,
                        "target": pane["target"],
                        "status": eval_result["status"],
                        "explanation": eval_result["explanation"],
                        "capture": capture,
                    })
                except Exception as _bc_err:
                    logger.warning("WS broadcast failed: %s", _bc_err)
            except Exception as e:
                try:
                    await ws.broadcast({
                        "type": "tmux_result",
                        "monitor_id": monitor.id,
                        "target": pane["target"],
                        "status": "FAILED",
                        "explanation": str(e),
                        "capture": "",
                    })
                except Exception as _bc_err:
                    logger.warning("WS broadcast failed: %s", _bc_err)
