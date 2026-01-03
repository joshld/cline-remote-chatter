
# Cutesy Agent Router

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Telegram bot that routes AI agent requests through a PTY session to run Cline commands.

## Table of Contents

- [Description](#description)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Bot Commands](#bot-commands)
- [How It Works](#how-it-works)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Logging](#logging)
- [Security](#security)
- [License](#license)

## Description

Cutesy Agent Router is a Python-based Telegram bot that provides a remote interface to Cline (an AI coding agent). It creates a PTY (pseudo-terminal) session that runs Cline and allows you to interact with it through Telegram messages. The bot manages the PTY session, handles command execution, and monitors output in real-time.

## Requirements

- Python 3.8+
- python-telegram-bot
- psutil
- python-dotenv
- Cline CLI tool (tested with CLI v1.0.8, Core v3.39.2)

## Quick Start

```bash
# Clone and setup
git clone https://github.com/joshld/cutesy-agent-router.git
cd cutesy-agent-router
pip install -r requirements.txt

# Configure environment - create .env file
echo "TELEGRAM_BOT_TOKEN=your_bot_token_here" > .env
echo "AUTHORIZED_USER_ID=your_telegram_user_id_here" >> .env
# Edit the .env file with your actual values

# Run the bot
python cline_telegram_bot.py
```

Then message your bot on Telegram and use `/start` to begin!

## Features

- **PTY Session Management**: Creates and manages a pseudo-terminal session for Cline
- **Real-time Output Monitoring**: Background thread continuously reads and sends output to Telegram
- **Interactive Command Handling**: Supports both commands and natural language input
- **Session State Management**: Tracks active sessions, waiting states, and input prompts
- **Process Management**: Properly handles process trees and cleanup
- **Interactive Prompts**: Detects and handles interactive prompts requiring user input
- **Command Cancellation**: Supports Ctrl+C to cancel running tasks
- **Mode Switching**: Built-in support for Plan/Act mode switching in Cline
- **Health Monitoring**: Monitors output reader health and session status

## Architecture

The bot uses a PTY (pseudo-terminal) to run Cline in a controlled environment:
- **PTY Session**: Runs Cline as a subprocess with proper terminal emulation
- **Output Reader**: Background thread reads output from the PTY master file descriptor
- **Output Queue**: Accumulates output for batch sending to Telegram
- **State Management**: Thread-safe state tracking for session status, prompts, and commands
- **Telegram Integration**: Uses python-telegram-bot library for messaging

## Installation

1. Clone the repository:
```bash
git clone https://github.com/joshld/cutesy-agent-router.git
cd cutesy-agent-router
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Install Cline CLI tool:

   Follow the [official Cline CLI installation guide](https://docs.cline.bot/cline-cli/installation) for detailed instructions.

   Quick install via npm:
   ```bash
   npm install -g cline
   cline auth  # Authenticate with your Cline account
   cline version
   ```

## Configuration

Create a `.env` file with your Telegram bot token and authorized user ID:
TELEGRAM_BOT_TOKEN=your_bot_token_here
AUTHORIZED_USER_ID=your_telegram_user_id_here

To get your Telegram bot token:
- Message @BotFather on Telegram
- Use `/newbot` to create a bot
- Copy the token provided

To get your user ID:
- Message @userinfobot on Telegram
- It will reply with your user ID

## Usage

1. Start the bot:
```bash
python cline_telegram_bot.py
```

2. In Telegram, message your bot and use `/start` to begin a Cline session

3. Send commands or natural language messages:
   - Natural language: `show me the current directory`
   - CLI commands: `git status`, `ls -la`
   - Mode switching: `/plan`, `/act`
   - Cancel task: `/cancel`
   - Check status: `/status`
   - Stop session: `/stop`

## Bot Commands

- `/start` - Start a new Cline PTY session
- `/stop` - Stop the current session
- `/status` - Check session and reader status
- `/plan` - Switch to Plan mode
- `/act` - Switch to Act mode
- `/cancel` - Cancel current task (send Ctrl+C)
- **Any other text** - Send as input to Cline

## How It Works

1. **Session Start**: When you send `/start`, the bot:
   - Creates a PTY (pseudo-terminal)
   - Launches Cline as a subprocess
   - Starts a background thread to read output
   - Sends you a confirmation message

2. **Command Execution**: When you send a message:
   - If Cline is waiting for input, it's sent directly
   - Otherwise, it's sent as a command to Cline
   - The bot waits and collects output
   - Output is sent back to you in Telegram

3. **Output Monitoring**: The background thread:
   - Continuously reads from the PTY master file descriptor
   - Filters out UI elements and repetitive content
   - Accumulates output in a queue
   - Sends formatted output to your Telegram chat

4. **Session Cleanup**: When you send `/stop` or the bot shuts down:
   - All PTY processes are terminated
   - File descriptors are closed
   - Background threads are stopped
   - Resources are cleaned up

## Requirements

- Python 3.9+ (tested with 3.10)
- Requirements listed in `requirements.txt`
- Cline CLI tool (tested with CLI v1.0.8, Core v3.39.2)

## Development

### Code Structure

The code is structured with separation of concerns:
- `ClineTelegramBot` class: Core bot logic and PTY management
- `output_monitor`: Background task for real-time output
- `main()`: Telegram bot setup and event loop
- Signal handlers: Graceful shutdown on SIGINT/SIGTERM

### Development Setup

```bash
# Install development dependencies
pip install -r requirements-dev.txt  # If available

# Run with debug logging
DEBUG=1 python cline_telegram_bot.py

# Check logs
tail -f bot.log
```

### Key Components

- **PTY Management**: Uses `pty.openpty()` for terminal emulation
- **Process Tree Handling**: `psutil` for comprehensive process cleanup
- **Thread Safety**: Multiple locks for state, output queue, and PTY writes
- **Output Filtering**: Intelligent filtering of UI elements and duplicates
- **Health Monitoring**: Background thread health checks and recovery

### Testing

```bash
# Run tests (if implemented)
python -m pytest

# Manual testing
# 1. Start bot locally
# 2. Use Telegram Bot API to simulate messages
# 3. Check PTY session creation and cleanup
```

## Troubleshooting

**Bot won't start:**
- Check TELEGRAM_BOT_TOKEN is set correctly
- Verify Cline is installed and in PATH
- Check port 8443 is available (if using webhooks)

**No output from Cline:**
- Use `/status` to check if session is active
- Check if output reader is healthy
- Look at bot.log for debug information

**Session stuck:**
- Use `/cancel` to interrupt current task
- Use `/stop` then `/start` to restart session
- Check bot.log for errors

**Process cleanup issues:**
- The bot uses psutil to kill process trees
- Check for orphaned Cline processes
- Manual cleanup: `pkill -f cline`

## Logging

The bot creates a `bot.log` file with detailed debug information:
- Session lifecycle events
- PTY operations
- Output processing
- Error conditions
- Health monitoring

## Security

- Only authorized users (AUTHORIZED_USER_ID) can interact with the bot
- All commands are executed in a sandboxed PTY session
- Process isolation prevents system-wide impact
- Proper signal handling ensures cleanup on shutdown

## License

[MIT](https://opensource.org/licenses/MIT)