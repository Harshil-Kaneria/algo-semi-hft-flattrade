# CI/CD Setup Guide

Whenever you push to the `main` branch, GitHub Actions automatically SSHes into
your server and runs `git pull origin main`.

---

## One-time server setup

### 1. Clone the repo on your server (if not already done)
```bash
git clone https://github.com/Harshil-Kaneria/algo-semi-hft-flattrade.git
cd algo-semi-hft-flattrade
```

### 2. Create credentials on the server
```bash
# Copy the .env template and fill in your credentials
cp .env.example .env
nano .env          # set FLATTRADE_USER_ID, FLATTRADE_API_KEY, FLATTRADE_API_TOKEN

# Copy the config and adjust if needed
cp trader_config.json trader_config.json   # already in repo, edit values as needed
```

### 3. Install Python dependencies
```bash
pip install -r requirements.txt
```

---

## One-time GitHub Secrets setup

Go to your repo on GitHub:
**Settings → Secrets and variables → Actions → New repository secret**

Add these 4 secrets:

| Secret name | Value |
|---|---|
| `SERVER_HOST` | Your server's IP address or hostname (e.g. `123.45.67.89`) |
| `SERVER_USER` | SSH username on the server (e.g. `ubuntu`, `root`) |
| `SERVER_SSH_KEY` | The **private** SSH key that can access the server (see below) |
| `SERVER_REPO_PATH` | Absolute path to the cloned repo (e.g. `/home/ubuntu/algo-semi-hft-flattrade`) |
| `SERVER_PORT` | SSH port — **optional**, defaults to `22` |

### Generating an SSH key pair (if you don't have one)
Run this **on your local machine**:
```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/deploy_key
```
- Copy **`~/.ssh/deploy_key`** (private key) → paste as `SERVER_SSH_KEY` in GitHub Secrets
- Copy **`~/.ssh/deploy_key.pub`** (public key) → append to `~/.ssh/authorized_keys` on the server:
```bash
cat ~/.ssh/deploy_key.pub >> ~/.ssh/authorized_keys   # run this on the server
```

---

## How the workflow runs

1. You push code to `main`
2. GitHub Actions starts a runner
3. It SSHes into `SERVER_HOST` as `SERVER_USER`
4. Runs `cd <SERVER_REPO_PATH> && git pull origin main`
5. Done — server has the latest code

---

## Optional: Auto-restart the trader process

If you run `trader.py` as a systemd service (recommended for production), uncomment
this line in `.github/workflows/deploy.yml`:

```yaml
# sudo systemctl restart trader
```

### Setting up trader.service on the server
Create `/etc/systemd/system/trader.service`:
```ini
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
```

Then enable it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable trader
sudo systemctl start trader
```

To allow GitHub Actions to restart it without a password prompt:
```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart trader" | sudo tee /etc/sudoers.d/trader
```

---

## Daily morning flow (refresh session token)

The session token expires every day. SSH into your server each morning and run:
```bash
cd /home/ubuntu/algo-semi-hft-flattrade
python get_token.py    # opens auth URL, saves token to .env
# trader.service will pick up the new token on next restart, or restart it manually:
sudo systemctl restart trader
```
