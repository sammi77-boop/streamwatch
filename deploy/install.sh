#!/usr/bin/env bash
# Run on the VM from ~/streamwatch after copying the project there. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! python3 -m venv --help >/dev/null 2>&1 || ! dpkg -s python3-venv >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m unittest discover tests
# Fill in this machine's user and project folder.
sed "s|__DIR__|$PWD|g; s|__USER__|$USER|g" deploy/streamwatch.service \
  | sudo tee /etc/systemd/system/streamwatch.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable streamwatch >/dev/null
if [ -f .env ]; then
  sudo systemctl restart streamwatch
  echo "StreamWatch (re)started. Logs: journalctl -u streamwatch -f"
else
  echo "Installed. Create .env (see .env.example), then: sudo systemctl start streamwatch"
fi
