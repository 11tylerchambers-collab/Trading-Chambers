# INSTALL — Phase 0 host setup

Two supported hosts. **Use the Linux VPS.** A desktop that sleeps, updates, or
loses Wi-Fi is the single biggest cause of "it was green all day but nothing
happened." The Windows path is a fallback only.

Everything below assumes the repo is at `/opt/chambers` (Linux) or
`C:\chambers` (Windows). Adjust paths if you clone elsewhere.

---

## A. Linux VPS (recommended)

Target: Ubuntu 24.04, 2 vCPU, 2 GB RAM. Any small VPS provider works.

### A.1 System packages and a service user

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
sudo useradd --system --create-home --shell /usr/sbin/nologin chambers
```

### A.2 Clone and create the venv

```bash
sudo git clone https://github.com/11tylerchambers-collab/Trading-Chambers.git /opt/chambers
sudo chown -R chambers:chambers /opt/chambers
sudo -u chambers bash -c 'cd /opt/chambers && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt'
```

### A.3 Secrets

```bash
sudo -u chambers cp /opt/chambers/.env.example /opt/chambers/.env
sudo -u chambers nano /opt/chambers/.env
sudo chmod 600 /opt/chambers/.env
```

Fill in the four values. `ALPACA_PAPER` must be exactly `true` or the process
refuses to start. Use the **paper** key pair from the Alpaca dashboard.

### A.4 Smoke test before installing the service

```bash
sudo -u chambers /opt/chambers/.venv/bin/python -m chambers.main --smoke
```

Expected: account dict, market clock, 3 AAPL bars, one SPY quote. If this
fails, fix the keys before going on. Then run one cycle:

```bash
sudo -u chambers /opt/chambers/.venv/bin/python -m chambers.main --once
```

After hours this writes one `cycles` row and 20 `signals` rows with
`reason = entries_closed`.

### A.5 Install the systemd service

```bash
sudo cp /opt/chambers/deploy/chambers.service /etc/systemd/system/chambers.service
sudo systemctl daemon-reload
sudo systemctl enable --now chambers
sudo systemctl status chambers
```

`Restart=always` with `RestartSec=10`: if the process dies for any reason,
systemd restarts it within 10 seconds, the engine reconciles against the
broker, and trading resumes. This is the §13.8 restart test:

```bash
sudo systemctl kill -s SIGKILL chambers    # simulate a crash mid-session
sleep 15 && sudo systemctl status chambers # should be active (running) again
sudo journalctl -u chambers -n 50          # look for "reconcile:" lines
```

### A.6 Logs

```bash
sudo journalctl -u chambers -f              # live
sudo journalctl -u chambers --since today   # today's
tail -f /opt/chambers/data/logs/chambers.log   # same content, rotating file
```

The database is `/opt/chambers/data/chambers.db`. Back it up whenever you
like with `sqlite3 chambers.db ".backup backup.db"` — WAL mode makes that
safe while the engine runs.

### A.7 Updating code

Code changes require a restart; config/param changes do not (the engine reads
`params` from the database every cycle, and pause/flatten are database flags).

```bash
cd /opt/chambers && sudo -u chambers git pull
sudo -u chambers .venv/bin/pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart chambers
```

Restart outside market hours when you can. Restarting mid-session is safe
(reconcile adopts open positions) but costs one or two cycles.

---

## B. Phone access with Tailscale (both hosts)

The dashboard binds to `0.0.0.0:8080`. Do **not** open that port to the
internet. Reach it over Tailscale instead: no public port, no port
forwarding, no TLS setup.

1. **Host:** install Tailscale and log in.
   Linux: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
   Windows: install from https://tailscale.com/download, sign in.
2. **Phone:** install the Tailscale app from the App Store / Play Store and
   sign in to the same account. Turn it on.
3. **Open the dashboard:** on the host run `tailscale ip -4` to get its
   Tailscale IP (100.x.y.z). On the phone browse to `http://100.x.y.z:8080`.
   Enter `DASH_PASSWORD`. Add to Home Screen for an app-like icon.

The password is still required on the Tailscale network. Tokens live in
memory only; a host restart logs every browser out.

### B.1 Firewall: Tailscale only

