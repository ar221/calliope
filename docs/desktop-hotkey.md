# Desktop hotkey

The `dictate` bash client (`scripts/dictate`, deployed to
`~/.local/bin/dictate`) is the desktop push-to-talk surface. It records
via `pw-record`, transcribes either against the local Calliope server
(`--mode rp_enhance`) or directly via `whisper-cli`, and pastes the
result via `wtype` (most apps) or `wl-copy + wtype Ctrl+V` (terminals
that need clipboard paste — kitty / foot / Alacritty / ghostty / wezterm).

The script is the trigger surface; your compositor / desktop environment
binds the hotkey.

---

## niri (Wayland, current setup)

`~/.config/niri/config.d/70-binds.kdl` — three bindings, three modes:

```kdl
Mod+Shift+M       hotkey-overlay-title="Dictation Toggle"     { spawn "bash" "-c" "$HOME/.local/bin/dictate toggle"; }
Mod+Ctrl+M        hotkey-overlay-title="Dictation RP Toggle"  { spawn "bash" "-c" "$HOME/.local/bin/dictate --rp toggle"; }
Mod+Ctrl+Shift+M  hotkey-overlay-title="Dictation RP+ Toggle" { spawn "bash" "-c" "$HOME/.local/bin/dictate --rp+ toggle"; }
```

- `Mod+Shift+M` — plain-mode dictation. Whisper output, no formatting.
- `Mod+Ctrl+M` — `rp_format` mode (asterisks for actions, quotes for
  dialogue).
- `Mod+Ctrl+Shift+M` — `rp_enhance` (`+` modifier sends through the
  pyrite-preset enhancer, persona + character + chat-context aware).

Reload niri (`niri msg action reload-config` or restart your session) and
the bindings are live. The `hotkey-overlay-title` strings appear in
niri's hotkey overlay (Mod+?).

Smart-paste detection (`detect_paste_mode` in `dictate`) queries
`niri msg --json focused-window`. If the focused app is a terminal that
swallows raw `wtype` keystrokes, the script falls back to clipboard +
paste. Everything else gets direct typing.

The `dictate` script also installs three fish aliases via
`~/.config/fish/conf.d/dictate.fish`: `dictate-cpu`, `dictate-md`,
`dictate-rp`, `dictate-rp+` — all `toggle`. Useful for terminal-driven
dictation when you don't want the hotkey.

---

## GNOME

GNOME's custom-shortcut UI binds command strings to keystrokes:

1. Open **Settings → Keyboard → View and Customize Shortcuts → Custom
   Shortcuts**.
2. Click **Add Shortcut**.
3. Fill in:
   - **Name:** `Calliope dictate (toggle)`
   - **Command:** `bash -c "$HOME/.local/bin/dictate toggle"`
4. Click **Set Shortcut**, press `Super+Shift+M`. Save.
5. Repeat for the other two modes:
   - `bash -c "$HOME/.local/bin/dictate --rp toggle"` → `Super+Ctrl+M`
   - `bash -c "$HOME/.local/bin/dictate --rp+ toggle"` → `Super+Ctrl+Shift+M`

