import os
import pty
import select
import subprocess
import threading
import time
import asyncio
import re
import signal
from collections import deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import psutil

def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text"""
    return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)

def debug_log(level, message, **kwargs):
    """Centralized debug logging"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    context = " | ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    suffix = f" | {context}" if context else ""
    print(f"[{timestamp}] [{level}] {message}{suffix}")

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
CLINE_COMMAND = ["cline"]

DEBUG_INFO, DEBUG_WARN, DEBUG_ERROR, DEBUG_DEBUG = "INFO", "WARN", "ERROR", "DEBUG"

class ClineTelegramBot:
    def __init__(self):
        debug_log(DEBUG_INFO, "ClineTelegramBot.__init__ called")
        self.master_fd = None
        self.slave_fd = None
        self.process = None
        self.is_running = False
        self.output_queue = deque()
        self.output_thread = None
        self.stop_reading = False
        self.current_command = None
        self.waiting_for_input = False
        self.input_prompt = ""
        self.last_prompt_time = 0
        self.session_active = False
        self.child_pids = set()
        self.application = None
        self.last_chat_id = None
        self._output_monitor_started = False

    def _find_child_processes(self, parent_pid):
        """Find all child processes of a given PID"""
        children = set()
        try:
            parent = psutil.Process(parent_pid)
            for child in parent.children(recursive=True):
                children.add(child.pid)
            children.add(parent_pid)
        except psutil.NoSuchProcess:
            pass
        return children

    def _kill_process_tree(self, pid):
        """Kill a process and all its children"""
        try:
            children = self._find_child_processes(pid)
            debug_log(DEBUG_DEBUG, "Killing process tree", parent_pid=pid, children_count=len(children))
            
            for child_pid in children:
                try:
                    psutil.Process(child_pid).terminate()
                except psutil.NoSuchProcess:
                    continue
            
            time.sleep(0.5)
            
            for child_pid in children:
                try:
                    p = psutil.Process(child_pid)
                    if p.is_running():
                        p.kill()
                except psutil.NoSuchProcess:
                    pass
            
            time.sleep(0.2)
            debug_log(DEBUG_DEBUG, "Process tree killed", parent_pid=pid)
        except Exception as e:
            debug_log(DEBUG_ERROR, "Error killing process tree", pid=pid, error=str(e))

    def _ensure_session_clean(self):
        """Ensure no existing Cline processes are running"""
        cline_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if 'cline' in cmdline and 'python' not in cmdline:
                    cline_processes.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        if cline_processes:
            debug_log(DEBUG_WARN, "Found existing Cline processes", count=len(cline_processes))
            for pid in cline_processes:
                self._kill_process_tree(pid)
            time.sleep(1)

    def _close_fd(self, fd, fd_name):
        """Close a file descriptor safely"""
        if fd:
            try:
                os.close(fd)
                debug_log(DEBUG_DEBUG, f"Closed {fd_name}")
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Error closing {fd_name}", error=str(e))
            return None
        return None

    def _cleanup_resources(self):
        """Comprehensive cleanup of all resources"""
        debug_log(DEBUG_INFO, "Performing comprehensive cleanup")
        self.stop_reading = True
        if self.process:
            self._kill_process_tree(self.process.pid)
            self.process = None
        self._ensure_session_clean()
        self.master_fd = self._close_fd(self.master_fd, "master_fd")
        self.slave_fd = self._close_fd(self.slave_fd, "slave_fd")
        self.is_running = False
        self.session_active = False
        self.child_pids.clear()
        self.output_queue.clear()
        debug_log(DEBUG_DEBUG, "Cleanup complete")

    def start_pty_session(self, application=None):
        """Start PTY session with proper process management"""
        debug_log(DEBUG_INFO, "start_pty_session called")
        
        if self.session_active:
            debug_log(DEBUG_WARN, "Session already active")
            return False
        
        self._ensure_session_clean()
        
        try:
            self.master_fd, self.slave_fd = pty.openpty()
            env = dict(os.environ, TERM='xterm-256color', COLUMNS='80', LINES='24')
            
            self.process = subprocess.Popen(
                CLINE_COMMAND,
                stdin=self.slave_fd, stdout=self.slave_fd, stderr=self.slave_fd,
                preexec_fn=os.setsid, env=env
            )
            
            self.child_pids = {self.process.pid}
            time.sleep(0.5)
            
            if self.process.poll() is not None:
                raise RuntimeError("Cline process died immediately")

            self.is_running = True
            self.session_active = True
            self.stop_reading = False
            self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
            self.output_thread.start()

            debug_log(DEBUG_INFO, "PTY session started successfully")
            time.sleep(1)
            
            if application:
                async def notify():
                    await self._send_notification(
                        AUTHORIZED_USER_ID,
                        "🟢 **Cline Session Started**\n\nPTY session is now active and ready for commands.",
                        "Session start notification sent",
                        "Failed to send session start notification"
                    )
                
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(notify())
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to schedule notification", error=str(e))
            
            return True
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to start PTY session", error=str(e))
            self._cleanup_resources()
            return False

    def stop_pty_session(self, application=None):
        """Stop PTY session with comprehensive cleanup"""
        debug_log(DEBUG_INFO, "stop_pty_session called")
        
        if not self.session_active:
            return

        self.stop_reading = True
        self.session_active = False

        if self.process:
            self._kill_process_tree(self.process.pid)
            self._ensure_session_clean()

        if self.output_thread and self.output_thread.is_alive():
            self.output_thread.join(timeout=2.0)

        self._cleanup_resources()
        self._output_monitor_started = False
        
        if application:
            async def notify():
                await self._send_notification(
                    AUTHORIZED_USER_ID,
                    "🔴 **Cline Session Stopped**\n\nUse /start to begin a new session.",
                    "Session stop notification sent",
                    "Failed to send session stop notification"
                )
            
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(notify())
            except Exception as e:
                debug_log(DEBUG_ERROR, "Failed to schedule notification", error=str(e))

    async def _send_notification(self, chat_id, message, success_log, error_log):
        """Send a notification message with error handling"""
        try:
            await self.application.bot.send_message(chat_id=chat_id, text=message)
            debug_log(DEBUG_INFO, success_log)
        except Exception as e:
            debug_log(DEBUG_ERROR, error_log, error=str(e))

    async def _send_message(self, chat_id, text):
        """Send a message to Telegram"""
        try:
            await self.application.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send message", error=str(e))

    def _output_reader(self):
        """Background thread to continuously read PTY output"""
        debug_log(DEBUG_INFO, "Output reader thread started")
        read_count = 0
        error_count = 0
        
        while not self.stop_reading and self.is_running:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        output = data.decode('utf-8', errors='replace')
                        read_count += 1
                        self._process_output(output)
                    else:
                        debug_log(DEBUG_WARN, "EOF received from PTY")
                        break
                else:
                    time.sleep(0.05)
            except Exception as e:
                error_count += 1
                if error_count > 10:
                    debug_log(DEBUG_ERROR, "Too many errors, stopping output reader")
                    break
                time.sleep(0.1)

        debug_log(DEBUG_INFO, "Output reader thread stopped", total_reads=read_count, total_errors=error_count)

    def _process_output(self, output):
        """Process incoming output from Cline"""
        clean_output = strip_ansi_codes(output)
        
        ui_indicators = ['╭', '╰', '│', '┃', '╮', '╯', 'cline cli', '/plan or /act', 'alt+enter']
        ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)
        
        is_welcome_screen = 'cline cli' in clean_output
        is_box_line = bool(re.match(r'^[\s│┃╭╰╮╯]+$', clean_output.strip()))
        is_mode_switch = any(x in clean_output.lower() for x in ['switch to plan', 'switch to act', 'plan mode', 'act mode'])
        is_mostly_empty_ui = (clean_output.strip() in ['╭', '╰', '│', '┃', '╮', '╯'] or is_box_line) and len(clean_output.strip()) <= 3

        if not is_welcome_screen and not is_mode_switch and is_mostly_empty_ui:
            return
        
        # Detect interactive prompts
        prompt_patterns = [
            r'\[y/N\]', r'\[Y/n\]', r'\(y/n\)', r'\(Y/N\)', r'Continue\?', r'Proceed\?',
            r'Are you sure\?', r'Enter .*:\s*$', r'Password:\s*$', r'Press.*Enter.*to.*continue',
            r'Press.*any.*key', r'\[.*\]\s*$', r'Press .*to exit', r'Press .* to return',
        ]

        for pattern in prompt_patterns:
            if re.search(pattern, clean_output, re.IGNORECASE):
                self.waiting_for_input = True
                self.input_prompt = clean_output.strip()
                self.last_prompt_time = time.time()
                debug_log(DEBUG_INFO, "Interactive prompt detected", pattern=pattern)
                break

        if not self.waiting_for_input and re.search(r'[\[\(].*[\]\)]\s*$', clean_output.strip()):
            self.waiting_for_input = True
            self.input_prompt = clean_output.strip()

        self.output_queue.append(clean_output)
        if len(self.output_queue) > 100:
            self.output_queue.popleft()
            debug_log(DEBUG_WARN, "Queue overflow, removing oldest entry")

    def send_command(self, command):
        """Send command to Cline"""
        debug_log(DEBUG_INFO, "send_command called", command=command)
        
        if not self.is_running:
            debug_log(DEBUG_ERROR, "Cannot send command - PTY not running")
            return "Error: PTY session not running"

        current_time = time.time()
        if self.waiting_for_input and (current_time - self.last_prompt_time) > 30:
            debug_log(DEBUG_INFO, "Resetting stale waiting_for_input state")
            self.waiting_for_input = False
            self.input_prompt = ""

        try:
            self.waiting_for_input = False
            self.input_prompt = ""
            
            os.write(self.master_fd, f"{command}\r\n".encode())
            time.sleep(0.2)
            self.current_command = command
            
            debug_log(DEBUG_INFO, "Command sent successfully", command=command)
            return "Command sent"
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send command", command=command, error=str(e))
            return f"Error sending command: {e}"

    def get_pending_output(self, max_length=4000):
        """Get accumulated output"""
        if not self.output_queue:
            return None

        combined = ""
        chunks_used = 0
        
        while self.output_queue and len(combined) < max_length:
            chunk = self.output_queue.popleft()
            if len(combined + chunk) > max_length:
                self.output_queue.appendleft(chunk)
                break
            combined += chunk
            chunks_used += 1

        result = combined.strip() if combined else None
        debug_log(DEBUG_DEBUG, "Output prepared", chunks_used=chunks_used, final_length=len(result) if result else 0)
        return result

    async def _ensure_session_active(self, update: Update) -> bool:
        """Check if session is active"""
        if not self.session_active:
            await update.message.reply_text("❌ No active session. Use /start first")
            return False
        return True

    async def _command_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Generic command handler"""
        debug_log(DEBUG_INFO, f"Processing {cmd} command")
        
        handlers = {
            "/start": self._start,
            "/stop": self._stop,
            "/status": self._status,
            "/cancel": self._cancel,
            "/plan": self._mode_switch,
            "/act": self._mode_switch,
        }
        
        if cmd in handlers:
            await handlers[cmd](update, context, cmd)

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Handle /start"""
        if self.session_active:
            await update.message.reply_text("ℹ️ Cline session already running")
            return
        
        if self.start_pty_session(self.application):
            await update.message.reply_text(
                "✅ Cline session started\n\n**Bot Commands:**\n"
                "• Natural language: `show me the current directory`\n"
                "• CLI commands: `git status`, `ls`\n"
                "• `/plan` - Plan mode\n• `/act` - Act mode\n• `/cancel` - Cancel task\n• `/status` - Check status\n• `/stop` - End session"
            )
            if not self._output_monitor_started:
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(output_monitor(self, self.application, self.last_chat_id))
                    self._output_monitor_started = True
                    debug_log(DEBUG_DEBUG, "Output monitor task created")
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to create output monitor", error=str(e))
        else:
            await update.message.reply_text("❌ Failed to start Cline session")

    async def _stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Handle /stop"""
        self.stop_pty_session(self.application)
        await update.message.reply_text("🛑 Cline session stopped")

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Handle /status"""
        status = "🟢 Running" if self.session_active else "🔴 Stopped"
        waiting = " (waiting for input)" if self.waiting_for_input else ""
        await update.message.reply_text(f"Status: {status}{waiting}")

    async def _cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Handle /cancel"""
        if not await self._ensure_session_active(update):
            return
        await self._send_message(update.effective_chat.id, "🛑 Cancel signal sent")
        self.send_command("\x03")
        await asyncio.sleep(0.5)
        output = self.get_pending_output()
        if output:
            await self._send_message(update.effective_chat.id, output)

    async def _mode_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
        """Handle /plan and /act"""
        if not await self._ensure_session_active(update):
            return
        mode = cmd[1:].upper()
        await self._send_message(update.effective_chat.id, f"📋 Switched to **{mode} MODE**")
        self.send_command(cmd)
        await asyncio.sleep(0.5)
        output = self.get_pending_output()
        if output:
            await self._send_message(update.effective_chat.id, output)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages"""
        if update.effective_user.id != AUTHORIZED_USER_ID:
            await update.message.reply_text("❌ Unauthorized")
            return

        message_text = update.message.text.strip() if update.message.text else ""
        self.last_chat_id = update.effective_chat.id
        
        # Command dispatch
        if message_text.startswith('/'):
            await self._command_handler(update, context, message_text)
            return

        # Handle interactive input
        if self.waiting_for_input:
            debug_log(DEBUG_INFO, "Processing interactive input")
            self.send_command(message_text)
            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            if output:
                await self._send_message(update.effective_chat.id, output)
            return

        # Regular commands
        if self.session_active:
            debug_log(DEBUG_INFO, "Processing regular command", command=message_text)
            self.send_command(message_text)
            await self._send_message(update.effective_chat.id, f"📤 Message sent: {message_text}")
            await asyncio.sleep(2.0)
            output = self.get_pending_output()
            if output:
                await self._send_message(update.effective_chat.id, output)
        else:
            await update.message.reply_text("❌ Cline session not running. Use /start first")

