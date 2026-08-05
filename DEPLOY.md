# Auto-Deploy Setup (GitHub + Ubuntu Server)

Your repo is on **GitHub**. Two options are available:

---

## ✅ Option A — GitHub Actions + SSH (Recommended)

**How it works:**
```
You push to main
       ↓
GitHub Actions starts a free runner
       ↓
Runner SSHes into YOUR server
       ↓
Runs:  git reset --hard origin/main
       ↓
Server is on latest main within ~30 seconds
```

The pipeline file is already in the repo at `.github/workflows/deploy.yml`.
You just need to do a **one-time server + GitHub setup** below.

---

### Step 1 — Clone the repo on your server

SSH into your Ubuntu server:
```bash
git clone https://github.com/Harshil-Kaneria/algo-semi-hft-flattrade.git
cd algo-semi-hft-flattrade
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your Flattrade credentials
```

---

### Step 2 — Generate a deploy SSH key (on your server)

Run this **on the server** (not your laptop):
```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/deploy_key -N ""
```

This creates two files:
- `~/.ssh/deploy_key` → **private key** (goes into GitHub)
- `~/.ssh/deploy_key.pub` → **public key** (stays on server)

Add the public key to `authorized_keys` so GitHub can SSH in:
```bash
cat ~/.ssh/deploy_key.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Print the private key — you'll copy this into GitHub:
```bash
cat ~/.ssh/deploy_key
```

---

### Step 3 — Add 3 secrets to GitHub

Go to: **GitHub → Your repo → Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `DEPLOY_HOST` | Your server's IP address (e.g. `123.45.67.89`) |
| `DEPLOY_USER` | Your SSH username on the server (e.g. `ubuntu`) |
| `DEPLOY_SSH_KEY` | The **entire output** of `cat ~/.ssh/deploy_key` (starts with `-----BEGIN`) |
| `DEPLOY_PATH` | Full path to the repo on server (e.g. `/home/ubuntu/algo-semi-hft-flattrade`) |

---

### Step 4 — Done. Test it.

Push any change to main:
```bash
git commit --allow-empty -m "test deploy"
git push origin main
```

Go to **GitHub → Actions tab** — you'll see the pipeline run and succeed (green ✅).

---

## 🔁 Option B — Cron Poll (Simplest, no GitHub config)

If you don't want to touch GitHub settings at all, add a cron job on the server
that checks for new commits every minute and pulls automatically:

```bash
# On your server — run this once:
crontab -e
```

Add this line:
```cron
* * * * * cd /home/ubuntu/algo-semi-hft-flattrade && git fetch origin main && git reset --hard origin/main >> /var/log/deploy.log 2>&1
```

No GitHub Secrets, no Actions, no SSH key setup. The server just polls GitHub
every 60 seconds. Slight delay (up to 1 min) vs instant deploy with Option A.

---

## Optional — Auto-restart trader after deploy

For both options, uncomment this in `.github/workflows/deploy.yml` (Option A):
```yaml
# sudo systemctl restart trader
```

Or add to the cron command (Option B):
```bash
... && git reset --hard origin/main && sudo systemctl restart trader
```

### Set up trader as a systemd service
```bash
sudo tee /etc/systemd/system/trader.service > /dev/null <<EOF
[Unit]
Description=Flattrade Semi-HFT Algo Trader
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/algo-semi-hft-flattrade
ExecStart=/usr/bin/python3 trader.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/home/ubuntu/algo-semi-hft-flattrade/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now trader
```

Allow passwordless restart for GitHub Actions / cron:
```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart trader" \
  | sudo tee /etc/sudoers.d/trader
```

---

## Daily morning flow (refresh session token)

```bash
# On your server, every morning before market open:
cd /home/ubuntu/algo-semi-hft-flattrade
python get_token.py        # refreshes FLATTRADE_API_TOKEN in .env
sudo systemctl restart trader
```
