# Calliope on Tailscale (Tier 3, power-user)

Tailscale issues real Let's Encrypt certs for `<host>.<tailnet>.ts.net`
over DNS-01, transparently to the user. Pointing Calliope's TLS at one of
those certs gives you:

- **No "your connection is not private" warning** on any device — the
  cert is signed by Let's Encrypt, which every browser already trusts.
- **No per-device cert install dance.** Skip [`cert-trust.md`](cert-trust.md)
  entirely.
- **Phone reachability from anywhere on the tailnet** — coffee shop,
  cellular, friend's WiFi — no port-forward, no public DNS, no exposing
  port 8384 to the internet.
- **Automatic cert rotation** — Tailscale renews the cert before expiry.

The cost is: every device that wants to reach Calliope needs Tailscale
installed and signed in to your tailnet. For a single-user setup, this is
generally a tax you'd pay once and benefit from across many services.

This doc walks through the setup. It assumes you already have Calliope
running with the default self-signed cert.

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

`tailscale up` prints a URL — open it in a browser, sign in (or sign up
if this is your first node). The node joins your tailnet and gets a
stable name like `apollo-pc` (configured in the admin console).

Verify:

```bash
tailscale status
# Should list your node and any other tailnet nodes.

tailscale ip -4
# Should print a 100.x.y.z address.
```

The MagicDNS hostname is `<node>.<tailnet>.ts.net`. Find the tailnet name
under [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
("Tailnet name").

## 3. Enable HTTPS on your tailnet (one-time)

Tailscale's cert issuance requires MagicDNS + HTTPS to be enabled in the
admin console. Visit:

- [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
  → enable **MagicDNS**.
- [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
  → scroll to **HTTPS Certificates** → enable.

This is a one-time per-tailnet action. Subsequent nodes inherit the
setting.

## 4. Issue the cert

```bash
sudo tailscale cert "$(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')"
# OR explicitly:
sudo tailscale cert apollo-pc.tail-scale.ts.net
```

Tailscale writes two files into the current working directory:

```
apollo-pc.tail-scale.ts.net.crt
apollo-pc.tail-scale.ts.net.key
```

Move them somewhere stable and readable by your user:

```bash
sudo mv apollo-pc.tail-scale.ts.net.crt ~/.local/share/dictation-server/tailscale-cert.pem
sudo mv apollo-pc.tail-scale.ts.net.key ~/.local/share/dictation-server/tailscale-key.pem
sudo chown $USER:$USER ~/.local/share/dictation-server/tailscale-{cert,key}.pem
chmod 0644 ~/.local/share/dictation-server/tailscale-cert.pem
chmod 0600 ~/.local/share/dictation-server/tailscale-key.pem
```

Or use the helper subcommand (Phase 4 MVP-24):

```bash
dictation-server tailscale-cert
```

This wraps the steps above and rewrites the runtime cert path for you.

## 5. Point Calliope at the new cert

Edit `~/.config/systemd/user/dictation-server.service.d/override.conf`
(create the file + parent dir if needed):

```ini
[Service]
Environment=DICTATION_CERT_FILE=%h/.local/share/dictation-server/tailscale-cert.pem
Environment=DICTATION_KEY_FILE=%h/.local/share/dictation-server/tailscale-key.pem
Environment=DICTATION_BIND_HOST=100.x.y.z
```

Replace `100.x.y.z` with the output of `tailscale ip -4`. Binding to the
tailnet IP — not `0.0.0.0` — restricts the server to tailnet clients
only. LAN devices that aren't on the tailnet can no longer reach it,
which is what you want.

Reload + restart:

```bash
systemctl --user daemon-reload
systemctl --user restart dictation-server
```

Verify:

```bash
journalctl --user -u dictation-server -n 20
# look for: bound to 100.x.y.z:8384, cert sha256: ...
```

## 6. Point your phone at the tailnet hostname

On the phone:

1. Install the Tailscale app, sign in to the same tailnet.
2. In the phone browser, visit `https://apollo-pc.tail-scale.ts.net:8384`
   — replacing the hostname with your own.
3. **No cert warning.** The page loads, you log in with the bearer token
   (auto-filled if you scanned the wizard QR), bookmark, install as PWA.

The phone will reach Calliope over the tailnet from any network — coffee
shop WiFi, cellular, friend's network — provided the phone is signed in
to Tailscale.

## 7. Cert rotation

Tailscale renews the cert automatically. Calliope re-reads the cert files
on restart, so add a periodic restart or a file-watcher:

```ini
# ~/.config/systemd/user/dictation-server.service.d/cert-rotate.conf
[Service]
# Re-read cert files weekly (Tailscale renews before 30d expiry)
ExecReload=/bin/kill -HUP $MAINPID
```

Plus a small timer to re-issue the cert via Tailscale:

```bash
# ~/.config/systemd/user/tailscale-cert-renew.timer
[Unit]
Description=Re-issue Tailscale cert weekly

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

Or accept that you'll restart the service manually every 60 days — for
single-user use this is fine.

---

## Why not Tailscale Funnel?

Funnel exposes a tailnet service to the public internet. Calliope is a
single-user, LAN-or-tailnet-only service; making it public is the wrong
posture and reintroduces the LAN-attack-surface concerns from PRIVACY.md.
**Don't enable Funnel for Calliope.**

If you genuinely need internet access (rare — RP isn't usually time-critical
across networks you don't control), the safer path is: Tailscale on a
mobile data plan + your phone, no Funnel.

## Troubleshooting

- **`tailscale cert` fails with "DNS-01 challenge failed":** ensure
  MagicDNS + HTTPS are both enabled in the admin console (step 3). Wait
  60s after enabling before retrying — propagation isn't instant.
- **Phone can't resolve the hostname:** ensure the phone is signed in to
  the tailnet (`tailscale status` on the phone). Some Android VPN apps
  conflict — disable other VPNs while testing.
- **Bearer token rejected after switching:** the token is independent of
  the cert. Re-fetch the token from the wizard QR or
  `cat ~/.local/share/dictation-server/token` and paste it into the
  phone PWA settings.