async def output_monitor(bot_instance, application, chat_id):
    """Monitor for new output and send to user"""
    debug_log(DEBUG_INFO, "Output monitor started")
    iteration_count = 0
    recent_messages = deque(maxlen=10)
    
    while True:
        iteration_count += 1
        if iteration_count % 30 == 0:
            debug_log(DEBUG_DEBUG, "Output monitor heartbeat", iterations=iteration_count)
        
        if not chat_id:
            await asyncio.sleep(2)
            continue
        
        if bot_instance.session_active and bot_instance.output_queue:
            output = bot_instance.get_pending_output()
            if output:
                clean_output = strip_ansi_codes(output)
                lines = [l.strip() for l in clean_output.split('\n')]
                lines = list(dict.fromkeys(lines))
                clean_output = '\n'.join(lines)

                ui_indicators = ['╭', '╰', '│', '┃', '/plan or /act']
                ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)
                
                normalized = ' '.join(clean_output.split())
                msg_hash = hash(normalized)
                is_cline_response = '###' in clean_output
                is_repetitive_ui = ui_score >= 1 and '/plan or /act' in clean_output

                should_filter = (
                    msg_hash in recent_messages or
                    (is_repetitive_ui and not is_cline_response) or
                    (ui_score >= 2 and len(clean_output.strip()) <= 50)
                )

                if should_filter:
                    debug_log(DEBUG_DEBUG, "Filtered message", ui_score=ui_score)
                    if is_repetitive_ui:
                        recent_messages.append(msg_hash)
                    await asyncio.sleep(2)
                    continue
                
                debug_log(DEBUG_INFO, "Sending output to user", output_length=len(clean_output))
                try:
                    await application.bot.send_message(chat_id=chat_id, text=clean_output)
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Error sending output", error=str(e))

        await asyncio.sleep(2)