Lock the host down so that nothing, SSH included, is reachable from the
public internet. Do this only **after** you have confirmed SSH works over the
Tailscale IP (`ssh root@100.x.y.z`) and that `tailscaled` starts on boot
(`systemctl is-enabled tailscaled` → `enabled`).

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0      # SSH, dashboard, everything, over Tailscale only
sudo ufw --force enable
sudo ufw status verbose              # only the two "on tailscale0" rules (v4 + v6)
```

If you enabled ufw earlier with `ufw allow OpenSSH`, remove it with
`sudo ufw delete allow OpenSSH` (removes the v4 and v6 rules). Existing
sessions survive; new public connections are refused.

Verify from outside, not from the host itself (local traffic bypasses the
firewall): an external TCP check of `<public IP>:22` and `:8080`, for example
check-host.net, should time out.

### B.2 Lockout fallback: DigitalOcean Recovery Console

If Tailscale is down, logged out, or its node key expired, SSH is unreachable
by design. Get in through the provider's out-of-band console instead:

1. DigitalOcean control panel → the droplet → **Access** → **Launch Recovery
   Console**. This is a virtual screen attached to the VM. It does not use the
   network or SSH, so ufw does not affect it.
2. Log in as `root` with the **root password**. Key-only access does not work
   here. If you never set one, use **Access → Reset Root Password** (DigitalOcean
   emails a new one; this reboots the droplet, and the engine reconciles on
   start), or set one now while you still have SSH: `sudo passwd root`.
3. Fix Tailscale: `systemctl status tailscaled`, `tailscale status`, and
   `tailscale up` to log in again (open the printed URL on your phone).
4. As a last resort, reopen public SSH temporarily with `ufw allow OpenSSH`, then
   `ufw delete allow OpenSSH` once you are back in over Tailscale.

The **Droplet Console** button (the browser-based one, not Recovery) connects
over SSH on port 22 through DigitalOcean's agent, so expect it to fail while
public SSH is closed. Use Recovery Console.

To avoid the most common lockout, disable key expiry for this machine in the
Tailscale admin console (Machines → the host → **Disable key expiry**). By
default node keys expire after 180 days.

---

## C. Editing from the phone

- **Thresholds, universe params, pause, flatten:** the dashboard. Params
  saved there apply on the next cycle and are recorded in `params_history`
  with `source = manual`.
- **Code:** the repo is on GitHub. Use a Claude Code remote session from the
  Claude mobile app against this repository, or SSH to the host over its
  Tailscale IP (`ssh user@100.x.y.z`), pull, and `systemctl restart chambers`.

---

## D. Windows desktop (fallback)

**The risk, plainly:** Windows will sleep, hibernate, install updates and
reboot, or drop Wi-Fi, and the engine stops with it. The heartbeat on the
dashboard will show red; nothing trades until it is back. If you must use
this path, disable sleep and check the heartbeat every morning.

### D.1 Install

```powershell
winget install Python.Python.3.12 Git.Git
git clone https://github.com/11tylerchambers-collab/Trading-Chambers.git C:\chambers
cd C:\chambers
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env      # fill in the four values; ALPACA_PAPER=true
.venv\Scripts\python -m chambers.main --smoke
```

### D.2 Disable sleep (run PowerShell as Administrator)

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /hibernate off
```

Also: Settings → System → Power → "When plugged in, put my device to sleep
after" → Never. Pause Windows Update during market hours
(Settings → Windows Update → Advanced → Active hours 9:00–17:00).

### D.3 Start at logon with Task Scheduler

`deploy\run_windows.bat` runs the engine in a restart loop (10 s delay), the
Windows equivalent of `Restart=always`.

```powershell
schtasks /Create /TN "Trading Chambers" /SC ONLOGON /RL HIGHEST /F ^
  /TR "\"C:\chambers\deploy\run_windows.bat\""
```

Or in the Task Scheduler UI: Create Task → Trigger "At log on" → Action
"Start a program" → `C:\chambers\deploy\run_windows.bat` → check "Run with
highest privileges" → in Settings, uncheck "Stop the task if it runs longer
than" and check "If the task fails, restart every 1 minute".

Enable automatic logon (`netplwiz`, uncheck "Users must enter a user name and
password") so a reboot comes back trading. Logs are in
`C:\chambers\data\logs\chambers.log`.

### D.4 Restart test

Open Task Manager, end the `python.exe` process. The bat loop restarts it
within 10 seconds; the log shows `reconcile:` lines and the next cycle.
