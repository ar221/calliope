"""Embedded phone PWA assets — HTML/JS blob, PWA manifest, pairing page.

Pure data: no config reads, no runtime state. Extracted from the executable
`calliope-server` script (Stage 2 split). The CI gate
`scripts/check-web-ui-js` extracts the <script> blocks from WEB_UI in THIS
file and runs `node --check` over them.
"""

import json

# ─── Web UI ───────────────────────────────────────────────
WEB_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0E0B08">
<title>Dictate</title>
<link rel="manifest" href="/manifest.json">
<style>
  /* POL-5 — Apollo re-skin. Mirrors apollo-st.css :root tokens (Agent 4 §8.2).
   * Twelve token swap; the rest of the rules consume them via custom-prop
   * cascade so most of the file is theme-portable as-is. */
  :root {
    /* Apollo palette */
    --primary:        #FFB648;          /* was #c4a7e7 — Apollo amber */
    --primary-dim:    rgba(255, 182, 72, 0.18);
    --primary-deep:   #5A4420;
    --on-primary:     #0E0B08;
    --surface:        #1C150C;          /* was #1e1e2a */
    --surface-high:   #2B1F10;          /* was #272736 */
    --bg:             #0E0B08;          /* was #121218 — Apollo near-black */
    --text:           #F2E3C6;          /* was #e0def4 — cream */
    --text-dim:       #98876F;          /* was #908caa — bronze-dim */
    --error:          #FF5A4E;          /* ember */
    --warn:           #C99545;          /* amber-dim */
    --warning:        #C99545;          /* alias — legacy uses --warning */
    --success:        #A8C97B;          /* sage */
    --recording:      #FF5A4E;          /* ember — was rosé pink */

    /* Sharp Apollo geometry (was 16/10 — soft rounded slate) */
    --radius:    4px;
    --radius-md: 4px;
    --radius-sm: 2px;

    /* Apollo monospace identity */
    font-family: 'Monaspace Krypton', 'IBM Plex Mono', 'JetBrains Mono',
                 ui-monospace, 'SF Mono', Menlo, monospace;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Monaspace Krypton', 'IBM Plex Mono', 'JetBrains Mono',
                 ui-monospace, 'SF Mono', Menlo, monospace;
    background: var(--bg);
    color: var(--text);
    height: 100dvh;
    display: flex;
    flex-direction: column;
    -webkit-tap-highlight-color: transparent;
    overflow-x: hidden;
    overflow-y: auto;
    padding-bottom: env(safe-area-inset-bottom, 0);
  }

  .header {
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    border-bottom: 1px solid var(--surface-high);
  }
  .header h1 {
    font-size: 20px;
    font-weight: 600;
    color: var(--primary);
  }
  .header .status {
    margin-left: auto;
    font-size: 12px;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--success);
  }
  .status-dot.offline { background: var(--error); }

  .controls {
    padding: 12px 20px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .controls.mode-chips {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--surface-high) transparent;
    padding-right: 8px;
    -webkit-overflow-scrolling: touch;
  }
  .controls.mode-chips::-webkit-scrollbar { height: 4px; }
  .controls.mode-chips::-webkit-scrollbar-thumb {
    background: var(--surface-high);
    border-radius: 2px;
  }
  .chip {
    padding: 6px 14px;
    border-radius: 20px;
    border: 1px solid var(--surface-high);
    background: var(--surface);
    color: var(--text-dim);
    font-size: 13px;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
    -webkit-user-select: none;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .chip.active {
    background: var(--primary-dim);
    border-color: var(--primary);
    color: var(--primary);
  }
  .chip:active { transform: scale(0.95); }
  .chip .chip-icon {
    width: 14px;
    height: 14px;
    vertical-align: -2px;
    margin-right: 4px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .raw-badge {
    display: none;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--warning);
    color: var(--on-primary);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    align-self: center;
    flex-shrink: 0;
    animation: fadeInBadge 0.2s ease-out;
  }
  .raw-badge.visible { display: inline-block; }
  @keyframes fadeInBadge {
    from { opacity: 0; transform: scale(0.85); }
    to { opacity: 1; transform: scale(1); }
  }

  select.chip {
    appearance: none;
    -webkit-appearance: none;
    padding-right: 28px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23908caa' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
  }
  select.chip option {
    background: var(--surface);
    color: var(--text);
  }

  .rp-controls {
    transition: opacity 0.2s, max-height 0.3s;
  }
  .rp-controls.disabled {
    opacity: 0.35;
    pointer-events: none;
  }

  /* Context section */
  .context-section {
    padding: 0 20px;
    width: 100%;
    max-width: 640px;
    margin: 0 auto;
  }
  .context-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    cursor: pointer;
    color: var(--text-dim);
    /* POL-5 / Agent 4 §8.4 — Apollo SECTION-LABEL chrome. */
    font-size: 11.5px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    user-select: none;
  }
  .context-toggle svg {
    width: 14px; height: 14px;
    transition: transform 0.2s;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
  }
  .context-toggle.open svg { transform: rotate(90deg); }
  .context-toggle .badge {
    background: var(--primary-dim);
    color: var(--primary);
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 11px;
    margin-left: auto;
  }
  .context-body {
    display: none;
    padding-bottom: 10px;
  }
  .context-body.open { display: block; }
  .context-body textarea {
    width: 100%;
    min-height: 80px;
    max-height: 200px;
    background: var(--surface);
    border: 1px solid var(--surface-high);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: 14px;
    padding: 12px;
    resize: vertical;
    font-family: inherit;
    line-height: 1.5;
  }
  .context-body textarea::placeholder { color: var(--text-dim); }
  .context-body textarea:focus {
    outline: none;
    border-color: var(--primary);
  }

  /* Chat preview messages */
  .chat-msg {
    padding: 8px 12px;
    margin-bottom: 6px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .chat-msg.partner {
    background: rgba(156, 207, 216, 0.08);
    border-left: 3px solid var(--success);
  }
  .chat-msg.user-msg {
    background: rgba(196, 167, 231, 0.08);
    border-left: 3px solid var(--primary);
  }
  .chat-msg .chat-sender {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 2px;
    color: var(--text-dim);
  }
  .chat-msg.partner .chat-sender { color: var(--success); }
  .chat-msg.user-msg .chat-sender { color: var(--primary); }
  .chat-msg .chat-text {
    color: var(--text);
  }

  .main {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 20px;
    gap: 24px;
    flex: 1 0 auto;
  }

  .record-btn {
    width: 120px;
    height: 120px;
    border-radius: 50%;
    border: 3px solid var(--primary);
    background: var(--primary-dim);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    -webkit-user-select: none;
    user-select: none;
  }
  .record-btn:active { transform: scale(0.92); }
  /* POL-5 / Agent 4 §8.5 — Apollo amber glow replaces the rosé-pink pulse.
   * Layered shadow gives an ember-aura at rest and a soft outer halo;
   * pulse-amber animates scale rather than shadow-radius (cheaper on
   * mobile compositors, less flicker). */
  .record-btn.recording {
    border-color: var(--primary);
    box-shadow:
      0 0 0 4px rgba(255, 182, 72, 0.15),
      0 0 24px -4px rgba(255, 182, 72, 0.5),
      inset 0 1px 0 rgba(255, 182, 72, 0.1);
    animation: pulse-amber 1.4s ease-in-out infinite;
  }
  .record-btn.processing {
    border-color: var(--text-dim);
    background: var(--surface);
    pointer-events: none;
  }
  .record-btn svg {
    width: 40px;
    height: 40px;
    fill: var(--primary);
    transition: fill 0.3s;
  }
  .record-btn.recording svg { fill: var(--primary); }
  .record-btn.processing svg { fill: var(--text-dim); }
  /* Phase 2.5A: subtle scale while hold-to-record is engaged so the user
     feels the press is registered. Combines with .recording's pulse-amber. */
  .record-btn.holding { transform: scale(0.95); box-shadow: 0 0 0 4px rgba(255, 182, 72, 0.35); }

  @keyframes pulse-amber {
    0%, 100% { transform: scale(1); }
    50%      { transform: scale(1.04); }
  }
  @media (prefers-reduced-motion: reduce) {
    .record-btn.recording { animation: none; }
  }

  .record-label {
    font-size: 14px;
    color: var(--text-dim);
    text-align: center;
  }
  .record-sub {
    font-size: 12px;
    color: var(--text-dim);
    text-align: center;
    min-height: 14px;
    margin-top: 2px;
    opacity: 0;
    transition: opacity 0.2s;
  }
  .record-sub.visible { opacity: 1; }

  .vu-meter {
    width: 160px;
    height: 32px;
    display: none;
    opacity: 0;
    transition: opacity 0.25s;
  }
  .vu-meter.visible { display: block; opacity: 1; }

  .result-area {
    width: 100%;
    max-width: 600px;
    display: none;
  }
  .result-area.visible { display: block; }

  .result-box {
    background: var(--surface);
    border-radius: var(--radius);
    padding: 16px;
    min-height: 80px;
    max-height: 300px;
    overflow-y: auto;
    font-size: 15px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    border: 1px solid var(--surface-high);
    color: var(--text);
    font-family: inherit;
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    outline: none;
    display: block;
  }
  .result-box:focus {
    border-color: var(--primary);
  }

  /* Model attribution caption under the result box. */
  .result-meta {
    margin-top: 6px;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: .02em;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .result-meta .model-chip {
    color: var(--primary);
    font-variant-numeric: tabular-nums;
  }
  .result-meta .model-chip.fallback {
    color: var(--warning, #FFB648);
  }

  .result-actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .btn {
    padding: 8px 18px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--surface-high);
    background: var(--surface);
    color: var(--text);
    font-size: 14px;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn:active { transform: scale(0.95); }
  .btn:disabled {
    opacity: 0.4;
    pointer-events: none;
  }
  .btn.primary {
    background: var(--primary);
    color: var(--on-primary);
    border-color: var(--primary);
    font-weight: 600;
  }
  .btn.warning {
    border-color: var(--warning);
    color: var(--warning);
  }
  .btn svg { width: 16px; height: 16px; }

  .toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: var(--surface-high);
    color: var(--text);
    padding: 10px 20px;
    border-radius: var(--radius-sm);
    font-size: 14px;
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    z-index: 100;
    pointer-events: none;
  }
  .toast.show { transform: translateX(-50%) translateY(0); }

  .cert-banner {
    display: none;
    margin: 8px 16px 0;
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    background: var(--surface-high);
    color: var(--text);
    font-size: 13px;
    border-left: 3px solid var(--primary);
  }
  .cert-banner.show { display: block; }
  .cert-banner.urgent { border-left-color: var(--error); }

  /* Phase 1: ST follow banner — signals phone is tracking ST's current context. */
  .st-follow-banner {
    margin: 8px 16px 0;
    padding: 8px 14px;
    border-radius: var(--radius-sm);
    background: var(--surface-high);
    color: var(--text);
    font-size: 13px;
    border-left: 3px solid var(--primary);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .st-follow-banner.fresh { border-left-color: var(--primary); }
  .st-follow-banner.stale { border-left-color: #e6a756; color: var(--text-dim); }
  .st-follow-banner.override { border-left-color: #7a7a9a; }
  .st-follow-main { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
  .st-follow-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .st-follow-icon { flex-shrink: 0; }
  .st-follow-btn {
    background: var(--primary);
    color: #121218;
    border: none;
    padding: 5px 12px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .st-follow-btn:hover { filter: brightness(1.1); }

  /* Phase 2.5C: last-result pill.
     Sticky so it stays visible as the user scrolls down through mode chips
     and the result area — the whole point is quick re-copy/re-send without
     hunting for it. Z-index above the record button area. */
  .last-result-pill {
    position: sticky;
    top: 8px;
    z-index: 20;
    margin: 8px 16px 0;
    padding: 8px 10px 8px 12px;
    border-radius: 999px;
    background: var(--surface-high);
    border: 1px solid var(--primary);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-dim);
    overflow: hidden;
  }
  .last-result-pill::before {
    /* TTL progress bar — animated width controlled by inline style */
    content: "";
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: var(--ttl-pct, 100%);
    background: linear-gradient(90deg, rgba(196,167,231,0.18), rgba(196,167,231,0));
    pointer-events: none;
    transition: width 1s linear;
  }
  .pill-preview {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
    color: var(--text);
    position: relative;
    z-index: 1;
  }
  .pill-preview:hover { color: var(--primary); }
  .pill-actions { display: flex; gap: 4px; flex-shrink: 0; position: relative; z-index: 1; }
  .pill-btn {
    background: transparent;
    border: none;
    padding: 4px 6px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    color: var(--text-dim);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .pill-btn:hover { background: rgba(255,255,255,0.06); color: var(--text); }
  .pill-btn svg { width: 14px; height: 14px; }
  .pill-btn.pill-close:hover { color: var(--error); }
  .pill-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .pill-btn:disabled:hover { background: transparent; color: var(--text-dim); }

  @keyframes pillSlideIn {
    from { transform: translateY(-8px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }
  .last-result-pill.entering { animation: pillSlideIn 0.25s ease-out; }

  /* Phase 1: result-view accordion rows (Raw / Cleaned / Formatted). */
  .result-rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 8px;
  }
  .result-row {
    border: 1px solid var(--surface-high);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .result-row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    background: var(--surface-high);
    font-size: 12px;
    color: var(--text-dim);
    cursor: pointer;
    user-select: none;
  }
  .result-row-head .label { font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; font-size: 11px; }
  .result-row-head .eye { opacity: 0.7; }
  .result-row-body {
    display: none;
    padding: 8px 10px;
    font-size: 13px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-dim);
    max-height: 200px;
    overflow-y: auto;
  }
  .result-row.open .result-row-body { display: block; }
  .result-row.open .result-row-head .eye { opacity: 1; }

  .error-text { color: var(--error); font-size: 14px; text-align: center; }

  /* Duration display */
  .duration {
    font-size: 28px;
    font-weight: 300;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    letter-spacing: 2px;
  }
  .duration.recording-dur { color: var(--recording); }

  /* Session log */
  .log-section {
    width: 100%;
    max-width: 640px;
    margin: 0 auto;
    padding: 0 20px 20px;
  }
  .log-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 0;
    cursor: pointer;
    color: var(--text-dim);
    /* POL-5 / Agent 4 §8.4 — Apollo SECTION-LABEL chrome. */
    font-size: 11.5px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    user-select: none;
    border-top: 1px solid var(--surface-high);
  }
  .log-toggle svg {
    width: 14px; height: 14px;
    transition: transform 0.2s;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
  }
  .log-toggle.open svg { transform: rotate(90deg); }
  .log-toggle .badge {
    background: var(--primary-dim);
    color: var(--primary);
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 11px;
  }
  .log-toggle .clear-log {
    color: var(--error);
    font-size: 12px;
    cursor: pointer;
    padding: 2px 8px;
  }
  .log-toggle .log-header-actions {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .log-toggle .download-log {
    color: var(--text-dim);
    font-size: 12px;
    cursor: pointer;
    padding: 2px 8px;
    background: transparent;
    border: 1px solid var(--surface-high);
    border-radius: var(--radius-sm);
  }
  .log-toggle .download-log:hover:not(:disabled) {
    color: var(--primary);
    border-color: var(--primary);
  }
  .log-toggle .download-log:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .log-body {
    display: none;
    max-height: 300px;
    overflow-y: auto;
  }
  .log-body.open { display: block; }
  .log-entry {
    padding: 8px 12px;
    margin-bottom: 6px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
    word-break: break-word;
    position: relative;
  }
  .log-entry.context {
    background: rgba(156, 207, 216, 0.08);
    border-left: 3px solid var(--success);
  }
  .log-entry.user {
    background: rgba(196, 167, 231, 0.08);
    border-left: 3px solid var(--primary);
  }
  .log-entry .log-role {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 2px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .log-entry .log-role-ts {
    font-weight: 400;
    text-transform: none;
    letter-spacing: 0;
    color: var(--text-dim);
    font-size: 10px;
  }
  .log-entry.context .log-role { color: var(--success); }
  .log-entry.user .log-role { color: var(--primary); }
  .log-entry .log-text {
    white-space: pre-wrap;
    cursor: pointer;
  }
  .log-entry:not(.expanded) .log-text {
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .log-entry-actions {
    display: flex;
    gap: 4px;
    margin-top: 6px;
    justify-content: flex-end;
    opacity: 0.55;
    transition: opacity 0.15s;
  }
  .log-entry:hover .log-entry-actions,
  .log-entry.expanded .log-entry-actions {
    opacity: 1;
  }
  .log-action-btn {
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-dim);
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
  .log-action-btn:hover {
    background: rgba(255,255,255,0.05);
    border-color: var(--surface-high);
    color: var(--text);
  }
  .log-action-btn.regen:hover { color: var(--primary); border-color: var(--primary); }
  .log-action-btn.delete:hover { color: var(--error); border-color: var(--error); }
  .log-action-btn.star { color: var(--text-dim); }
  .log-action-btn.star:hover { color: var(--primary); border-color: var(--primary); }
  .log-action-btn.star.active { color: var(--primary); }
  .log-action-btn.star.active:hover { color: var(--primary); border-color: var(--primary); }
  .log-entry.starred.user {
    background: rgba(196, 167, 231, 0.15);
    border-left: 4px solid var(--primary);
  }
  .log-action-btn svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .log-action-btn[disabled] {
    opacity: 0.4;
    cursor: wait;
  }
  .log-action-btn.regen.spinning svg {
    animation: spin 1s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  @media (pointer: coarse), (max-width: 900px) {
    .log-action-btn { width: 32px; height: 32px; }
    .log-action-btn svg { width: 16px; height: 16px; }
  }
  .log-empty {
    color: var(--text-dim);
    font-size: 13px;
    padding: 12px;
    text-align: center;
  }

  .char-opt {
    padding: 8px 12px;
    font-size: 13px;
    color: var(--text);
    cursor: pointer;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .char-opt:hover, .char-opt.highlighted {
    background: var(--primary-dim);
    color: var(--primary);
  }
  .char-opt:last-child { border-bottom: none; }
  .char-none { color: var(--text-dim); font-style: italic; }

  /* ─── Mobile / Touch ──────────────────────────────── */
  @media (pointer: coarse), (max-width: 900px) {
    .header {
      padding: 14px 16px;
      position: sticky;
      top: 0;
      background: var(--bg);
      z-index: 20;
      border-bottom: 1px solid var(--surface-high);
    }

    .controls {
      padding: 10px 16px;
      gap: 6px;
    }

    .chip {
      padding: 10px 16px;
      font-size: 14px;
      border-radius: 24px;
      min-height: 40px;
      display: inline-flex;
      align-items: center;
    }

    select.chip {
      padding-right: 32px;
    }

    #characterSearch {
      width: 180px !important;
      min-height: 40px;
    }

    #charDropdown {
      width: 280px !important;
      max-height: 320px !important;
    }

    .char-opt {
      padding: 12px 14px;
      font-size: 14px;
    }

    .main {
      padding: 28px 20px;
      gap: 24px;
      flex: 1;
    }

    .record-btn {
      width: 160px;
      height: 160px;
      border-width: 3.5px;
    }
    .record-btn svg {
      width: 56px;
      height: 56px;
    }

    .duration {
      font-size: 42px;
      letter-spacing: 4px;
    }

    .record-label {
      font-size: 17px;
      margin-top: 4px;
    }

    .result-area {
      max-width: 100%;
    }

    .result-box {
      font-size: 16px;
      padding: 16px;
      max-height: 50vh;
      border-radius: var(--radius-sm);
    }

    .result-actions {
      gap: 8px;
    }
    .result-actions .btn {
      padding: 12px 20px;
      font-size: 15px;
      min-height: 44px;
      border-radius: var(--radius-sm);
    }

    .context-section {
      padding: 0 16px;
      max-width: 100%;
    }
    .context-toggle {
      padding: 12px 0;
      font-size: 14px;
      min-height: 44px;
    }
    .context-body textarea {
      font-size: 15px;
      min-height: 100px;
      padding: 14px;
    }

    #chatPreview {
      max-height: 40vh;
    }
    .chat-msg {
      padding: 10px 14px;
      font-size: 14px;
    }

    .log-section {
      padding: 0 16px 20px;
      max-width: 100%;
      flex: 0 0 auto;
    }
    .log-toggle {
      padding: 14px 0;
      font-size: 14px;
      min-height: 44px;
    }
    .log-body {
      max-height: 50vh;
    }
    .log-entry {
      padding: 10px 14px;
      font-size: 14px;
    }

    .toast {
      bottom: calc(16px + env(safe-area-inset-bottom, 0));
      font-size: 15px;
      padding: 12px 24px;
    }
  }

  /* Settings / VAD / Vocab */
  .settings-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    font-size: 13px;
    color: var(--text-dim);
    flex-wrap: wrap;
  }
  .settings-row label {
    min-width: 140px;
  }
  .settings-row input[type="range"] {
    flex: 1;
    min-width: 100px;
    accent-color: var(--primary);
  }
  .settings-row input[type="number"] {
    width: 72px;
    background: var(--surface);
    border: 1px solid var(--surface-high);
    border-radius: var(--radius-sm);
    color: var(--text);
    padding: 6px 8px;
    font-size: 13px;
    font-family: inherit;
  }
  .settings-row input[type="number"]:focus {
    outline: none;
    border-color: var(--primary);
  }
  .settings-row input[type="checkbox"] {
    accent-color: var(--primary);
    width: 16px;
    height: 16px;
  }
  .settings-row .value-readout {
    color: var(--text);
    min-width: 44px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .vocab-row {
    display: grid;
    grid-template-columns: 1fr 1fr auto;
    gap: 8px;
    align-items: center;
    padding: 6px 0;
    font-size: 13px;
    border-bottom: 1px solid var(--surface-high);
  }
  .vocab-row:last-of-type { border-bottom: none; }
  .vocab-row .vocab-correct {
    color: var(--text);
    font-weight: 500;
    word-break: break-word;
  }
  .vocab-row .vocab-aliases {
    color: var(--text-dim);
    font-size: 12px;
    word-break: break-word;
  }
  .vocab-row .vocab-del {
    background: transparent;
    border: 1px solid var(--error);
    color: var(--error);
    border-radius: var(--radius-sm);
    font-size: 11px;
    padding: 4px 8px;
    cursor: pointer;
  }
  .vocab-row .vocab-del:active { transform: scale(0.95); }

  .vocab-add {
    display: grid;
    grid-template-columns: 1fr 1fr auto;
    gap: 8px;
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--surface-high);
  }
  .vocab-add input[type="text"] {
    background: var(--surface);
    border: 1px solid var(--surface-high);
    border-radius: var(--radius-sm);
    color: var(--text);
    padding: 8px 10px;
    font-size: 13px;
    font-family: inherit;
    min-width: 0;
  }
  .vocab-add input[type="text"]:focus {
    outline: none;
    border-color: var(--primary);
  }
  .vocab-add button {
    background: var(--primary);
    color: var(--on-primary);
    border: none;
    border-radius: var(--radius-sm);
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .vocab-add button:active { transform: scale(0.95); }
  .vocab-empty {
    color: var(--text-dim);
    font-style: italic;
    padding: 8px 0;
    font-size: 13px;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* MVP-23 / POL-15 — Apollo modal primitives + privacy badge.
   * Modal infra is shared by privacy-modal and cheatsheet-modal. Sharp
   * 2px-radius cards on amber-bordered translucent backdrop. */
  .privacy-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 32px;
    padding: 4px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--success);
    background: var(--surface);
    color: var(--text);
    font-size: 11.5px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
    transition: background 0.15s, border-color 0.15s;
  }
  .privacy-badge:hover { background: var(--surface-high); }
  .privacy-badge.alert {
    border-color: var(--error);
    color: var(--error);
    animation: pulse-amber 1.4s ease-in-out infinite;
  }
  .privacy-badge .glyph { font-size: 12px; line-height: 1; }
  /* Tap target ≥40px on phones; padding pads visually but a11y-only width
   * is set via min-width when the badge is the only header chip. */
  @media (max-width: 480px) {
    .privacy-badge { min-width: 84px; min-height: 40px; }
  }
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 3, 2, 0.78);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
    z-index: 100;
  }
  .modal-backdrop.open { display: flex; }
  .modal-card {
    width: 100%;
    max-width: 540px;
    max-height: 85vh;
    overflow-y: auto;
    background: var(--surface);
    border: 1px solid var(--primary);
    border-radius: var(--radius-sm);
    color: var(--text);
    box-shadow:
      0 8px 24px -8px rgba(0, 0, 0, 0.6),
      0 2px 6px rgba(0, 0, 0, 0.4);
  }
  .modal-card.alert { border-color: var(--error); }
  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 18px;
    border-bottom: 1px solid var(--surface-high);
    font-size: 12px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--text-dim);
  }
  .modal-card.alert .modal-head { color: var(--error); }
  .modal-close {
    background: transparent;
    border: 0;
    color: var(--text-dim);
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
    padding: 4px 8px;
  }
  .modal-close:hover { color: var(--primary); }
  .modal-body { padding: 16px 18px; font-size: 13px; }
  .modal-body dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 14px;
    row-gap: 6px;
    margin-bottom: 14px;
  }
  .modal-body dt { color: var(--text-dim); }
  .modal-body dd { color: var(--text); word-break: break-all; }
  .modal-body .audit-section-label {
    margin-top: 10px;
    margin-bottom: 6px;
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--text-dim);
  }
  .audit-row {
    display: grid;
    grid-template-columns: 70px 50px 1fr 60px;
    column-gap: 8px;
    padding: 4px 0;
    font-size: 11.5px;
    border-top: 1px solid var(--surface-high);
  }
  .audit-row:first-child { border-top: 0; }
  .audit-row .a-host.local { color: var(--success); }
  .audit-row .a-host.remote { color: var(--error); font-weight: 600; }
  .audit-row .a-ts, .audit-row .a-method, .audit-row .a-lat {
    color: var(--text-dim);
  }
  .modal-body .alert-banner {
    padding: 10px 12px;
    margin-bottom: 12px;
    border: 1px solid var(--error);
    background: rgba(255, 90, 78, 0.08);
    color: var(--error);
    font-size: 12px;
    border-radius: var(--radius-sm);
  }
  .modal-foot {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding: 12px 18px;
    border-top: 1px solid var(--surface-high);
  }
  .modal-foot .btn {
    flex: 1;
    padding: 8px 12px;
    border: 1px solid var(--surface-high);
    background: var(--surface);
    color: var(--text-dim);
    font-size: 12px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .modal-foot .btn:hover { color: var(--primary); border-color: var(--primary); }

  /* POL-15 — voice-command cheatsheet overlay. Reuses .modal-* infra
   * from MVP-23; adds only body styling for the preformatted command
   * grid + the "soon" tag that flags voice grammar as not-yet-shipped. */
  .help-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px; height: 32px;
    margin-left: 8px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--surface-high);
    background: var(--surface);
    color: var(--text-dim);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    user-select: none;
  }
  .help-btn:hover { color: var(--primary); border-color: var(--primary); }
  .cheatsheet pre {
    font-family: inherit;
    font-size: 12px;
    color: var(--text);
    line-height: 1.7;
    white-space: pre-wrap;
  }
  .cheatsheet .note {
    margin-top: 12px;
    color: var(--text-dim);
    font-size: 11.5px;
    font-style: italic;
  }
  .cheatsheet .coming-soon {
    display: inline-block;
    padding: 1px 6px;
    border: 1px solid var(--warning);
    color: var(--warning);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border-radius: var(--radius-sm);
    margin-left: 8px;
  }
