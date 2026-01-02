import os
import pty
import select
import subprocess
import threading
import time
import asyncio
import re
from collections import deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import psutil

def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text"""
    ansi_pattern = r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])'
    return re.sub(ansi_pattern, '', text)

# Load environment variables
load_dotenv()

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
CLINE_COMMAND = ["cline"]

def debug_log(level, message, **kwargs):
    """Centralized debug logging function"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    prefix = f"[{timestamp}] [{level}]"
    
    if kwargs:
        context = " | ".join([f"{k}={v}" for k, v in kwargs.items()])
        message = f"{prefix} {message} | {context}"
    else:
        message = f"{prefix} {message}"
    
    print(message)

# Debug level constants
DEBUG_INFO = "INFO"
DEBUG_WARN = "WARN"
DEBUG_ERROR = "ERROR"
DEBUG_DEBUG = "DEBUG"

class ClineTelegramBot:
    def __init__(self):
        debug_log(DEBUG_INFO, "ClineTelegramBot.__init__ called")
        
        self.master_fd = None
        self.slave_fd = None
        self.process = None
        self.is_running = False

        # Output handling
        self.output_queue = deque()
        self.output_thread = None
        self.stop_reading = False

        # Command handling
        self.command_queue = deque()
        self.current_command = None
        self.waiting_for_input = False
        self.input_prompt = ""
        self.last_prompt_time = 0

        # Session state
        self.session_active = False
        
        # Process tracking for cleanup
        self.child_pids = set()
        
        # Application reference for notifications
        self.application = None

        self.last_chat_id = None
        
        debug_log(DEBUG_DEBUG, "Bot initialized with default state", 
                 master_fd=self.master_fd, slave_fd=self.slave_fd, 
                 is_running=self.is_running, session_active=self.session_active)

    def _find_child_processes(self, parent_pid):
        """Find all child processes of a given PID"""
        children = set()
        try:
            parent = psutil.Process(parent_pid)
            # Get all descendants
            for child in parent.children(recursive=True):
                children.add(child.pid)
            # Also add the parent itself
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
                    process = psutil.Process(child_pid)
                    # Try graceful termination first
                    process.terminate()
                except psutil.NoSuchProcess:
                    continue
            
            # Wait for processes to terminate
            time.sleep(0.5)
            
            # Force kill any remaining processes
            for child_pid in children:
                try:
                    process = psutil.Process(child_pid)
                    if process.is_running():
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
            
            # Wait for cleanup
            time.sleep(0.2)
            debug_log(DEBUG_DEBUG, "Process tree killed", parent_pid=pid)
            
        except Exception as e:
            debug_log(DEBUG_ERROR, "Error killing process tree", 
                     pid=pid, error_type=type(e).__name__, error=str(e))

    def _ensure_session_clean(self):
        """Ensure no existing Cline processes are running"""
        debug_log(DEBUG_INFO, "Checking for existing Cline processes")
        
        # Look for any running cline processes
        cline_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info['cmdline'] or []
                cmdline_str = ' '.join(cmdline)
                if 'cline' in cmdline_str and 'python' not in cmdline_str:
                    cline_processes.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        if cline_processes:
            debug_log(DEBUG_WARN, "Found existing Cline processes", 
                     pids=cline_processes, count=len(cline_processes))
            
            for pid in cline_processes:
                self._kill_process_tree(pid)
            
            # Wait for cleanup
            time.sleep(1)
            debug_log(DEBUG_INFO, "Cleaned up existing processes")
        else:
            debug_log(DEBUG_DEBUG, "No existing Cline processes found")

    def start_pty_session(self, application=None):
        """Start PTY session with proper process management"""
        debug_log(DEBUG_INFO, "start_pty_session called")
        
        # Check if already running
        if self.session_active:
            debug_log(DEBUG_WARN, "Session already active, refusing to start new one")
            return False
        
        # Ensure clean state before starting
        self._ensure_session_clean()
        
        try:
            debug_log(DEBUG_DEBUG, "Opening PTY...")
            self.master_fd, self.slave_fd = pty.openpty()
            debug_log(DEBUG_DEBUG, "PTY opened successfully", 
                     master_fd=self.master_fd, slave_fd=self.slave_fd)

            debug_log(DEBUG_DEBUG, "Starting subprocess", 
                     command=CLINE_COMMAND, slave_fd=self.slave_fd)
            
            # Start Cline with proper environment
            env = dict(os.environ, TERM='xterm-256color', COLUMNS='80', LINES='24')
            
            # Use process group to kill entire tree
            self.process = subprocess.Popen(
                CLINE_COMMAND,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid,  # Create new process group
                env=env
            )
            
            debug_log(DEBUG_DEBUG, "Subprocess started", 
                     pid=self.process.pid, returncode=self.process.poll())

            # Track the main process
            self.child_pids = {self.process.pid}
            
            # Wait a moment and check if process is still alive
            time.sleep(0.5)
            if self.process.poll() is not None:
                raise RuntimeError("Cline process died immediately")

            self.is_running = True
            self.session_active = True
            debug_log(DEBUG_DEBUG, "State updated", 
                     is_running=self.is_running, session_active=self.session_active)

            # Start background output reader
            self.stop_reading = False
            self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
            self.output_thread.start()
            debug_log(DEBUG_DEBUG, "Output reader thread started", 
                     thread_name=self.output_thread.name, daemon=self.output_thread.daemon)

            debug_log(DEBUG_INFO, "PTY session started successfully")
            
            # Wait for Cline to initialize
            time.sleep(1)
            
            # Send session start notification
            if application:
                async def send_session_start_notification():
                    try:
                        await application.bot.send_message(
                            chat_id=AUTHORIZED_USER_ID,
                            text="🟢 **Cline Session Started**\n\n"
                                 "PTY session is now active and ready for commands.\n"
                                 "Output will be sent automatically as it becomes available.\n\n"
                                 "**Mode Commands:**\n"
                                 "/plan - Switch to plan mode\n"
                                 "/act - Switch to act mode\n"
                                 "/cancel - Cancel current task"
                        )
                        debug_log(DEBUG_INFO, "Session start notification sent")
                    except Exception as e:
                        debug_log(DEBUG_ERROR, "Failed to send session start notification", 
                                 error_type=type(e).__name__, error=str(e))
                
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(send_session_start_notification())
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to schedule session start notification", 
                             error_type=type(e).__name__, error=str(e))
            
            return True
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to start PTY session", 
                     error_type=type(e).__name__, error=str(e), exc_info=True)
            # Cleanup on failure
            self._cleanup_resources()
            return False

    def stop_pty_session(self, application=None):
        """Stop PTY session with comprehensive cleanup"""
        debug_log(DEBUG_INFO, "stop_pty_session called")
        
        if not self.session_active:
            debug_log(DEBUG_DEBUG, "No active session to stop")
            return

        self.stop_reading = True
        self.session_active = False
        debug_log(DEBUG_DEBUG, "State updated", 
                 stop_reading=self.stop_reading, session_active=self.session_active)

        # Kill all tracked processes
        if self.process:
            debug_log(DEBUG_DEBUG, "Stopping process", 
                     pid=self.process.pid, returncode=self.process.poll())
            
            # Kill the entire process tree
            self._kill_process_tree(self.process.pid)
            
            # Also kill any processes we might have missed
            self._ensure_session_clean()

        # Wait for output thread to finish
        if self.output_thread and self.output_thread.is_alive():
            debug_log(DEBUG_DEBUG, "Waiting for output thread to finish")
            self.output_thread.join(timeout=2.0)
            if self.output_thread.is_alive():
                debug_log(DEBUG_WARN, "Output thread did not finish cleanly")

        # Cleanup file descriptors
        self._cleanup_file_descriptors()

        # Reset state
        self.process = None
        self.child_pids.clear()
        self.is_running = False
        debug_log(DEBUG_DEBUG, "Final state", is_running=self.is_running)
        debug_log(DEBUG_INFO, "PTY session stopped")
        
        # Send session stop notification
        if application:
            async def send_session_stop_notification():
                try:
                    await application.bot.send_message(
                        chat_id=AUTHORIZED_USER_ID,
                        text="🔴 **Cline Session Stopped**\n\n"
                             "PTY session has been terminated.\n"
                             "Use /start to begin a new session."
                    )
                    debug_log(DEBUG_INFO, "Session stop notification sent")
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to send session stop notification", 
                             error_type=type(e).__name__, error=str(e))
            
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(send_session_stop_notification())
            except Exception as e:
                debug_log(DEBUG_ERROR, "Failed to schedule session stop notification", 
                         error_type=type(e).__name__, error=str(e))

    def _cleanup_file_descriptors(self):
        """Close file descriptors safely"""
        if self.master_fd:
            debug_log(DEBUG_DEBUG, "Closing master_fd", fd=self.master_fd)
            try:
                os.close(self.master_fd)
            except Exception as e:
                debug_log(DEBUG_ERROR, "Error closing master_fd", error=str(e))
            self.master_fd = None

        if self.slave_fd:
            debug_log(DEBUG_DEBUG, "Closing slave_fd", fd=self.slave_fd)
            try:
                os.close(self.slave_fd)
            except Exception as e:
                debug_log(DEBUG_ERROR, "Error closing slave_fd", error=str(e))
            self.slave_fd = None

    def _cleanup_resources(self):
        """Comprehensive cleanup of all resources"""
        debug_log(DEBUG_INFO, "Performing comprehensive cleanup")
        
        # Stop reading
        self.stop_reading = True
        
        # Kill processes
        if self.process:
            self._kill_process_tree(self.process.pid)
            self.process = None
        
        # Ensure clean state
        self._ensure_session_clean()
        
        # Cleanup file descriptors
        self._cleanup_file_descriptors()
        
        # Reset state
        self.is_running = False
        self.session_active = False
        self.child_pids.clear()
        
        # Clear queues
        self.output_queue.clear()
        self.command_queue.clear()
        
        debug_log(DEBUG_DEBUG, "Cleanup complete")

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
                        # EOF reached - process died
                        debug_log(DEBUG_WARN, "EOF received from PTY")
                        break
                else:
                    # Timeout, no data available
                    time.sleep(0.05)
            except Exception as e:
                error_count += 1
                if error_count > 10:
                    debug_log(DEBUG_ERROR, "Too many errors, stopping output reader")
                    break
                time.sleep(0.1)

        debug_log(DEBUG_INFO, "Output reader thread stopped", 
                 total_reads=read_count, total_errors=error_count)

    def _process_output(self, output):
        """Process incoming output from Cline"""
        clean_output = strip_ansi_codes(output)
        
        # Filter out Cline CLI UI prompts and repetitive UI elements
        ui_indicators = [
            '╭', '╰', '│', '┃', '╮', '╯',
            'cline cli preview',
            '/plan or /act',
            'alt+enter',
            'openrouter/xiaomi',
            '~/cline-workspace',
            'enter submit',
            'new line',
            'open editor',
        ]
        
        ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)
        lines = clean_output.split('\n')
        empty_lines = sum(1 for line in lines if not line.strip())
        
        is_welcome_screen = 'cline cli preview' in clean_output and 'openrouter/xiaomi' in clean_output
        is_ui_heavy = ui_score >= 2 and not is_welcome_screen
        is_ui_heavy = is_ui_heavy or (len(lines) > 0 and empty_lines / len(lines) > 0.5)
        is_box_char = clean_output.strip() in ['╭', '╰', '│', '┃', '╮', '╯']
        is_box_line = bool(re.match(r'^[\s│┃╭╰╮╯]+$', clean_output.strip()))
        
        api_patterns = [
            r'## API request completed',
            r'↑.*↓.*\$',
            r'Tokens:.*Prompt:.*Completion:',
            r'Cost:.*\$',
            r'Elapsed:.*s',
        ]
        is_api_metadata = any(re.search(pattern, clean_output, re.IGNORECASE) for pattern in api_patterns)
        
        is_command_echo = False
        if self.current_command:
            if self.current_command not in ['/plan', '/act']:
                echo_pattern = r'^[\s│┃]*' + re.escape(self.current_command) + r'[\s│┃]*$'
                is_command_echo = bool(re.match(echo_pattern, clean_output.strip()))
        
        is_mode_switch_confirmation = False
        if self.current_command in ['/plan', '/act']:
            mode_indicators = ['switch to plan mode', 'switch to act mode', 'plan mode', 'act mode']
            is_mode_switch_confirmation = any(indicator in clean_output.lower() for indicator in mode_indicators)
        
        is_mostly_empty_ui = (is_box_char or is_box_line) and len(clean_output.strip()) <= 3

        if not is_welcome_screen and not is_mode_switch_confirmation and is_mostly_empty_ui:
            if clean_output.strip():
                debug_log(DEBUG_DEBUG, f"Filtered out pure UI: {clean_output.replace(chr(10), '\\n')}")
            return
        
        if clean_output.strip():
            debug_log(DEBUG_DEBUG, f"Queued output: {clean_output.replace(chr(10), '\\n')}")

        prompt_patterns = [
            r'\[y/N\]', r'\[Y/n\]', r'\(y/n\)', r'\(Y/N\)',
            r'Continue\?', r'Proceed\?', r'Are you sure\?',
            r'Enter .*:\s*$', r'Password:\s*$',
            r'Press.*Enter.*to.*continue',
            r'Press.*any.*key',
            r'\[.*\]\s*$',
            r'Press .*to exit',
            r'Press .* to return', 
        ]

        prompt_detected = False
        for pattern in prompt_patterns:
            if re.search(pattern, clean_output, re.IGNORECASE):
                old_state = self.waiting_for_input
                self.waiting_for_input = True
                self.input_prompt = clean_output.strip()
                self.last_prompt_time = time.time()
                prompt_detected = True
                debug_log(DEBUG_INFO, "Interactive prompt detected", 
                         pattern=pattern, prompt=self.input_prompt[:50],
                         old_state=old_state, new_state=self.waiting_for_input)
                break

        if not prompt_detected:
            if re.search(r'[\[\(].*[\]\)]\s*$', clean_output.strip()):
                old_state = self.waiting_for_input
                self.waiting_for_input = True
                self.input_prompt = clean_output.strip()
                prompt_detected = True
                debug_log(DEBUG_INFO, "Detected continuation prompt", 
                         prompt_preview=clean_output[:50])

        if not prompt_detected and self.waiting_for_input:
            debug_log(DEBUG_DEBUG, "Output received while waiting for input", 
                     was_waiting=True)

        self.output_queue.append(clean_output)
        debug_log(DEBUG_DEBUG, "Output added to queue", 
                 queue_size=len(self.output_queue), 
                 waiting_for_input=self.waiting_for_input)

        if len(self.output_queue) > 100:
            self.output_queue.popleft()
            debug_log(DEBUG_WARN, "Queue overflow, removing oldest entry")

    def send_command(self, command):
        """Send command to Cline"""
        debug_log(DEBUG_INFO, "send_command called", command=command, is_running=self.is_running)
        
        if not self.is_running:
            debug_log(DEBUG_ERROR, "Cannot send command - PTY not running")
            return "Error: PTY session not running"

        # Reset stale waiting state (if waiting for more than 30 seconds)
        current_time = time.time()
        if self.waiting_for_input and hasattr(self, 'last_prompt_time') and (current_time - self.last_prompt_time) > 30:
            debug_log(DEBUG_INFO, "Resetting stale waiting_for_input state", 
                     time_since_prompt=current_time - self.last_prompt_time)
            self.waiting_for_input = False
            self.input_prompt = ""

        try:
            # CRITICAL FIX: Reset input state BEFORE sending command
            old_waiting = self.waiting_for_input
            old_prompt = self.input_prompt
            self.waiting_for_input = False
            self.input_prompt = ""
            debug_log(DEBUG_DEBUG, "Reset input state", 
                     old_waiting=old_waiting, old_prompt_preview=old_prompt[:30] if old_prompt else None,
                     new_waiting=self.waiting_for_input)

            command_with_newline = f"{command}\r\n"
            command_bytes = command_with_newline.encode()
            bytes_written = os.write(self.master_fd, command_bytes)
            debug_log(DEBUG_DEBUG, "Command sent with newline", 
                    bytes_written=bytes_written, expected=len(command_bytes))

            time.sleep(0.2)

            self.current_command = command
            
            if self.process:
                returncode = self.process.poll()
                debug_log(DEBUG_DEBUG, "Subprocess status", 
                         returncode=returncode, 
                         alive=returncode is None)
            
            debug_log(DEBUG_INFO, "Command sent successfully", command=command)
            return "Command sent"
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send command", 
                     command=command, error_type=type(e).__name__, error=str(e),
                     master_fd=self.master_fd, is_running=self.is_running,
                     exc_info=True)
            return f"Error sending command: {e}"

    def send_enter(self):
        """Send Enter key to dismiss continuation prompts"""
        debug_log(DEBUG_INFO, "send_enter called")
        
        if not self.is_running:
            debug_log(DEBUG_ERROR, "Cannot send Enter - PTY not running")
            return False

        try:
            os.write(self.master_fd, b"\n")
            debug_log(DEBUG_DEBUG, "Enter key sent")
            time.sleep(0.2)
            return True
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send Enter", 
                     error_type=type(e).__name__, error=str(e))
            return False

    def get_pending_output(self, max_length=4000):
        """Get accumulated output, formatted for Telegram"""
        queue_size = len(self.output_queue)
        debug_log(DEBUG_DEBUG, "get_pending_output called", 
                 queue_size=queue_size, max_length=max_length)
        
        if not self.output_queue:
            debug_log(DEBUG_DEBUG, "No pending output")
            return None

        combined = ""
        chunks_used = 0
        original_queue_size = queue_size
        
        while self.output_queue and len(combined) < max_length:
            chunk = self.output_queue.popleft()
            if len(combined + chunk) > max_length:
                self.output_queue.appendleft(chunk)
                debug_log(DEBUG_DEBUG, "Hit max length limit", 
                         combined_len=len(combined), chunk_len=len(chunk),
                         remaining_in_queue=len(self.output_queue))
                break
            combined += chunk
            chunks_used += 1

        result = combined.strip() if combined else None
        
        debug_log(DEBUG_DEBUG, "Output prepared", 
                 original_queue_size=original_queue_size,
                 chunks_used=chunks_used,
                 final_length=len(result) if result else 0,
                 remaining_queue_size=len(self.output_queue),
                 preview=(result[:50].replace('\n', '\\n') if result else None))
        
        return result

    def is_waiting_for_input(self):
        """Check if Cline is waiting for user input"""
        return self.waiting_for_input

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming Telegram messages"""
        debug_log(DEBUG_INFO, "handle_message called")
        
        user_id = update.effective_user.id
        message_text = update.message.text.strip() if update.message.text else ""
        
        debug_log(DEBUG_DEBUG, "Message details", 
                 user_id=user_id, 
                 authorized_id=AUTHORIZED_USER_ID,
                 message_text_preview=message_text[:50],
                 message_length=len(message_text))

        if user_id != AUTHORIZED_USER_ID:
            debug_log(DEBUG_WARN, "Unauthorized access attempt", 
                     user_id=user_id, authorized_id=AUTHORIZED_USER_ID)
            await update.message.reply_text("❌ Unauthorized access")
            return

        debug_log(DEBUG_DEBUG, "Authorized user message", message_text=message_text)

        self.last_chat_id = update.effective_chat.id
        debug_log(DEBUG_DEBUG, "Chat ID set", 
                last_chat_id=self.last_chat_id, 
                effective_chat_id=update.effective_chat.id)

        # Special commands
        if message_text == "/start":
            debug_log(DEBUG_INFO, "Processing /start command", 
                     session_active=self.session_active)
            if not self.session_active:
                if self.start_pty_session(self.application):
                    debug_log(DEBUG_INFO, "/start: Session started successfully")
                    await update.message.reply_text("✅ Cline session started\n\n**Bot Commands:**\n• Natural language: `show me the current directory`\n• CLI commands: `git status`, `ls`\n• `/plan` - Switch Cline to plan mode\n• `/act` - Switch Cline to act mode\n• `/cancel` - Cancel current task\n• `/status` - Check status\n• `/stop` - End session")
                    try:
                        loop = asyncio.get_event_loop()
                        if not hasattr(self, '_output_monitor_started'):
                            loop.create_task(output_monitor(self, self.application, self.last_chat_id))
                            self._output_monitor_started = True
                            debug_log(DEBUG_DEBUG, "Output monitor task created for session")
                    except Exception as e:
                        debug_log(DEBUG_ERROR, "Failed to create output monitor task", 
                                error_type=type(e).__name__, error=str(e))
                else:
                    debug_log(DEBUG_ERROR, "/start: Failed to start session")
                    await update.message.reply_text("❌ Failed to start Cline session")
            else:
                debug_log(DEBUG_INFO, "/start: Session already running")
                await update.message.reply_text("ℹ️ Cline session already running")
            return

        if message_text == "/stop":
            debug_log(DEBUG_INFO, "Processing /stop command")
            self.stop_pty_session(self.application)
            await update.message.reply_text("🛑 Cline session stopped")
            return

        if message_text == "/status":
            debug_log(DEBUG_INFO, "Processing /status command")
            status = "🟢 Running" if self.session_active else "🔴 Stopped"
            waiting = " (waiting for input)" if self.is_waiting_for_input() else ""
            debug_log(DEBUG_DEBUG, "Status check", 
                     session_active=self.session_active, 
                     waiting_for_input=self.is_waiting_for_input(),
                     status=status)
            await update.message.reply_text(f"Status: {status}{waiting}")
            return

        if message_text == "/cancel":
            debug_log(DEBUG_INFO, "Processing /cancel command")
            if self.session_active:
                result = self.send_command("\x03")
                debug_log(DEBUG_DEBUG, "Cancel signal sent", result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🛑 Cancel signal sent to Cline"
                )
                
                await asyncio.sleep(0.5)
                output = self.get_pending_output()
                if output:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=output
                    )
            else:
                await update.message.reply_text("❌ No active session to cancel")
            return

        if message_text == "/plan":
            debug_log(DEBUG_INFO, "Processing /plan command")
            if self.session_active:
                result = self.send_command("/plan")
                debug_log(DEBUG_DEBUG, "Plan mode switch sent", result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="📋 Switched Cline to **PLAN MODE**"
                )
                
                await asyncio.sleep(0.5)
                output = self.get_pending_output()
                if output:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=output
                    )
            else:
                await update.message.reply_text("❌ No active session. Use /start first")
            return

        if message_text == "/act":
            debug_log(DEBUG_INFO, "Processing /act command")
            if self.session_active:
                result = self.send_command("/act")
                debug_log(DEBUG_DEBUG, "Act mode switch sent", result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚡ Switched Cline to **ACT MODE**"
                )
                
                await asyncio.sleep(0.5)
                output = self.get_pending_output()
                debug_log(DEBUG_DEBUG, "After /act - queue size", 
                         queue_size=len(self.output_queue), output=output)
                if output:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=output
                    )
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="ℹ️ Command sent. Waiting for Cline response..."
                    )
            else:
                await update.message.reply_text("❌ No active session. Use /start first")
            return

        # Handle interactive input
        if self.is_waiting_for_input():
            debug_log(DEBUG_INFO, "Processing interactive input", 
                     waiting_for_input=self.waiting_for_input,
                     prompt_preview=self.input_prompt[:50] if self.input_prompt else None)
            
            result = self.send_command(message_text)
            debug_log(DEBUG_DEBUG, "Interactive input sent", 
                     input=message_text, result=result)
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📤 Input sent: {message_text}"
            )

            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            if output:
                debug_log(DEBUG_DEBUG, "Interactive output received", 
                         output_length=len(output))
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=output
                )
                '''
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ Response complete"
                )
                '''
            else:
                debug_log(DEBUG_DEBUG, "No output received after interactive input")
            return

        # Regular commands
        if self.session_active:
            debug_log(DEBUG_INFO, "Processing regular command", 
                     command=message_text, session_active=self.session_active)
            
            # Enhanced debugging: Check state before sending command
            debug_log(DEBUG_DEBUG, "State before command", 
                     waiting_for_input=self.waiting_for_input,
                     queue_size_before=len(self.output_queue),
                     current_command=self.current_command)
            
            result = self.send_command(message_text)
            
            debug_log(DEBUG_DEBUG, "Command send result", 
                     result=result,
                     queue_size_after_send=len(self.output_queue))
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📤 {result}"
            )

            is_long_running = any(keyword in message_text.lower() for keyword in ['run', 'build', 'install', 'download', 'clone'])
            if is_long_running:
                debug_log(DEBUG_INFO, "Long-running task detected", command=message_text)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏳ Long-running task started. Output will be sent as it becomes available..."
                )

            await asyncio.sleep(2.0)
            
            # Enhanced debugging: Check state before getting output
            debug_log(DEBUG_DEBUG, "Before get_pending_output", 
                     queue_size=len(self.output_queue),
                     waiting_for_input=self.is_waiting_for_input())
            
            output = self.get_pending_output()
            
            debug_log(DEBUG_DEBUG, "After get_pending_output", 
                     got_output=bool(output),
                     output_length=len(output) if output else 0,
                     queue_size_after=len(self.output_queue),
                     waiting_for_input_after=self.is_waiting_for_input())
            
            if not output and self.is_waiting_for_input():
                debug_log(DEBUG_INFO, "No output but waiting for input, sending Enter to dismiss prompt")
                self.send_enter()
                await asyncio.sleep(0.3)
                output = self.get_pending_output()
                debug_log(DEBUG_DEBUG, "After Enter key, got output", 
                         got_output=bool(output),
                         output_length=len(output) if output else 0)
            
            if output:
                debug_log(DEBUG_DEBUG, "Immediate output received", output_length=len(output))
                chunks = [output[i:i+4000] for i in range(0, len(output), 4000)]
                debug_log(DEBUG_DEBUG, "Sending output in chunks", 
                         total_chunks=len(chunks), total_length=len(output))
                for i, chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=chunk
                    )
                    debug_log(DEBUG_DEBUG, "Sent chunk", chunk_num=i+1, chunk_length=len(chunk))
                
                '''
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ Response complete"
                )
                '''
            else:
                debug_log(DEBUG_DEBUG, "No immediate output, waiting for background reader")
                # Enhanced: Check if queue is being populated by background thread
                await asyncio.sleep(1)
                queue_after_wait = len(self.output_queue)
                debug_log(DEBUG_DEBUG, "Queue after 1 second wait", 
                         queue_size=queue_after_wait)
                if queue_after_wait > 0:
                    debug_log(DEBUG_WARN, "Output appeared after delay - this suggests timing issue")
        else:
            debug_log(DEBUG_WARN, "Command received but session not active", 
                     message_text=message_text, session_active=self.session_active)
            await update.message.reply_text("❌ Cline session not running. Use /start first")

async def output_monitor(bot_instance, application, chat_id):
    """Monitor for new output and send to user"""
    debug_log(DEBUG_INFO, "Output monitor started")
    iteration_count = 0
    last_send_time = 0

    # Initialize deduplication tracking
    recent_messages = set()
    MAX_RECENT_MESSAGES = 10
    
    while True:
        iteration_count += 1
        if iteration_count % 30 == 0:
            debug_log(DEBUG_DEBUG, "Output monitor heartbeat", iterations=iteration_count)
        
        if bot_instance.session_active and bot_instance.output_queue:
            debug_log(DEBUG_DEBUG, "Output monitor found data", 
                     queue_size=len(bot_instance.output_queue))
            
            current_time = time.time()
            
            output = bot_instance.get_pending_output()
            if output:
                # DEBUG: Log raw output from Cline
                debug_log(DEBUG_DEBUG, f"RAW CLINE OUTPUT: {repr(output)}")
                clean_output = strip_ansi_codes(output)

                clean_lines = clean_output.split('\n')
                clean_lines = list(dict.fromkeys(line.strip() for line in clean_lines))
                clean_output = '\n'.join(clean_lines)
                
                # DEBUG: Log cleaned output  
                debug_log(DEBUG_DEBUG, f"CLEAN CLINE OUTPUT: {repr(clean_output)}")

                lines = clean_output.split('\n')
                # Deduplicate while preserving original formatting
                seen = set()
                deduplicated_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped not in seen:
                        seen.add(stripped)
                        deduplicated_lines.append(line)  # Keep original formatting
                lines = deduplicated_lines
                ui_status_lines = []
                other_lines = []

                for line in lines:
                    if line.strip().startswith('┃') and len(line.strip()) > 5:
                        ui_status_lines.append(line)
                    else:
                        other_lines.append(line)
                
                # Send UI status lines immediately
                for ui_line in ui_status_lines:
                    ui_message = ui_line.strip()
                    if ui_message and len(ui_message) > 3:  # Not just "┃"
                        # Check if this UI message was already sent
                        ui_hash = hash(ui_message)
                        if ui_hash not in recent_messages:
                            debug_log(DEBUG_INFO, f"Sending immediate UI status: {ui_message}")
                            try:
                                '''
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"💬 {ui_message}"
                                )
                                '''
                                # Add to recent messages to prevent repeats
                                recent_messages.add(ui_hash)
                                if len(recent_messages) > MAX_RECENT_MESSAGES:
                                    recent_messages.pop()
                            except Exception as e:
                                debug_log(DEBUG_ERROR, "Failed to send UI status", error=str(e))
                        else:
                            debug_log(DEBUG_DEBUG, f"Skipping duplicate UI status: {ui_message}")
                
                # Process remaining output normally
                remaining_output = '\n'.join(other_lines).strip()

                # Deduplicate remaining output lines
                remaining_lines = remaining_output.split('\n')
                debug_log(DEBUG_DEBUG, f"BEFORE dedup: {len(remaining_lines)} lines")
                for i, line in enumerate(remaining_lines):
                    debug_log(DEBUG_DEBUG, f"Line {i}: '{line}' -> stripped: '{line.strip()}'")
                seen = set()
                deduplicated_lines = []
                for line in remaining_lines:
                    stripped = line.strip()
                    if stripped not in seen:
                        seen.add(stripped)
                        deduplicated_lines.append(line)  # Keep original formatting
                    else:
                        debug_log(DEBUG_DEBUG, f"REMOVED DUPLICATE: '{stripped}'")
                remaining_output = '\n'.join(deduplicated_lines)
                debug_log(DEBUG_DEBUG, f"AFTER dedup: {len(deduplicated_lines)} lines")

                if remaining_output:
                    # Use REMAINING_OUTPUT for filtering (not clean_output)
                    is_welcome_screen = 'cline cli' in remaining_output
                    ui_indicators = ['╭', '╰', '│', '┃', 'cline cli preview', '/plan or /act']
                    ui_score = sum(1 for indicator in ui_indicators if indicator in remaining_output)
                    
                    # All other filtering logic uses remaining_output
                    normalized_output = ' '.join(remaining_output.split())
                    message_hash = hash(normalized_output)  # ADD THIS LINE
                    is_repetitive_ui = ui_score >= 2 and '/plan or /act' in remaining_output
                    is_duplicate = message_hash in recent_messages  # Now defined
                    is_cline_response = '###' in remaining_output

                    should_filter = False

                    if is_duplicate:
                        should_filter = True
                    elif is_cline_response:
                        should_filter = False  # Allow Cline responses
                    elif '/plan or /act to switch modes' in remaining_output and ui_score >= 1:  # FIX: use remaining_output
                        should_filter = True  # Repetitive spam
                    elif ui_score >= 2 and len(remaining_output.strip()) <= 50:
                        should_filter = True  # Short UI junk
                    # Allow all messages with '###' (Cline responses)

                    if should_filter:
                        debug_log(DEBUG_DEBUG, "Filtered message", 
                                content=clean_output[:200].replace('\n', '\\n'),
                                ui_score=ui_score, 
                                output_length=len(clean_output),
                                is_duplicate=is_duplicate)
                        if '/plan or /act' in clean_output:  # Add repetitive messages to dedup list
                            recent_messages.add(normalized_output)
                            if len(recent_messages) > MAX_RECENT_MESSAGES:
                                recent_messages.pop()
                        continue
                    
                    debug_log(DEBUG_INFO, f"Sending output to user: {clean_output.replace(chr(10), '\\n')}", 
                        output_length=len(clean_output),
                        chat_id=chat_id)
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=clean_output
                        )
                        last_send_time = current_time
                        debug_log(DEBUG_DEBUG, "Output sent successfully")
                    except Exception as e:
                        debug_log(DEBUG_ERROR, "Error sending output", 
                                error_type=type(e).__name__, error=str(e))
            else:
                debug_log(DEBUG_DEBUG, "No output after get_pending_output")
        else:
            if not bot_instance.session_active:
                debug_log(DEBUG_DEBUG, "Output monitor: session not active")
            elif not bot_instance.output_queue:
                debug_log(DEBUG_DEBUG, "Output monitor: queue empty")

        await asyncio.sleep(2)

    debug_log(DEBUG_INFO, "Output monitor stopped")

def main():
    debug_log(DEBUG_INFO, "main() called")
    
    debug_log(DEBUG_DEBUG, "Validating configuration", 
             token_present=bool(TELEGRAM_BOT_TOKEN),
             authorized_user_id=AUTHORIZED_USER_ID,
             cline_command=CLINE_COMMAND)
    
    if not TELEGRAM_BOT_TOKEN:
        debug_log(DEBUG_ERROR, "TELEGRAM_BOT_TOKEN not set")
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable is required")
        return
    
    if AUTHORIZED_USER_ID == 0:
        debug_log(DEBUG_WARN, "AUTHORIZED_USER_ID not set or invalid")

    bot = ClineTelegramBot()
    debug_log(DEBUG_DEBUG, "Bot instance created")

    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        debug_log(DEBUG_DEBUG, "Telegram application built")
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to build Telegram application", 
                 error_type=type(e).__name__, error=str(e))
        return

    bot.application = application
    debug_log(DEBUG_DEBUG, "Application reference set in bot")

    application.add_handler(CommandHandler("start", bot.handle_message))
    application.add_handler(CommandHandler("stop", bot.handle_message))
    application.add_handler(CommandHandler("status", bot.handle_message))
    application.add_handler(CommandHandler("plan", bot.handle_message))
    application.add_handler(CommandHandler("act", bot.handle_message))
    application.add_handler(CommandHandler("cancel", bot.handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    debug_log(DEBUG_DEBUG, "Message handlers added")

    try:
        loop = asyncio.get_event_loop()
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to create output monitor task", 
                 error_type=type(e).__name__, error=str(e))

    debug_log(DEBUG_INFO, "Bot starting with long-running task support")
    print("Bot started with long-running task support")
    
    async def send_startup_notification():
        try:
            await application.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text="🤖 **Cline Remote Chatter Bot Started**\n\n"
                     "• PTY session management ready\n"
                     "• Background output monitoring active\n"
                     "• Interactive command support enabled\n\n"
                     "Use /start to begin a Cline session\n"
                     "Use /status to check bot status\n"
                     "Use /stop to end session"
            )
            debug_log(DEBUG_INFO, "Startup notification sent")
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send startup notification", 
                     error_type=type(e).__name__, error=str(e))

    async def send_shutdown_notification():
        try:
            await application.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text="🛑 **Cline Remote Chatter Bot Stopping**\n\n"
                     "The bot is shutting down. All active sessions will be terminated."
            )
            debug_log(DEBUG_INFO, "Shutdown notification sent")
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send shutdown notification", 
                     error_type=type(e).__name__, error=str(e))

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(send_startup_notification())
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to schedule startup notification", 
                 error_type=type(e).__name__, error=str(e))

    try:
        def signal_handler(signum, frame):
            debug_log(DEBUG_INFO, f"Received signal {signum}, initiating shutdown")
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    loop.create_task(send_shutdown_notification())
            except Exception as e:
                debug_log(DEBUG_ERROR, "Failed to schedule shutdown notification", 
                         error_type=type(e).__name__, error=str(e))
            
            if bot.session_active:
                debug_log(DEBUG_INFO, "Stopping active session due to shutdown")
                bot.stop_pty_session()
            
            debug_log(DEBUG_INFO, "Bot shutting down")
            import sys
            sys.exit(0)

        import signal
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        debug_log(DEBUG_DEBUG, "Signal handlers registered")

        application.run_polling()
        debug_log(DEBUG_INFO, "Bot polling started")
    except Exception as e:
        debug_log(DEBUG_ERROR, "Bot polling failed", 
                 error_type=type(e).__name__, error=str(e), exc_info=True)
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                async def send_error_notification():
                    try:
                        await application.bot.send_message(
                            chat_id=AUTHORIZED_USER_ID,
                            text=f"❌ **Cline Bot Error**\n\nBot crashed with error:\n```\n{str(e)}\n```"
                        )
                    except:
                        pass
                loop.create_task(send_error_notification())
        except:
            pass

if __name__ == "__main__":
    debug_log(DEBUG_INFO, "Script execution started")
    main()
    debug_log(DEBUG_INFO, "Script execution ended")