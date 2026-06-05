# Trusting the Calliope cert (Tier 1, default ship)

Calliope ships with a self-signed TLS cert generated at first run. That
cert is valid for 90 days and rotates automatically at startup once it's
within the renewal threshold. It is signed by your own machine, not by a
public CA, so every browser will warn on first visit.

This document walks through making that warning go away on each
platform — once per device, not once per session.

You have three options, in order of effort:

1. **Click through the warning.** Acceptable for a quick test. The
   browser remembers the exception per profile.
2. **Install the Calliope cert as a user CA on each device** (this doc).
   One-time setup per device. The cert is then trusted system-wide on
   that device.
3. **Use a real public-CA cert via Tailscale.** No warnings, no manual
   trust. See [`tailscale.md`](tailscale.md). Requires Tailscale on every
   device.

---

## Verify the fingerprint first

Calliope prints the SHA-256 fingerprint of the cert at server startup:

```
$ journalctl --user -u dictation-server | grep -i fingerprint
calliope: cert fingerprint sha256: 8F:23:1C:...:E7:42
```

It is also written to `~/.local/share/dictation-server/cert.fingerprint`.

**Match this fingerprint before trusting the cert** on any device. If you
see a different fingerprint in the browser's "view certificate" dialog,
someone is MITMing your LAN — back out and investigate.

---

## Pull the cert off the server

The cert lives at `~/.local/share/dictation-server/cert.pem`. Get it onto
the device that needs to trust it:

```bash
# To another laptop on LAN (SSH)
scp pc:~/.local/share/dictation-server/cert.pem ./calliope-cert.pem

# To phone via USB
adb push ~/.local/share/dictation-server/cert.pem /sdcard/Download/calliope-cert.pem

# To phone via SSH from the phone (Termux)
scp pc:~/.local/share/dictation-server/cert.pem ~/storage/downloads/calliope-cert.pem

# To phone via email — mail it to yourself, save the attachment
```

Phones generally need the cert with a `.crt` or `.pem` extension and the
file picker to find it in `Downloads/` or local storage.

---

## Android (Samsung Internet, Chrome)

Tested on Samsung Z Fold 6 (Samsung Internet, Chrome).

1. Copy `calliope-cert.pem` to the phone (`Downloads/` is fine).
2. Open **Settings → Biometrics and security → Other security settings →
   Install from device storage**. (Path varies by OEM. On stock Android:
   **Settings → Security → Encryption & credentials → Install a
   certificate → CA certificate**.)
3. Confirm the "your data may not be private" prompt — this is Android
   warning that user-installed CAs are sandboxed away from app TLS by
   default. That's correct: it only affects browsers and apps that have
   opted in to the user CA store. Calliope's phone PWA runs in the
   browser, so user CAs apply.
4. Pick `calliope-cert.pem` from the file picker.
5. Name the cert (`Calliope LAN`).
6. Verify in **Settings → Security → Encryption & credentials → Trusted
   credentials → User**. The cert appears with the name you gave it.
7. Restart Samsung Internet / Chrome. Visit `https://<host>:8384` —
   no warning.

Note: Android 11+ scopes user-installed CAs to user-space (system apps
ignore them). This is a deliberate sandbox; it does not affect the
phone-PWA flow.

## iOS (Safari)

1. Mail `calliope-cert.pem` to yourself, or AirDrop / drop it into iCloud
   Drive.
2. Tap the cert in Mail / Files. iOS prompts: "Profile downloaded.
   Review the profile in Settings."
3. Open **Settings → General → VPN & Device Management → Downloaded
   Profile** → tap `Calliope LAN` → **Install** (top right). Enter
   passcode. Confirm "Install" and "Install" again.
4. Now the cert is *installed* but not *trusted for SSL*. iOS requires a
   second explicit step: **Settings → General → About → Certificate Trust
   Settings**.
5. Toggle the switch next to `Calliope LAN` to ON. iOS shows a final
   warning: "Enabling this certificate will allow third parties to monitor
   your data." That warning is generic — for a self-signed cert you
   verified the fingerprint of, the only "third party" is your own
   server.
6. Visit `https://<host>:8384` in Safari. No warning.

## Firefox (desktop)

Firefox maintains its own trust store, separate from the OS. Importing the
cert into the OS trust store does not help Firefox; you must import here.

1. Open `about:preferences#privacy`.
2. Scroll to **Certificates** → **View Certificates**.
3. Tab to **Authorities**. Click **Import**.
4. Pick `calliope-cert.pem`.
5. Check **Trust this CA to identify websites**. Click OK.
6. Close the dialog. Refresh `https://<host>:8384`. No warning.

## Chrome / Chromium / Brave / Edge (desktop)

Chromium-based browsers use the OS trust store on Windows + macOS, and
NSS on Linux.

**Linux:**

```bash
# Add to NSS user store (Chromium on Linux uses NSS)
mkdir -p ~/.pki/nssdb
certutil -d sql:$HOME/.pki/nssdb -A -t "CT,," -n "Calliope LAN" \
    -i ~/.local/share/dictation-server/cert.pem
```

`certutil` lives in `nss` / `libnss3-tools` (`pacman -S nss`,
`apt install libnss3-tools`).

Or via the Chromium UI:

1. `chrome://settings/certificates` → tab to **Authorities** →
   **Import**.
2. Pick `calliope-cert.pem`.
3. Check **Trust this certificate for identifying websites**. Click OK.

**macOS:**

1. Double-click `calliope-cert.pem`. Keychain Access opens.
2. Add to **System** keychain (or **login** if you don't have admin).
3. Double-click the cert in Keychain → expand **Trust** → set **When
   using this certificate** to **Always Trust**. Close (admin password).

**Windows:**

1. Double-click `calliope-cert.pem`. **Install Certificate**.
2. Place in **Trusted Root Certification Authorities**. Confirm.

---

## Server-side fingerprint check

Whenever you re-pair a device, re-verify the fingerprint matches:

```bash
# On the server
openssl x509 -in ~/.local/share/dictation-server/cert.pem -fingerprint -sha256 -noout
# SHA256 Fingerprint=8F:23:1C:...:E7:42

# Alternatively, the cached fingerprint
cat ~/.local/share/dictation-server/cert.fingerprint

# Calliope startup log
journalctl --user -u dictation-server | grep -i fingerprint | tail -1
```

For phone pairing, use the SillyTavern extension's **Show local QR** or
**Copy pairing URL** controls. The QR is rendered locally in the browser and
contains the bearer-token pairing URL, so treat it like a password and avoid
screenshots/logs.

## When the cert rotates

Calliope rotates the cert at server startup if it's within the renewal
threshold (`CERT_RENEW_THRESHOLD_DAYS`, currently 14). After rotation:

1. The fingerprint changes. The startup log shows the new value.
2. Devices that trusted the *old* cert will see a fresh "your connection
   is not private" warning. Re-import the new cert, or click through.
3. Re-verify the new fingerprint before trusting the rotated cert on each
   phone/browser profile.

If you want to avoid the rotation churn entirely, use Tailscale (cert is
managed by Tailscale + Let's Encrypt — see
[`tailscale.md`](tailscale.md)).