</style>
</head>
<body>

<div class="header">
  <h1>Dictate</h1>
  <div class="status">
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText">Ready</span>
  </div>
  <!-- MVP-23: privacy indicator. Tap opens the audit modal; flips to
       .alert (red border, ember pulse) if /audit/network reports a
       non-loopback hit in the last 60s. -->
  <div class="privacy-badge" id="privacyBadge" role="button" tabindex="0"
       onclick="openPrivacyModal()" onkeydown="if(event.key==='Enter'||event.key===' ')openPrivacyModal()"
       title="Tap to view network audit log"
       aria-label="Privacy: local-only. Tap for details.">
    <span class="glyph">&#128274;</span>
    <span>LOCAL</span>
  </div>
  <!-- POL-15: voice-edit cheatsheet trigger. Even before voice grammar
       lands, surface the affordance so users expect commands to exist. -->
  <button class="help-btn" id="helpBtn" type="button"
          onclick="openCheatsheetModal()"
          aria-label="Voice command cheatsheet">?</button>
</div>

<!-- MVP-23: privacy / audit modal. -->
<div class="modal-backdrop" id="privacyBackdrop"
     onclick="if(event.target===this)closePrivacyModal()"
     role="dialog" aria-modal="true" aria-labelledby="privacyTitle">
  <div class="modal-card" id="privacyCard">
    <div class="modal-head">
      <span id="privacyTitle">&#128274; Local-only network audit</span>
      <button class="modal-close" type="button" onclick="closePrivacyModal()" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body" id="privacyBody">
      <div id="privacyAlertBanner" class="alert-banner" style="display:none"></div>
      <dl>
        <dt>Audio</dt><dd>whisper.cpp (HIP/ROCm) &middot; localhost</dd>
        <dt>Transcript</dt><dd>in-RAM only, wiped on restart</dd>
        <dt>LLM cleanup</dt><dd id="privacyCleanupHost">&hellip;</dd>
        <dt>Phone &harr; PC</dt><dd id="privacyPhonePc">&hellip;</dd>
      </dl>
      <div class="audit-section-label">Network calls in last 60s</div>
      <div id="auditRows"><div style="color:var(--text-dim)">&hellip;</div></div>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" onclick="toggleAuditAll()" id="auditAllBtn">View full audit log</button>
      <button class="btn" type="button" onclick="explainPrivacy()">What does this mean?</button>
    </div>
  </div>
</div>

<!-- POL-15: voice-command cheatsheet modal. Voice grammar is live when
     voiceCommandsEnabled is on; this lists the current v1 command set. -->
<div class="modal-backdrop" id="cheatsheetBackdrop"
     onclick="if(event.target===this)closeCheatsheetModal()"
     role="dialog" aria-modal="true" aria-labelledby="cheatsheetTitle">
  <div class="modal-card cheatsheet">
    <div class="modal-head">
      <span id="cheatsheetTitle">Voice commands <span class="coming-soon">live</span></span>
      <button class="modal-close" type="button" onclick="closeCheatsheetModal()" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body">
<pre>"scratch that"      &mdash; undo last utterance
"append: &lt;words&gt;"   &mdash; append even if mode=replace
"replace: &lt;words&gt;"  &mdash; replace even if mode=append
"send" / "send it"  &mdash; fire after commit
"clear"             &mdash; empty the textarea</pre>
      <div class="note">
        Held mic = dictate while held, release to send.<br>
        Tap mic = toggle (tap again to send).
      </div>
      <div class="note" style="margin-top:14px;color:var(--warning)">
        Voice grammar is live when enabled in SillyTavern settings. Commands
        are dispatched through the ST extension that is subscribed to SSE.
      </div>
    </div>
  </div>
