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

## Troubleshooting

### Bot Not Responding to Commands

If the bot doesn't respond to `/act`, `/plan`, or other commands, you likely have multiple bot instances running. Follow these steps:

#### 1. Stop All Existing Bot Processes
```bash
pkill -9 -f "cline_telegram_bot.py"
```

#### 2. Clear the Bot Log
```bash
> bot.log
```

#### 3. Start Fresh
```bash
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &
```

#### 4. Verify It's Running
```bash
ps aux | grep cline_telegram_bot | grep -v grep
tail -f bot.log  # Should show startup messages
```

#### 5. Test the Bot
Send these commands in Telegram:
- `/start` - Should start Cline session
- `/status` - Should show bot status
- `/act` - Should switch to act mode

### Common Issues

**Issue: "Conflict: terminated by other getUpdates request"**
- **Cause**: Multiple bot instances running simultaneously
- **Solution**: Run `pkill -9 -f "cline_telegram_bot.py"` and restart

**Issue: Bot starts but doesn't respond**
- **Cause**: Bot process may be stuck or crashed
- **Solution**: Check logs with `tail -f bot.log`, then restart

**Issue: Commands like `/act` don't work**
- **Cause**: Bot not properly started or session issues
- **Solution**: Follow the full restart process above

### Quick Restart Script
```bash
#!/bin/bash
echo "Restarting Cline Telegram Bot..."
pkill -9 -f "cline_telegram_bot.py" 2>/dev/null
sleep 2
> bot.log
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &
sleep 3
echo "Bot restarted. Check status:"
ps aux | grep cline_telegram_bot | grep -v grep
echo "View logs: tail -f bot.log"
```

### Kill All Cline Processes (One-Liner)
```bash
pkill -9 -f "cline" && pkill -9 -f "cline-host" && pkill -9 -f "cline-core"
```

## Notes

- The bot runs continuously in the background
- Logs are saved to `bot.log`
- Use `tail -f bot.log` to monitor in real-time
- The bot automatically restarts if it crashes (with nohup)
- For production, use the systemd service method