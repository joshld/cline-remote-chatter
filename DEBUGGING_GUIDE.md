# Enhanced Debugging Guide for Cline Telegram Bot

## What I've Added

Your bot now has **comprehensive debugging** that captures EVERYTHING happening in the system. Here's what gets logged:

### 1. **Complete User Message Capture**
```
[2026-01-01 16:34:15.123] [DEBUG] User message received | user_id=113732979 | authorized_id=113732979 | full_message=git status | message_length=10
```

### 2. **Complete Bot State Snapshots**
```
[2026-01-01 16:34:15.124] [DEBUG] Complete bot state snapshot | session_active=True | is_running=True | waiting_for_input=False | bot_state=idle | queue_size=0 | command_queue_size=0 | current_command=None | process_alive=None
```

### 3. **Raw Cline Output Capture**
```
[2026-01-01 16:34:15.456] [DEBUG] Raw output from Cline | raw_length=245 | clean_length=245 | preview=On branch main\nYour branch is up to date...
```

### 4. **Process Health Monitoring**
```
[2026-01-01 16:34:17.789] [DEBUG] Process health check OK | checks=5 | pid=12345
[2026-01-01 16:34:19.789] [DEBUG] Process health check OK | checks=6 | pid=12345
```

### 5. **Filtering Decisions**
```
[2026-01-01 16:34:15.890] [DEBUG] Filtered output | preview=┃┃┃ | reason=ui_element
[2026-01-01 16:34:15.891] [DEBUG] Queued output | preview=git status output | line_count=10
```

### 6. **Telegram Delivery Confirmation**
```
[2026-01-01 16:34:16.012] [INFO] Sending output to user via monitor | output_length=245
[2026-01-01 16:34:16.234] [DEBUG] Output sent successfully via monitor | sent_length=245 | preview=On branch main...
```

### 7. **Complete Flow Tracking**
```
[2026-01-01 16:34:15.123] [INFO] handle_message called
[2026-01-01 16:34:15.456] [INFO] send_command called | command=git status | is_running=True
[2026-01-01 16:34:15.789] [DEBUG] Method 2 bytes written | bytes_written=11 | expected=11 | success=True
[2026-01-01 16:34:16.012] [INFO] Command sent successfully | command=git status | method=2
[2026-01-01 16:34:16.234] [DEBUG] Data read from PTY | bytes=245 | preview=On branch main...
[2026-01-01 16:34:16.456] [DEBUG] Output added to queue | queue_size=1 | waiting_for_input=False
[2026-01-01 16:34:16.678] [INFO] Sending output to user via monitor | output_length=245
[2026-01-01 16:34:16.901] [DEBUG] Output sent successfully via monitor | sent_length=245
[2026-01-01 16:34:17.123] [INFO] Bot state changed | old_state=processing | new_state=idle
```

## What Gets Logged to bot.log

**EVERYTHING:**
- ✅ Every message you send (full content)
- ✅ Every Cline response (full content)
- ✅ All filtering decisions (what got filtered and why)
- ✅ Process health checks (every 2 seconds)
- ✅ State changes (idle → processing → idle)
- ✅ All errors with full stack traces
- ✅ Telegram delivery confirmations
- ✅ Output reader heartbeat (every 5 seconds)
- ✅ Queue sizes and command flow

## How to Use

### 1. Start the Bot
```bash
python cline_telegram_bot.py
```

### 2. Watch Real-Time Logs
```bash
tail -f bot.log
```

### 3. Check for Issues
Look for these patterns:
- `[ERROR]` - Something went wrong
- `[WARN]` - Potential issues
- `process_alive=no_process` - Cline died
- `reason=plan_act_loop` - Filtering the problematic pattern
- `reason=duplicate` - Duplicate output being filtered

### 4. Test Commands
Send any message to the bot and watch the logs in real-time. You'll see:
1. Your message received
2. Bot state snapshot
3. Command sent to Cline
4. Raw output from Cline
5. Filtering decisions
6. Telegram delivery confirmation

## Common Log Patterns

### ✅ **Working Correctly**
```
[16:34:15.123] [DEBUG] User message received | full_message=git status
[16:34:15.124] [DEBUG] Complete bot state snapshot | session_active=True | is_running=True
[16:34:15.456] [INFO] send_command called | command=git status | is_running=True
[16:34:15.789] [DEBUG] Method 2 bytes written | success=True
[16:34:16.012] [INFO] Command sent successfully | method=2
[16:34:16.234] [DEBUG] Data read from PTY | bytes=245
[16:34:16.456] [DEBUG] Queued output | preview=On branch main
[16:34:16.678] [INFO] Sending output to user via monitor | output_length=245
[16:34:16.901] [DEBUG] Output sent successfully via monitor
[16:34:17.123] [INFO] Bot state changed | old_state=processing | new_state=idle
```

### ❌ **Process Died**
```
[16:34:15.123] [ERROR] Process died during output reading | returncode=1 | checks=5
[16:34:15.124] [ERROR] Process died but session still active - stopping session
[16:34:15.125] [INFO] stop_pty_session called
```

### ⚠️ **Filtered Output**
```
[16:34:15.123] [DEBUG] Filtered output | preview=┃┃┃ | reason=ui_element
[16:34:15.124] [DEBUG] Filtered output | preview=/plan or /act to switch modes | reason=plan_act_loop
[16:34:15.125] [DEBUG] Filtered output | preview=Cline is ready... | reason=duplicate
```

## What This Solves

With this enhanced debugging, you can now:

1. **See exactly what you sent** - Full message content in logs
2. **See exactly what Cline wrote** - Complete raw output
3. **See what got filtered** - And why it was filtered
4. **See if the process is alive** - Health checks every 2 seconds
5. **See the complete flow** - Every step from message to response
6. **See Telegram delivery** - Confirmation that messages were sent
7. **Identify bottlenecks** - Timing information for each step

## DEBUG_MODE Environment Variable

- `DEBUG_MODE=true` - Shows all debug logs in Telegram AND console/file
- `DEBUG_MODE=false` - Only shows INFO/WARN/ERROR logs in console/file

**Currently set to:** `true` (in your .env file)

## Next Steps

1. **Start the bot** with `python cline_telegram_bot.py`
2. **Send a test message** like "git status"
3. **Watch bot.log** with `tail -f bot.log`
4. **Analyze the flow** - You'll see every single step

You now have **complete visibility** into what's happening with your bot!