</div>


<div class="cert-banner" id="certBanner" role="status" aria-live="polite"></div>

<!-- Phase 1: ST follow banner. Hidden by default; shown once /state is fetched. -->
<div class="st-follow-banner" id="stFollowBanner" style="display:none" role="status" aria-live="polite">
  <div class="st-follow-main">
    <span class="st-follow-icon" id="stFollowIcon">📡</span>
    <span class="st-follow-text" id="stFollowText">Following ST</span>
  </div>
  <div class="st-follow-actions">
    <button class="st-follow-btn" id="stResyncBtn" onclick="resyncWithST()" style="display:none">Re-sync</button>
  </div>
</div>

<!-- Phase 2.5C: last-result pill. Persistent for 60s after a successful take,
     giving quick re-copy / re-send access even after you keep dictating. -->
<div class="last-result-pill" id="lastResultPill" style="display:none" role="status" aria-live="polite">
  <span class="pill-preview" id="pillPreview" onclick="expandLastResult()" title="Tap to restore to editor">…</span>
  <div class="pill-actions">
    <button class="pill-btn" id="pillCopyBtn" onclick="pillCopy()" title="Re-copy">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
    </button>
    <button class="pill-btn" id="pillSendBtn" onclick="pillSend()" title="Re-send to ST">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
    </button>
    <button class="pill-btn pill-close" onclick="dismissLastResult()" title="Dismiss">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
</div>

<div class="controls">
  <select class="chip" id="formatterModelSelect" onchange="setFormatterModel(this.value)">
    <option value="">OmniRoute Auto</option>
  </select>
  <select class="chip" id="modelSelect">
    <option value="large-v3-turbo">Large Turbo</option>
    <option value="medium">Medium</option>
    <option value="small">Small</option>
    <option value="base">Base</option>
  </select>
</div>
<div class="controls mode-chips" id="modeChips" aria-label="Formatting mode">
  <span class="raw-badge" id="rawBadge" title="Formatting was skipped">raw</span>
</div>
<div class="controls rp-controls" id="rpControls">
  <div style="position:relative;display:inline-block">
    <input class="chip" id="characterSearch" type="text" placeholder="Search character..."
           autocomplete="off" onfocus="showCharDropdown()" oninput="filterCharacters()"
           style="width:160px;cursor:text">
    <input type="hidden" id="characterValue" value="none">
    <div id="charDropdown" style="display:none;position:absolute;top:100%;left:0;right:0;
         background:var(--surface);border:1px solid var(--surface-high);border-radius:var(--radius-sm);
         max-height:240px;overflow-y:auto;z-index:50;margin-top:4px;box-shadow:0 8px 24px rgba(0,0,0,0.4)">
    </div>
  </div>
  <select class="chip" id="personaSelect" onchange="onPersonaChange(this.value)">
    <option value="none">No Persona</option>
  </select>
  <div class="chip" id="rulesChip" onclick="toggleRules()">Rules</div>
  <div class="chip" id="proseChip" onclick="toggleProse()">Prose</div>
</div>

<!-- Chat Source section -->
<div class="context-section" id="chatSourceSection">
  <div class="context-toggle" id="chatSourceToggle" onclick="toggleChatSource()">
    <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
    Chat Context
    <span class="badge" id="chatSourceBadge">Manual</span>
  </div>
  <div class="context-body" id="chatSourceBody">
    <!-- Source selector -->
    <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <div class="chip active" id="srcManual" onclick="setChatSource('manual')">Manual</div>
      <div class="chip" id="srcAuto" onclick="setChatSource('auto')">Auto</div>
      <select class="chip" id="chatSelect" onchange="setChatSource(this.value)" style="max-width:200px">
        <option value="" disabled selected>Pick a chat...</option>
      </select>
    </div>

    <!-- Manual paste (shown when source=manual) -->
    <div id="manualContextArea">
      <textarea id="contextInput" placeholder="Paste the other character's last message here..."></textarea>
    </div>

    <!-- Chat preview (shown when source=auto or specific chat) -->
    <div id="chatPreviewArea" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;color:var(--text-dim)" id="chatPreviewLabel">Loading...</span>
        <span style="font-size:12px;color:var(--primary);cursor:pointer" onclick="refreshChatContext()">Refresh</span>
      </div>
      <div id="chatPreview" style="max-height:250px;overflow-y:auto"></div>
    </div>
  </div>
</div>

<div class="main">
  <div class="duration" id="duration">0:00</div>

  <button class="record-btn" id="recordBtn" onclick="toggleRecord()">
    <svg id="micIcon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
      <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
    </svg>
    <svg id="stopIcon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" style="display:none">
      <rect x="6" y="6" width="12" height="12" rx="2"/>
    </svg>
    <svg id="spinnerIcon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" style="display:none;animation:spin 1s linear infinite">
      <path d="M12 2a10 10 0 0 1 10 10h-2a8 8 0 0 0-8-8V2z" opacity="0.8"/>
    </svg>
  </button>

  <canvas class="vu-meter" id="vuMeter" width="160" height="32" aria-hidden="true"></canvas>

  <div class="record-label" id="recordLabel">Tap or hold to record</div>
  <div class="record-sub" id="recordSub"></div>

  <div class="result-area" id="resultArea">
    <div class="result-rows" id="resultRows" style="display:none">
      <div class="result-row" id="rowRaw">
        <div class="result-row-head" onclick="toggleResultRow('rowRaw')">
          <span class="label">Raw</span>
          <span class="eye">👁</span>
        </div>
        <div class="result-row-body" id="rowRawBody"></div>
      </div>
      <div class="result-row" id="rowCleaned">
        <div class="result-row-head" onclick="toggleResultRow('rowCleaned')">
          <span class="label">Cleaned</span>
          <span class="eye">👁</span>
        </div>
        <div class="result-row-body" id="rowCleanedBody"></div>
      </div>
    </div>
    <div class="repair-card" id="repairCard" style="display:none;margin:8px 0;padding:10px;border:1px solid var(--surface-high);border-radius:var(--radius-sm);background:rgba(255,255,255,0.03)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px">
        <span style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.08em">Repair trace</span>
        <span style="font-size:11px;color:var(--text-dim)">RAM only · vocab persists only on Accept</span>
      </div>
      <div id="repairStages" style="display:flex;flex-direction:column;gap:6px"></div>
    </div>
    <textarea class="result-box" id="resultText" rows="6" spellcheck="true" placeholder="Transcription appears here. Edit it, then copy."></textarea>
    <div class="result-meta" id="resultMeta" style="display:none"></div>
    <div class="result-actions">
      <button class="btn" onclick="clearResult()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
        Clear
      </button>
      <button class="btn warning" id="regenBtn" onclick="regenerate()" style="display:none">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
        Regen
      </button>
      <button class="btn" id="copyBtn" onclick="copyResult()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        Copy
      </button>
      <button class="btn primary" id="sendToStBtn" onclick="sendToST()" title="Send directly to SillyTavern">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
        Send to ST
      </button>
    </div>
    <div class="result-actions-sub" id="resultActionsSub" style="margin-top:6px;display:flex;justify-content:flex-end;align-items:center;gap:8px;font-size:12px;color:var(--text-dim)">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none">
        <input type="checkbox" id="autoSendToggle" onchange="toggleAutoSend(this.checked)" style="cursor:pointer">
        <span>Auto-send in ST</span>
      </label>
    </div>
  </div>
</div>

<!-- VAD settings (collapsible) -->
<div class="log-section">
  <div class="log-toggle" id="vadToggle" onclick="toggleCollapsible('vad')">
    <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
    Voice Detection
    <span class="badge" id="vadBadge">auto-stop</span>
  </div>
  <div class="log-body" id="vadBody">
    <div class="settings-row">
      <label for="vadEnabled">Auto-stop on silence</label>
      <input type="checkbox" id="vadEnabled">
    </div>
    <div class="settings-row">
      <label for="vadThreshold">Silence threshold (dB)</label>
      <input type="range" id="vadThreshold" min="-60" max="-20" step="1" value="-40">
      <span class="value-readout" id="vadThresholdVal">-40</span>
    </div>
    <div class="settings-row">
      <label for="vadDuration">Silence duration (s)</label>
      <input type="range" id="vadDuration" min="0.5" max="5.0" step="0.1" value="2.0">
      <span class="value-readout" id="vadDurationVal">2.0</span>
    </div>
  </div>
</div>

<!-- Vocab quick panel (collapsible) -->
<div class="log-section">
  <div class="log-toggle" id="vocabToggle" onclick="toggleCollapsible('vocab')">
    <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
    Custom Vocabulary
    <span class="badge" id="vocabBadge">0</span>
  </div>
  <div class="log-body" id="vocabBody">
    <div id="vocabList">
      <div class="vocab-empty">Loading…</div>
    </div>
    <div class="vocab-add">
      <input type="text" id="vocabCorrect" placeholder="Correct form (e.g. Elara)" autocomplete="off">
      <input type="text" id="vocabAliases" placeholder="Aliases, comma-separated" autocomplete="off">
      <button type="button" onclick="addVocabEntry()">Add</button>
    </div>
  </div>
</div>

<!-- Session log (collapsible) -->
<div class="log-section">
  <div class="log-toggle" id="logToggle" onclick="toggleLog()">
    <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
    Session Log
    <span class="badge" id="logBadge">0</span>
    <span class="log-header-actions">
      <button type="button" class="download-log" id="downloadSessionBtn"
              onclick="event.stopPropagation(); downloadSession()"
              aria-label="Download session as markdown" disabled>Download</button>
      <button type="button" class="download-log" id="starFilterBtn"
              onclick="event.stopPropagation(); toggleStarFilter()"
              aria-label="Show starred only" title="Toggle starred-only filter">&#9733;</button>
      <span class="clear-log" onclick="event.stopPropagation(); clearSession(event)" title="Clear unstarred. Shift+click to clear all.">Clear</span>
    </span>
  </div>
  <div class="log-body" id="logBody">
    <div id="logFilterStrip" style="display:none; padding:6px 10px; font-size:12px; color:var(--text-dim); border-bottom:1px solid var(--surface-high)">
      <span id="logFilterText">Filtered to current chat</span>
      <a href="#" onclick="event.preventDefault(); toggleChatFilter()" style="color:var(--primary); margin-left:6px" id="logFilterToggle">view all</a>
    </div>
    <div class="log-empty" id="logEmpty">No entries yet. Dictate something with RP+ to start building context.</div>
    <div id="logEntries"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// Safe phone bootstrap. Pairing URLs carry only a short-lived one-time code;
// the durable bearer is returned once to this same-origin page and kept in
// sessionStorage for the existing authenticated fetch/EventSource behavior.
(function () {
  const params = new URLSearchParams(location.search);
  const pairingCode = params.get('pair') || '';
  let TOKEN = sessionStorage.getItem('dictationToken') || '';
  const _origFetch = window.fetch.bind(window);
  window.calliopeAuthStatus = {
    hasToken: !!TOKEN,
    tokenSource: TOKEN ? 'session' : (pairingCode ? 'pairing' : 'missing'),
    unauthorized: false,
    lastStatus: 0,
    lastError: '',
  };
  if (pairingCode) {
    try {
      params.delete('pair');
      const clean = location.pathname + (params.toString() ? '?' + params.toString() : '') + location.hash;
      history.replaceState(null, '', clean);
    } catch (e) { /* best-effort URL scrub */ }
  }
  const bootstrap = pairingCode
    ? _origFetch('/pair/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: pairingCode }),
      }).then(async (resp) => {
        window.calliopeAuthStatus.lastStatus = resp.status;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.token) {
          window.calliopeAuthStatus.lastError = data.code || 'pairing_failed';
          try { sessionStorage.removeItem('dictationToken'); } catch (e) {}
          TOKEN = '';
          throw new Error(window.calliopeAuthStatus.lastError);
        }
        TOKEN = data.token;
        sessionStorage.setItem('dictationToken', TOKEN);
        window.calliopeAuthStatus.hasToken = true;
        window.calliopeAuthStatus.tokenSource = 'pairing';
      }).catch(() => {})
    : Promise.resolve();
  window.calliopeAuthReady = bootstrap;
  window.fetch = function (input, init) {
    return bootstrap.then(() => {
      init = init || {};
      const headers = new Headers(init.headers || {});
      if (TOKEN && !headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + TOKEN);
      init.headers = headers;
      return _origFetch(input, init).then((resp) => {
        window.calliopeAuthStatus.lastStatus = resp.status;
        if (resp.status === 401) {
          window.calliopeAuthStatus.unauthorized = true;
          window.calliopeAuthStatus.lastError = 'unauthorized';
          try { sessionStorage.removeItem('dictationToken'); } catch (e) {}
        }
        return resp;
      });
    });
  };
  const _OrigES = window.EventSource;
  window.EventSource = function (url, opts) {
    try {
      const u = new URL(url, location.origin);
      if (TOKEN && !u.searchParams.has('token')) u.searchParams.set('token', TOKEN);
      url = u.toString();
    } catch (e) { /* fall through with original url */ }
    return new _OrigES(url, opts);
  };
})();

let mediaRecorder = null;
let audioChunks = [];
let recording = false;
let rpMode = 0;                // derived legacy numeric mode (kept for any lingering callers)
let currentMode = '';          // active mode id (e.g. 'rp_enhance')
let currentProvider = 'omniroute';
let currentFormatterModel = '';
let lastUsedMode = '';         // mode id used for last transcription
let availableModes = [];       // [{id, label, icon, whisper_model, preset, use_persona, use_character, use_chat_context, pipeline}, ...]
let durationInterval = null;
let recordStart = 0;
let lastRawText = '';          // raw whisper output (before RP formatting)
let sessionLog = [];           // local mirror of server transcript
let selectedPersona = 'none';
let personaPinned = false;       // explicit URL/UI persona survives ST state refreshes
let selectedCharacter = 'none';
let useRules = false;
let proseFormat = false;
let chatSource = (function () {
  // Persist user's last choice; default to 'auto' so phone picks up
  // the active ST chat without a per-session toggle.
  try { return localStorage.getItem('dictation.chatSource') || 'auto'; }
  catch { return 'auto'; }
})();
let _lastSeenAiMessage = null;  // tracks /state lastAiMessage to detect changes for auto re-fetch
let chatContextText = '';      // assembled context string from chat history
let recentChats = [];

// Phase 1: ST state following
let stState = null;            // last /state snapshot from server
let stStateFresh = false;      // whether ST state is currently fresh
let stFollow = true;           // auto-adopt ST state; user override sets this false
let statePollTimer = null;     // poll /state every STATE_POLL_MS when visible
let statePollVisible = true;
let statePollLifecycleBound = false;
const STATE_POLL_MS = 10_000;
const FOLLOW_ST_FROM_URL = new URLSearchParams(location.search).get('follow') !== '0';

// Phase 2: Send-to-ST direct inject
let autoSendToST = localStorage.getItem('dictation.autoSend') === '1';

// Phase 2.5C: last-result pill
const PILL_TTL_MS = 60_000;
const PILL_PREVIEW_MAX = 60;
let lastResult = null;         // { text, raw, cleaned, mode, expiresAt }
let lastRepairTrace = null;    // POL-17: in-RAM raw→cleaned→final review state
let pillTimer = null;
let pillTickTimer = null;

