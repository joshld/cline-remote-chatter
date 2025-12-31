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

def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text"""
    ansi_pattern = r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])'
    return re.sub(ansi_pattern, '', text)

# Load environment variables
load_dotenv()

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
CLINE_COMMAND = ["cline"]  # Removed --no-interactive to keep process alive

def debug_log(level, message, **kwargs):
    """Centralized debug logging function"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    prefix = f"[{timestamp}] [{level}]"
    
    # Add context if provided
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

        # Session state
        self.session_active = False
        
        # Application reference for notifications
        self.application = None
        
        debug_log(DEBUG_DEBUG, "Bot initialized with default state", 
                 master_fd=self.master_fd, slave_fd=self.slave_fd, 
                 is_running=self.is_running, session_active=self.session_active)

    def start_pty_session(self, application=None):
        """Start PTY session with background output reader"""
        debug_log(DEBUG_INFO, "start_pty_session called")
        
        try:
            debug_log(DEBUG_DEBUG, "Opening PTY...")
            self.master_fd, self.slave_fd = pty.openpty()
            debug_log(DEBUG_DEBUG, "PTY opened successfully", 
                     master_fd=self.master_fd, slave_fd=self.slave_fd)

            debug_log(DEBUG_DEBUG, "Starting subprocess", 
                     command=CLINE_COMMAND, slave_fd=self.slave_fd)
            
            # Start Cline with proper environment for interactive session
            # Remove --no-interactive flag to keep Cline running
            env = dict(os.environ, TERM='xterm-256color', COLUMNS='80', LINES='24')
            
            # Use cline without --no-interactive to keep it alive
            cline_cmd = ["cline"]
            
            self.process = subprocess.Popen(
                cline_cmd,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid,
                env=env
            )
            
            debug_log(DEBUG_DEBUG, "Subprocess started", 
                     pid=self.process.pid, returncode=self.process.poll())

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
            
            # Wait a moment for Cline to initialize
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
            return False

    def stop_pty_session(self, application=None):
        """Stop PTY session and cleanup"""
        debug_log(DEBUG_INFO, "stop_pty_session called")
        
        self.stop_reading = True
        self.session_active = False
        debug_log(DEBUG_DEBUG, "State updated", 
                 stop_reading=self.stop_reading, session_active=self.session_active)

        if self.process:
            debug_log(DEBUG_DEBUG, "Stopping process", 
                     pid=self.process.pid, returncode=self.process.poll())
            try:
                debug_log(DEBUG_DEBUG, "Sending SIGTERM")
                os.killpg(os.getpgid(self.process.pid), 15)
                time.sleep(1)
                if self.process.poll() is None:
                    debug_log(DEBUG_WARN, "Process still running, sending SIGKILL")
                    os.killpg(os.getpgid(self.process.pid), 9)
                else:
                    debug_log(DEBUG_DEBUG, "Process terminated with SIGTERM")
            except Exception as e:
                debug_log(DEBUG_ERROR, "Error stopping process", 
                         error_type=type(e).__name__, error=str(e))
                pass

        if self.master_fd:
            debug_log(DEBUG_DEBUG, "Closing master_fd", fd=self.master_fd)
            try:
                os.close(self.master_fd)
            except Exception as e:
                debug_log(DEBUG_ERROR, "Error closing master_fd", error=str(e))

        if self.slave_fd:
            debug_log(DEBUG_DEBUG, "Closing slave_fd", fd=self.slave_fd)
            try:
                os.close(self.slave_fd)
            except Exception as e:
                debug_log(DEBUG_ERROR, "Error closing slave_fd", error=str(e))

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
                        # EOF reached
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
        # Strip ANSI codes for cleaner processing
        clean_output = strip_ansi_codes(output)
        
        # Filter out Cline CLI UI prompts and repetitive UI elements
        # These are not actual responses, just interface elements
        
        # Check for common UI patterns even in partial chunks
        ui_indicators = [
            '╭', '╰', '│', '┃', '╮', '╯',  # Box drawing characters
            'cline cli preview',             # Header
            '/plan or /act',                 # Mode switch
            'alt+enter',                     # Help text
            'openrouter/xiaomi',             # Model info
            '~/cline-workspace',             # Path
            'enter submit',                  # Help text
            'new line',                      # Help text
            'open editor',                   # Help text
        ]
        
        # Check if output is mostly UI elements
        ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)
        
        # Also check for empty or whitespace-only lines
        lines = clean_output.split('\n')
        empty_lines = sum(1 for line in lines if not line.strip())
        
        # Check if this is the initial welcome screen
        is_welcome_screen = 'cline cli preview' in clean_output and 'openrouter/xiaomi' in clean_output
        
        # If it's mostly UI or mostly empty, filter it out
        # BUT preserve the welcome screen
        is_ui_heavy = ui_score >= 2 and not is_welcome_screen
        is_ui_heavy = is_ui_heavy or (len(lines) > 0 and empty_lines / len(lines) > 0.5)
        
        # Also filter single characters that are likely box drawing
        is_box_char = clean_output.strip() in ['╭', '╰', '│', '┃', '╮', '╯']
        
        # Filter lines that are just spaces and box characters
        is_box_line = bool(re.match(r'^[\s│┃╭╰╮╯]+$', clean_output.strip()))
        
        # Filter API metadata and completion messages
        api_patterns = [
            r'## API request completed',
            r'↑.*↓.*\$',
            r'Tokens:.*Prompt:.*Completion:',
            r'Cost:.*\$',
            r'Elapsed:.*s',
        ]
        is_api_metadata = any(re.search(pattern, clean_output, re.IGNORECASE) for pattern in api_patterns)
        
        # Check if this is command echo (Cline showing what you typed)
        # Only filter if it's EXACTLY the command with box characters, not if it has other text
        # Example to filter: "┃ /act" or "│ /act"
        # Example to keep: "go for it /act" (has other text)
        # Also: Don't filter if it's a mode switch command (/plan or /act) - we want to see those!
        is_command_echo = False
        if self.current_command:
            # Don't filter mode switch commands - they're important to see
            if self.current_command in ['/plan', '/act']:
                is_command_echo = False
            else:
                # Check if line is just box chars + command (echo to filter)
                # But NOT if there's other text before/after
                echo_pattern = r'^[\s│┃]*' + re.escape(self.current_command) + r'[\s│┃]*$'
                is_command_echo = bool(re.match(echo_pattern, clean_output.strip()))
        
        # Check if this contains mode switch confirmation (important to preserve)
        is_mode_switch_confirmation = False
        if self.current_command in ['/plan', '/act']:
            # Look for mode-related text that should be preserved
            mode_indicators = ['switch to plan mode', 'switch to act mode', 'plan mode', 'act mode']
            is_mode_switch_confirmation = any(indicator in clean_output.lower() for indicator in mode_indicators)
        
        # Preserve welcome screen and mode switch confirmations, filter everything else
        if not is_welcome_screen and not is_mode_switch_confirmation and (is_ui_heavy or is_box_char or is_box_line or is_api_metadata or is_command_echo):
            # Only log filtered items occasionally to reduce noise
            if clean_output.strip():
                debug_log(DEBUG_DEBUG, "Filtered out UI/metadata/echo", 
                         preview=clean_output[:30].replace('\n', '\\n'))
            return  # Don't add to queue
        
        # Only log queued output occasionally
        if clean_output.strip() and len(clean_output) > 20:
            debug_log(DEBUG_DEBUG, "Queued output", 
                     preview=clean_output[:50].replace('\n', '\\n'))

        # Check for interactive prompts (use clean output)
        prompt_patterns = [
            r'\[y/N\]', r'\[Y/n\]', r'\(y/n\)', r'\(Y/N\)',
            r'Continue\?', r'Proceed\?', r'Are you sure\?',
            r'Enter .*:\s*$', r'Password:\s*$',
            r'Press.*Enter.*to.*continue',  # "Press Enter to continue"
            r'Press.*any.*key',             # "Press any key"
            r'\[.*\]\s*$',                  # "[Press Enter]"
            r'Press.*to.*exit',             # "Press any key to exit"
            r'Press.*to.*return',           # "Press any key to return"
        ]

        prompt_detected = False
        for pattern in prompt_patterns:
            if re.search(pattern, clean_output, re.IGNORECASE):
                old_state = self.waiting_for_input
                self.waiting_for_input = True
                self.input_prompt = clean_output.strip()
                prompt_detected = True
                debug_log(DEBUG_INFO, "Interactive prompt detected", 
                         pattern=pattern, prompt=self.input_prompt[:50],
                         old_state=old_state, new_state=self.waiting_for_input)
                break

        # Also check for common continuation patterns in Cline CLI
        if not prompt_detected:
            # Check if output ends with a prompt-like pattern
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

        # Add to output queue (store clean output)
        self.output_queue.append(clean_output)

        # Keep queue size manageable
        if len(self.output_queue) > 100:
            self.output_queue.popleft()
            debug_log(DEBUG_WARN, "Queue overflow, removing oldest entry")

    def send_command(self, command):
        """Send command to Cline"""
        debug_log(DEBUG_INFO, "send_command called", command=command, is_running=self.is_running)
        
        if not self.is_running:
            debug_log(DEBUG_ERROR, "Cannot send command - PTY not running")
            return "Error: PTY session not running"

        try:
            # Reset input waiting state
            old_waiting = self.waiting_for_input
            old_prompt = self.input_prompt
            self.waiting_for_input = False
            self.input_prompt = ""
            debug_log(DEBUG_DEBUG, "Reset input state", 
                     old_waiting=old_waiting, old_prompt_preview=old_prompt[:30] if old_prompt else None,
                     new_waiting=self.waiting_for_input)

            # Cline CLI is an interactive tool that needs proper submission
            # Try different submission methods
            submission_methods = [
                f"{command}\n",           # Standard newline
                f"{command}\r",           # Carriage return
                f"{command}\r\n",         # CRLF
                f"{command}\x04",         # Ctrl+D (EOF)
            ]
            
            for i, method in enumerate(submission_methods):
                debug_log(DEBUG_DEBUG, f"Trying submission method {i+1}", 
                         method_repr=repr(method), method_num=i+1)
                
                command_bytes = method.encode()
                bytes_written = os.write(self.master_fd, command_bytes)
                debug_log(DEBUG_DEBUG, f"Method {i+1} bytes written", 
                         bytes_written=bytes_written, expected=len(command_bytes))
                
                # Give Cline time to process
                time.sleep(0.3)
                
                # Check if we got any output
                if len(self.output_queue) > 0:
                    debug_log(DEBUG_INFO, f"Success with method {i+1}", method_num=i+1)
                    break
            
            self.current_command = command
            
            # Check if process is still alive
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
            # Send just a newline to dismiss prompts like "Press Enter to continue"
            os.write(self.master_fd, b"\n")
            debug_log(DEBUG_DEBUG, "Enter key sent")
            time.sleep(0.2)  # Brief pause for Cline to process
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

        # Combine available output
        combined = ""
        chunks_used = 0
        original_queue_size = queue_size
        
        while self.output_queue and len(combined) < max_length:
            chunk = self.output_queue.popleft()
            if len(combined + chunk) > max_length:
                # Put back the chunk that would exceed limit
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

        # Special commands
        if message_text == "/start":
            debug_log(DEBUG_INFO, "Processing /start command", 
                     session_active=self.session_active)
            if not self.session_active:
                if self.start_pty_session(self.application):
                    debug_log(DEBUG_INFO, "/start: Session started successfully")
                    await update.message.reply_text("✅ Cline session started\n\n**Bot Commands:**\n• Natural language: `show me the current directory`\n• CLI commands: `git status`, `ls`\n• `/plan` - Switch Cline to plan mode\n• `/act` - Switch Cline to act mode\n• `/cancel` - Cancel current task\n• `/status` - Check status\n• `/stop` - End session")
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
                # Send Ctrl+C to Cline to cancel current task
                result = self.send_command("\x03")  # Ctrl+C
                debug_log(DEBUG_DEBUG, "Cancel signal sent", result=result)
                
                # Send confirmation without quoting
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🛑 Cancel signal sent to Cline"
                )
                
                # Wait a moment and check for output
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
                # Send /plan to Cline to switch to plan mode
                result = self.send_command("/plan")
                debug_log(DEBUG_DEBUG, "Plan mode switch sent", result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="📋 Switched Cline to **PLAN MODE**"
                )
                
                # Wait for response
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
                # Send /act to Cline to switch to act mode
                result = self.send_command("/act")
                debug_log(DEBUG_DEBUG, "Act mode switch sent", result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚡ Switched Cline to **ACT MODE**"
                )
                
                # Wait for response
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
                    # Send status even if no output
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
            
            # Send user input directly
            result = self.send_command(message_text)
            debug_log(DEBUG_DEBUG, "Interactive input sent", 
                     input=message_text, result=result)
            
            # Send confirmation without quoting
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📤 Input sent: {message_text}"
            )

            # Wait a moment for response
            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            if output:
                debug_log(DEBUG_DEBUG, "Interactive output received", 
                         output_length=len(output))
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=output
                )
                # Send completion indicator
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ Response complete"
                )
            else:
                debug_log(DEBUG_DEBUG, "No output received after interactive input")
            return

        # Regular commands
        if self.session_active:
            debug_log(DEBUG_INFO, "Processing regular command", 
                     command=message_text, session_active=self.session_active)
            
            # Send command
            result = self.send_command(message_text)
            
            # Send confirmation without quoting
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📤 {result}"
            )

            # For long-running tasks, acknowledge once
            is_long_running = any(keyword in message_text.lower() for keyword in ['run', 'build', 'install', 'download', 'clone'])
            if is_long_running:
                debug_log(DEBUG_INFO, "Long-running task detected", command=message_text)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⏳ Long-running task started. Output will be sent as it becomes available..."
                )

            # Check for immediate output
            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            
            # If no output but waiting for input, send Enter to dismiss continuation prompt
            if not output and self.is_waiting_for_input():
                debug_log(DEBUG_INFO, "No output but waiting for input, sending Enter to dismiss prompt")
                self.send_enter()
                await asyncio.sleep(0.3)
                output = self.get_pending_output()
            
            if output:
                debug_log(DEBUG_DEBUG, "Immediate output received", output_length=len(output))
                # Send output in chunks if needed
                chunks = [output[i:i+4000] for i in range(0, len(output), 4000)]
                debug_log(DEBUG_DEBUG, "Sending output in chunks", 
                         total_chunks=len(chunks), total_length=len(output))
                for i, chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=chunk
                    )
                    debug_log(DEBUG_DEBUG, "Sent chunk", chunk_num=i+1, chunk_length=len(chunk))
                
                # Send completion indicator
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ Response complete"
                )
            else:
                debug_log(DEBUG_DEBUG, "No immediate output, waiting for background reader")
                # Don't send "waiting" message - let output_monitor handle it
        else:
            debug_log(DEBUG_WARN, "Command received but session not active", 
                     message_text=message_text, session_active=self.session_active)
            await update.message.reply_text("❌ Cline session not running. Use /start first")

