# Auto-Deploy Setup (Server Webhook)

When you push to `main`, GitHub notifies your server and the server automatically
runs `git pull`. No GitHub Actions, no SSH keys in GitHub — everything runs on
your server.

**Tool used:** [`webhook`](https://github.com/adnanh/webhook) — a tiny Go binary,
installable with `apt`. It listens for GitHub push events and runs your deploy script.

---

## One-time server setup

SSH into your Ubuntu server and run these commands:

### 1. Install webhook and git
```bash
sudo apt update
sudo apt install -y webhook git
```

### 2. Clone the repo
```bash
cd /home/ubuntu
git clone https://github.com/Harshil-Kaneria/algo-semi-hft-flattrade.git
```

### 3. Install Python dependencies
```bash
cd algo-semi-hft-flattrade
pip install -r requirements.txt
```

### 4. Copy server config files
```bash
sudo mkdir -p /opt/webhook

# Copy the deploy script and make it executable
sudo cp server/deploy.sh /opt/webhook/deploy.sh
sudo chmod +x /opt/webhook/deploy.sh

# Copy the hooks config
sudo cp server/hooks.json /opt/webhook/hooks.json
```

### 5. Set your webhook secret
Pick any random string as your secret (you'll use the same one in GitHub):
```bash
# Edit the hooks.json and replace REPLACE_WITH_YOUR_WEBHOOK_SECRET
sudo nano /opt/webhook/hooks.json
```
Change this line:
```
"secret": "REPLACE_WITH_YOUR_WEBHOOK_SECRET"
```
to something like:
```
"secret": "my-super-secret-abc123"
```

### 6. Edit the deploy script (set correct paths)
```bash
sudo nano /opt/webhook/deploy.sh
```
Make sure `REPO_PATH` matches where you cloned the repo:
```bash
REPO_PATH="/home/ubuntu/algo-semi-hft-flattrade"
```

### 7. Install the webhook systemd service
```bash
sudo cp server/webhook.service /etc/systemd/system/webhook.service
sudo systemctl daemon-reload
sudo systemctl enable webhook
sudo systemctl start webhook

# Confirm it is running:
sudo systemctl status webhook
```

### 8. Open port 9000 on your server firewall
```bash
sudo ufw allow 9000
```
> If your server is on AWS/GCP/Azure, also open port 9000 in the security group / firewall rules.

---

## One-time GitHub setup

Go to your repo on GitHub:
**Settings → Webhooks → Add webhook**

| Field | Value |
|---|---|
| **Payload URL** | `http://YOUR_SERVER_IP:9000/hooks/deploy` |
| **Content type** | `application/json` |
| **Secret** | The same secret you set in `hooks.json` |
| **Which events** | Just the push event |
| **Active** | ✅ checked |

Click **Add webhook**. GitHub will send a test ping — you should see a green ✅.

---

## How it works after setup

```
You push to main
      ↓
GitHub sends POST to http://YOUR_SERVER:9000/hooks/deploy
      ↓
webhook verifies the secret signature
webhook verifies the branch is "main"
      ↓
Runs /opt/webhook/deploy.sh
      ↓
cd /home/ubuntu/algo-semi-hft-flattrade
git reset --hard origin/main      ← server always stays on main
```

Deploy log is written to `/var/log/deploy.log`.

---

## Optional: auto-restart the trader after deploy

If you run `trader.py` as a systemd service, uncomment this line in
`/opt/webhook/deploy.sh`:
```bash
# systemctl restart trader
```

### Setting up trader.service
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

```bash
sudo systemctl daemon-reload
sudo systemctl enable trader
sudo systemctl start trader
```

Allow the webhook to restart it without a password:
```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart trader" \
  | sudo tee /etc/sudoers.d/trader
```

---

## Daily morning flow (refresh session token)

The Flattrade session token expires every day. On your server each morning:
```bash
cd /home/ubuntu/algo-semi-hft-flattrade
python get_token.py         # opens browser, saves token to .env
sudo systemctl restart trader
```