// Embed mode (ST extension iframe)
const isEmbed = document.querySelector('meta[name="dictation-embed"]')?.content === '1';
const embedConfig = window.DICTATION_EMBED_CONFIG || {};
const transcriptionLanguage = String(
  embedConfig.language || new URLSearchParams(location.search).get('language') || 'auto'
).trim().toLowerCase() || 'auto';
const embedParentOrigin = (() => {
  try { return new URL(document.referrer).origin || ''; }
  catch { return ''; }
})();

function postToEmbedParent(payload) {
  if (!isEmbed || !embedParentOrigin) return;
  try { window.parent.postMessage(payload, embedParentOrigin); } catch {}
}

// VAD / level meter
let audioContext = null;
let analyserNode = null;
let micSourceNode = null;
let vadRafId = null;
let vadCheckInterval = null;
let silenceStartTs = 0;
let vadEnabled = true;
let vadThreshold = -40;        // dB
let vadSilenceDuration = 2.0;  // seconds
let vocabLoaded = false;       // lazy-load guard
let rawBadgeTimer = null;
let resultInputDebounce = null;

// Legacy rp mapping for any code path that still reads rpMode.
const MODE_TO_RP = {
  plain: 0,
  grammar_clean: 1,
  rp_format: 1,
  rp_enhance: 2,
  persona_pov: 2,
};

const btn = document.getElementById('recordBtn');
const label = document.getElementById('recordLabel');
const recordSub = document.getElementById('recordSub');
const formatterModelSelect = document.getElementById('formatterModelSelect');
const resultArea = document.getElementById('resultArea');
const resultText = document.getElementById('resultText');
const durationEl = document.getElementById('duration');
const regenBtn = document.getElementById('regenBtn');
const vuCanvas = document.getElementById('vuMeter');
const modeChips = document.getElementById('modeChips');
const rawBadge = document.getElementById('rawBadge');

// ─── Icon pack (inline SVGs, 16x16 strokes) ─────────────
const ICONS = {
  mic: '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 18v4"/><path d="M8 22h8"/>',
  broom: '<path d="M20 4 10 14"/><path d="m5 19 5-5"/><path d="m6 16 4 4"/><path d="M14 10l-7 8a3 3 0 0 0 4 4l8-7"/>',
  italic: '<line x1="19" y1="4" x2="10" y2="4"/><line x1="14" y1="20" x2="5" y2="20"/><line x1="15" y1="4" x2="9" y2="20"/>',
  sparkles: '<path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/><path d="m5.6 5.6 2.8 2.8"/><path d="m15.6 15.6 2.8 2.8"/><path d="m5.6 18.4 2.8-2.8"/><path d="m15.6 8.4 2.8-2.8"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  circle: '<circle cx="12" cy="12" r="5"/>',
};

