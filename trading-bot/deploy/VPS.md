# Deploy the bot on a VPS

This walkthrough takes you from an empty server to a paper-trading bot that runs around the
clock. It takes about an hour. You finish with:

- A hardened Ubuntu 24.04 server that accepts SSH keys only.
- The bot and its dashboard running in Docker, restarting on crashes and on reboot.
- The dashboard reachable only through an SSH tunnel.
- Telegram alerts, approvals and `/kill`.
- Nightly backups of the bot's state.

> **Not financial advice.** This bot trades a paper account by default. Paper results don't
> predict live results. Read the **WHAT COULD BLOW UP THIS ACCOUNT?** section of `strategy.md`
> before you ever consider live money.

The guide uses `trader` as your username and `~/trading-bot` as the repository path. Replace them
if yours differ. `SERVER_IP` stands for your server's public IP address.

## What runs where

| Where | What | Why |
|---|---|---|
| Docker: `bot` | `python -m bot run` | The live loop. It restarts on crashes and at boot. |
| Docker: `dashboard` | `python -m bot dashboard` | The read-only web UI, published on `127.0.0.1:8080` only. |
| Host: Python virtual environment | `fetch-data`, `tournament`, `pytest`, `drill-kill-switch`, `final-check` | The tournament and the final check write `strategy.md` and `results/`, which the containers mount read-only. The image also ships without test dependencies. |
| Host: cron | `deploy/backup.sh` | Nightly copies of `var/`. |

Both sides share `var/` and `data/` in the repository folder.

## Before you start

You need:

- An Alpaca account with **paper trading** API keys.
- A TypeSafe AI API key for Jev. Without it, the bot blocks every entry, because Jev fails closed.
- Telegram on your phone. You create the bot in [TELEGRAM.md](TELEGRAM.md).
- A terminal with `ssh` on your own computer.
- A password manager for your keys.

If you don't have an SSH key yet, create one on your computer:

```bash
ssh-keygen -t ed25519 -C "trading-bot vps"
cat ~/.ssh/id_ed25519.pub
```

The `.pub` file is your public key, and you can share it safely. The private key
(`~/.ssh/id_ed25519`) never leaves your computer.

## 1. Pick a provider

The bot trades daily bars, so it needs very little power. Pick any of these:

| Provider | Plan to pick | Notes |
|---|---|---|
| Hetzner Cloud | Smallest shared vCPU plan | Usually the cheapest. |
| DigitalOcean | Basic Droplet, 1 vCPU, 1–2 GB | Simple console. |
| Amazon Lightsail | Linux/Unix, OS only, 1–2 GB | The default user is `ubuntu`. |

