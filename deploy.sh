#!/bin/bash
# deploy.sh — Full deployment script for owner-enrichment Flask app
# Run this as root in the DigitalOcean droplet console

set -e
APP_DIR="/opt/owner-enrichment"
REPO="https://github.com/operationboardwalk/owner-enrichment.git"
SERVICE="owner-enrichment"
PORT=5000
PYTHON="python3"

echo "========================================="
echo " Owner Enrichment — Deployment Script"
echo "========================================="

# ── 1. System packages ──────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq git python3 python3-pip python3-venv ufw curl

# ── 2. Clone / update repo ──────────────────────────────────────────────────
echo "[2/6] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    echo "  Repo exists — pulling latest..."
    git -C "$APP_DIR" pull origin master 2>/dev/null || git -C "$APP_DIR" pull origin main 2>/dev/null || true
else
    git clone "$REPO" "$APP_DIR"
fi

# ── 3. Python virtualenv + dependencies ─────────────────────────────────────
echo "[3/6] Setting up Python environment..."
$PYTHON -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/pip" install --quiet gunicorn

echo "  Installed packages:"
"$APP_DIR/venv/bin/pip" list --format=columns | grep -E "Flask|gunicorn|pandas|requests|openpyxl"

# ── 4. Systemd service ───────────────────────────────────────────────────────
echo "[4/6] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE}.service <<SERVICE
[Unit]
Description=Owner Enrichment Flask App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/gunicorn \
    --workers 2 \
    --bind 0.0.0.0:${PORT} \
    --timeout 120 \
    --access-logfile /var/log/${SERVICE}-access.log \
    --error-logfile  /var/log/${SERVICE}-error.log \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable  "$SERVICE"
systemctl restart "$SERVICE"
sleep 2

# ── 5. Firewall ──────────────────────────────────────────────────────────────
echo "[5/6] Configuring firewall..."
ufw allow OpenSSH   2>/dev/null || true
ufw allow "$PORT"/tcp
# Enable ufw non-interactively if not already active
ufw --force enable 2>/dev/null || true
ufw status

# ── 6. Health check ──────────────────────────────────────────────────────────
echo "[6/6] Running health check..."
sleep 2
STATUS=$(systemctl is-active "$SERVICE")
echo "  Service status: $STATUS"

HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ 2>/dev/null || echo "000")
echo "  HTTP response:  $HTTP"

if [ "$STATUS" = "active" ] && [ "$HTTP" = "200" ]; then
    echo ""
    echo "✅  Deployment successful!"
    echo "    App is live at: http://64.23.133.114:${PORT}"
else
    echo ""
    echo "⚠  Check logs:"
    echo "    journalctl -u $SERVICE -n 30"
    echo "    tail -30 /var/log/${SERVICE}-error.log"
    systemctl status "$SERVICE" --no-pager | tail -20
fi
