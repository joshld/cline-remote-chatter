# Daemon Scripts

This directory contains various scripts for running the Cline Telegram Bot as a daemon or service.

## ⚠️ Important: Configure Paths Before Use

**Before using any of these scripts, you MUST edit the hardcoded paths to match your environment:**

1. **`scripts/cline-bot.service`** - Update WorkingDirectory, PYTHONPATH, ExecStart, and ReadWritePaths
2. **`scripts/bot-healthcheck.sh`** - Update WORKING_DIR path

**Default paths assume:** `/path/to/your/cutesy-agent-router`

## Project Structure

```
scripts/
├── quick_start.py          # Python daemon with auto-restart
├── start_bot.sh            # Bash daemon script
├── cline-bot.service       # Systemd service file
├── bot-healthcheck.sh      # Health check script
├── bot-healthcheck.timer   # Systemd timer for periodic checks
└── README.md               # This documentation
```

## Scripts

### `quick_start.py`
**Python-based daemon manager with auto-restart functionality.**

Features:
- Process monitoring and auto-restart
- PID file management
- Graceful shutdown
- Status checking and logging
- Survives terminal close

Usage:
```bash
# Start with auto-restart monitoring
python3 scripts/quick_start.py monitor

# Manual start/stop
python3 scripts/quick_start.py start
python3 scripts/quick_start.py stop

# Check status
python3 scripts/quick_start.py status

# View logs
python3 scripts/quick_start.py logs
```

### `start_bot.sh`
**Bash-based daemon script with optional auto-restart monitoring.**

Features:
- Background process management
- PID file tracking
- Status checking and logging
- Auto-restart monitoring mode
- Manual and automated control

Usage:
```bash
# Auto-restart mode (recommended for production)
./scripts/start_bot.sh monitor

# Manual control
./scripts/start_bot.sh start
./scripts/start_bot.sh stop
./scripts/start_bot.sh restart

# Monitoring and logs
./scripts/start_bot.sh status
./scripts/start_bot.sh logs
./scripts/start_bot.sh tail
```

**Auto-restart mode:**
  - Monitors bot every 10 seconds as a background daemon
  - Automatically restarts if bot crashes
  - Survives terminal disconnection (like quick_start.py)
  - 30-second delay between restart attempts
  - Limits to 10 restart attempts to prevent loops
  - Use 'stop' command to stop both bot and monitor

### `cline-bot.service`
**Systemd service file for production deployment.**

Features:
- Automatic startup on boot
- Process monitoring and restart
- System logging integration
- Security hardening
- Resource limits

Installation:
```bash
# 1. Edit paths in service file
nano scripts/cline-bot.service
# Update WorkingDirectory, PYTHONPATH, ExecStart, and ReadWritePaths

# 2. Copy to systemd
sudo cp scripts/cline-bot.service /etc/systemd/system/

# 3. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable cline-telegram-bot
sudo systemctl start cline-telegram-bot

# 4. Check status
sudo systemctl status cline-telegram-bot
```

### `bot-healthcheck.sh` & `bot-healthcheck.timer`
**Automated health monitoring and alerting system.**

The health check script monitors bot status and can trigger alerts or restarts. The timer runs health checks every 5 minutes.

Features:
- Process existence checking
- Log file activity monitoring
- Configurable alert thresholds
- Integration with monitoring systems

Setup:
```bash
# 1. Edit paths in health check script
nano scripts/bot-healthcheck.sh
# Update WORKING_DIR path

# 2. Copy health check script
sudo cp scripts/bot-healthcheck.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/bot-healthcheck.sh

# 3. Copy timer
sudo cp scripts/bot-healthcheck.timer /etc/systemd/system/

# 4. Enable timer
sudo systemctl daemon-reload
sudo systemctl enable bot-healthcheck.timer
sudo systemctl start bot-healthcheck.timer

# 5. Manual health check
/usr/local/bin/bot-healthcheck.sh
```

## Choosing the Right Option

| Method | Use Case | Auto-restart | Terminal Independent | Production Ready |
|--------|----------|-------------|-------------------|-------------------|
| `quick_start.py monitor` | Development/Testing | ✅ | ✅ | ⚠️ Good for dev |
| `start_bot.sh monitor` | Simple production | ✅ | ✅ | ⚠️ Good for simple |
| `start_bot.sh` | Manual control | ❌ | ✅ | ❌ Basic |
| `systemd service` | Enterprise deployment | ✅ | ✅ | ✅ Enterprise |

## Log Files

When using daemon scripts, logs are created in the project root:
- `bot.log` - Main bot application logs
- `monitor.log` - Auto-restart monitor logs (when using `quick_start.py monitor`)
- PID files: `bot.pid`, `monitor.pid`

## Development vs Production

- **Development**: Use `quick_start.py monitor` for easy testing with auto-restart
- **Production**: Use systemd service for robust, system-integrated deployment