async def output_monitor(bot_instance, application):
    """Monitor for new output and send to user"""
    debug_log(DEBUG_INFO, "Output monitor started")
    iteration_count = 0
    last_send_time = 0
    RATE_LIMIT_SECONDS = 3  # Minimum seconds between sends
    
    while True:
        iteration_count += 1
        if iteration_count % 30 == 0:  # Log every 60 seconds
            debug_log(DEBUG_DEBUG, "Output monitor heartbeat", iterations=iteration_count)
        
        if bot_instance.session_active and bot_instance.output_queue:
            debug_log(DEBUG_DEBUG, "Output monitor found data", 
                     queue_size=len(bot_instance.output_queue))
            
            # Check rate limit
            current_time = time.time()
            if current_time - last_send_time < RATE_LIMIT_SECONDS:
                debug_log(DEBUG_DEBUG, "Rate limited, waiting", 
                         time_since_last_send=current_time - last_send_time)
                await asyncio.sleep(0.5)
                continue
            
            output = bot_instance.get_pending_output()
            if output:
                # Filter out UI elements before sending
                clean_output = strip_ansi_codes(output)
                
                # Check if this is the welcome screen (preserve it)
                is_welcome_screen = 'cline cli preview' in clean_output and 'openrouter/xiaomi' in clean_output
                
                # Check if output is mostly UI (but preserve welcome screen)
                ui_indicators = ['╭', '╰', '│', '┃', 'cline cli preview', '/plan or /act']
                ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)
                
                if ui_score >= 2 and not is_welcome_screen:
                    debug_log(DEBUG_DEBUG, "Filtered UI from monitor output", 
                             ui_score=ui_score, output_length=len(clean_output))
                    continue
                
                debug_log(DEBUG_INFO, "Sending output to user", 
                         output_length=len(clean_output))
                try:
                    # Send output to authorized user
                    await application.bot.send_message(
                        chat_id=AUTHORIZED_USER_ID,
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

        await asyncio.sleep(2)  # Check every 2 seconds

    debug_log(DEBUG_INFO, "Output monitor stopped")

def main():
    debug_log(DEBUG_INFO, "main() called")
    
    # Validate configuration
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

    # Set application reference in bot for notifications
    bot.application = application
    debug_log(DEBUG_DEBUG, "Application reference set in bot")

    # Add handlers
    application.add_handler(CommandHandler("start", bot.handle_message))
    application.add_handler(CommandHandler("stop", bot.handle_message))
    application.add_handler(CommandHandler("status", bot.handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    debug_log(DEBUG_DEBUG, "Message handlers added")

    # Start output monitoring task
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(output_monitor(bot, application))
        debug_log(DEBUG_DEBUG, "Output monitor task created")
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to create output monitor task", 
                 error_type=type(e).__name__, error=str(e))

    debug_log(DEBUG_INFO, "Bot starting with long-running task support")
    print("Bot started with long-running task support")
    
    # Send startup notification
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

    # Send shutdown notification
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

    # Schedule startup notification
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(send_startup_notification())
    except Exception as e:
        debug_log(DEBUG_ERROR, "Failed to schedule startup notification", 
                 error_type=type(e).__name__, error=str(e))

    try:
        # Add shutdown handler
        def signal_handler(signum, frame):
            debug_log(DEBUG_INFO, f"Received signal {signum}, initiating shutdown")
            try:
                # Schedule shutdown notification
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    loop.create_task(send_shutdown_notification())
            except Exception as e:
                debug_log(DEBUG_ERROR, "Failed to schedule shutdown notification", 
                         error_type=type(e).__name__, error=str(e))
            
            # Stop any active session
            if bot.session_active:
                debug_log(DEBUG_INFO, "Stopping active session due to shutdown")
                bot.stop_pty_session()
            
            # Exit
            debug_log(DEBUG_INFO, "Bot shutting down")
            import sys
            sys.exit(0)

        # Register signal handlers
        import signal
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        debug_log(DEBUG_DEBUG, "Signal handlers registered")

        application.run_polling()
        debug_log(DEBUG_INFO, "Bot polling started")
    except Exception as e:
        debug_log(DEBUG_ERROR, "Bot polling failed", 
                 error_type=type(e).__name__, error=str(e), exc_info=True)
        # Try to send error notification
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