function iconSvg(name) {
  const paths = ICONS[name] || ICONS.circle;
  return `<svg class="chip-icon" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
}

function normalizeProvider(provider) {
  return 'omniroute';
}

function setProvider(provider, opts) {
  currentProvider = 'omniroute';
}

function formatterModelLabel(model) {
  const labels = {
    'no-think/antigravity/claude-sonnet-5': 'Sonnet 5',
    'no-think/cc/claude-opus-4-8': 'Opus 4.8',
    'no-think/cc/claude-opus-4-6': 'Opus 4.6',
    'codex/gpt-5.6-sol-high': 'GPT 5.6 High',
  };
  return labels[model] || model;
}

function setFormatterModel(model, opts) {
  opts = opts || {};
  currentProvider = 'omniroute';
  currentFormatterModel = String(model || '');
  if (formatterModelSelect) formatterModelSelect.value = currentFormatterModel;
  if (opts.persist !== false) {
    try { localStorage.setItem('dictation.formatterModel', currentFormatterModel); } catch {}
  }
}

async function loadFormatterModels() {
  try {
    const resp = await fetch('/formatter-models');
    const data = await resp.json();
    const models = Array.isArray(data.models) ? data.models : [];
    formatterModelSelect.innerHTML = '<option value="">OmniRoute Auto</option>';
    models.forEach(model => {
      const opt = document.createElement('option');
      opt.value = model;
      opt.textContent = formatterModelLabel(model);
      formatterModelSelect.appendChild(opt);
    });
    const requested = embedConfig.formatter_model || localStorage.getItem('dictation.formatterModel') || '';
    setFormatterModel(models.includes(requested) ? requested : '', { persist: false });
  } catch {
    setFormatterModel('', { persist: false });
  }
}

// ─── Mode chips ─────────────────────────────────────────
async function loadModes() {
  try {
    const resp = await fetch('/modes');
    const data = await resp.json();
    availableModes = data.modes || [];
  } catch (err) {
    console.warn('Failed to load /modes:', err);
    availableModes = [];
  }

  if (!availableModes.length) {
    // Minimal fallback so UI isn't broken if backend is old / empty.
    availableModes = [
      { id: 'plain', label: 'Plain', icon: 'mic', use_persona: false, use_character: false, use_chat_context: false, pipeline: ['whisper'] },
      { id: 'rp_enhance', label: 'RP+', icon: 'sparkles', use_persona: true, use_character: true, use_chat_context: true, pipeline: ['whisper','rp_enhance'] },
    ];
  }

  renderModeChips();

  setProvider('omniroute', { persist: false });

  // Resolve initial mode: embed config → localStorage → rp_enhance → first.
  const stored = localStorage.getItem('dictation.mode') || '';
  const candidates = [embedConfig.mode, stored, 'rp_enhance'].filter(Boolean);
  let initial = '';
  for (const id of candidates) {
    if (availableModes.some(m => m.id === id)) { initial = id; break; }
  }
  if (!initial) initial = availableModes[0].id;
  setMode(initial, { persist: false });

  // Signal readiness to parent window (if embedded).
  postToEmbedParent({ type: 'dictation-ready' });
}

function renderModeChips() {
  // Remove existing chips (keep raw badge).
  Array.from(modeChips.querySelectorAll('.chip')).forEach(el => el.remove());
  availableModes.forEach(m => {
    const chip = document.createElement('div');
    chip.className = 'chip';
    chip.dataset.mode = m.id;
    chip.setAttribute('role', 'button');
    chip.setAttribute('tabindex', '0');
    chip.setAttribute('aria-label', m.label || m.id);
    chip.innerHTML = iconSvg(m.icon || 'mic') + escHtml(m.label || m.id);
    chip.addEventListener('click', () => setMode(m.id));
    chip.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setMode(m.id); }
    });
    // Insert before raw badge so badge stays at the end.
    modeChips.insertBefore(chip, rawBadge);
  });
}

function getMode(id) {
  return availableModes.find(m => m.id === id) || null;
}

function setMode(id, opts) {
  opts = opts || {};
  const prev = currentMode;
  let mode = getMode(id);
  if (!mode) {
    // Unknown mode → fall back to rp_enhance, then first.
    mode = getMode('rp_enhance') || availableModes[0];
    if (!mode) return;
    id = mode.id;
  }
  currentMode = id;
  rpMode = MODE_TO_RP[id] ?? 0;

  // Persist (skip during initial load).
  if (opts.persist !== false) {
    try { localStorage.setItem('dictation.mode', id); } catch {}
  }

  // Update chip active state.
  modeChips.querySelectorAll('.chip').forEach(el => {
    el.classList.toggle('active', el.dataset.mode === id);
  });

  // Toggle persona/character/rules controls based on mode capabilities.
  const rpControls = document.getElementById('rpControls');
  const needsCast = !!(mode.use_persona || mode.use_character);
  rpControls.classList.toggle('disabled', !needsCast);

  // Toggle chat-source section based on mode.
  const chatSourceSection = document.getElementById('chatSourceSection');
  chatSourceSection.style.display = mode.use_chat_context ? '' : 'none';

  // Phase 1: if this is a user-driven mode change (not silent) and we have a fresh
  // ST character, persist the char→mode mapping. Also update the follow banner.
  if (!opts.silent && prev && prev !== id) {
    if (stStateFresh && stState && stState.characterId) {
      persistCharMode(stState.characterId, id);
    }
    renderFollowBanner();
  } else if (opts.silent) {
    renderFollowBanner();
  }
}

// ─── Raw badge (formatting_skipped indicator) ───────────
function flashRawBadge() {
  rawBadge.classList.add('visible');
  if (rawBadgeTimer) clearTimeout(rawBadgeTimer);
  rawBadgeTimer = setTimeout(() => {
    rawBadge.classList.remove('visible');
    rawBadgeTimer = null;
  }, 10000);
}

function toggleRules() {
  useRules = !useRules;
  document.getElementById('rulesChip').classList.toggle('active', useRules);
}

function toggleProse() {
  proseFormat = !proseFormat;
  document.getElementById('proseChip').classList.toggle('active', proseFormat);
}

async function loadPersonas() {
  try {
    const resp = await fetch('/personas');
    const data = await resp.json();
    const select = document.getElementById('personaSelect');
    data.personas.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      select.appendChild(opt);
    });
    // Active ST persona wins. Otherwise keep an explicit no-persona state;
    // never silently choose the alphabetically first persona.
    if (embedConfig.persona && data.personas.some(p => p.id === embedConfig.persona)) {
      select.value = embedConfig.persona;
      selectedPersona = embedConfig.persona;
      personaPinned = true;
    } else {
      select.value = 'none';
      selectedPersona = 'none';
    }
  } catch { /* ignore */ }
}

let allCharacters = [];

async function loadCharacters() {
  try {
    const resp = await fetch('/characters');
    const data = await resp.json();
    allCharacters = data.characters || [];
    // Apply embed config character if present.
    if (embedConfig.character) {
      const match = allCharacters.find(c => c.name === embedConfig.character
                                         || c.id === embedConfig.character);
      if (match) selectCharacter(match.id || match.name, match.name);
    }
  } catch { /* ignore */ }
}

function showCharDropdown() {
  filterCharacters();
  document.getElementById('charDropdown').style.display = 'block';
}

function hideCharDropdown() {
  setTimeout(() => {
    document.getElementById('charDropdown').style.display = 'none';
  }, 200);
}

function filterCharacters() {
  const query = document.getElementById('characterSearch').value.toLowerCase();
  const dropdown = document.getElementById('charDropdown');
  const matches = query
    ? allCharacters.filter(c => c.name.toLowerCase().includes(query)).slice(0, 30)
    : allCharacters.slice(0, 30);

  let html = '<div class="char-opt char-none" onclick="selectCharacter(\'none\',\'No Character\')">No Character</div>';
  matches.forEach(c => {
    const esc = c.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    html += `<div class="char-opt" onclick="selectCharacter('${esc}','${esc}')">${c.name.replace(/</g,'&lt;')}</div>`;
  });
  if (allCharacters.length > 30 && !query) {
    html += '<div class="char-opt char-none">Type to search ' + allCharacters.length + ' characters...</div>';
  }
  dropdown.innerHTML = html;
  dropdown.style.display = 'block';
}

function onPersonaChange(val) {
  if (stStateFresh && stState && val && val !== stState.personaId) {
    markOverride('persona');
  }
  selectedPersona = val;
  personaPinned = true;
}

function selectCharacter(id, name) {
  // Phase 1: if user picks a character that differs from fresh ST state, mark override.
  if (stStateFresh && stState && id !== 'none' && id !== stState.characterId) {
    markOverride('character');
  }
  selectedCharacter = id;
  document.getElementById('characterSearch').value = (id === 'none') ? '' : name;
  document.getElementById('characterValue').value = id;
  document.getElementById('charDropdown').style.display = 'none';
  // Visual feedback
  const input = document.getElementById('characterSearch');
  input.classList.toggle('active', id !== 'none');
}

// Close dropdown on outside click
document.addEventListener('click', function(e) {
  if (!e.target.closest('#characterSearch') && !e.target.closest('#charDropdown')) {
    document.getElementById('charDropdown').style.display = 'none';
  }
});

function showToast(msg, ms = 2000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}

function formatDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function setIcon(name) {
  document.getElementById('micIcon').style.display = name === 'mic' ? '' : 'none';
  document.getElementById('stopIcon').style.display = name === 'stop' ? '' : 'none';
  document.getElementById('spinnerIcon').style.display = name === 'spinner' ? '' : 'none';
}

function toggleChatSource() {
  const toggle = document.getElementById('chatSourceToggle');
  const body = document.getElementById('chatSourceBody');
  toggle.classList.toggle('open');
  body.classList.toggle('open');
}

// ─── Chat Source ──────────────────────────────────────
async function loadRecentChats() {
  try {
    const resp = await fetch('/recent-chats');
    const data = await resp.json();
    recentChats = data.chats || [];
    const select = document.getElementById('chatSelect');
    select.innerHTML = '<option value="" disabled selected>Pick a chat...</option>';
    recentChats.forEach(c => {
      const opt = document.createElement('option');
      opt.value = `${c.type}:${c.name}`;
      opt.textContent = `${c.type === 'group' ? '\u{1F465} ' : ''}${c.name}`;
      select.appendChild(opt);
    });
    // Apply embed chat config if present.
    if (embedConfig.chat) {
      const match = recentChats.find(c => c.name === embedConfig.chat);
      if (match) {
        select.value = `${match.type}:${match.name}`;
        setChatSource(`${match.type}:${match.name}`);
      }
    }
  } catch { /* ignore */ }
}

function setChatSource(source) {
  if (source.includes(':') || source === 'manual' || source === 'auto') {
    chatSource = source;
    try { localStorage.setItem('dictation.chatSource', source); } catch {}
  }

  document.getElementById('srcManual').classList.toggle('active', source === 'manual');
  document.getElementById('srcAuto').classList.toggle('active', source === 'auto');

  const badge = document.getElementById('chatSourceBadge');
  if (source === 'manual') {
    badge.textContent = 'Manual';
    document.getElementById('manualContextArea').style.display = '';
    document.getElementById('chatPreviewArea').style.display = 'none';
  } else {
    document.getElementById('manualContextArea').style.display = 'none';
    document.getElementById('chatPreviewArea').style.display = '';
    if (source === 'auto') {
      badge.textContent = 'Auto';
    } else {
      const name = source.split(':').slice(1).join(':');
      badge.textContent = name.length > 20 ? name.slice(0, 20) + '...' : name;
    }
    loadChatContext();
  }
}

async function loadChatContext() {
  const preview = document.getElementById('chatPreview');
  const label = document.getElementById('chatPreviewLabel');
  preview.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:12px">Loading...</div>';

  try {
    let url;
    if (chatSource === 'auto') {
      url = '/active-chat';
    } else {
      const [type, ...nameParts] = chatSource.split(':');
      const name = nameParts.join(':');
      url = `/chat-context?name=${encodeURIComponent(name)}&type=${type}&last=8`;
    }

    const resp = await fetch(url);
    const data = await resp.json();

    if (data.error) {
      preview.innerHTML = `<div style="color:var(--error);text-align:center;padding:12px">${escHtml(data.error)}</div>`;
      chatContextText = '';
      return;
    }

    const messages = data.messages || [];
    chatContextText = data.context_text || '';
    label.textContent = `${data.name || 'Chat'} \u2014 ${messages.length} messages`;

    if (data.name && data.type === 'individual') {
      autoSelectCharacter(data.name);
    }

    if (messages.length === 0) {
      preview.innerHTML = '<div style="color:var(--text-dim);text-align:center;padding:12px">No messages found</div>';
      return;
    }

    preview.innerHTML = messages.map(m => {
      const cls = m.is_user ? 'user-msg' : 'partner';
      const sender = m.name || (m.is_user ? 'You' : 'Partner');
      const text = m.text.length > 300 ? m.text.slice(0, 300) + '...' : m.text;
      return `<div class="chat-msg ${cls}"><div class="chat-sender">${escHtml(sender)}</div><div class="chat-text">${escHtml(text)}</div></div>`;
    }).join('');

    preview.scrollTop = preview.scrollHeight;
  } catch (err) {
    preview.innerHTML = `<div style="color:var(--error);text-align:center;padding:12px">Failed to load</div>`;
    chatContextText = '';
  }
}

function refreshChatContext() {
  loadChatContext();
}

function autoSelectCharacter(chatName) {
  const match = allCharacters.find(c =>
    chatName.toLowerCase().includes(c.name.toLowerCase()) ||
    c.name.toLowerCase().includes(chatName.toLowerCase())
  );
  if (match && selectedCharacter === 'none') {
    selectCharacter(match.id, match.name);
  }
}

function toggleLog() {
  const toggle = document.getElementById('logToggle');
  const body = document.getElementById('logBody');
  toggle.classList.toggle('open');
  body.classList.toggle('open');
}

function getContext() {
  // Manual context from textarea (always available as fallback)
  return document.getElementById('contextInput').value.trim();
}

// ─── Recording ────────────────────────────────────────
async function toggleRecord() {
  if (recording) {
    stopRecording();
  } else {
    await startRecording();
  }
}

// Phase 2.5A: hold-to-record.
// Start recording IMMEDIATELY on touchstart (preserves user-gesture context
// for getUserMedia — delaying via setTimeout loses permission on mobile).
// Then on touchend decide: short press = "tap" (leave recording on, wait for
// next tap to stop) / long press = "hold" (stop now on release).
//
// Desktop mouse: ignored here, click handler uses toggleRecord() as before.
const HOLD_THRESHOLD_MS = 350;
let touchStartTs = 0;
let touchActive = false;       // a touch-initiated recording is in flight
let touchRecordingStarted = false;  // did this touch actually trigger startRecording()
let suppressNextClick = false; // block the synthetic click after a hold release
let pendingTouchHeldMs = 0;    // captured at touchend so the late-resolving
                                // startRecording().then() can honour the user's
                                // gesture intent if they released before the
                                // OS permission prompt resolved.

// MVP-11 / ADR-4 — hold-to-record race fix.
//
// Diagnosis (vds-agent-2-audio §2): `await startRecording()` inside touchstart
// suspended while the OS showed the mic prompt; the user lifted their finger;
// touchend fired while `recording === false`, so the hold-release branch's
// `if (recording) stopRecording()` no-op'd; the await resolved later with the
// mic stuck hot. Tap-toggle worked (no race) but hold did not.
//
// Fix: don't await inside touchstart. Fire-and-forget startRecording(); record
// the user's gesture intent in pendingTouchHeldMs at touchend; let the .then()
// honour that intent when the mic actually opens. The user-gesture flag
// survives the first microtask of an async function in Chromium [4], so kicking
// off the promise synchronously preserves getUserMedia eligibility.
function onRecordTouchStart(ev) {
  // Only single-finger touches.
  if (ev.touches.length > 1) return;
  // If already recording from a previous tap, treat this touch as a stop gesture.
  if (recording) {
    touchActive = true;
    touchRecordingStarted = false;  // we did NOT start recording this touch
    touchStartTs = Date.now();
    pendingTouchHeldMs = 0;
    return;
  }
  touchActive = true;
  touchStartTs = Date.now();
  pendingTouchHeldMs = 0;
  btn.classList.add('holding');
  // Haptic hint so user knows the press registered.
  if (navigator.vibrate) { try { navigator.vibrate(15); } catch {} }
  // Fire-and-forget. Critical: returning quickly from touchstart lets touchend
  // fire with accurate state if the user releases during the OS prompt.
  startRecording().then(() => {
    touchRecordingStarted = true;
    if (!touchActive) {
      // touchend already fired while we were awaiting permission.
      // Honour the gesture intent recorded at release time.
      if (pendingTouchHeldMs >= HOLD_THRESHOLD_MS) {
        // Long hold + late mic → user expected hold-to-record. Stop now.
        if (recording) stopRecording();
      }
      // Short tap + late mic → leave recording on (existing tap-toggle UX).
    }
  }).catch(err => {
    touchRecordingStarted = false;
    btn.classList.remove('holding');
    WARN_UI('startRecording failed', err);
    // Surface to user — silent failure was a major QoL bug pre-MVP-11.
    if (label) {
      const prev = label.textContent;
      label.textContent = 'Microphone access denied';
      label.classList.add('error-text');
      setTimeout(() => {
        label.textContent = prev || 'Tap or hold to record';
        label.classList.remove('error-text');
      }, 3000);
    }
  });
}

function onRecordTouchEnd(ev) {
  if (!touchActive) return;
  const heldMs = Date.now() - touchStartTs;
  pendingTouchHeldMs = heldMs;
  touchActive = false;
  btn.classList.remove('holding');
  // Suppress the synthetic click that browsers fire after touchend.
  suppressNextClick = true;
  // Clear the suppress flag on the next tick in case no click arrives.
  setTimeout(() => { suppressNextClick = false; }, 400);

  // MVP-11: if recording hasn't started yet, we're racing the OS permission
  // prompt. Don't no-op stopRecording() — record the intent and let the
  // startRecording().then() above act on it when the mic actually opens.
  if (!recording && !touchRecordingStarted) {
    try { ev.preventDefault(); } catch {}
    return;
  }

  if (heldMs >= HOLD_THRESHOLD_MS) {
    // Hold gesture → stop recording now.
    if (recording) stopRecording();
    try { ev.preventDefault(); } catch {}
  } else {
    // Short tap.
    if (touchRecordingStarted) {
      // We just started on touchstart — leave it running, user will tap to stop.
      // (Existing tap-to-stop behavior.)
    } else {
      // We were already recording (this touch was intended as a tap-to-stop).
      if (recording) stopRecording();
      try { ev.preventDefault(); } catch {}
    }
  }
}

function onRecordTouchCancel() {
  if (!touchActive) return;
  touchActive = false;
  btn.classList.remove('holding');
  // A cancel usually means scroll hijack — stop recording to avoid leaving mic hot.
  if (recording && touchRecordingStarted) stopRecording();
}

function onRecordClick(ev) {
  // Swallow the synthetic click that fires after any touch sequence we handled.
  if (suppressNextClick) {
    suppressNextClick = false;
    ev.preventDefault();
    ev.stopPropagation();
    return;
  }
  // Mouse/keyboard path: plain toggle.
  toggleRecord();
}

// Helper: warn without crashing if the console is absent (it isn't in reality,
// but keeps lint-passes quiet about unused WARN_UI).
function WARN_UI(...args) { try { console.warn('[dictation]', ...args); } catch {} }

(function wireHoldToRecord() {
  if (!btn) return;
  // Strip the inline onclick so our gated click handler is the only click path.
  btn.removeAttribute('onclick');
  btn.addEventListener('click', onRecordClick);
  // touchstart must NOT be passive — we need to preventDefault() if we want
  // to suppress the synthetic mousedown-click chain on some browsers.
  btn.addEventListener('touchstart', onRecordTouchStart, { passive: false });
  btn.addEventListener('touchend', onRecordTouchEnd);
  btn.addEventListener('touchcancel', onRecordTouchCancel, { passive: true });
})();

async function startRecording() {
  try {
    // Mobile tab switching can leave the phone UI with a stale snapshot. Pull
    // once at the moment of capture so context is current without manual refresh.
    if (FOLLOW_ST_FROM_URL) await loadStateAndApply({ force: true });
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
    });

    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus' : 'audio/webm';

    mediaRecorder = new MediaRecorder(stream, { mimeType });
    audioChunks = [];

    mediaRecorder.ondataavailable = e => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      clearInterval(durationInterval);
      teardownAudioAnalysis();
      await sendAudio();
    };

    mediaRecorder.start(100);
    recording = true;
    recordStart = Date.now();

    // Hook up VAD + VU meter on the same stream.
    setupAudioAnalysis(stream);

    btn.classList.add('recording');
    btn.classList.remove('processing');
    setIcon('stop');
    label.textContent = 'Tap to stop';
    recordSub.textContent = '';
    recordSub.classList.remove('visible');
    durationEl.classList.add('recording-dur');

    durationInterval = setInterval(() => {
      durationEl.textContent = formatDuration((Date.now() - recordStart) / 1000);
    }, 200);

  } catch (err) {
    console.error('Mic access error:', err);
    label.textContent = 'Microphone access denied';
    label.classList.add('error-text');
    setTimeout(() => {
      label.textContent = 'Tap to record';
      label.classList.remove('error-text');
    }, 3000);
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    recording = false;
    mediaRecorder.stop();
    btn.classList.remove('recording');
    btn.classList.add('processing');
    setIcon('spinner');
    label.textContent = 'Transcribing...';
    recordSub.textContent = '';
    recordSub.classList.remove('visible');
  }
}

// ─── VAD + VU meter ───────────────────────────────────
function setupAudioAnalysis(stream) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioContext = new Ctx();
    micSourceNode = audioContext.createMediaStreamSource(stream);
    analyserNode = audioContext.createAnalyser();
    analyserNode.fftSize = 512;
    analyserNode.smoothingTimeConstant = 0.7;
    micSourceNode.connect(analyserNode);

    // Show VU meter with fade-in.
    vuCanvas.classList.add('visible');
    startVuDraw();

    // Start VAD silence polling.
    silenceStartTs = 0;
    if (vadEnabled) {
      vadCheckInterval = setInterval(checkSilence, 100);
    }
  } catch (err) {
    console.warn('Audio analysis setup failed:', err);
  }
}

function teardownAudioAnalysis() {
  if (vadCheckInterval) {
    clearInterval(vadCheckInterval);
    vadCheckInterval = null;
  }
  if (vadRafId) {
    cancelAnimationFrame(vadRafId);
    vadRafId = null;
  }
  try { micSourceNode && micSourceNode.disconnect(); } catch {}
  try { analyserNode && analyserNode.disconnect(); } catch {}
  if (audioContext) {
    try { audioContext.close(); } catch {}
    audioContext = null;
  }
  analyserNode = null;
  micSourceNode = null;
  vuCanvas.classList.remove('visible');
  clearVuMeter();
}

function computeDb() {
  if (!analyserNode) return -Infinity;
  const buf = new Uint8Array(analyserNode.fftSize);
  analyserNode.getByteTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = buf[i] - 128;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / buf.length);
  if (rms < 0.0001) return -Infinity;
  return 20 * Math.log10(rms / 128);
}

function checkSilence() {
  if (!recording || !analyserNode) return;
  const db = computeDb();
  const now = Date.now();

  if (db < vadThreshold) {
    if (silenceStartTs === 0) silenceStartTs = now;
    const silenceSec = (now - silenceStartTs) / 1000;
    if (silenceSec >= vadSilenceDuration) {
      // Auto-stop
      recordSub.textContent = '';
      recordSub.classList.remove('visible');
      stopRecording();
      return;
    }
    // Show countdown subtitle (how long we've been silent).
    recordSub.textContent = `Listening… (${silenceSec.toFixed(1)}s silence)`;
    recordSub.classList.add('visible');
  } else {
    silenceStartTs = 0;
    recordSub.textContent = '';
    recordSub.classList.remove('visible');
  }
}

// VU meter peak-hold falloff state
const VU_BAR_COUNT = 24;
const vuPeaks = new Array(VU_BAR_COUNT).fill(0);

function clearVuMeter() {
  const ctx = vuCanvas.getContext('2d');
  ctx.clearRect(0, 0, vuCanvas.width, vuCanvas.height);
  for (let i = 0; i < VU_BAR_COUNT; i++) vuPeaks[i] = 0;
}

function startVuDraw() {
  const ctx = vuCanvas.getContext('2d');
  const W = vuCanvas.width;
  const H = vuCanvas.height;
  const barW = 4;
  const gap = (W - VU_BAR_COUNT * barW) / (VU_BAR_COUNT - 1);

  const rootStyle = getComputedStyle(document.documentElement);
  const primary = rootStyle.getPropertyValue('--primary').trim() || '#c4a7e7';
  const dim = rootStyle.getPropertyValue('--primary-dim').trim() || 'rgba(196,167,231,0.15)';

  const freqBins = new Uint8Array(analyserNode.frequencyBinCount);

  function draw() {
    if (!analyserNode) return;
    analyserNode.getByteFrequencyData(freqBins);

    // Bucket linearly into VU_BAR_COUNT bands.
    const binsPerBar = Math.floor(freqBins.length / VU_BAR_COUNT);
    ctx.clearRect(0, 0, W, H);
    for (let i = 0; i < VU_BAR_COUNT; i++) {
      let sum = 0;
      const start = i * binsPerBar;
      for (let j = 0; j < binsPerBar; j++) sum += freqBins[start + j];
      const avg = sum / binsPerBar / 255;   // 0..1
      // Peak-hold with 0.9 falloff.
      vuPeaks[i] = Math.max(avg, vuPeaks[i] * 0.9);

      const h = Math.max(2, vuPeaks[i] * H);
      const x = i * (barW + gap);
      const y = H - h;
      ctx.fillStyle = dim;
      ctx.fillRect(x, 0, barW, H);
      ctx.fillStyle = primary;
      ctx.fillRect(x, y, barW, h);
    }
    vadRafId = requestAnimationFrame(draw);
  }
  draw();
}

async function sendAudio() {
  // Re-check right before formatting/transcription too; this covers hold-to-record
  // and long recordings where ST context changed while the phone page was open.
  if (FOLLOW_ST_FROM_URL) await loadStateAndApply({ force: true });
  const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType });

  const model = document.getElementById('modelSelect').value;
  const context = getContext();
  const mode = getMode(currentMode);
  const params = new URLSearchParams({ model });
  params.set('provider', currentProvider);
  if (currentFormatterModel) params.set('formatter_model', currentFormatterModel);
  params.set('language', transcriptionLanguage);
  if (currentMode) params.set('mode', currentMode);
  if (context) params.set('context', context);

  // Only send cast/chat params when the current mode actually uses them.
  const useCast = mode && (mode.use_persona || mode.use_character);
  const useChat = mode && mode.use_chat_context;
  if (useCast) {
    if (selectedPersona !== 'none') params.set('persona', selectedPersona);
    if (selectedCharacter !== 'none') params.set('character', selectedCharacter);
    if (useRules) params.set('rules', '1');
    if (proseFormat) params.set('prose', '1');
  }
  if (useChat && chatSource !== 'manual') {
    params.set('chat_source', chatSource);
  }

  try {
    const resp = await fetch(`/transcribe?${params}`, {
      method: 'POST',
      headers: { 'Content-Type': mediaRecorder.mimeType },
      body: blob,
    });

    const data = await resp.json();

    if (data.error) {
      showError(data.error);
    } else {
      lastRawText = data.raw || data.text;
      // Backend echoes the resolved mode id; fall back if missing.
      lastUsedMode = data.mode || currentMode;
      showResult(data.text, { raw: lastRawText, cleaned: data.cleaned || '',
                              repair_trace: data.repair_trace || null,
                              mode: lastUsedMode,
                              formatting_skipped: !!data.formatting_skipped,
                              formatting_reason: data.formatting_reason || '',
                              model: data.model || '', model_fallback: !!data.model_fallback });

      // Regen is useful any time we have raw text to re-run through formatting,
      // and especially critical when enhancement refused or proxy was offline.
      regenBtn.style.display = lastRawText ? '' : 'none';

      // Flag formatting problems prominently so the user knows what they got.
      if (data.formatting_skipped) {
        showToast('⚠ ' + (data.formatting_reason || 'Formatting skipped — raw text shown'), 5000);
        flashRawBadge();
      }

      // Update transcript display
      if (data.transcript_count !== undefined) {
        updateLogDisplay(data.transcript_count);
      }
    }
  } catch (err) {
    console.error('Transcription error:', err);
    showError('Connection error');
  }

  btn.classList.remove('processing');
  setIcon('mic');
  durationEl.classList.remove('recording-dur');
}

// ─── Regenerate ───────────────────────────────────────
async function regenerate() {
  if (!lastRawText) return;

  regenBtn.disabled = true;
  label.textContent = 'Regenerating...';
  btn.classList.add('processing');
  setIcon('spinner');

  const context = getContext();
  // Prefer the mode used for the last transcription; fall back to current.
  const regenMode = lastUsedMode || currentMode || 'rp_enhance';

  try {
    const resp = await fetch('/reformat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: lastRawText,
        provider: currentProvider,
        formatter_model: currentFormatterModel,
        mode: regenMode,
        context: context,
        chat_source: chatSource !== 'manual' ? chatSource : '',
        persona: selectedPersona,
        character: selectedCharacter,
        rules: useRules,
        prose: proseFormat,
      }),
    });

    const data = await resp.json();

    if (data.error) {
      showError(data.error);
    } else {
      const resolvedMode = data.mode || regenMode;
      lastUsedMode = resolvedMode;
      showResult(data.text, { raw: lastRawText, cleaned: data.cleaned || '',
                              repair_trace: data.repair_trace || null,
                              mode: resolvedMode,
                              formatting_skipped: !!data.formatting_skipped,
                              formatting_reason: data.formatting_reason || '',
                              model: data.model || '', model_fallback: !!data.model_fallback });
      if (data.formatting_skipped) {
        showToast('⚠ ' + (data.formatting_reason || 'Formatting skipped — raw text shown'), 5000);
        flashRawBadge();
      }
      if (data.transcript_count !== undefined) {
        updateLogDisplay(data.transcript_count);
      }
    }
  } catch (err) {
    showError('Regen failed');
  }

  regenBtn.disabled = false;
  btn.classList.remove('processing');
  setIcon('mic');
  label.textContent = 'Tap to record';
}

// ─── Result display ───────────────────────────────────
function showResult(text, meta) {
  resultText.value = text;
  resultArea.classList.add('visible');
  label.textContent = 'Tap to record';
  lastRepairTrace = (meta && meta.repair_trace && meta.repair_trace.persistence === 'in_ram_only')
    ? meta.repair_trace : null;

  // Model attribution caption: which formatter model produced this text, and
  // whether the OmniRoute chain fell through to a fallback tier.
  const resultMeta = document.getElementById('resultMeta');
  if (resultMeta) {
    const model = meta && meta.model ? shortModelLabel(meta.model) : '';
    if (model && !(meta && meta.formatting_skipped)) {
      const fb = meta && meta.model_fallback;
      resultMeta.innerHTML = 'via <span class="model-chip' + (fb ? ' fallback' : '') + '">'
        + (fb ? '↳ ' : '') + escapeHtml(model) + '</span>'
        + (fb ? ' <span style="opacity:.7">(chain fell through)</span>' : '');
      resultMeta.style.display = '';
    } else {
      resultMeta.style.display = 'none';
    }
  }

  // Phase 1: populate Raw / Cleaned accordion rows when different from formatted text.
  const rows = document.getElementById('resultRows');
  const rawBody = document.getElementById('rowRawBody');
  const cleanedBody = document.getElementById('rowCleanedBody');
  const rowRaw = document.getElementById('rowRaw');
  const rowCleaned = document.getElementById('rowCleaned');
  const hasRaw = meta && meta.raw && meta.raw !== text;
  const hasCleaned = meta && meta.cleaned && meta.cleaned !== text && meta.cleaned !== meta.raw;
  if (rows) {
    if (hasRaw || hasCleaned) {
      rows.style.display = '';
      if (rowRaw) rowRaw.style.display = hasRaw ? '' : 'none';
      if (rowCleaned) rowCleaned.style.display = hasCleaned ? '' : 'none';
      if (rawBody && hasRaw) rawBody.textContent = meta.raw;
      if (cleanedBody && hasCleaned) cleanedBody.textContent = meta.cleaned;
      // Start collapsed
      if (rowRaw) rowRaw.classList.remove('open');
      if (rowCleaned) rowCleaned.classList.remove('open');
    } else {
      rows.style.display = 'none';
    }
  }

  renderRepairTrace();

  // Phase 2.5C: start the last-result pill (lives 60s independently of result area).
  showLastResultPill(text, meta);

  // Auto-copy (clipboard)
  navigator.clipboard.writeText(text)
    .then(() => showToast('Copied to clipboard'))
    .catch(() => showToast('Tap Copy to copy'));

  // POL-17: do not post final dictation text through iframe parent messaging.
  // The ST extension receives canonical text via authed SSE; parent messaging
  // is reserved for non-sensitive readiness only.

  // Autofocus the textarea on desktop (pointer:fine), never on touch.
  try {
    if (!window.matchMedia('(pointer:coarse)').matches) {
      resultText.focus();
    }
  } catch {}
}

function showError(msg) {
  label.textContent = msg;
  label.classList.add('error-text');
  setTimeout(() => {
    label.textContent = 'Tap to record';
    label.classList.remove('error-text');
  }, 3000);
}

async function copyResult() {
  try {
    await navigator.clipboard.writeText(resultText.value);
    showToast('Copied to clipboard');
  } catch {
    resultText.select();
    document.execCommand('copy');
    resultText.setSelectionRange(0, 0);
    showToast('Copied');
  }
}

function clearResult() {
  resultArea.classList.remove('visible');
  resultText.value = '';
  durationEl.textContent = '0:00';
  regenBtn.style.display = 'none';
  lastRawText = '';
  const rows = document.getElementById('resultRows');
  if (rows) rows.style.display = 'none';
  const resultMeta = document.getElementById('resultMeta');
  if (resultMeta) resultMeta.style.display = 'none';
  lastRepairTrace = null;
  renderRepairTrace();
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// Shorten an OmniRoute model slug for compact display:
// "claude/claude-opus-4-8" -> "opus-4-8"; "codex/gpt-5.5" -> "gpt-5.5".
function shortModelLabel(slug) {
  if (!slug || typeof slug !== 'string') return '';
  let s = slug.trim();
  const slash = s.lastIndexOf('/');
  if (slash >= 0) s = s.slice(slash + 1);
  return s.replace(/^claude-/, '');
}

function repairStageLabel(stage) {
  return stage === 'raw' ? 'Raw ASR'
    : stage === 'cleaned' ? 'Cleaned'
    : stage === 'final' ? 'Final'
    : stage;
}

function renderRepairTrace() {
  const card = document.getElementById('repairCard');
  const stagesEl = document.getElementById('repairStages');
  if (!card || !stagesEl) return;
  const trace = lastRepairTrace;
  if (!trace || !trace.has_changes || !Array.isArray(trace.stages) || !trace.stages.length) {
    card.style.display = 'none';
    stagesEl.innerHTML = '';
    return;
  }
  card.style.display = '';
  const finalText = String(resultText.value || trace.final || '');
  const rawText = String(trace.raw || '');
  stagesEl.innerHTML = trace.stages.map(stage => {
    const value = String(trace[stage] || '');
    const isFinal = stage === 'final';
    const accept = isFinal && finalText && rawText && rawText !== finalText
      ? `<button class="btn" style="padding:5px 8px;font-size:12px" onclick="acceptRepairAsVocab()">Accept as vocab</button>` : '';
    return `<div style="display:grid;grid-template-columns:70px 1fr auto;gap:8px;align-items:start">
      <span style="font-size:12px;color:var(--text-dim);padding-top:4px">${escapeHtml(repairStageLabel(stage))}</span>
      <span style="font-size:13px;line-height:1.35;white-space:pre-wrap">${escapeHtml(value)}</span>
      ${accept}
    </div>`;
  }).join('');
}

async function acceptRepairAsVocab() {
  if (!lastRepairTrace) return;
  const raw = String(lastRepairTrace.raw || '').trim();
  const finalText = String(resultText.value || lastRepairTrace.final || '').trim();
  if (!raw || !finalText || raw === finalText) {
    showToast('No repair to save');
    return;
  }
  try {
    const resp = await fetch('/vocab', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ correct: finalText, aliases: [raw] }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      showToast('⚠ ' + (data.error || 'Failed to save vocab'), 4000);
      return;
    }
    renderVocab(data.vocab || []);
    showToast('Saved explicit vocab repair');
  } catch (err) {
    showToast('Network error');
  }
}

// ─── Session log ──────────────────────────────────────
async function loadTranscript() {
  try {
    const resp = await fetch('/transcript');
    const data = await resp.json();
    sessionLog = data.transcript || [];
    renderLog();
  } catch { /* ignore */ }
}

function updateLogDisplay(count) {
  document.getElementById('logBadge').textContent = count;
  loadTranscript(); // refresh the full log
}

// Lucide-style inline SVG icons for per-entry actions (stroke-based, 24x24).
const LOG_ICONS = {
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
  regen: '<svg viewBox="0 0 24 24"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>',
  delete: '<svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M6 6l1 14a2 2 0 002 2h6a2 2 0 002-2l1-14"/></svg>',
  starEmpty: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
  starFilled: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
};

function renderLog() {
  const entries = document.getElementById('logEntries');
  const empty = document.getElementById('logEmpty');
  const badge = document.getElementById('logBadge');
  const downloadBtn = document.getElementById('downloadSessionBtn');

  badge.textContent = sessionLog.length;
  if (downloadBtn) downloadBtn.disabled = sessionLog.length === 0;

  if (sessionLog.length === 0) {
    empty.style.display = '';
    entries.innerHTML = '';
    return;
  }

  empty.style.display = 'none';
  const filterStarred = localStorage.getItem('dictation.log.filterStarred') === '1';
  const filterChatActive = !!(stStateFresh && stState && stState.chatId
                              && localStorage.getItem('dictation.log.filterChat') !== '0');
  const currentChatId = filterChatActive ? stState.chatId : null;

  // Phase 1: show/hide the "filtered to current chat" strip.
  const strip = document.getElementById('logFilterStrip');
  const stripToggle = document.getElementById('logFilterToggle');
  const stripText = document.getElementById('logFilterText');
  if (strip && stripToggle && stripText) {
    if (stStateFresh && stState && stState.chatId) {
      strip.style.display = '';
      if (currentChatId) {
        stripText.textContent = `Filtered to current chat (${stState.characterName || 'ST'})`;
        stripToggle.textContent = 'view all';
      } else {
        stripText.textContent = 'Showing all chats';
        stripToggle.textContent = 'filter to current';
      }
    } else {
      strip.style.display = 'none';
    }
  }

  let displayed = sessionLog;
  if (filterStarred) displayed = displayed.filter(e => e.starred);
  if (currentChatId) displayed = displayed.filter(e => !e.chat_id || e.chat_id === currentChatId);

  entries.innerHTML = displayed.map(e => {
    const isUser = e.role === 'user';
    const isStarred = isUser && !!e.starred;
    const cls = [e.role === 'context' ? 'context' : 'user', isStarred ? 'starred' : ''].filter(Boolean).join(' ');
    const roleLabel = e.role === 'context' ? 'Partner' : 'You';
    const id = e.id || '';
    const ts = e.timestamp || '';
    const starCls = isStarred ? 'star active' : 'star';
    const starIcon = isStarred ? LOG_ICONS.starFilled : LOG_ICONS.starEmpty;
    const starBtn = isUser
      ? `<button type="button" class="log-action-btn ${starCls}" aria-label="${isStarred ? 'Unstar' : 'Star'}" data-action="star" data-id="${escHtml(id)}">${starIcon}</button>`
      : '';
    const actions = [
      starBtn,
      `<button type="button" class="log-action-btn copy" aria-label="Copy" data-action="copy" data-id="${escHtml(id)}">${LOG_ICONS.copy}</button>`,
      isUser
        ? `<button type="button" class="log-action-btn regen" aria-label="Regenerate" data-action="regen" data-id="${escHtml(id)}">${LOG_ICONS.regen}</button>`
        : '',
      `<button type="button" class="log-action-btn delete" aria-label="Delete" data-action="delete" data-id="${escHtml(id)}">${LOG_ICONS.delete}</button>`,
    ].filter(Boolean).join('');
    return `<div class="log-entry ${cls}" data-id="${escHtml(id)}">
      <div class="log-role"><span>${roleLabel}</span>${ts ? `<span class="log-role-ts">${escHtml(ts)}</span>` : ''}</div>
      <div class="log-text" role="button" tabindex="0" aria-expanded="false" data-action="toggle">${escHtml(e.text)}</div>
      <div class="log-entry-actions">${actions}</div>
    </div>`;
  }).join('');

  // Scroll to bottom
  const body = document.getElementById('logBody');
  body.scrollTop = body.scrollHeight;
}

// Event delegation for per-entry actions (copy / regen / delete / expand toggle).
document.addEventListener('click', (ev) => {
  const target = ev.target.closest('[data-action]');
  if (!target) return;
  const entry = target.closest('.log-entry');
  if (!entry) return;
  const action = target.dataset.action;
  const id = target.dataset.id || entry.dataset.id || '';
  if (action === 'toggle') {
    const expanded = entry.classList.toggle('expanded');
    const txt = entry.querySelector('.log-text');
    if (txt) txt.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    return;
  }
  ev.stopPropagation();
  if (action === 'star') toggleStarLogEntry(id, target);
  else if (action === 'copy') copyLogEntry(id);
  else if (action === 'regen') regenLogEntry(id, target);
  else if (action === 'delete') deleteLogEntry(id);
});

// Keyboard activation for the collapsible text body.
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const t = ev.target;
  if (!t || !t.classList || !t.classList.contains('log-text')) return;
  ev.preventDefault();
  const entry = t.closest('.log-entry');
  if (!entry) return;
  const expanded = entry.classList.toggle('expanded');
  t.setAttribute('aria-expanded', expanded ? 'true' : 'false');
});

function _findEntry(id) {
  return sessionLog.find(e => e.id === id);
}

async function toggleStarLogEntry(id, btn) {
  if (!id) return;
  try {
    const resp = await fetch('/transcript/' + encodeURIComponent(id) + '/star', {
      method: 'POST',
    });
    if (resp.status === 404) {
      const data = await resp.json().catch(() => ({}));
      showToast('Entry missing');
      await loadTranscript();
      return;
    }
    if (!resp.ok) {
      showToast('Star failed');
      return;
    }
    const data = await resp.json();
    // Update local mirror in-place, re-render
    const entry = _findEntry(id);
    if (entry) entry.starred = data.starred;
    renderLog();
  } catch {
    showToast('Star failed');
  }
}

async function copyLogEntry(id) {
  const entry = _findEntry(id);
  if (!entry) { showToast('Entry missing'); return; }
  try {
    await navigator.clipboard.writeText(entry.text);
    showToast('Copied');
  } catch {
    showToast('Copy failed — clipboard blocked');
  }
}

async function regenLogEntry(id, btn) {
  const entry = _findEntry(id);
  if (!entry || entry.role !== 'user') return;
  const regenMode = lastUsedMode || currentMode || 'rp_enhance';
  const context = (typeof getContext === 'function') ? getContext() : '';
  btn.classList.add('spinning');
  btn.disabled = true;
  try {
    const resp = await fetch('/reformat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: entry.text,
        entry_id: id,
        provider: currentProvider,
        formatter_model: currentFormatterModel,
        mode: regenMode,
        context: context,
        chat_source: chatSource !== 'manual' ? chatSource : '',
        persona: selectedPersona,
        character: selectedCharacter,
        rules: useRules,
        prose: proseFormat,
      }),
    });
    const data = await resp.json();
    if (data.error) {
      showToast('⚠ ' + data.error, 4000);
    } else {
      if (data.formatting_skipped) {
        showToast('⚠ ' + (data.formatting_reason || 'Formatting skipped'), 4000);
      } else {
        showToast('Regenerated');
      }
      await loadTranscript();
    }
  } catch {
    showToast('Regen failed');
  } finally {
    btn.classList.remove('spinning');
    btn.disabled = false;
  }
}

async function deleteLogEntry(id) {
  if (!id) return;
  try {
    const resp = await fetch('/transcript/' + encodeURIComponent(id), {
      method: 'DELETE',
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      showToast('⚠ ' + (data.error || 'Delete failed'), 3000);
      return;
    }
    await loadTranscript();
    showToast('Deleted');
  } catch {
    showToast('Delete failed');
  }
}

function downloadSession() {
  if (!sessionLog.length) return;
  // Server's Content-Disposition header triggers save-as.
  window.open('/transcript/export.md', '_blank');
}

function toggleStarFilter() {
  const active = localStorage.getItem('dictation.log.filterStarred') === '1';
  localStorage.setItem('dictation.log.filterStarred', active ? '0' : '1');
  const btn = document.getElementById('starFilterBtn');
  if (btn) btn.style.color = active ? '' : 'var(--primary)';
  renderLog();
}

function toggleChatFilter() {
  // Default is filter ON when state fresh. Flipping stores '0' (disable).
  const cur = localStorage.getItem('dictation.log.filterChat');
  if (cur === '0') localStorage.removeItem('dictation.log.filterChat');
  else localStorage.setItem('dictation.log.filterChat', '0');
  renderLog();
}

// Apply star filter button visual on load
(function initStarFilter() {
  const btn = document.getElementById('starFilterBtn');
  if (btn && localStorage.getItem('dictation.log.filterStarred') === '1') {
    btn.style.color = 'var(--primary)';
  }
})();

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function clearSession(ev) {
  const wipeAll = ev && ev.shiftKey;
  if (wipeAll && !confirm('Clear everything including starred takes?')) return;
  try {
    const url = wipeAll ? '/transcript?all=1' : '/transcript';
    const resp = await fetch(url, { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    await loadTranscript();
    if (wipeAll) {
      showToast('All entries cleared');
    } else {
      const kept = data.kept_starred || 0;
      const cleared = data.cleared || 0;
      if (kept > 0) {
        showToast(`Cleared ${cleared} entries, kept ${kept} starred`);
      } else {
        showToast('Session cleared');
      }
    }
  } catch {
    showToast('Failed to clear');
  }
}

// ─── Collapsible panels (VAD / Vocab) ───────────────────
function toggleCollapsible(name) {
  const toggle = document.getElementById(name + 'Toggle');
  const body = document.getElementById(name + 'Body');
  if (!toggle || !body) return;
  const opening = !toggle.classList.contains('open');
  toggle.classList.toggle('open');
  body.classList.toggle('open');

  // Lazy-load vocab on first open.
  if (name === 'vocab' && opening && !vocabLoaded) {
    loadVocab();
  }
}

// ─── VAD settings ───────────────────────────────────────
function loadVadSettings() {
  try {
    const e = localStorage.getItem('dictation.vad.enabled');
    if (e !== null) vadEnabled = e === '1';
    const t = parseFloat(localStorage.getItem('dictation.vad.threshold'));
    if (!isNaN(t)) vadThreshold = t;
    const d = parseFloat(localStorage.getItem('dictation.vad.silenceDuration'));
    if (!isNaN(d)) vadSilenceDuration = d;
  } catch {}

  const enabledEl = document.getElementById('vadEnabled');
  const thresholdEl = document.getElementById('vadThreshold');
  const durationEl2 = document.getElementById('vadDuration');
  const thresholdVal = document.getElementById('vadThresholdVal');
  const durationVal = document.getElementById('vadDurationVal');
  const badge = document.getElementById('vadBadge');

  enabledEl.checked = vadEnabled;
  thresholdEl.value = vadThreshold;
  thresholdVal.textContent = vadThreshold;
  durationEl2.value = vadSilenceDuration;
  durationVal.textContent = vadSilenceDuration.toFixed(1);
  badge.textContent = vadEnabled ? 'auto-stop' : 'off';

  enabledEl.addEventListener('change', () => {
    vadEnabled = enabledEl.checked;
    try { localStorage.setItem('dictation.vad.enabled', vadEnabled ? '1' : '0'); } catch {}
    badge.textContent = vadEnabled ? 'auto-stop' : 'off';
    // If currently recording and user toggled off, kill the interval.
    if (!vadEnabled && vadCheckInterval) {
      clearInterval(vadCheckInterval);
      vadCheckInterval = null;
      recordSub.textContent = '';
      recordSub.classList.remove('visible');
    }
  });
  thresholdEl.addEventListener('input', () => {
    vadThreshold = parseInt(thresholdEl.value, 10);
    thresholdVal.textContent = vadThreshold;
    try { localStorage.setItem('dictation.vad.threshold', String(vadThreshold)); } catch {}
  });
  durationEl2.addEventListener('input', () => {
    vadSilenceDuration = parseFloat(durationEl2.value);
    durationVal.textContent = vadSilenceDuration.toFixed(1);
    try { localStorage.setItem('dictation.vad.silenceDuration', String(vadSilenceDuration)); } catch {}
  });
}

// ─── Vocab panel ────────────────────────────────────────
async function loadVocab() {
  vocabLoaded = true;
  try {
    const resp = await fetch('/vocab');
    const data = await resp.json();
    renderVocab(data.vocab || []);
  } catch (err) {
    console.warn('Failed to load vocab:', err);
    renderVocab([]);
  }
}

function renderVocab(entries) {
  const list = document.getElementById('vocabList');
  const badge = document.getElementById('vocabBadge');
  badge.textContent = entries.length;

  if (!entries.length) {
    list.innerHTML = '<div class="vocab-empty">No custom vocabulary yet. Add names or terms the transcriber misspells.</div>';
    return;
  }

  list.innerHTML = entries.map(e => {
    const correct = escHtml(e.correct || '');
    const aliases = (e.aliases || []).map(escHtml).join(', ');
    const chars = (e.characters && e.characters.length)
      ? `<div style="font-size:11px;color:var(--text-dim);margin-top:2px">scope: ${e.characters.map(escHtml).join(', ')}</div>` : '';
    return `<div class="vocab-row">
      <div class="vocab-correct">${correct}${chars}</div>
      <div class="vocab-aliases">${aliases || '<span style="opacity:0.5">—</span>'}</div>
      <button class="vocab-del" type="button" data-correct="${correct}">Delete</button>
    </div>`;
  }).join('');

  list.querySelectorAll('.vocab-del').forEach(btn => {
    btn.addEventListener('click', () => deleteVocabEntry(btn.dataset.correct));
  });
}

async function addVocabEntry() {
  const correctEl = document.getElementById('vocabCorrect');
  const aliasesEl = document.getElementById('vocabAliases');
  const correct = correctEl.value.trim();
  if (!correct) {
    showToast('Enter the correct form first');
    correctEl.focus();
    return;
  }
  const aliases = aliasesEl.value.split(',').map(s => s.trim()).filter(Boolean);

  try {
    const resp = await fetch('/vocab', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ correct, aliases }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      showToast('⚠ ' + (data.error || 'Failed to add'), 4000);
      return;
    }
    renderVocab(data.vocab || []);
    correctEl.value = '';
    aliasesEl.value = '';
    showToast('Added "' + correct + '"');
  } catch (err) {
    showToast('Network error');
  }
}

async function deleteVocabEntry(correct) {
  if (!correct) return;
  try {
    const resp = await fetch('/vocab/' + encodeURIComponent(correct), { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      showToast('⚠ ' + (data.error || 'Failed to delete'), 4000);
      return;
    }
    renderVocab(data.vocab || []);
    showToast('Removed "' + correct + '"');
  } catch (err) {
    showToast('Network error');
  }
}

// ─── Embed postMessage bridge ───────────────────────────
function setupEmbedMessaging() {
  if (!isEmbed) return;

  // Live-mirror textarea edits to parent (debounced).
  resultText.addEventListener('input', () => {
    if (resultInputDebounce) clearTimeout(resultInputDebounce);
    resultInputDebounce = setTimeout(() => {
      try {
        window.parent.postMessage({
          type: 'dictation-edit',
          text: resultText.value,
        }, '*');
      } catch {}
    }, 300);
  });

  window.addEventListener('message', (e) => {
    const msg = e.data || {};
    if (!msg || typeof msg !== 'object') return;
    switch (msg.type) {
      case 'dictation-set-context': {
        // Drop the pushed context into the manual textarea; surface it.
        const ta = document.getElementById('contextInput');
        if (typeof msg.context === 'string') {
          ta.value = msg.context;
          // Ensure user sees it: switch to manual source + open the section.
          setChatSource('manual');
          const body = document.getElementById('chatSourceBody');
          const toggle = document.getElementById('chatSourceToggle');
          body.classList.add('open');
          toggle.classList.add('open');
        }
        break;
      }
      case 'dictation-set-mode': {
        if (typeof msg.mode === 'string' && getMode(msg.mode)) {
          setMode(msg.mode);
        }
        break;
      }
      default:
        // Ignore unknown types.
        break;
    }
  });
}

async function checkCertHealth() {
  try {
    const resp = await fetch('/health');
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.cert_warning) return;
    const banner = document.getElementById('certBanner');
    if (!banner) return;
    banner.textContent = '⚠ ' + data.cert_warning;
    banner.classList.add('show');
    if (typeof data.cert_expires_days === 'number' && data.cert_expires_days < 7) {
      banner.classList.add('urgent');
    }
  } catch (_) { /* network flakes — silent */ }
}

// ─── Init ───────────────────────────────────────────────
// ─── Phase 1: ST state following ──────────────────────
function ageHuman(seconds) {
  if (!seconds && seconds !== 0) return '?';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds/60)}m ago`;
  return `${Math.round(seconds/3600)}h ago`;
}