GNOME on Wayland sandboxes global shortcuts behind the
`xdg-desktop-portal` global-shortcut portal — see
[caveat](#xdg-desktop-portal-status) below.

---

## KDE Plasma

KDE has a richer shortcuts UI:

1. **System Settings → Shortcuts → Custom Shortcuts**.
2. **Edit → New → Global Shortcut → Command/URL**.
3. Set the trigger (e.g. `Meta+Shift+M`) and the action command:
   `bash -c "$HOME/.local/bin/dictate toggle"`.
4. Apply. Repeat for `--rp toggle` and `--rp+ toggle`.

KDE on Wayland uses the same global-shortcut portal contract as GNOME
under the hood, but Plasma's KGlobalAccel also has direct paths for
KDE-native apps. For shelling out to `bash`, the portal route is what
applies.

---

## Hyprland / Sway

Both use `wlroots`-style config; binding pattern is similar.

**Hyprland** (`~/.config/hypr/hyprland.conf`):

```ini
bind = SUPER SHIFT, M, exec, bash -c "$HOME/.local/bin/dictate toggle"
bind = SUPER CTRL, M, exec, bash -c "$HOME/.local/bin/dictate --rp toggle"
bind = SUPER CTRL SHIFT, M, exec, bash -c "$HOME/.local/bin/dictate --rp+ toggle"
```

**Sway** (`~/.config/sway/config`):

```
bindsym $mod+Shift+m exec bash -c "$HOME/.local/bin/dictate toggle"
bindsym $mod+Ctrl+m exec bash -c "$HOME/.local/bin/dictate --rp toggle"
bindsym $mod+Ctrl+Shift+m exec bash -c "$HOME/.local/bin/dictate --rp+ toggle"
```

Reload (`hyprctl reload` / `swaymsg reload`) and the bindings are live.

---

## X11 (sxhkd / xbindkeys / DE-native)

If you're still on X11, `sxhkd` is the cleanest:

```
# ~/.config/sxhkd/sxhkdrc
super + shift + m
    bash -c "$HOME/.local/bin/dictate toggle"

super + ctrl + m
    bash -c "$HOME/.local/bin/dictate --rp toggle"

super + ctrl + shift + m
    bash -c "$HOME/.local/bin/dictate --rp+ toggle"
```

Smart-paste detection on X11 uses `xprop` to read the focused window's
`WM_CLASS` instead of `niri msg`. The `dictate` script handles both.

---

## xdg-desktop-portal status

The Wayland global-shortcut portal (`org.freedesktop.portal.GlobalShortcuts`)
landed in `xdg-desktop-portal` 1.18 (March 2024) and is reaching general
availability across compositors through 2025–2026. Pre-1.0 status as of
this writing: usable in GNOME and KDE for portal-aware apps; not yet
adopted by `wlroots`-flavor compositors that prefer direct config-file
keybinds.

**What this means in practice:**

- **niri / Sway / Hyprland:** bind via the compositor's config file (as
  shown above). The portal is irrelevant; the compositor dispatches the
  hotkey directly.
- **GNOME / KDE:** bind via the desktop's settings UI. The portal
  provides a permission dialog to the user the first time a sandboxed
  app requests a global shortcut. `dictate` is not sandboxed (it's a
  bash script you run from your own `$HOME`), so this dialog won't
  fire — but be aware that if `dictate` is ever wrapped in a Flatpak
  / Snap, the portal becomes the ingress.

If you're on a Wayland compositor that lacks both a config-file binding
and portal support (rare in 2026), the workaround is to launch
`xdg-desktop-portal-gnome` or `-kde` separately and use it as the
shortcut broker. This is unusual; document it only if you actually run
into it.

---

## Smart-paste detection

`dictate detect_paste_mode` decides between two paste paths:

| Focused app | Paste path | Why |
|---|---|---|
| `kitty`, `foot`, `Alacritty`, `ghostty`, `wezterm` | `wl-copy <text>` then `wtype Ctrl+V` | Terminals swallow raw `wtype` keystrokes (CSI / OSC sequences confuse them). Clipboard+paste is reliable. |
| Anything else (browsers, editors, ST in a Chromium webapp) | `wtype --` followed by the text | Direct keystroke injection. Preserves cursor position, undo stack, IME state. |

The detection runs after every dictation, so if you alt-tab between a
terminal and a browser, each gets the right paste path.

---

## Test the hotkey end-to-end

```bash
# 1. Trigger manually
~/.local/bin/dictate toggle
# (start of recording — talk for 3 seconds)
~/.local/bin/dictate toggle
# (stop. text is pasted into the focused window.)

# 2. Verify whisper-cli ran (server-mode skips this)
journalctl --user -u dictation-server -n 30 --grep timing
# Should show a timing JSON line if you used --mode

# 3. Verify the lockfile cleaned up
ls /tmp/dictate.{pid,lock} 2>/dev/null
# (both should be absent post-toggle)
```

If the script appears to hang, kill it with `pkill -f pw-record` and
check `~/.local/share/dictation-server/cert.pem` is still valid (the
HTTPS POST to `/transcribe` will hang if cert validation fails on the
client side — `dictate` uses curl with `--cacert`).