async def send_startup_message(app):
    """Send startup notification"""
    try:
        await app.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text="🤖 **Cline Remote Chatter Bot Started**\n\n"
                 "• PTY session management ready\n"
                 "• Background output monitoring active\n\n"
                 "Use /start to begin a Cline session"
        )
        debug_log(DEBUG_INFO, "Startup notification sent")
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to send startup notification", error=str(e))

def main():
    debug_log(DEBUG_INFO, "main() called")
    
    if not TELEGRAM_BOT_TOKEN:
        debug_log(DEBUG_ERROR, "TELEGRAM_BOT_TOKEN not set")
        return

    bot = ClineTelegramBot()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot.application = application

    application.add_handler(CommandHandler("start", bot.handle_message))
    application.add_handler(CommandHandler("stop", bot.handle_message))
    application.add_handler(CommandHandler("status", bot.handle_message))
    application.add_handler(CommandHandler("plan", bot.handle_message))
    application.add_handler(CommandHandler("act", bot.handle_message))
    application.add_handler(CommandHandler("cancel", bot.handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    async def post_init(app):
        """Called after bot is initialized"""
        await send_startup_message(app)

    application.post_init = post_init

    def signal_handler(signum, frame):
        debug_log(DEBUG_INFO, f"Received signal {signum}, shutting down")
        if bot.session_active:
            bot.stop_pty_session()
        import sys
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    debug_log(DEBUG_INFO, "Bot starting")
    application.run_polling()

if __name__ == "__main__":
    main()