function formatStContext(s) {
  if (!s) return '';
  const parts = [];
  const chatType = String(s.chatType || '').toLowerCase();
  if (chatType === 'group') {
    if (s.characterName) parts.push(`Group: ${s.characterName}`);
    else if (s.groupId || s.chatId) parts.push(`Group: ${s.groupId || s.chatId}`);
    if (s.lastSpeaker) parts.push(`Last speaker: ${s.lastSpeaker}`);
  } else {
    if (s.characterName) parts.push(`Character: ${s.characterName}`);
    else if (s.characterId) parts.push(`Character: ${s.characterId}`);
  }
  if (s.personaId) parts.push(`Persona: ${String(s.personaId).split(/[\\/]/).pop()}`);
  const modeId = currentMode || s.rememberedMode || s.mode;
  if (modeId) {
    const m = getMode(modeId);
    parts.push(`Mode: ${m?.label || modeId}`);
  }
  return parts.join(' • ');
}

function authProblemText() {
  const auth = window.calliopeAuthStatus || {};
  if (auth.unauthorized) return 'Not paired — stale token. Reopen from SillyTavern.';
  if (!auth.hasToken) return 'Not paired — open this page from SillyTavern.';
  return '';
}

function renderFollowBanner() {
  // Keep Send-to-ST button enable state in sync with ST connection status.
  updateSendToStButton();
  updatePillActions();
  const el = document.getElementById('stFollowBanner');
  const icon = document.getElementById('stFollowIcon');
  const text = document.getElementById('stFollowText');
  const resync = document.getElementById('stResyncBtn');
  if (!el) return;
  el.style.display = '';
  el.classList.remove('fresh', 'stale', 'override');

  const authText = authProblemText();
  if (authText) {
    el.classList.add('stale');
    icon.textContent = '🔐';
    text.textContent = authText;
    resync.style.display = 'none';
    return;
  }

  if (!stState || !stState.lastUpdated) {
    el.classList.add('stale');
    icon.textContent = '⚠';
    text.textContent = 'Paired — waiting for SillyTavern context. Reopen from the ST mic if this stays here.';
    resync.style.display = 'none';
    return;
  }
  if (!stFollow) {
    el.classList.add('override');
    icon.textContent = '✋';
    text.textContent = `Manual override — ${formatStContext(stState) || 'no ST context selected'}`;
    resync.style.display = '';
    resync.textContent = 'Follow ST';
    return;
  }
  if (stStateFresh) {
    el.classList.add('fresh');
    icon.textContent = '📡';
    text.textContent = `Paired + following ST — ${formatStContext(stState) || 'context live'}`;
    resync.style.display = 'none';
  } else {
    el.classList.add('stale');
    icon.textContent = '⚠';
    text.textContent = `Paired, ST context stale (${ageHuman(stState.ageSeconds)}) — reopen from SillyTavern.`;
    resync.style.display = '';
    resync.textContent = 'Re-check';
  }
}

