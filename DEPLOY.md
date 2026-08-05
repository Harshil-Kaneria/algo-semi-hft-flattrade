# Auto-Deploy with GitLab Runner

When you push to `main` on GitLab, the pipeline automatically runs `git pull`
on your server. The GitLab Runner is installed directly on your server — it
picks up pipeline jobs and runs them **locally**, so no SSH keys or webhooks
are needed.

---

## Architecture

```
You push to main on GitLab
         ↓
GitLab triggers the pipeline (.gitlab-ci.yml)
         ↓
GitLab Runner (installed on your server) picks up the job
         ↓
Runner runs:  git reset --hard origin/main
         ↓
Server is always on latest main
```

---

## One-time setup on your Ubuntu server

SSH into your server and run all of these once.

### Step 1 — Install GitLab Runner
```bash
# Download and add the GitLab Runner repo
curl -L "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" | sudo bash

# Install
sudo apt install -y gitlab-runner
```

### Step 2 — Clone the repo on the server
```bash
cd /home/ubuntu
git clone https://gitlab.com/YOUR_GITLAB_USERNAME/algo-semi-hft-flattrade.git
cd algo-semi-hft-flattrade
pip install -r requirements.txt
```

### Step 3 — Register the Runner
Get your registration token from GitLab:
**Your repo → Settings → CI/CD → Runners → New project runner**

Then register:
```bash
sudo gitlab-runner register
```

You will be prompted for:

| Prompt | What to enter |
|---|---|
| GitLab instance URL | `https://gitlab.com` |
| Registration token | Paste the token from GitLab |
| Description | `my-server` (any name) |
| Tags | `self-hosted` ← **must match the tag in `.gitlab-ci.yml`** |
| Executor | `shell` |

### Step 4 — Allow the runner user to access the repo
The runner runs as the `gitlab-runner` system user. Give it access to the repo:
```bash
sudo usermod -aG ubuntu gitlab-runner      # add to ubuntu group if needed
sudo chown -R ubuntu:ubuntu /home/ubuntu/algo-semi-hft-flattrade
sudo chmod -R g+rw /home/ubuntu/algo-semi-hft-flattrade
```

Or simpler — run the runner as the `ubuntu` user (recommended):
```bash
sudo gitlab-runner uninstall
sudo gitlab-runner install --user=ubuntu --working-directory=/home/ubuntu
sudo gitlab-runner start
```

### Step 5 — Confirm the runner is active
```bash
sudo gitlab-runner status
```

Go to GitLab → **Your repo → Settings → CI/CD → Runners** and you should see
your runner listed as **Online** with a green dot.

---

## One-time GitLab repo setup

If your code is currently on **GitHub**, push a copy to GitLab:
```bash
# Add GitLab as a second remote
git remote add gitlab https://gitlab.com/YOUR_GITLAB_USERNAME/algo-semi-hft-flattrade.git

# Push to GitLab
git push gitlab main
```

Or just use GitLab as your primary remote going forward.

---

## Daily workflow

```bash
# Morning: refresh session token on the server
cd /home/ubuntu/algo-semi-hft-flattrade
python get_token.py

# Afternoon: push code from your laptop
git push origin main        # or:  git push gitlab main
# → pipeline triggers → server auto-pulls → done
```

---

## Optional: auto-restart trader after deploy

Uncomment this line in `.gitlab-ci.yml`:
```yaml
# - sudo systemctl restart trader
```

Then allow the runner to restart without password:
```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart trader" \
  | sudo tee /etc/sudoers.d/trader
```

### Setting up trader.service
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
