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

# Debug level constants
DEBUG_INFO = "INFO"
DEBUG_WARN = "WARN"
DEBUG_ERROR = "ERROR"
DEBUG_DEBUG = "DEBUG"

# NEW: Debug mode control - set to False to disable debug logs in Telegram
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

def debug_log(level, message, **kwargs):
    """Centralized debug logging function - ONLY goes to console/file"""
    if not DEBUG_MODE and level == DEBUG_DEBUG:
        return  # Skip debug logs when debug mode is off
        
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    prefix = f"[{timestamp}] [{level}]"
    
    if kwargs:
        context = " | ".join([f"{k}={v}" for k, v in kwargs.items()])
        message = f"{prefix} {message} | {context}"
    else:
        message = f"{prefix} {message}"
    
    print(message)

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
        
        # Process tracking for cleanup
        self.child_pids = set()
        
        # Application reference for notifications
        self.application = None
        
        # Bot state tracking for visual feedback
        self.bot_state = "idle"
        self.last_command_time = 0
        self.last_output_time = 0
        self.is_sending_output = False
        
        # User notification tracking
        self.last_user_notification_time = 0
        self.user_notification_cooldown = 2
        
        # Timeout detection
        self.last_activity_time = time.time()
        self.timeout_threshold = 30
        self.is_hanging = False
        
        # Output deduplication
        self.last_sent_output = ""
        self.recent_outputs = deque(maxlen=5)
        
        # Help message
        self.help_message = (
            "🤖 **Cline Bot Help**\n\n"
            "**When you don't get a response:**\n"
            "1. Wait 30 seconds - Cline might be thinking\n"
            "2. If still nothing, use `/reset` to restart\n"
            "3. Or use `/stop` then `/start` to restart\n\n"
            "**Better Commands:**\n"
            "Instead of: \"Ok then how does the uncommitted changes look\"\n"
            "Use: `git status` or just `status`\n\n"
            "**Quick Fix:**\n"
            "/reset - Restart Cline cleanly\n"
            "/status - Check if bot is working\n"
            "/cancel - Stop current task"
        )
        
        debug_log(DEBUG_DEBUG, "Bot initialized", 
                 master_fd=self.master_fd, slave_fd=self.slave_fd, 
                 is_running=self.is_running, session_active=self.session_active)

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
                    process = psutil.Process(child_pid)
                    process.terminate()
                except psutil.NoSuchProcess:
                    continue
            
            time.sleep(0.5)
            
            for child_pid in children:
                try:
                    process = psutil.Process(child_pid)
                    if process.is_running():
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
            
            time.sleep(0.2)
            debug_log(DEBUG_DEBUG, "Process tree killed", parent_pid=pid)
            
        except Exception as e:
            debug_log(DEBUG_ERROR, "Error killing process tree", 
                     pid=pid, error_type=type(e).__name__, error=str(e))

    def _ensure_session_clean(self):
        """Ensure no existing Cline processes are running"""
        debug_log(DEBUG_INFO, "Checking for existing Cline processes")
        
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
            
            time.sleep(1)
            debug_log(DEBUG_INFO, "Cleaned up existing processes")
        else:
            debug_log(DEBUG_DEBUG, "No existing Cline processes found")

    def start_pty_session(self, application=None):
        """Start PTY session with proper process management"""
        debug_log(DEBUG_INFO, "start_pty_session called")
        
        # NEW: Enhanced startup debugging
        debug_log(DEBUG_DEBUG, "Bot startup verification", 
                 debug_mode=DEBUG_MODE,
                 token_present=bool(TELEGRAM_BOT_TOKEN),
                 authorized_user=AUTHORIZED_USER_ID)
        
        if self.session_active:
            debug_log(DEBUG_WARN, "Session already active, refusing to start new one")
            return False
        
        self._ensure_session_clean()
        
        try:
            debug_log(DEBUG_DEBUG, "Opening PTY...")
            self.master_fd, self.slave_fd = pty.openpty()
            debug_log(DEBUG_DEBUG, "PTY opened successfully", 
                     master_fd=self.master_fd, slave_fd=self.slave_fd)

            debug_log(DEBUG_DEBUG, "Starting subprocess", 
                     command=CLINE_COMMAND, slave_fd=self.slave_fd)
            
            env = dict(os.environ, TERM='xterm-256color', COLUMNS='80', LINES='24')
            
            self.process = subprocess.Popen(
                CLINE_COMMAND,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid,
                env=env
            )
            
            debug_log(DEBUG_DEBUG, "Subprocess started", 
                     pid=self.process.pid, returncode=self.process.poll())

            self.child_pids = {self.process.pid}
            
            time.sleep(0.5)
            if self.process.poll() is not None:
                raise RuntimeError("Cline process died immediately")

            self.is_running = True
            self.session_active = True
            debug_log(DEBUG_DEBUG, "State updated", 
                     is_running=self.is_running, session_active=self.session_active)

            self.stop_reading = False
            self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
            self.output_thread.start()
            debug_log(DEBUG_DEBUG, "Output reader thread started", 
                     thread_name=self.output_thread.name, daemon=self.output_thread.daemon)

            debug_log(DEBUG_INFO, "PTY session started successfully")
            
            time.sleep(1)
            
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
                                 "/cancel - Cancel current task\n\n"
                                 "**Permission Prompts:**\n"
                                 "When Cline asks for permission to use tools, you can respond with:\n"
                                 "• `y` or `1` - Yes (default)\n"
                                 "• `a` or `2` - Yes, and don't ask again\n"
                                 "• `n` or `3` - No, with feedback"
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

        if self.process:
            debug_log(DEBUG_DEBUG, "Stopping process", 
                     pid=self.process.pid, returncode=self.process.poll())
            
            self._kill_process_tree(self.process.pid)
            self._ensure_session_clean()

        if self.output_thread and self.output_thread.is_alive():
            debug_log(DEBUG_DEBUG, "Waiting for output thread to finish")
            self.output_thread.join(timeout=2.0)
            if self.output_thread.is_alive():
                debug_log(DEBUG_WARN, "Output thread did not finish cleanly")

        self._cleanup_file_descriptors()

        self.process = None
        self.child_pids.clear()
        self.is_running = False
        debug_log(DEBUG_DEBUG, "Final state", is_running=self.is_running)
        debug_log(DEBUG_INFO, "PTY session stopped")
        
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
        
        self.stop_reading = True
        
        if self.process:
            self._kill_process_tree(self.process.pid)
            self.process = None
        
        self._ensure_session_clean()
        self._cleanup_file_descriptors()
        
        self.is_running = False
        self.session_active = False
        self.child_pids.clear()
        
        self.output_queue.clear()
        self.command_queue.clear()
        
        debug_log(DEBUG_DEBUG, "Cleanup complete")

    def _output_reader(self):
        """Background thread to continuously read PTY output"""
        debug_log(DEBUG_INFO, "Output reader thread started")
        
        read_count = 0
        error_count = 0
        consecutive_timeouts = 0
        
        # NEW: Enhanced process monitoring
        last_process_check = time.time()
        process_health_checks = 0
        
        while not self.stop_reading and self.is_running:
            try:
                # NEW: Periodic process health check
                current_time = time.time()
                if current_time - last_process_check > 2.0:  # Every 2 seconds
                    if self.process:
                        returncode = self.process.poll()
                        process_health_checks += 1
                        if returncode is not None:
                            debug_log(DEBUG_ERROR, "Process died during output reading", 
                                     returncode=returncode, checks=process_health_checks)
                            break
                        else:
                            debug_log(DEBUG_DEBUG, "Process health check OK", 
                                     checks=process_health_checks, pid=self.process.pid)
                    last_process_check = current_time
                
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                
                if ready:
                    consecutive_timeouts = 0
                    data = os.read(self.master_fd, 4096)
                    if data:
                        output = data.decode('utf-8', errors='replace')
                        read_count += 1
                        debug_log(DEBUG_DEBUG, "Data read from PTY", 
                                 bytes=len(data), preview=output[:50].replace('\n', '\\n'))
                        self._process_output(output)
                    else:
                        debug_log(DEBUG_WARN, "EOF received from PTY - process likely died")
                        break
                else:
                    consecutive_timeouts += 1
                    if consecutive_timeouts % 50 == 0:  # Every 5 seconds of timeouts
                        debug_log(DEBUG_DEBUG, "Output reader heartbeat", 
                                 consecutive_timeouts=consecutive_timeouts,
                                 is_running=self.is_running,
                                 stop_reading=self.stop_reading)
                    time.sleep(0.05)
            except Exception as e:
                error_count += 1
                debug_log(DEBUG_ERROR, f"Read attempt {error_count} failed", 
                         error_type=type(e).__name__, error=str(e),
                         read_count=read_count, consecutive_timeouts=consecutive_timeouts)
                
                if error_count > 10:
                    debug_log(DEBUG_ERROR, "Too many errors, stopping output reader", 
                             total_reads=read_count, total_errors=error_count)
                    break
                time.sleep(0.1)

        debug_log(DEBUG_INFO, "Output reader thread stopped", 
                 total_reads=read_count, total_errors=error_count,
                 final_consecutive_timeouts=consecutive_timeouts,
                 process_health_checks=process_health_checks)

    def _process_output(self, output):
        """Process incoming output from Cline"""
        clean_output = strip_ansi_codes(output)
        
        # NEW: Enhanced logging - capture EVERYTHING from Cline
        if clean_output.strip():
            debug_log(DEBUG_DEBUG, "Raw output from Cline", 
                     raw_length=len(output),
                     clean_length=len(clean_output),
                     preview=clean_output[:100].replace('\n', '\\n'))
        
        # Enhanced filtering with multiple checks
        
        # 1. Check for welcome screen (should NOT be filtered)
        is_welcome_screen = 'cline cli preview' in clean_output and 'openrouter/xiaomi' in clean_output
        
        # 2. Check for mode switch confirmations (should NOT be filtered)
        is_mode_switch_confirmation = False
        if self.current_command in ['/plan', '/act']:
            mode_indicators = ['switch to plan mode', 'switch to act mode', 'plan mode', 'act mode']
            is_mode_switch_confirmation = any(indicator in clean_output.lower() for indicator in mode_indicators)
        
        # 3. Check for Cline permission prompts (should NOT be filtered)
        cline_permission_patterns = [
            r'Let Cline use this tool\?',
            r'Allow Cline to use.*tool\?',
            r'\[act mode\].*Let Cline use',
            r'\[plan mode\].*Let Cline use',
        ]
        is_permission_prompt = any(re.search(pattern, clean_output, re.IGNORECASE) for pattern in cline_permission_patterns)
        
        # 4. Check for interactive prompts (should NOT be filtered)
        prompt_patterns = [
            r'\[y/N\]', r'\[Y/n\]', r'\(y/n\)', r'\(Y/N\)',
            r'Continue\?', r'Proceed\?', r'Are you sure\?',
            r'Enter .*:\s*$', r'Password:\s*$',
            r'Press.*Enter.*to.*continue',
            r'Press.*any.*key',
            r'\[.*\]\s*$',
            r'Press.*to.*exit',
            r'Press.*to.*return',
        ]
        is_interactive_prompt = any(re.search(pattern, clean_output, re.IGNORECASE) for pattern in prompt_patterns)
        
        # 5. Only filter if it's purely UI elements with no actual content
        lines = clean_output.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]

        # Check if this is just box characters with no real content
        is_only_box_chars = len(non_empty_lines) == 0
        if non_empty_lines:
            has_real_content = any(
                re.search(r'[a-zA-Z0-9]', line)
                for line in non_empty_lines
            )
            if not has_real_content:
                is_only_box_chars = all(
                    re.match(r'^[\s│┃╭╰╮╯─]*$', line.strip())
                    for line in non_empty_lines
                )
            else:
                is_only_box_chars = False
        
        # 6. Check for repetitive single-line UI elements
        is_repetitive_ui = False
        if len(non_empty_lines) == 1:
            line = non_empty_lines[0].strip()
            if line.startswith('┃') or line.startswith('│'):
                text_parts = re.findall(r'[a-zA-Z0-9]+', line)
                if len(text_parts) == 0:
                    is_repetitive_ui = True
        
        # 7. Check for excessive repetition within the output itself
        if len(non_empty_lines) > 1:
            unique_lines = set(non_empty_lines)
            if len(unique_lines) <= 2 and len(non_empty_lines) >= 3:
                repetition_ratio = len(non_empty_lines) / max(len(unique_lines), 1)
                if repetition_ratio >= 3:
                    is_repetitive_ui = True
        
        # 8. Check if this is just a single box character line with no real content
        is_just_box_line = False
        if len(non_empty_lines) == 1:
            line = non_empty_lines[0].strip()
            if line.startswith('┃') or line.startswith('│'):
                content = re.sub(r'^[┃│]+', '', line).strip()
                if not content or re.match(r'^\s*$', content):
                    is_just_box_line = True
        
        # 9. NEW: Check for duplicate output
        is_duplicate = False
        if clean_output.strip():
            if clean_output in self.recent_outputs:
                is_duplicate = True
            if clean_output == self.last_sent_output:
                is_duplicate = True
        
        # 10. NEW: Check if this is a repetitive UI prompt that would cause infinite loop
        is_repetitive_prompt = False
        if clean_output.strip() and len(non_empty_lines) <= 2:
            # Check if this is the same as recent output but slightly different (like UI prompts)
            for recent in self.recent_outputs:
                if recent.strip() and clean_output.strip() != recent.strip():
                    # If they're very similar (80%+ same), it's likely repetitive
                    import difflib
                    similarity = difflib.SequenceMatcher(None, recent.strip(), clean_output.strip()).ratio()
                    if similarity > 0.8:
                        is_repetitive_prompt = True
                        break
        
        # Filter decision - ONLY filter exact duplicates, pure UI box characters, and repetitive prompts
        should_filter = False
        filter_reason = ""
        
        # Always filter exact duplicates
        if is_duplicate:
            should_filter = True
            filter_reason = "duplicate"
        
        # Filter repetitive prompts to prevent infinite loops
        elif is_repetitive_prompt:
            should_filter = True
            filter_reason = "repetitive_prompt"
        
        # Only filter if it's purely UI with NO real content
        elif is_only_box_chars:
            should_filter = True
            filter_reason = "only_box_chars"
        
        # Don't filter other UI elements - let them through
        # This fixes the main issue where legitimate Cline UI was being filtered
        
        if should_filter and clean_output.strip():
            debug_log(DEBUG_DEBUG, "Filtered output", 
                     preview=clean_output[:50].replace('\n', '\\n'),
                     reason=filter_reason)
            return

        if clean_output.strip():
            debug_log(DEBUG_DEBUG, "Queued output", 
                     preview=clean_output[:50].replace('\n', '\\n'),
                     line_count=len(non_empty_lines))

        # Check for Cline permission prompts
        for pattern in cline_permission_patterns:
            if re.search(pattern, clean_output, re.IGNORECASE):
                debug_log(DEBUG_INFO, "Detected Cline permission prompt", 
                         pattern=pattern, prompt_preview=clean_output[:50])
                
                old_state = self.waiting_for_input
                self.waiting_for_input = True
                self.input_prompt = clean_output.strip()
                
                debug_log(DEBUG_INFO, "Cline permission prompt detected - waiting for user response", 
                         old_state=old_state, new_state=self.waiting_for_input)
                
                self.output_queue.append(clean_output)
                return

        # Check for interactive prompts
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

        try:
            # Reset input state BEFORE sending command
            old_waiting = self.waiting_for_input
            old_prompt = self.input_prompt
            self.waiting_for_input = False
            self.input_prompt = ""
            debug_log(DEBUG_DEBUG, "Reset input state", 
                     old_waiting=old_waiting, old_prompt_preview=old_prompt[:30] if old_prompt else None,
                     new_waiting=self.waiting_for_input)

            # Clear any existing output before sending new command
            pre_clear_size = len(self.output_queue)
            if pre_clear_size > 0:
                debug_log(DEBUG_DEBUG, "Clearing pre-existing output", queue_size=pre_clear_size)
                self.output_queue.clear()
            
            # Force state recovery
            if old_waiting:
                debug_log(DEBUG_INFO, "Force-recovering from waiting state", old_prompt=old_prompt[:50] if old_prompt else None)
                try:
                    os.write(self.master_fd, b"\n")
                    time.sleep(0.1)
                    self.output_queue.clear()
                except:
                    pass

            # Verify process health before sending
            if self.process:
                process_status = self.process.poll()
                debug_log(DEBUG_DEBUG, "Process health check before send", 
                         pid=self.process.pid, returncode=process_status, 
                         alive=process_status is None)
                
                if process_status is not None:
                    debug_log(DEBUG_ERROR, "Process died before sending command", 
                             returncode=process_status)
                    return "Error: Process died"

            submission_methods = [
                f"{command}\n",
                f"{command}\r",
                f"{command}\r\n",
                f"{command}\x04",
            ]
            
            success_method = None
            for i, method in enumerate(submission_methods):
                debug_log(DEBUG_DEBUG, f"Trying submission method {i+1}", 
                         method_repr=repr(method), method_num=i+1)
                
                command_bytes = method.encode()
                try:
                    bytes_written = os.write(self.master_fd, command_bytes)
                    debug_log(DEBUG_DEBUG, f"Method {i+1} bytes written", 
                             bytes_written=bytes_written, expected=len(command_bytes),
                             success=bytes_written == len(command_bytes))
                    
                    time.sleep(0.3)
                    
                    if self.process:
                        post_write_status = self.process.poll()
                        debug_log(DEBUG_DEBUG, f"Method {i+1} post-write process check", 
                                 returncode=post_write_status, alive=post_write_status is None)
                    
                    queue_after = len(self.output_queue)
                    debug_log(DEBUG_DEBUG, f"Method {i+1} queue state", 
                             queue_size=queue_after, had_output=queue_after > 0)
                    
                    if queue_after > 0:
                        debug_log(DEBUG_INFO, f"Success with method {i+1}", method_num=i+1)
                        success_method = i+1
                        break
                except Exception as write_error:
                    debug_log(DEBUG_ERROR, f"Method {i+1} write failed", 
                             error_type=type(write_error).__name__, error=str(write_error))
                    continue
            
            self.current_command = command
            
            if self.process:
                returncode = self.process.poll()
                debug_log(DEBUG_DEBUG, "Subprocess status after send", 
                         returncode=returncode, 
                         alive=returncode is None,
                         success_method=success_method)
            
            if success_method:
                debug_log(DEBUG_INFO, "Command sent successfully", command=command, method=success_method)
            else:
                debug_log(DEBUG_WARN, "Command sent but no immediate output", command=command)
            
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

    def update_bot_state(self, new_state):
        """Update bot state and log the change"""
        old_state = self.bot_state
        self.bot_state = new_state
        if old_state != new_state:
            debug_log(DEBUG_INFO, "Bot state changed", 
                     old_state=old_state, new_state=new_state)

    def check_and_recover_state(self):
        """Check for stuck states and attempt recovery"""
        debug_log(DEBUG_DEBUG, "State recovery check")
        
        if self.waiting_for_input and not self.input_prompt:
            debug_log(DEBUG_WARN, "Stuck in waiting_for_input with no prompt - resetting")
            self.waiting_for_input = False
            self.input_prompt = ""
            return True
        
        if len(self.output_queue) > 10:
            ui_only = True
            for item in list(self.output_queue):
                clean_item = strip_ansi_codes(item).strip()
                if clean_item and not re.match(r'^[\s│┃╭╰╮╯─]*$', clean_item):
                    ui_only = False
                    break
            
            if ui_only:
                debug_log(DEBUG_WARN, "Output queue filled with UI elements - clearing")
                self.output_queue.clear()
                return True
        
        if self.session_active and self.process:
            returncode = self.process.poll()
            if returncode is not None:
                debug_log(DEBUG_ERROR, "Process died but session still active - stopping session")
                self.stop_pty_session()
                return True
        
        return False

    def can_send_message_to_user(self):
        """Check if bot can send messages to user - NO RATE LIMITING"""
        return True, 0

    async def send_user_notification(self, message, force=False):
        """Send notification to user - NO rate limiting for bot responses"""
        if not self.application:
            debug_log(DEBUG_WARN, "Cannot send notification - no application reference")
            return
        
        try:
            await self.application.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=message
            )
            debug_log(DEBUG_INFO, "User notification sent", message_preview=message[:50])
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send user notification", 
                     error_type=type(e).__name__, error=str(e))

    async def send_typing_indicator(self):
        """Send Telegram typing indicator"""
        if not self.application:
            return
        
        try:
            await self.application.bot.send_chat_action(
                chat_id=AUTHORIZED_USER_ID,
                action="typing"
            )
            debug_log(DEBUG_DEBUG, "Typing indicator sent")
        except Exception as e:
            debug_log(DEBUG_DEBUG, "Failed to send typing indicator", 
                     error_type=type(e).__name__, error=str(e))

    # Rate limiting methods removed - no longer needed

    async def handle_processing_command(self, update, command):
        """Handle command that's being processed"""
        debug_log(DEBUG_INFO, "Processing command with visual feedback", command=command)
        
        self.update_bot_state("processing")
        await self.send_typing_indicator()
        
        long_running_keywords = ['run', 'build', 'install', 'download', 'clone', 'test', 'compile']
        is_long_running = any(keyword in command.lower() for keyword in long_running_keywords)
        
        if is_long_running:
            await update.message.reply_text(
                f"⚙️ **Processing Command**\n\n"
                f"Command: `{command}`\n"
                f"This may take a while. I'll send output as it becomes available..."
            )
        else:
            await self.send_typing_indicator()

    async def handle_busy_state(self, update):
        """Handle when bot is busy with multiple queued operations"""
        debug_log(DEBUG_INFO, "Bot busy state - user sent message")
        
        self.update_bot_state("busy")
        
        queue_size = len(self.command_queue)
        if queue_size > 0:
            await update.message.reply_text(
                f"🔄 **Bot Busy**\n\n"
                f"Your command is queued.\n"
                f"Position in queue: {queue_size + 1}\n"
                f"Please wait a moment..."
            )
        else:
            await update.message.reply_text(
                f"🔄 **Bot Processing**\n\n"
                f"Working on your previous command.\n"
                f"Please wait a moment..."
            )
        
        await self.send_typing_indicator()

    async def handle_permission_response(self, response):
        """Handle user response to Cline permission prompts"""
        debug_log(DEBUG_INFO, "Handling permission response", response=response)
        
        response_lower = response.lower().strip()
        
        try:
            if response_lower in ['y', 'yes', '1']:
                os.write(self.master_fd, b'\n')
                debug_log(DEBUG_INFO, "Sent 'Yes' to permission prompt")
            elif response_lower in ['a', 'always', '2']:
                os.write(self.master_fd, b'\x1b[B')
                time.sleep(0.05)
                os.write(self.master_fd, b'\n')
                debug_log(DEBUG_INFO, "Sent 'Yes, and don't ask again' to permission prompt")
            elif response_lower in ['n', 'no', '3']:
                os.write(self.master_fd, b'\x1b[B')
                time.sleep(0.05)
                os.write(self.master_fd, b'\x1b[B')
                time.sleep(0.05)
                os.write(self.master_fd, b'\n')
                debug_log(DEBUG_INFO, "Sent 'No, with feedback' to permission prompt")
            else:
                os.write(self.master_fd, b'\n')
                debug_log(DEBUG_INFO, "Sent default response to permission prompt")
            
            time.sleep(0.2)
            return True
        except Exception as e:
            debug_log(DEBUG_ERROR, "Failed to send permission response", 
                     error_type=type(e).__name__, error=str(e))
            return False

    async def _process_queued_command(self, command, update, context):
        """Process a queued command without modifying the update object"""
        debug_log(DEBUG_INFO, "Processing queued command directly", command=command)
        
        debug_log(DEBUG_DEBUG, "Queued command bypassing rate limit", command=command)
        
        await self.handle_processing_command(update, command)
        
        current_time = time.time()
        self.last_command_time = current_time
        
        result = self.send_command(command)
        debug_log(DEBUG_DEBUG, "Queued command send result", result=result)
        
        self.update_bot_state("processing")
        
        output = None
        max_retries = 3
        retry_delay = 0.4
        
        for retry in range(max_retries):
            queue_size = len(self.output_queue)
            is_waiting = self.is_waiting_for_input()
            
            current_output = self.get_pending_output()
            
            if current_output:
                if not output:
                    output = current_output
                else:
                    output += current_output
                
                if not is_waiting and retry < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    break
            else:
                if is_waiting and retry == 0:
                    self.send_enter()
                    await asyncio.sleep(0.2)
                    continue
                
                is_long_running = any(keyword in command.lower() for keyword in ['run', 'build', 'install', 'download', 'clone', 'test'])
                if is_long_running and retry < max_retries - 1:
                    await asyncio.sleep(0.6)
                    continue
                
                if retry < max_retries - 1:
                    await asyncio.sleep(retry_delay)
        
        if not output and len(self.output_queue) > 0:
            output = self.get_pending_output()
        
        if output:
            debug_log(DEBUG_DEBUG, "Queued command output collected", output_length=len(output))
            chunks = [output[i:i+4000] for i in range(0, len(output), 4000)]
            for i, chunk in enumerate(chunks):
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk
                )
            
            self.last_output_time = time.time()
            self.update_bot_state("idle")
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ Queued command complete"
            )
        else:
            debug_log(DEBUG_WARN, "No output from queued command")
            self.update_bot_state("idle")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="ℹ️ Queued command sent. No output received."
            )
            self.last_output_time = time.time()

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming Telegram messages"""
        debug_log(DEBUG_INFO, "handle_message called")
        
        user_id = update.effective_user.id
        message_text = update.message.text.strip() if update.message.text else ""
        
        # NEW: Enhanced logging - capture EVERYTHING user sends
        debug_log(DEBUG_DEBUG, "User message received", 
                 user_id=user_id, 
                 authorized_id=AUTHORIZED_USER_ID,
                 full_message=message_text,
                 message_length=len(message_text))
        
        # NEW: Complete state snapshot
        debug_log(DEBUG_DEBUG, "Complete bot state snapshot", 
                 session_active=self.session_active,
                 is_running=self.is_running,
                 waiting_for_input=self.waiting_for_input,
                 bot_state=self.bot_state,
                 queue_size=len(self.output_queue),
                 command_queue_size=len(self.command_queue),
                 current_command=self.current_command,
                 process_alive=self.process.poll() if self.process else "no_process")

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

        if message_text == "/reset":
            debug_log(DEBUG_INFO, "Processing /reset command")
            if self.session_active:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🔄 **Reset Cline Session**\n\n"
                         "This will:\n"
                         "• Stop the current Cline session\n"
                         "• Kill all related processes\n"
                         "• Start a fresh session\n\n"
                         "Reply with `/reset confirm` to proceed."
                )
                return
            else:
                await update.message.reply_text("❌ No active session to reset. Use /start first")
            return

        if message_text == "/reset confirm":
            debug_log(DEBUG_INFO, "Processing /reset confirm command")
            if self.session_active:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🔄 **Resetting Cline Session...**\n\n"
                         "Stopping current session and starting fresh..."
                )
                
                self.stop_pty_session(self.application)
                await asyncio.sleep(1)
                
                if self.start_pty_session(self.application):
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="✅ **Reset Complete**\n\n"
                             "Fresh Cline session started.\n"
                             "You can now send commands."
                    )
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ **Reset Failed**\n\n"
                             "Could not start new session. Check logs for details."
                    )
            else:
                await update.message.reply_text("❌ No active session to reset")
            return

        if message_text == "/help":
            debug_log(DEBUG_INFO, "Processing /help command")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=self.help_message
            )
            return

        if message_text == "/timeout":
            debug_log(DEBUG_INFO, "Processing /timeout command")
            if self.session_active:
                time_since_activity = time.time() - self.last_activity_time
                is_hanging = time_since_activity > self.timeout_threshold
                
                status = "🟢 Active" if self.session_active else "🔴 Stopped"
                hanging_status = "⚠️ **HANGING**" if is_hanging else "✅ Normal"
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⏱️ **Timeout Status**\n\n"
                         f"Session: {status}\n"
                         f"Status: {hanging_status}\n"
                         f"Last activity: {time_since_activity:.1f} seconds ago\n"
                         f"Timeout threshold: {self.timeout_threshold} seconds\n\n"
                         f"If hanging, use /reset to restart."
                )
            else:
                await update.message.reply_text("❌ No active session")
            return

        # Handle interactive input (including permission prompts)
        if self.is_waiting_for_input():
            debug_log(DEBUG_INFO, "Processing interactive input", 
                     waiting_for_input=self.waiting_for_input,
                     prompt_preview=self.input_prompt[:50] if self.input_prompt else None)
            
            is_permission_prompt = any(pattern in self.input_prompt.lower() for pattern in ['let cline use', 'allow cline'])
            
            if is_permission_prompt:
                debug_log(DEBUG_INFO, "Handling permission prompt response", 
                         prompt=self.input_prompt[:50])
                
                success = await self.handle_permission_response(message_text)
                
                if success:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"✅ Permission response sent: `{message_text}`"
                    )
                    
                    self.waiting_for_input = False
                    self.input_prompt = ""
                    
                    await asyncio.sleep(0.5)
                    output = self.get_pending_output()
                    
                    if output:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=output
                        )
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ Failed to send permission response"
                    )
            else:
                result = self.send_command(message_text)
                debug_log(DEBUG_DEBUG, "Interactive input sent", 
                         input=message_text, result=result)
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"📤 Input sent: {message_text}"
                )

                output = None
                got_response = False
                
                for retry in range(5):
                    await asyncio.sleep(0.4)
                    current_output = self.get_pending_output()
                    if current_output:
                        if not output:
                            output = current_output
                        else:
                            output += current_output
                        got_response = True
                        
                        if not self.is_waiting_for_input():
                            break
                    elif got_response and retry > 1:
                        break
                
                if output:
                    debug_log(DEBUG_DEBUG, "Interactive output received", 
                             output_length=len(output))
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=output
                    )
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="✅ Response complete"
                    )
                else:
                    debug_log(DEBUG_DEBUG, "No output received after interactive input")
                    
                    if self.is_waiting_for_input():
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text="ℹ️ Input sent, but Cline appears to be waiting at a prompt.\n\n"
                                 "Try sending:\n"
                                 "- `y` or `n` for yes/no questions\n"
                                 "- A specific answer if it asked for input\n"
                                 "- `/cancel` to exit the current task\n"
                                 "- `/reset` if stuck"
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text="ℹ️ Input sent. Cline didn't generate a response.\n\n"
                                 "This can happen when:\n"
                                 "- Cline is in plan mode and the question doesn't require action\n"
                                 "- The question needs more context\n"
                                 "- Try asking a more specific command like `git status` or `git log`"
                        )
            return

        # Regular commands
        if self.session_active:
            self.check_and_recover_state()
            
            debug_log(DEBUG_INFO, "Processing regular command", 
                     command=message_text, session_active=self.session_active)
            
            debug_log(DEBUG_DEBUG, "User command processing started", 
                     bot_state=self.bot_state,
                     queue_size=len(self.output_queue))
            
            await self.handle_processing_command(update, message_text)
            
            self.last_command_time = time.time()
            
            debug_log(DEBUG_DEBUG, "State before command", 
                     waiting_for_input=self.waiting_for_input,
                     queue_size_before=len(self.output_queue),
                     current_command=self.current_command)
            
            result = self.send_command(message_text)
            
            debug_log(DEBUG_DEBUG, "Command send result", 
                     result=result,
                     queue_size_after_send=len(self.output_queue))
            
            self.update_bot_state("processing")
            
            output = None
            max_retries = 3
            retry_delay = 0.4
            
            for retry in range(max_retries):
                debug_log(DEBUG_DEBUG, f"Output collection attempt {retry + 1}/{max_retries}")
                
                queue_size = len(self.output_queue)
                is_waiting = self.is_waiting_for_input()
                
                debug_log(DEBUG_DEBUG, "State check", 
                         queue_size=queue_size, waiting_for_input=is_waiting, retry=retry)
                
                if queue_size > 0:
                    queue_preview = str(list(self.output_queue))[:100]
                    debug_log(DEBUG_DEBUG, "Queue contents", 
                             queue_preview=queue_preview,
                             queue_size=queue_size)
                
                current_output = self.get_pending_output()
                
                if current_output:
                    if not output:
                        output = current_output
                    else:
                        output += current_output
                    
                    debug_log(DEBUG_DEBUG, "Got output on this attempt", 
                             chunk_length=len(current_output), total_length=len(output))
                    
                    if not is_waiting and retry < max_retries - 1:
                        debug_log(DEBUG_DEBUG, "Not waiting for input, checking for more output")
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        break
                else:
                    debug_log(DEBUG_DEBUG, "No output on this attempt", 
                             waiting_for_input=is_waiting, queue_size=queue_size)
                    
                    if is_waiting and retry == 0:
                        debug_log(DEBUG_INFO, "Waiting for input detected, sending Enter")
                        self.send_enter()
                        await asyncio.sleep(0.2)
                        continue
                    
                    is_long_running = any(keyword in message_text.lower() for keyword in ['run', 'build', 'install', 'download', 'clone', 'test'])
                    if is_long_running and retry < max_retries - 1:
                        debug_log(DEBUG_DEBUG, "Long-running command, extending wait time")
                        await asyncio.sleep(0.6)
                        continue
                    
                    if retry < max_retries - 1:
                        await asyncio.sleep(retry_delay)
            
            if not output and len(self.output_queue) > 0:
                debug_log(DEBUG_DEBUG, "Final check: queue has data, trying one more time")
                output = self.get_pending_output()
            
            # DEBUG: Final decision before sending
            debug_log(DEBUG_INFO, "Final output decision", 
                     has_output=output is not None,
                     output_length=len(output) if output else 0,
                     queue_size=len(self.output_queue),
                     will_send=output is not None)
            
            if output:
                debug_log(DEBUG_DEBUG, "Final output collected", output_length=len(output))
                chunks = [output[i:i+4000] for i in range(0, len(output), 4000)]
                debug_log(DEBUG_DEBUG, "Sending output in chunks", 
                         total_chunks=len(chunks), total_length=len(output))
                for i, chunk in enumerate(chunks):
                    debug_log(DEBUG_INFO, f"About to send chunk {i+1}/{len(chunks)}", 
                             chunk_length=len(chunk),
                             chat_id=update.effective_chat.id)
                    try:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=chunk
                        )
                        debug_log(DEBUG_DEBUG, "Chunk sent successfully", chunk_num=i+1)
                    except Exception as e:
                        debug_log(DEBUG_ERROR, f"Failed to send chunk {i+1}", 
                                 error_type=type(e).__name__, error=str(e))
                    debug_log(DEBUG_DEBUG, "Sent chunk", chunk_num=i+1, chunk_length=len(chunk))
                
                self.last_output_time = time.time()
                self.update_bot_state("idle")
                
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="✅ Response complete"
                    )
                    debug_log(DEBUG_DEBUG, "Response complete message sent")
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to send response complete message", 
                             error_type=type(e).__name__, error=str(e))
                
                if len(self.command_queue) > 0:
                    debug_log(DEBUG_INFO, "Processing queued commands", 
                             queue_size=len(self.command_queue))
                    await asyncio.sleep(1)
                    
                    while len(self.command_queue) > 0:
                        queued_command = self.command_queue.popleft()
                        debug_log(DEBUG_INFO, "Processing queued command", 
                                 command=queued_command, remaining=len(self.command_queue))
                        
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"🔄 **Processing Queued Command**\n\n`{queued_command}`"
                        )
                        
                        await self._process_queued_command(queued_command, update, context)
                        
                        if len(self.command_queue) > 0:
                            await asyncio.sleep(1)
            else:
                debug_log(DEBUG_WARN, "No output collected after all retries")
                self.update_bot_state("idle")
                
                debug_log(DEBUG_INFO, "Sending no-output message to user", 
                         queue_size=len(self.output_queue),
                         waiting_for_input=self.is_waiting_for_input())
                
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="ℹ️ Command sent. If no response appears, Cline might be waiting for input or processing..."
                    )
                    debug_log(DEBUG_DEBUG, "No-output message sent successfully")
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Failed to send no-output message", 
                             error_type=type(e).__name__, error=str(e))
                
                self.last_output_time = time.time()
            
            if len(self.output_queue) > 0:
                debug_log(DEBUG_DEBUG, "handle_message completing, output still in queue", 
                         queue_size=len(self.output_queue))
        else:
            debug_log(DEBUG_WARN, "Command received but session not active", 
                     message_text=message_text, session_active=self.session_active)
            await update.message.reply_text("❌ Cline session not running. Use /start first")

async def output_monitor(bot_instance, application):
    """Monitor for new output and send to user"""
    debug_log(DEBUG_INFO, "Output monitor started")
    iteration_count = 0
    
    while True:
        iteration_count += 1
        if iteration_count % 30 == 0:
            debug_log(DEBUG_DEBUG, "Output monitor heartbeat", iterations=iteration_count)
        
        # DEBUG: Monitor state check
        debug_log(DEBUG_DEBUG, "Output monitor state check", 
                 session_active=bot_instance.session_active,
                 queue_size=len(bot_instance.output_queue) if bot_instance.output_queue else 0,
                 bot_state=bot_instance.bot_state)
        
        if (bot_instance.session_active and 
            bot_instance.output_queue):
            
            debug_log(DEBUG_DEBUG, "Output monitor found data", 
                     queue_size=len(bot_instance.output_queue),
                     bot_state=bot_instance.bot_state)
            
            # Check if bot is busy (no rate limiting, just busy state)
            if bot_instance.bot_state == "busy":
                debug_log(DEBUG_DEBUG, "Skipping output due to bot busy state", 
                         state=bot_instance.bot_state)
                await asyncio.sleep(0.5)
                continue
            
            output = bot_instance.get_pending_output()
            debug_log(DEBUG_DEBUG, "Output monitor got pending output", 
                     has_output=output is not None,
                     output_length=len(output) if output else 0,
                     output_preview=(output[:50] if output else None))
            
            if output:
                clean_output = strip_ansi_codes(output)
                debug_log(DEBUG_DEBUG, "Cleaned output for sending", 
                         original_length=len(output),
                         clean_length=len(clean_output),
                         clean_preview=clean_output[:100].replace('\n', '\\n'))
                
                # NEW: Enhanced filtering for output monitor
                lines = clean_output.split('\n')
                non_empty_lines = [line for line in lines if line.strip()]
                
                # DEBUG: Show line analysis
                debug_log(DEBUG_DEBUG, "Output line analysis", 
                         total_lines=len(lines),
                         non_empty_lines=len(non_empty_lines),
                         first_line_preview=non_empty_lines[0][:50] if non_empty_lines else "empty")
                
                # Check for only box characters
                is_only_box_chars = len(non_empty_lines) == 0
                if non_empty_lines:
                    has_real_content = any(re.search(r'[a-zA-Z0-9]', line) for line in non_empty_lines)
                    if not has_real_content:
                        is_only_box_chars = all(re.match(r'^[\s│┃╭╰╮╯─]*$', line.strip()) for line in non_empty_lines)
                    else:
                        is_only_box_chars = False
                
                # Check for repetitive single-line UI elements
                is_repetitive_ui = False
                if len(non_empty_lines) == 1:
                    line = non_empty_lines[0].strip()
                    if line.startswith('┃') or line.startswith('│'):
                        text_parts = re.findall(r'[a-zA-Z0-9]+', line)
                        if len(text_parts) == 0:
                            is_repetitive_ui = True
                
                # Check for just box character lines
                is_just_box_line = False
                if len(non_empty_lines) == 1:
                    line = non_empty_lines[0].strip()
                    if line.startswith('┃') or line.startswith('│'):
                        content = re.sub(r'^[┃│]+', '', line).strip()
                        if not content or re.match(r'^\s*$', content):
                            is_just_box_line = True
                
                # NEW: Check for duplicates - only filter exact duplicates
                is_duplicate = False
                if clean_output.strip():
                    if clean_output == bot_instance.last_sent_output:
                        is_duplicate = True
                        debug_log(DEBUG_DEBUG, "Duplicate detected - same as last_sent_output")
                
                # NEW: Check if this is a repetitive UI prompt that would cause infinite loop
                is_repetitive_prompt = False
                if clean_output.strip() and len(non_empty_lines) <= 2:
                    # Check if this is the same as recent output but slightly different (like UI prompts)
                    for recent in bot_instance.recent_outputs:
                        if recent.strip() and clean_output.strip() != recent.strip():
                            # If they're very similar (80%+ same), it's likely repetitive
                            import difflib
                            similarity = difflib.SequenceMatcher(None, recent.strip(), clean_output.strip()).ratio()
                            if similarity > 0.8:
                                is_repetitive_prompt = True
                                debug_log(DEBUG_DEBUG, "Repetitive prompt detected", similarity=similarity)
                                break
                
                # NEW: Very permissive filtering - only filter exact duplicates, repetitive prompts, and pure UI
                should_filter = False
                filter_reason = ""
                
                # Only filter exact duplicates
                if is_duplicate:
                    should_filter = True
                    filter_reason = "duplicate"
                # Filter repetitive prompts to prevent infinite loops
                elif is_repetitive_prompt:
                    should_filter = True
                    filter_reason = "repetitive_prompt"
                # Only filter if it's purely UI with NO real content
                elif is_only_box_chars:
                    should_filter = True
                    filter_reason = "only_box_chars"
                
                if should_filter:
                    debug_log(DEBUG_DEBUG, "Filtered output (noise)", 
                             preview=clean_output[:50].replace('\n', '\\n'),
                             reason=filter_reason)
                    
                    # Send user feedback about filtered output (only once per session)
                    if filter_reason == "duplicate" and clean_output != bot_instance.last_sent_output:
                        # This is a duplicate but different from last sent, so it's noise
                        continue
                    elif filter_reason == "only_box_chars":
                        # Pure UI noise - silently filter
                        continue
                    
                    continue
                
                # Don't send if we're in rate_limited or busy state
                if bot_instance.bot_state in ["rate_limited", "busy"]:
                    debug_log(DEBUG_DEBUG, "Skipping output due to bot state", 
                             state=bot_instance.bot_state)
                    continue
                
                # DEBUG: Before sending to Telegram
                debug_log(DEBUG_INFO, "About to send output to Telegram", 
                         output_length=len(clean_output),
                         bot_state=bot_instance.bot_state,
                         chat_id=AUTHORIZED_USER_ID,
                         preview=clean_output[:100].replace('\n', '\\n'))
                
                try:
                    debug_log(DEBUG_DEBUG, "Calling Telegram API send_message")
                    await application.bot.send_message(
                        chat_id=AUTHORIZED_USER_ID,
                        text=clean_output
                    )
                    debug_log(DEBUG_DEBUG, "Telegram API call completed successfully")
                    
                    bot_instance.last_output_time = time.time()
                    bot_instance.last_sent_output = clean_output
                    bot_instance.recent_outputs.append(clean_output)
                    debug_log(DEBUG_INFO, "Output sent successfully via monitor", 
                             sent_length=len(clean_output),
                             preview=clean_output[:100].replace('\n', '\\n'))
                except Exception as e:
                    debug_log(DEBUG_ERROR, "Error sending output via monitor", 
                             error_type=type(e).__name__, error=str(e),
                             output_preview=clean_output[:50].replace('\n', '\\n'),
                             chat_id=AUTHORIZED_USER_ID)
                    # DEBUG: Show full exception details
                    debug_log(DEBUG_ERROR, "Full exception details", 
                             exc_info=True)
            else:
                debug_log(DEBUG_DEBUG, "No output after get_pending_output")
        else:
            if not bot_instance.session_active:
                debug_log(DEBUG_DEBUG, "Output monitor: session not active")
            elif not bot_instance.output_queue:
                debug_log(DEBUG_DEBUG, "Output monitor: queue empty")
            elif bot_instance.bot_state not in ["idle", "processing"]:
                debug_log(DEBUG_DEBUG, "Output monitor: bot not in idle/processing state", 
                         state=bot_instance.bot_state)

        await asyncio.sleep(0.5)  # Check every 0.5 seconds for faster response

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
    application.add_handler(CommandHandler("reset", bot.handle_message))
    application.add_handler(CommandHandler("help", bot.handle_message))
    application.add_handler(CommandHandler("timeout", bot.handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    debug_log(DEBUG_DEBUG, "Message handlers added")

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(output_monitor(bot, application))
        debug_log(DEBUG_DEBUG, "Output monitor task created")
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
                     "**Basic Commands:**\n"
                     "/start - Begin a Cline session\n"
                     "/status - Check bot status\n"
                     "/stop - End session\n\n"
                     "**Permission Prompts:**\n"
                     "When Cline asks for permission to use tools, respond with:\n"
                     "• `y` or `1` - Yes (default)\n"
                     "• `a` or `2` - Yes, and don't ask again\n"
                     "• `n` or `3` - No, with feedback\n\n"
                     f"**Debug Mode:** {'ON' if DEBUG_MODE else 'OFF'}\n"
                     "Set DEBUG_MODE=true in .env to see debug logs in Telegram"
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