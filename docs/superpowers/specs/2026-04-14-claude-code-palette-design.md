# Claude Code palette refactor — design spec

Date: 2026-04-14
Status: approved (autonomous execution)

## Goal

Shift the CCSwitch dashboard palette from its current **cool navy + violet**
aesthetic to match **Claude.ai / Claude Code** look-and-feel: warm cream paper
in light mode, warm near-black in dark mode, Anthropic coral as the single
primary accent. Replace the SVG bolt icon with the `logo.png` asset the user
provided, and use it as the browser favicon too.

Scope is **palette + logo only**. Layout, spacing, typography, radii, shadows,
status semantics, and component structure are out of scope.

## Palette — dark mode (default)

| Token | Before | After |
|---|---|---|
| `--bg` | `#0a0a0f` | `#1f1e1d` warm near-black |
| `--bg-2` | `#111118` | `#262624` |
| `--card` | `#1a1a2e` | `#262624` |
| `--card-2` | `#20203a` | `#30302e` |
| `--card-hover` | `#23233f` | `#3a3937` |
| `--border` | `rgba(255,255,255,0.08)` | `rgba(250,249,245,0.08)` cream-tint |
| `--border-strong` | `rgba(255,255,255,0.15)` | `rgba(250,249,245,0.16)` |
| `--text` | `#e2e8f0` | `#faf9f5` Anthropic cream white |
| `--text-dim` | `#94a3b8` | `#b8b5a8` |
| `--text-mute` | `#64748b` | `#8a8778` |
| `--accent` | `#7c3aed` violet | `#c96442` Anthropic coral |
| `--accent-2` | `#a855f7` | `#d97757` |
| `--accent-glow` | `rgba(124,58,237,0.45)` | `rgba(201,100,66,0.35)` |
| `--success` | `#10b981` | `#10b981` (unchanged) |
| `--warning` | `#f59e0b` | `#eab308` yellow-500 (separates from coral) |
| `--danger` | `#ef4444` | `#dc2626` red-600 |
| `--info` | `#3b82f6` | `#0284c7` sky-600 |

## Palette — light mode (warm paper)

| Token | After |
|---|---|
| `--bg` | `#faf9f5` Claude paper cream |
| `--bg-2` | `#f5f3ee` |
| `--card` | `#ffffff` |
| `--card-2` | `#f5f3ee` |
| `--card-hover` | `#efece3` |
| `--border` | `rgba(31,30,29,0.10)` |
| `--border-strong` | `rgba(31,30,29,0.18)` |
| `--text` | `#1f1e1d` |
| `--text-dim` | `#5d5d57` |
| `--text-mute` | `#8a8778` |
| `--accent` | `#c96442` (same coral — contrasts well in both themes) |
| `--accent-2` | `#b8573a` |
| `--accent-glow` | `rgba(201,100,66,0.18)` |

## Things that stay

- Terminal block (`--term-*`) stays GitHub-dark in both themes — intentional.
- Status colors: success green, danger red, warning yellow, info sky —
  semantic, not brand.
- Layout, spacing, typography (Inter + JetBrains Mono), radii, shadows,
  transition curves — untouched.
- All component structures, JS behavior, backend logic.

## Hardcoded literals to update

`style.css` has several hardcoded color literals outside the token system:

- `rgba(124,58,237, *)` (old accent) → `rgba(201,100,66, *)` globally
- `rgba(168,85,247, *)` (old accent-2) → `rgba(217,119,87, *)` globally
- `#c4b5fd` (violet-300 tint-text) → `#fed7aa` (coral-200 equivalent)
- `#6d28d9` (light theme violet text) → `#9a3412` (orange-800, coral-dark)
- `#2a2a45` / `#363659` (scrollbar dark) → `#3a3937` / `#4a4846`
- `#c8c8da` / `#adadc2` (scrollbar light) → `#d4cfbf` / `#b8b09a`
- `#d1d5db` (toggle track light) → `#d4cfbf`

Status-colored text tints (`#fca5a5`, `#34d399`, `#fcd34d`, `#93c5fd`,
`#fecaca`) are status semantics, not brand — left as is.

## Logo

- Move `logo.png` → `frontend/src/logo.png` (served via existing `/src` mount).
- Replace header SVG bolt (`index.html:20`) with
  `<img class="logo" src="src/logo.png" alt="CCSwitch">`.
- Add favicon: `<link rel="icon" type="image/png" href="src/logo.png">`.
- Add `.logo` CSS rule: 24px square, no extra drop-shadow (the PNG already
  has its own glow baked in).
- Drop `.title .bolt` rule that adds purple drop-shadow.

## Body background glow

The radial-gradient overlays on `<body>` currently use violet tints. Swap to
subtler coral tints:

- Dark: `rgba(201,100,66,0.08)` + `rgba(217,119,87,0.05)`
- Light: `rgba(201,100,66,0.05)` + `rgba(217,119,87,0.03)`

Calmer than the current violet glow to match Claude's paper-like feel.

## Out of scope

- Typography changes
- Component restructuring
- New features
- Terminal-block color (stays GitHub-dark)
- Status-color semantic meaning

## Verification

After implementation:

1. Start dev server (`uv run uvicorn backend.main:app --port 41924`).
2. `curl` `/`, `/src/style.css`, `/src/logo.png` — all must return 200.
3. `uv run pytest tests/ -q` — all tests must still pass (no backend change,
   should be green).
4. Manual visual check: user opens `http://127.0.0.1:41924` and confirms
   the look matches Claude.ai / Claude Code aesthetic.
