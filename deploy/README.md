# GhostNet Daemon Deploy Notes

This folder contains example systemd units for running:

- `ghostnet-api` – FastAPI / Ollama backend
- `ghostnet-bot` – Discord bot that calls the API

These files are **templates**. Adjust paths and users for your environment.

## 1. Install the code

On the target host:

```bash
git clone https://github.com/nebusoku/ghostnet-daemon.git /opt/ghostnet-daemon
cd /opt/ghostnet-daemon
python -m venv .venv
. .venv/bin/activate
pip install -r api/requirements.txt
