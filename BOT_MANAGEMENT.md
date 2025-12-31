# Telegram Bot Management Guide

## Starting the Bot

### Basic Start (foreground)
```bash
python3 cline_telegram_bot.py
```

### Background Mode (recommended)
```bash
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &
```

### Check if Running
```bash
ps aux | grep cline_telegram_bot | grep -v grep
```

### View Logs
```bash
tail -f bot.log
```

## Stopping the Bot

### Graceful Stop
```bash
pkill -f "python3 cline_telegram_bot.py"
```

### Force Stop (if needed)
```bash
pkill -9 -f "python3 cline_telegram_bot.py"
```

## Monitoring

### Check Process Status
```bash
ps aux | grep python3 | grep cline
```

### View Real-time Logs
```bash
tail -f bot.log
```

### Check Log Size
```bash
ls -lh bot.log
```

### Clear Logs (when needed)
```bash
> bot.log  # Clear log file
```

## Restarting

```bash
# Stop current instance
pkill -f "python3 cline_telegram_bot.py"

# Wait a moment
sleep 2

# Start new instance
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &
```

## Common Commands

### Check Bot Status
```bash
ps aux | grep cline_telegram_bot
```

### View Recent Logs
```bash
tail -20 bot.log
```

### Check Memory Usage
```bash
ps aux | grep cline_telegram_bot | grep -v grep | awk '{print "Memory: " $6 "%", "CPU: " $3 "%"}'
```

## Telegram Commands

Once the bot is running, you can use these Telegram commands:

- `/start` - Start Cline session
- `/stop` - Stop Cline session
- `/status` - Check session status
- Any text message - Send command to Cline

## Systemd Service (Advanced)

For production use, create a systemd service:

```bash
sudo nano /etc/systemd/system/cline-telegram-bot.service
```

Add this content:
```ini
[Unit]
Description=Cline Telegram Bot Service
After=network.target

[Service]
User=user_name
WorkingDirectory=/home/user_name/workspace/cutesy-agent-router
Environment="PYTHONUNBUFFERED=1"
ExecStart=/usr/bin/python3 /home/user_name/workspace/cutesy-agent-router/cline_telegram_bot.py
Restart=always
RestartSec=5
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=cline-telegram-bot

[Install]
WantedBy=multi-user.target
```

Then enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable cline-telegram-bot
sudo systemctl start cline-telegram-bot
sudo systemctl status cline-telegram-bot
```

## Notes

- The bot runs continuously in the background
- Logs are saved to `bot.log`
- Use `tail -f bot.log` to monitor in real-time
- The bot automatically restarts if it crashes (with nohup)
- For production, use the systemd service method