async function fetchState() {
  try {
    const r = await fetch('/state', { cache: 'no-store' });
    if (window.calliopeAuthStatus) {
      window.calliopeAuthStatus.lastStatus = r.status;
      if (r.status === 401) window.calliopeAuthStatus.unauthorized = true;
    }
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    if (window.calliopeAuthStatus) window.calliopeAuthStatus.lastError = e?.message || 'network';
    return null;
  }
}

async function applyStateIfFresh(snap, { force = false } = {}) {
  // Only auto-adopt if following and data is fresh (or forced by manual re-sync).
  if (!snap) return false;
  const canAdopt = force || (stFollow && snap.fresh);
  if (!canAdopt) return false;

  // Character
  if (snap.characterId) {
    const charEl = document.getElementById('characterValue');
    const searchEl = document.getElementById('characterSearch');
    if (charEl && charEl.value !== snap.characterId) {
      charEl.value = snap.characterId;
      selectedCharacter = snap.characterId;
      if (searchEl) searchEl.value = snap.characterName || snap.characterId;
    }
  }
  // Persona
  if (snap.personaId && !personaPinned) {
    const pEl = document.getElementById('personaSelect');
    if (pEl && pEl.value !== snap.personaId) {
      // Only set if option exists; otherwise ignore (persona file may not be loaded)
      const opt = Array.from(pEl.options).find(o => o.value === snap.personaId);
      if (opt) { pEl.value = snap.personaId; selectedPersona = snap.personaId; }
    }
  }
  // Chat context — adopt last AI message if chatSource is manual (don't clobber user-picked chats)
  if (snap.lastAiMessage && chatSource === 'manual') {
    const ci = document.getElementById('contextInput');
    if (ci && !ci.value.trim()) ci.value = snap.lastAiMessage;
  }
  // Auto/individual/group: refresh chat context preview when ST emits a new message.
  if (snap.lastAiMessage && snap.lastAiMessage !== _lastSeenAiMessage &&
      (chatSource === 'auto' || chatSource.includes(':'))) {
    _lastSeenAiMessage = snap.lastAiMessage;
    try { loadChatContext(); } catch {}
  }
  // Mode — prefer remembered per-char mode, else snap.mode
  const preferredMode = snap.rememberedMode || snap.mode;
  if (preferredMode && preferredMode !== currentMode && getMode(preferredMode)) {
    setMode(preferredMode, { silent: true });
  }
  return true;
}