Aim for 1 vCPU and 1–2 GB of RAM, which costs about $5–10 per month. With 1 GB, add swap as
shown in [Troubleshooting](#troubleshooting). Latency barely matters for daily bars, so pick a
region near you or on the US East Coast.

Turn on the provider's snapshot or backup add-on if it's cheap. It's your off-site copy.

## 2. Create the server

1. Choose the **Ubuntu 24.04 LTS** image.
2. Add your SSH public key during creation.
3. Create the server and copy its public IP address.

On Lightsail, open the instance's **Networking** tab and delete the HTTP (port 80) rule. Keep
only SSH.

Log in and update everything:

```bash
ssh root@SERVER_IP
apt update && apt full-upgrade -y
[ -f /var/run/reboot-required ] && reboot
```

If the server reboots, wait a minute and log in again.

On Lightsail, log in as `ubuntu@SERVER_IP` instead, and put `sudo` in front of both `apt`
commands and `reboot`.

## 3. Create a non-root sudo user

You'll run everything as a normal user. Root login gets switched off in the next step.

```bash
adduser trader
usermod -aG sudo trader
rsync --archive --chown=trader:trader ~/.ssh /home/trader
```

`adduser` asks for a password. You need it for `sudo`, not for SSH.

On Lightsail, skip this step. The `ubuntu` user already exists and has `sudo`. Use `ubuntu`
wherever this guide says `trader`.

Keep the root session open. In a **second** terminal on your computer, check that the new user
works:

```bash
ssh trader@SERVER_IP
sudo -v
```

## 4. Allow SSH keys only

Switch off password logins and root logins. The file name starts with `00-` so it wins over
any cloud-init defaults.

```bash
sudo tee /etc/ssh/sshd_config.d/00-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl restart ssh
sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

All three lines should end in `no`.

Test from a **new** terminal on your computer before you close any session:

```bash
ssh trader@SERVER_IP                            # works
ssh root@SERVER_IP                              # Permission denied (publickey)
ssh -o PubkeyAuthentication=no trader@SERVER_IP # Permission denied (publickey)
```

If you lock yourself out, use your provider's web console to undo the change.

## 5. Turn on the firewall

Allow SSH in and nothing else:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status verbose
```

> **Docker bypasses ufw for published ports.** That's why `docker-compose.yml` publishes the
> dashboard on `127.0.0.1` only. Never change it to `0.0.0.0`, and never remove the IP.

## 6. Install security updates automatically

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

Choose **Yes**. Then check that both settings are `"1"`:

```bash
cat /etc/apt/apt.conf.d/20auto-upgrades
```

Kernel updates need a reboot. You can let the server reboot itself at a quiet hour:

```bash
sudo tee /etc/apt/apt.conf.d/52trading-bot-reboot >/dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
```

At 04:30 UTC the US stock market is closed, and the 00:00 UTC crypto bar is long done. The
bot comes back on its own after the reboot. It checks no BTC/USD stops during the minute or two
the server is down.

## 7. Install fail2ban

fail2ban bans IP addresses that keep failing to log in.

```bash
sudo apt install -y fail2ban
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
sudo systemctl enable fail2ban
sudo systemctl restart fail2ban
sudo fail2ban-client status sshd
```

## 8. Sync the clock

The bot uses the system clock to decide when a daily bar has closed, when an approval expires,
and when the New York trading day resets the daily loss limit. A drifting clock means wrong
trades. chrony keeps it accurate.

```bash
sudo timedatectl set-timezone UTC
sudo apt install -y chrony
chronyc tracking
timedatectl
```

Check for `Leap status : Normal` in `chronyc tracking`. Check for
`System clock synchronized: yes` in `timedatectl`. Installing chrony replaces
`systemd-timesyncd`, which is expected.

## 9. Install Docker Engine and the compose plugin

Install Docker from Docker's own apt repository. Ubuntu's `docker.io` package lags behind.

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Let your user run Docker without `sudo`, then log out and back in:

```bash
sudo usermod -aG docker "$USER"
exit
```

```bash
ssh trader@SERVER_IP
docker run --rm hello-world
docker compose version
```

Membership in the `docker` group is equivalent to root. Add only your own user to it.

## 10. Install Python 3.11 and tools

The host needs Python for the tournament, the tests and the final check. Ubuntu 24.04 ships
Python 3.12. The project and its Docker image use 3.11, so install 3.11 from the deadsnakes PPA
to test on the same version you run.

```bash
sudo apt install -y git sqlite3 software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.11 python3.11-venv
```

## 11. Clone the repository

```bash
cd ~
git clone https://github.com/<you>/trading-bot.git
cd ~/trading-bot
mkdir -p var data/cache results
```

Create the three folders before the first `docker compose up`. If they're missing, Docker
creates them owned by root, and the bot can't write to them.

If the repository is private, add a read-only deploy key to it on GitHub and clone over SSH.

## 12. Create your .env

`.env` holds every secret. It stays on this server, readable only by you.

```bash
cp .env.example .env
chmod 600 .env
id -u && id -g
openssl rand -hex 24
nano .env
```

Fill in these values:

| Variable | Value |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Your Alpaca **paper** keys. |
| `TYPESAFE_API_KEY` | Your TypeSafe AI key for Jev. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Follow [TELEGRAM.md](TELEGRAM.md). |
| `DASHBOARD_PASSWORD` | The output of `openssl rand -hex 24`. The dashboard refuses to start in Docker without it. |
| `APP_UID`, `APP_GID` | The two numbers from `id -u` and `id -g`. |

Keep `TRADING_MODE=paper` and `ALPACA_PAPER=true`. Leave `LIVE_TRADING_ACK` empty.

Check the permissions. The line must start with `-rw-------`:

```bash
ls -l .env
```

> **Never paste a key into a chat.** That includes AI assistants, Telegram, Slack and email.
> Paste keys from your password manager straight into `nano` on the server. Give every key
> trade permission only, with withdrawals off. Alpaca Trading API keys can't withdraw money.

## 13. Fetch data, run the tournament and run the tests

Create the host virtual environment:

```bash
cd ~/trading-bot
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

Then run the pipeline:

```bash
python -m bot fetch-data
python -m bot tournament --apply
python -m pytest
python -m bot jev-ping
python -m bot status
```

- `fetch-data` downloads daily bars for SPY, QQQ and BTC/USD into `data/cache/`.
- `tournament --apply` backtests every strategy on every asset. It enables at most one winner per
  asset in `strategy.md`.
- `pytest` must pass. Don't deploy on a red test run.
- `jev-ping` checks your TypeSafe key.
- `status` shows the mode, the enabled assets and the kill-switch state.

Read the results before you go on:

```bash
less results/tournament.md
```

Also read the **Winners** section of `strategy.md`. An asset with no surviving strategy stays
disabled, and the bot won't trade it. That's the risk filter doing its job.

To watch the loop without a broker, run `python -m bot run --dry-run` for a minute. It uses a
simulated broker and logs alerts to the console. Press **Ctrl+C** to stop it.

> Never run `python -m bot run` without `--dry-run` on the host while the containers run. Two
> bots would trade the same account.

## 14. Run the kill-switch drill

The drill opens a small position, trips the kill switch, and checks that the bot cancels
orders, goes flat, refuses new entries and sends an alert.

```bash
python -m bot drill-kill-switch
python -m bot drill-kill-switch --paper
python -m bot status
```

- The first run uses a simulated broker.
- The second run uses your real Alpaca paper account.
- Both write `var/kill_switch_drill.json`.

If `status` still shows the kill switch as tripped, clear it with
`python -m bot resume --confirm`.

The final check needs a drill younger than 30 days, so repeat the drill every month. Once the
bot runs, stop it first with `docker compose stop bot`, and start it afterwards with
`docker compose start bot`. The `--paper` drill closes bot positions in your paper account, so
run it only while the bot holds no positions.

## 15. Start the bot

```bash
docker compose build
docker compose up -d
docker compose ps
```

After about 30 seconds, `bot` shows as running and `dashboard` shows as `healthy`.

Send `/status` to your Telegram bot. It should answer.

## 16. Open the dashboard through an SSH tunnel

The dashboard listens on the server's `127.0.0.1:8080` only. Reach it through SSH from your
computer:

```bash
ssh -N -L 8080:127.0.0.1:8080 trader@SERVER_IP
```

Open `http://localhost:8080` in your browser. Log in with `DASHBOARD_USER` and
`DASHBOARD_PASSWORD` from `.env`. Press **Ctrl+C** in the terminal to close the tunnel.

Never open port 8080 in ufw.

## 17. Read the logs

```bash
docker compose logs -f bot
docker compose logs --since 1h dashboard
docker compose exec bot python -m bot status
ls var/reports/
```

Docker rotates each container's log at 10 MB and keeps five files. The daily report also lands
in `var/reports/YYYY-MM-DD.md` and in Telegram.

## 18. Stop trading in an emergency

Use the first option that works:

1. Send `/kill` to your Telegram bot.
2. On the server, run
   `docker compose exec bot python -m bot kill --reason "manual stop"`.
3. In the Alpaca dashboard, close all positions and cancel all open orders.

The kill switch cancels the bot's orders, closes its positions and blocks new entries.

> **Stopping the containers doesn't close positions.** The bot checks stops itself and sends a
> market exit when the price crosses a stop. No stop orders rest at Alpaca. While the bot is
> down, nothing protects your open positions.

Clear the kill switch only after you know why it tripped:

```bash
docker compose exec bot python -m bot resume --confirm
```

If `.env` has `KILL_SWITCH=1`, `resume` refuses. Set it back to `0`, then run
`docker compose up -d` first.

## 19. Update the bot

```bash
cd ~/trading-bot
git pull
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
docker compose build
docker compose up -d
docker image prune -f
```

`docker compose up -d` recreates the containers whose image changed. Check
`docker compose ps` and the logs afterwards.

### Change the rules

The containers mount `strategy.md` and `results/` read-only. You change them from the host, for
example by running `python -m bot tournament --apply` again. Then recreate both containers so
they reload the rules:

```bash
docker compose up -d --force-recreate
```

After you edit `.env`, `docker compose up -d` is enough. A plain `docker compose restart`
doesn't reload `.env`.

## 20. Back up the bot's state

`deploy/backup.sh` backs up `var/` while the bot runs. It takes a consistent SQLite snapshot
with `sqlite3 .backup` and adds the other state files. It writes
`~/backups/trading-bot/var-YYYYMMDDTHHMMSSZ.tar.gz`, readable only by you, and keeps 14 days.

Run it once by hand:

```bash
~/trading-bot/deploy/backup.sh
ls -l ~/backups/trading-bot/
```

Schedule it nightly. Run `crontab -e` and add this line:

```text
17 3 * * * /home/trader/trading-bot/deploy/backup.sh 2>&1 | logger -t trading-bot-backup
```

Check the nightly result with `journalctl -t trading-bot-backup --since yesterday`.

To keep more or fewer days, set `BACKUP_KEEP_DAYS` before the command, for example
`BACKUP_KEEP_DAYS=30 /home/trader/trading-bot/deploy/backup.sh`.

A backup on the same disk dies with the server. Copy the backups to your computer now and then:

```bash
rsync -av trader@SERVER_IP:backups/trading-bot/ ./trading-bot-backups/
```

### Restore a backup

```bash
cd ~/trading-bot
docker compose stop
mv var "var.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir var
tar -xzf ~/backups/trading-bot/var-YYYYMMDDTHHMMSSZ.tar.gz -C var
docker compose start
```

The restored database doesn't know about trades after the backup. The bot reconciles its
positions with Alpaca on its next poll. Compare `/status` with the Alpaca dashboard afterwards.

## 21. Start the bot on boot

`restart: unless-stopped` restarts crashed containers. The systemd unit also brings the stack
up at boot, even after a `docker compose down`.

```bash
cd ~/trading-bot
sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
sudo sed -i "s#/home/trader#$HOME#g; s#^User=trader#User=$USER#" /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
systemctl status trading-bot --no-pager
```

Test it with `sudo reboot`. Log in again after a minute and run `docker compose ps`.

To keep the bot off across reboots, run `sudo systemctl disable --now trading-bot`.

## 22. Going live later

Live trading is off by default, and the bot never switches itself to live. Stay on paper until
every item below is true.

1. You've paper traded for at least 30 days and 8 trades (`live_gate` in `strategy.md`).
2. You've run a kill-switch drill in the last 30 days.
3. You've run the final check, and it passed:

   ```bash
   . .venv/bin/activate
   python -m bot final-check
   ```

   It compares paper trading with the backtest, confirms the drill, and names the regime most
   likely to break the strategy. It writes `var/live_gate.json` and the **Final check** section
   of `strategy.md`.
4. You've read **WHAT COULD BLOW UP THIS ACCOUNT?** at the end of that section, and you accept
   it.

Only then do all of these, together:

- Create live Alpaca keys with trade permission only.
- In `.env`, set `ALPACA_PAPER=false` and `TRADING_MODE=live`.
- Set `LIVE_TRADING_ACK` to the exact `LIVE_ACK_PHRASE` from `bot/config.py`.
- Consider a smaller `risk.capital_usd` in `strategy.md` for the first weeks.
- Run `docker compose up -d --force-recreate`.

The live gate expires after 7 days. Once `var/live_gate.json` is older than that, the guard
blocks every order, exits included. Re-run `final-check` at least weekly while you trade live,
and the drill at least monthly.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `dashboard` keeps restarting | `DASHBOARD_PASSWORD` is empty, so the dashboard refuses to listen on `0.0.0.0`. Set it in `.env`, then run `docker compose up -d`. |
| `PermissionError` or `unable to open database file` for `/app/var` | Docker created `var/` or `data/` as root, or `APP_UID`/`APP_GID` don't match your user. Run `sudo chown -R "$(id -u):$(id -g)" var data results`, fix `APP_UID`/`APP_GID` in `.env`, then run `docker compose build && docker compose up -d`. |
| `env file .env not found` | Run `cp .env.example .env && chmod 600 .env` in `~/trading-bot`. |
| `ConfigError` in the bot log | The CONFIG block in `strategy.md` is missing or invalid. Run `python -m bot status` on the host to see the error, and `git diff strategy.md` to see what changed. |
| The bot runs but never trades | Check `python -m bot status`. Common reasons: every asset is disabled (no tournament winner), the kill switch is tripped, the daily loss limit hit, Jev vetoed the entry, or an approval expired. The dashboard's `/signals` page shows the reason for each signal. |
| Every entry says `jev error` | Jev fails closed. Run `python -m bot jev-ping`, then check `TYPESAFE_API_KEY`. |
| Alpaca returns `401` or `403` | The keys don't match the account type. Paper keys need `ALPACA_PAPER=true`. |
| No Telegram messages | See the troubleshooting table in [TELEGRAM.md](TELEGRAM.md). |
| `git pull` refuses because of local changes | The server's tournament rewrote `strategy.md` or `results/`. Run `git stash`, `git pull`, then `git stash pop`. If that conflicts, take the new version and re-run `python -m bot tournament --apply`. |
| `pip` or `docker compose build` gets killed | The server ran out of memory. Add swap: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`, then add `/swapfile none swap sw 0 0` to `/etc/fstab`. |
| The SSH tunnel says `Address already in use` | Something on your computer uses port 8080. Use `ssh -N -L 8081:127.0.0.1:8080 trader@SERVER_IP` and open `http://localhost:8081`. |
| Times look off by hours | Run `timedatectl` and `chronyc tracking`. The server should be on UTC and synchronized. |
| You're locked out of SSH | Use your provider's web console, then fix `/etc/ssh/sshd_config.d/00-hardening.conf`. |
