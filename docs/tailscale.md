# Calliope on Tailscale (Tier 3, power-user)

Tailscale issues real Let's Encrypt certs for `<node>.<tailnet>.ts.net`
over DNS-01, transparently to the user. Pointing Calliope's TLS at one of
those certs gives you:

- **No "your connection is not private" warning** on any device — the
  cert is signed by Let's Encrypt, which every browser already trusts.
- **No per-device cert install dance.** Skip [`cert-trust.md`](cert-trust.md)
  entirely.
- **Phone reachability from anywhere on the tailnet** — coffee shop,
  cellular, friend's WiFi — no port-forward, no public DNS, no exposing
  port 8384 to the internet.

Renewal is **not** automatic: Calliope never regenerates an externally
issued cert (see [§7](#7-renewal)). Plan for it.

The cost is: every device that wants to reach Calliope needs Tailscale
installed and signed in to your tailnet.

This doc assumes you already have Calliope running with the default
self-signed cert (`dictation-server --setup`).

---

## 1. Install Tailscale

**Arch / CachyOS:**

```bash
sudo pacman -S tailscale
```

**Other distributions:** see [tailscale.com/download/linux](https://tailscale.com/download/linux).

## 2. Bring Tailscale up

```bash
sudo systemctl enable --now tailscaled
sudo tailscale up
```

`tailscale up` prints a URL — open it in a browser and sign in. Verify:

```bash
tailscale status    # lists your node
tailscale ip -4     # prints a 100.x.y.z address
```

The MagicDNS hostname is `<node>.<tailnet>.ts.net`.

## 3. Enable HTTPS on your tailnet (one-time)

In the admin console ([login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns))
enable **MagicDNS** and **HTTPS Certificates**. This is per-tailnet.

Let your user ask `tailscaled` for certs without root (needed for the
unattended renewal in §7; interactive runs fall back to `sudo`):

```bash
sudo tailscale set --operator="$USER"
```

## 4. Issue and install the cert

```bash
dictation-server --tailscale-cert
```

What it does (`run_tailscale_cert` in `server/calliope-server`):

1. Resolves this node's MagicDNS name from `tailscale status --json`.
2. Runs `tailscale cert --cert-file … --key-file …` writing temporary files
   straight into the Calliope data dir (`~/.local/share/dictation-server/`,
   or `$CALLIOPE_DATA_DIR`). Retries with `sudo` only if `tailscaled`
   refuses for lack of privilege, then chowns the files back to you.
3. Validates the new cert matches the hostname and that cert and key pair
   (`openssl x509 -checkhost`, public-key comparison).
4. Backs up the current pair as `cert.pem.pre-tailscale.bak` /
   `key.pem.pre-tailscale.bak` and atomically replaces the live
   **`cert.pem`** and **`key.pem`** in the data dir (restoring the backup
   if the second replace fails). Modes are set to 0644 / 0600, and
   `cert.fingerprint` is refreshed.
5. Offers to run `systemctl --user restart dictation-server`. Answer
   no and the running server keeps serving the old cert until you restart.

There are no cert-path environment variables: the server always serves
`cert.pem` / `key.pem` from its data dir. Once that pair is not a
Calliope self-signed cert, startup and the 12-hourly in-run check treat it
as externally managed and never overwrite it.

## 5. Bind to the tailnet (optional)

To make the server reachable only over the tailnet, create
`~/.config/systemd/user/dictation-server.service.d/tailscale.conf`:

```ini
[Service]
Environment=DICTATION_BIND_HOST=100.x.y.z
```

Replace `100.x.y.z` with `tailscale ip -4`. Binding to the tailnet IP — not
`0.0.0.0` — keeps LAN devices that aren't on the tailnet out. Then:

```bash
systemctl --user daemon-reload
systemctl --user restart dictation-server
journalctl --user -u dictation-server -n 20    # look for the bind address
```

## 6. Point your phone at the tailnet hostname

1. Install the Tailscale app on the phone and sign in to the same tailnet.
2. Open `https://<node>.<tailnet>.ts.net:8384` — no cert warning.
3. Pair with a fresh one-time `?pair=` URL from the ST extension's **Copy
   pairing URL** (swap in the tailnet hostname if the extension points
   elsewhere), bookmark, install as PWA.

## 7. Renewal

Let's Encrypt certs last 90 days. Calliope **does not renew** an
externally issued cert: near expiry it only logs
`Externally-managed SSL cert expires in N days` (warning below 30 days).
There is no SIGHUP reload; a new cert takes effect on restart.

Renew by re-running `dictation-server --tailscale-cert` (it restarts the
service at the end). To automate it, add a user service + timer. Under
systemd the confirmation prompts read EOF and take their defaults —
proceed, and restart `dictation-server` — so each run briefly interrupts
the server; schedule it when you aren't dictating. It requires the
`--operator` setting from §3 (no `sudo` prompt is possible unattended).

```ini
# ~/.config/systemd/user/calliope-tailscale-cert.service
[Unit]
Description=Re-issue Calliope's Tailscale cert

[Service]
Type=oneshot
ExecStart=%h/.local/bin/dictation-server --tailscale-cert
```

```ini
# ~/.config/systemd/user/calliope-tailscale-cert.timer
[Unit]
Description=Re-issue Calliope's Tailscale cert monthly

[Timer]
OnCalendar=monthly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now calliope-tailscale-cert.timer
systemctl --user list-timers calliope-tailscale-cert.timer
```

`tailscale cert` hands back the cached cert until it is inside its own
renewal window, so a monthly run renews well before expiry. Check the
result with `journalctl --user -u calliope-tailscale-cert`.

---

## Why not Tailscale Funnel?

Funnel exposes a tailnet service to the public internet. Calliope is a
single-user, LAN-or-tailnet-only service; making it public is the wrong
posture and reintroduces the attack-surface concerns from PRIVACY.md.
**Don't enable Funnel for Calliope.**

## Troubleshooting

- **`tailscale cert` fails with "DNS-01 challenge failed":** ensure
  MagicDNS + HTTPS are both enabled in the admin console (step 3). Wait
  60s after enabling before retrying — propagation isn't instant.
- **`new cert failed validation`:** the issued cert does not name this
  node, or cert and key don't match; the live pair was left untouched.
- **Timer run fails with access denied:** set the operator (§3).
- **Phone can't resolve the hostname:** ensure the phone is signed in to
  the tailnet. Some Android VPN apps conflict — disable other VPNs while
  testing.
- **Bearer token rejected after switching:** the token is independent of
  the cert. Re-pair with a fresh one-time `?pair=` URL from the ST extension.