async function loadStateAndApply({ force = false } = {}) {
  const snap = await fetchState();
  stState = snap;
  stStateFresh = !!(snap && snap.fresh);
  if (stStateFresh || force) await applyStateIfFresh(snap, { force });
  renderFollowBanner();
  // Re-render log with chat filter if chatId available.
  renderLog();
  return snap;
}

function markOverride(reason) {
  if (!stFollow) return;  // already overridden
  stFollow = false;
  renderFollowBanner();
}

async function resyncWithST() {
  stFollow = true;
  personaPinned = false;
  await loadStateAndApply({ force: true });
  showToast('Re-synced with ST');
}

// ─── Phase 2: Send-to-ST ──────────────────────────────
function updateSendToStButton() {
  const btn = document.getElementById('sendToStBtn');
  const copyBtn = document.getElementById('copyBtn');
  if (!btn) return;
  const canSend = !!(stStateFresh && stState);
  btn.disabled = !canSend;
  btn.title = canSend
    ? 'Send directly to SillyTavern'
    : 'ST not connected — use Copy instead';
  // Promote Copy to primary when Send unavailable, demote back when available.
  if (copyBtn) {
    copyBtn.classList.toggle('primary', !canSend);
  }
}

function toggleAutoSend(on) {
  autoSendToST = !!on;
  localStorage.setItem('dictation.autoSend', autoSendToST ? '1' : '0');
}

// ─── Phase 2.5C: last-result pill ─────────────────────
function showLastResultPill(text, meta) {
  if (!text) return;
  lastResult = {
    text,
    raw: (meta && meta.raw) || '',
    cleaned: (meta && meta.cleaned) || '',
    mode: (meta && meta.mode) || currentMode || '',
    expiresAt: Date.now() + PILL_TTL_MS,
  };

  const pill = document.getElementById('lastResultPill');
  const preview = document.getElementById('pillPreview');
  if (!pill || !preview) return;

  // Preview: collapse whitespace, truncate, keep a single line.
  const collapsed = text.replace(/\s+/g, ' ').trim();
  preview.textContent = collapsed.length > PILL_PREVIEW_MAX
    ? collapsed.slice(0, PILL_PREVIEW_MAX - 1) + '…'
    : collapsed;

  pill.style.display = '';
  pill.style.setProperty('--ttl-pct', '100%');
  // Re-trigger entrance animation each appearance.
  pill.classList.remove('entering');
  void pill.offsetWidth; // force reflow so animation replays
  pill.classList.add('entering');

  // Countdown + auto-dismiss.
  if (pillTimer) clearTimeout(pillTimer);
  if (pillTickTimer) clearInterval(pillTickTimer);
  pillTimer = setTimeout(dismissLastResult, PILL_TTL_MS);
  pillTickTimer = setInterval(() => {
    if (!lastResult) return;
    const remaining = Math.max(0, lastResult.expiresAt - Date.now());
    const pct = Math.max(0, (remaining / PILL_TTL_MS) * 100);
    pill.style.setProperty('--ttl-pct', pct + '%');
  }, 1000);

  updatePillActions();
}

function updatePillActions() {
  const sendBtn = document.getElementById('pillSendBtn');
  if (sendBtn) {
    const canSend = !!(stStateFresh && stState);
    sendBtn.disabled = !canSend;
    sendBtn.title = canSend ? 'Re-send to ST' : 'ST not connected';
  }
}

function dismissLastResult() {
  if (pillTimer) { clearTimeout(pillTimer); pillTimer = null; }
  if (pillTickTimer) { clearInterval(pillTickTimer); pillTickTimer = null; }
  const pill = document.getElementById('lastResultPill');
  if (pill) pill.style.display = 'none';
  lastResult = null;
}

async function pillCopy() {
  if (!lastResult) return;
  try {
    await navigator.clipboard.writeText(lastResult.text);
    showToast('Copied');
  } catch {
    showToast('Copy failed');
  }
}

async function pillSend() {
  if (!lastResult) return;
  if (!(stStateFresh && stState)) { showToast('ST not connected'); return; }
  try {
    const resp = await fetch('/send-to-st', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: lastResult.text,
        raw: lastResult.raw,
        cleaned: lastResult.cleaned,
        mode: lastResult.mode,
        auto_send: autoSendToST,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { showToast('Send failed'); return; }
    if ((data.subscribers || 0) === 0) showToast('No ST listeners');
    else showToast(autoSendToST ? 'Re-sent to ST ✓ (auto)' : 'Re-sent to ST ✓');
  } catch {
    showToast('Send failed');
  }
}

function expandLastResult() {
  // Restore the pill's text into the main result editor (tap-to-bring-back).
  if (!lastResult) return;
  resultText.value = lastResult.text;
  resultArea.classList.add('visible');
  // Scroll result into view on phone.
  try { resultArea.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch {}
}

async function sendToST() {
  const text = (resultText.value || '').trim();
  if (!text) { showToast('Nothing to send'); return; }
  const btn = document.getElementById('sendToStBtn');
  if (btn) { btn.disabled = true; }
  const body = {
    text,
    raw: lastRawText || '',
    mode: lastUsedMode || currentMode || '',
    auto_send: autoSendToST,
  };
  try {
    const resp = await fetch('/send-to-st', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showToast('Send failed: ' + (data.error || resp.status));
      return;
    }
    const subs = data.subscribers || 0;
    if (subs === 0) {
      // No ST tabs listening — fall back to clipboard so the user isn't stuck.
      try { await navigator.clipboard.writeText(text); } catch {}
      showToast('No ST listeners — copied to clipboard');
    } else if (autoSendToST) {
      showToast('Sent to ST ✓ (auto-sending)');
    } else {
      showToast('Sent to ST ✓');
    }
  } catch (e) {
    showToast('Send failed: ' + (e?.message || 'network'));
  } finally {
    // Re-evaluate state-driven enable/disable
    updateSendToStButton();
  }
}

function refreshStateFromLifecycle() {
  statePollVisible = !document.hidden;
  if (statePollVisible) loadStateAndApply();
}

function startStatePolling() {
  stopStatePolling();
  if (!FOLLOW_ST_FROM_URL) return;
  statePollTimer = setInterval(() => {
    if (statePollVisible) loadStateAndApply();
  }, STATE_POLL_MS);
  document.addEventListener('visibilitychange', refreshStateFromLifecycle);
  window.addEventListener('pageshow', refreshStateFromLifecycle);
  window.addEventListener('focus', refreshStateFromLifecycle);
  statePollLifecycleBound = true;
}

function stopStatePolling() {
  if (statePollTimer) { clearInterval(statePollTimer); statePollTimer = null; }
  if (statePollLifecycleBound) {
    document.removeEventListener('visibilitychange', refreshStateFromLifecycle);
    window.removeEventListener('pageshow', refreshStateFromLifecycle);
    window.removeEventListener('focus', refreshStateFromLifecycle);
    statePollLifecycleBound = false;
  }
}

async function persistCharMode(charId, modeId) {
  if (!charId || !modeId) return;
  try {
    await fetch('/state/mode-memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character: charId, mode: modeId }),
    });
  } catch { /* best-effort */ }
}

function toggleResultRow(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

function init() {
  loadVadSettings();
  loadModes();          // fetches /modes, renders chips, then posts 'dictation-ready'
  loadTranscript();
  loadPersonas();
  loadFormatterModels();
  loadCharacters();
  loadRecentChats();
  setupEmbedMessaging();
  checkCertHealth();
  // Apply persisted chatSource so the right chip lights up + auto re-loads
  // chat preview when starting in 'auto' mode.
  try { setChatSource(chatSource); } catch {}
  // Phase 2: restore auto-send preference.
  const autoToggle = document.getElementById('autoSendToggle');
  if (autoToggle) autoToggle.checked = autoSendToST;
  updateSendToStButton();
  // Phase 1: follow ST state. Wait for modes/personas/characters to load,
  // then apply state so selection actually sticks.
  setTimeout(() => {
    loadStateAndApply().then(() => startStatePolling());
  }, 400);
}

// MVP-23 — privacy badge + audit log modal.
let _auditAllMode = false;
async function fetchAudit(all) {
  try {
    const url = all ? '/audit/network?all=1' : '/audit/network';
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch (_e) { return null; }
}
function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function _renderAuditRows(entries) {
  if (!entries || !entries.length) {
    return '<div style="color:var(--text-dim);font-size:11.5px;padding:6px 0">No outbound calls in window.</div>';
  }
  const rows = entries.slice(-30).reverse().map(function (e) {
    const dt = new Date((e.ts || 0) * 1000);
    const hh = String(dt.getHours()).padStart(2, '0');
    const mm = String(dt.getMinutes()).padStart(2, '0');
    const ss = String(dt.getSeconds()).padStart(2, '0');
    const cls = e.is_loopback ? 'local' : 'remote';
    const host = (e.host || '?') + ':' + (e.port || '?');
    const path = (e.path || '/') + (e.error ? ' [' + e.error + ']' : '');
    const lat = (e.latency_ms != null) ? Math.round(e.latency_ms) + 'ms' : '';
    return '<div class="audit-row">' +
      '<span class="a-ts">' + hh + ':' + mm + ':' + ss + '</span>' +
      '<span class="a-method">' + _esc(e.method || 'GET') + '</span>' +
      '<span class="a-host ' + cls + '">' + _esc(host) + ' ' + _esc(path) + '</span>' +
      '<span class="a-lat">' + _esc(lat) + '</span>' +
      '</div>';
  }).join('');
  return rows;
}
async function _refreshPrivacyModal() {
  const data = await fetchAudit(_auditAllMode);
  const card = document.getElementById('privacyCard');
  const badge = document.getElementById('privacyBadge');
  const banner = document.getElementById('privacyAlertBanner');
  const rows = document.getElementById('auditRows');
  if (!data) {
    if (rows) rows.innerHTML = '<div style="color:var(--error);font-size:11.5px">Audit fetch failed.</div>';
    return;
  }
  // Cleanup host info — derived from /health if present, else fallback string.
  try {
    const hr = await fetch('/health', { cache: 'no-store' });
    if (hr.ok) {
      const hj = await hr.json();
      const cleanupTarget = (hj.providers || []).join(' / ') + ' (loopback)';
      const ch = document.getElementById('privacyCleanupHost');
      if (ch) ch.textContent = cleanupTarget;
    }
  } catch (_e) { /* ignore */ }
  const phonePc = document.getElementById('privacyPhonePc');
  if (phonePc) phonePc.textContent = window.location.origin + ' (LAN-only by default)';

  const hasWarn = !!data.warning;
  if (card) card.classList.toggle('alert', hasWarn);
  if (badge) badge.classList.toggle('alert', hasWarn);
  if (banner) {
    if (hasWarn) {
      banner.textContent = '⚠ ' + data.warning + ' — review.';
      banner.style.display = 'block';
    } else {
      banner.textContent = '';
      banner.style.display = 'none';
    }
  }
  if (rows) rows.innerHTML = _renderAuditRows(data.recent);
}
async function openPrivacyModal() {
  const bd = document.getElementById('privacyBackdrop');
  if (!bd) return;
  bd.classList.add('open');
  _auditAllMode = false;
  const allBtn = document.getElementById('auditAllBtn');
  if (allBtn) allBtn.textContent = 'View full audit log';
  await _refreshPrivacyModal();
}
function closePrivacyModal() {
  const bd = document.getElementById('privacyBackdrop');
  if (bd) bd.classList.remove('open');
}
async function toggleAuditAll() {
  _auditAllMode = !_auditAllMode;
  const allBtn = document.getElementById('auditAllBtn');
  if (allBtn) allBtn.textContent = _auditAllMode ? 'Last 60s only' : 'View full audit log';
  await _refreshPrivacyModal();
}
function explainPrivacy() {
  alert(
    "Calliope captures audio on this PC, runs whisper.cpp locally on the GPU, " +
    "and post-processes via loopback proxies on this machine. The audit log " +
    "above is recorded by intercepting every outgoing HTTP call this server " +
    "makes. Loopback (green) means the request never left the box. Non-loopback " +
    "(red) means a destination outside 127.0.0.0/8 — nothing here should " +
    "ever produce one in normal operation."
  );
}

// Background poll: keeps the badge color honest even when modal is closed.
async function _pollPrivacyBadge() {
  const data = await fetchAudit(false);
  const badge = document.getElementById('privacyBadge');
  if (!data || !badge) return;
  badge.classList.toggle('alert', !!data.warning);
}
setInterval(_pollPrivacyBadge, 30000);
setTimeout(_pollPrivacyBadge, 1500);

// POL-15 — voice-command cheatsheet modal. Reuses MVP-23 modal infra.
function openCheatsheetModal() {
  const bd = document.getElementById('cheatsheetBackdrop');
  if (bd) bd.classList.add('open');
}
function closeCheatsheetModal() {
  const bd = document.getElementById('cheatsheetBackdrop');
  if (bd) bd.classList.remove('open');
}

// Esc closes any open modal.
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  closePrivacyModal();
  closeCheatsheetModal();
});

function initAfterAuth() {
  Promise.resolve(window.calliopeAuthReady).then(init);
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAfterAuth);
} else {
  initAfterAuth();
}
</script>
</body>
</html>
"""

PWA_MANIFEST = json.dumps({
    "name": "Dictate",
    "short_name": "Dictate",
    "start_url": "/",
    "display": "standalone",
    # POL-5 / Agent 4 §8.3 — Apollo near-black for splash + status-bar.
    "background_color": "#0E0B08",
    "theme_color": "#0E0B08",
    # Icon binaries are not yet in tree (deploy-time concern). The
    # /icon-*.png routes return 404 with an image/png hint until the
    # asset exists; manifest still points at canonical paths so the PWA
    # install picks them up when generated.
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
})


# Served on `/` to non-loopback clients that present no valid credential. Two jobs:
#  1. Recover paired-session reloads by fetching the full UI with the bearer in
#     an Authorization header. The durable token never re-enters the URL.
#  2. Otherwise, tell the user how to pair. No server details are disclosed.
PAIRING_BOOTSTRAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calliope — pairing required</title>
<style>
  body { font-family: system-ui, sans-serif; background: #16120c; color: #e8ddc8;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; padding: 1.5rem; text-align: center; }
  .card { max-width: 26rem; }
  h1 { font-size: 1.2rem; }
  p { line-height: 1.5; color: #bfae8f; }
</style>
<script>
(function () {
  try {
    var stored = '';
    try { stored = sessionStorage.getItem('dictationToken') || ''; } catch (e) {}
    if (!stored) return;
    fetch(location.pathname + location.search + location.hash, {
      headers: { 'Authorization': 'Bearer ' + stored },
      cache: 'no-store'
    }).then(function (resp) {
      if (!resp.ok) throw new Error('unauthorized');
      return resp.text();
    }).then(function (html) {
      document.open();
      document.write(html);
      document.close();
    }).catch(function () {
      try { sessionStorage.removeItem('dictationToken'); } catch (e) {}
    });
  } catch (e) { /* fall through to the static message */ }
})();
</script>
</head>
<body>
<div class="card">
  <h1>Calliope — pairing required</h1>
  <p>This device isn't paired. Open the dictation page from the SillyTavern
  Dictation Bridge extension (<em>Pair phone</em> &rarr; scan the QR code or
  copy the pairing URL), or browse from the server machine itself.</p>
</div>
</body>
</html>
"""
