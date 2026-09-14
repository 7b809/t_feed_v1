# Project Update Service

A secure FastAPI service and Telegram bot that runs a fixed command, `python3 update_project.py`, inside an allowlisted project directory. It does not accept arbitrary shell commands.

## Features

- Telegram inline buttons for configured projects
- API route to queue an update and a route to read job status
- Background subprocess execution without `shell=True`
- One update at a time per project
- Telegram notifications for queued, started, succeeded, failed, and timed-out stages
- Captures combined stdout/stderr and sends a safe output tail
- Secrets loaded from `.env` through `core/config.py`
- File-based logs through `core/logger.py`, stored only under `logs/<filename>/<filename>.log`
- API-key and Telegram chat-ID authorization

## Structure

```text
project_update_service/
├── api/routes.py
├── core/config.py
├── core/logger.py
├── logs/.gitkeep
├── models/schemas.py
├── services/notifier.py
├── services/update_runner.py
├── telegram_bot/bot.py
├── systemd/project-update-service.service
├── .env.example
├── .gitignore
├── main.py
├── requirements.txt
└── start.sh
```

## Ubuntu installation

```bash
cd /home/ubuntu
unzip project_update_service.zip
cd project_update_service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env
chmod 600 .env
chmod +x start.sh
```

Set a strong API key:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit `.env` with the real bot token, allowed chat ID, and absolute project paths. The supplied example already shows `algo_app_v1` and `t_feed_v1_temp2`.

## Run manually

```bash
./start.sh
```

Open `http://SERVER_IP:8080/docs` for Swagger UI. Keep port 8080 private or place it behind HTTPS and a firewall.

## API examples

List allowlisted projects:

```bash
curl -s http://127.0.0.1:8080/api/v1/projects \
  -H 'X-API-Key: YOUR_API_KEY'
```

Queue an update:

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/projects/algo_app_v1/update \
  -H 'X-API-Key: YOUR_API_KEY'
```

Check status:

```bash
curl -s http://127.0.0.1:8080/api/v1/jobs/JOB_ID \
  -H 'X-API-Key: YOUR_API_KEY'
```

## Telegram

1. Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.
2. Start the service.
3. Send `/start` or `/projects` to the bot.
4. Tap a project button.

Only the exact configured chat ID is accepted.

## systemd deployment

Adjust paths in `systemd/project-update-service.service` if needed, then:

```bash
sudo cp systemd/project-update-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now project-update-service
sudo systemctl status project-update-service
journalctl -u project-update-service -f
```

Application logs are under `logs/`. systemd lifecycle output remains available through `journalctl`.

## Security notes

- Never commit `.env` or disclose the bot token/API key.
- The API cannot provide a path or command. It can select only a key in `PROJECTS_JSON`.
- The child process is launched with an argument list and `shell=False` behavior.
- Run as a non-root user and grant that user access only to required project directories.
- Use HTTPS through Nginx/Caddy if the API is exposed outside localhost.
- For durable, multi-server job processing, replace in-memory jobs with Redis/Celery or another queue.
