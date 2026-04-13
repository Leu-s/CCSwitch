// Tmux nudge — single toggle + message string that controls whether the
// background switch loop wakes stalled Claude Code tmux sessions after every
// account switch.  Backed by two settings rows: tmux_nudge_enabled and
// tmux_nudge_message.

import { qs } from "../utils.js";
import { api } from "../api.js";
import { toast } from "../toast.js";

let _initialMessage = "continue";

export async function loadTmuxNudge() {
  try {
    const settings = await api("/api/settings");
    const en = settings.find(s => s.key === "tmux_nudge_enabled");
    const msg = settings.find(s => s.key === "tmux_nudge_message");
    const cb = qs("#tnudge-enabled-cb");
    const input = qs("#tnudge-message-input");
    if (cb) cb.checked = en ? en.value === "true" : false;
    if (input) {
      _initialMessage = msg ? (msg.value || "continue") : "continue";
      input.value = _initialMessage;
    }
  } catch {
    /* initial load — ignore */
  }
}

export function initTmuxNudgeListeners() {
  const cb = qs("#tnudge-enabled-cb");
  if (cb) {
    cb.addEventListener("change", async () => {
      const val = cb.checked ? "true" : "false";
      cb.disabled = true;
      try {
        await api("/api/settings/tmux_nudge_enabled", {
          method: "PATCH",
          body: { value: val },
        });
        toast(
          cb.checked ? "Tmux nudge on" : "Tmux nudge off",
          cb.checked
            ? "Stalled tmux sessions will be nudged after every switch"
            : "Tmux sessions will be left alone on switch",
          "success",
          2200,
        );
      } catch (e) {
        cb.checked = !cb.checked;
        toast("Update failed", e.message || String(e), "error");
      } finally {
        cb.disabled = false;
      }
    });
  }

  const saveBtn = qs("#tnudge-save-btn");
  const input = qs("#tnudge-message-input");
  if (saveBtn && input) {
    const save = async () => {
      const v = (input.value || "").trim();
      if (!v) {
        toast("Message cannot be empty", "Type something to send to stalled panes", "error");
        return;
      }
      saveBtn.disabled = true;
      const original = saveBtn.textContent;
      saveBtn.textContent = "Saving…";
      try {
        await api("/api/settings/tmux_nudge_message", {
          method: "PATCH",
          body: { value: v },
        });
        _initialMessage = v;
        toast("Nudge message saved", v, "success", 2200);
      } catch (e) {
        toast("Save failed", e.message || String(e), "error");
      } finally {
        saveBtn.textContent = original;
        saveBtn.disabled = false;
      }
    };
    saveBtn.addEventListener("click", save);
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        save();
      }
    });
  }
}
