# Telegram Bot Management Guide

**📁 For comprehensive daemon scripts and production deployment options, see the `scripts/` directory and `scripts/README.md`.**

## Starting the Bot

### Basic Start (foreground)
```bash
python3 cline_telegram_bot.py
```

### Background Mode (simple)
```bash
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &
```

### Background Mode with Auto-Restart (recommended)
```bash
# Use the daemon script for better process management
python3 scripts/quick_start.py monitor
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

## Production Deployment Options

For production use, use the pre-configured systemd service and monitoring scripts in the `scripts/` directory:

### Systemd Service (Recommended for Production)
```bash
# 1. Configure paths in the service file
nano scripts/cline-bot.service
# Update all path placeholders for your environment

# 2. Copy and enable service
sudo cp scripts/cline-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cline-telegram-bot
sudo systemctl start cline-telegram-bot

# 3. Check status
sudo systemctl status cline-telegram-bot
```

### Python Daemon with Auto-Restart (Recommended for Development)
```bash
# Start with automatic crash recovery
python3 scripts/quick_start.py monitor

# Manual control
python3 scripts/quick_start.py start   # Start once
python3 scripts/quick_start.py stop    # Stop bot
python3 scripts/quick_start.py status  # Check status
```

### Health Monitoring (Optional)
```bash
# Enable automatic health checks every 5 minutes
sudo cp scripts/bot-healthcheck.sh /usr/local/bin/
sudo cp scripts/bot-healthcheck.timer /etc/systemd/system/
sudo systemctl enable bot-healthcheck.timer
sudo systemctl start bot-healthcheck.timer
```

See `scripts/README.md` for complete setup instructions and path configuration.

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
# Option 1: Simple background
nohup python3 cline_telegram_bot.py > bot.log 2>&1 &

# Option 2: Managed daemon (recommended)
python3 scripts/quick_start.py start
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
python3 scripts/quick_start.py start  # Use managed daemon
sleep 3
echo "Bot restarted. Check status:"
python3 scripts/quick_start.py status
echo "View logs: python3 scripts/quick_start.py logs"
```

### Managed Restart (Better)
```bash
# Use the built-in restart functionality
python3 scripts/quick_start.py restart
```

### Kill All Cline Processes (One-Liner)
```bash
pkill -9 -f "cline" && pkill -9 -f "cline-host" && pkill -9 -f "cline-core"
```

## Notes

- The bot runs continuously in the background
- Logs are saved to `bot.log` (and `monitor.log` for daemon monitoring)
- Use `python3 scripts/quick_start.py logs` or `tail -f bot.log` to monitor
- Daemon scripts provide automatic restart on crashes
- For production, use the systemd service from `scripts/cline-bot.service`
- For development, use `python3 scripts/quick_start.py monitor`
- See `scripts/README.md` for complete deployment options