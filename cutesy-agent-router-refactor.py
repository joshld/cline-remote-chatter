"""
Multi-Agent, Multi-Chat Interface Architecture
Pragmatic design: Works WITH telegram-bot library, not against it
Supports: Any CLI agent (Cline, Codex, etc) + Any chat service (Telegram, Discord, etc)
"""

import asyncio
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, Callable
import json

import psutil
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters


# ============================================================================
# LOGGING
# ============================================================================

def debug_log(level, message, **kwargs):
    """Centralized debug logging"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    context = " | ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    suffix = f" | {context}" if context else ""
    print(f"[{timestamp}] [{level}] {message}{suffix}")


DEBUG_INFO, DEBUG_WARN, DEBUG_ERROR, DEBUG_DEBUG = "INFO", "WARN", "ERROR", "DEBUG"


# ============================================================================
# UTILITIES
# ============================================================================

def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text"""
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


# ============================================================================
# MESSAGE/EVENT TYPES (Simplified)
# ============================================================================

class MessageType(Enum):
    USER_INPUT = "user_input"
    AGENT_OUTPUT = "agent_output"
    COMMAND = "command"
    ERROR = "error"


class Message:
    """Structured message"""
    def __init__(self, type: MessageType, content: str, sender: str = "unknown", metadata: Dict = None):
        self.type = type
        self.content = content
        self.sender = sender
        self.timestamp = datetime.now().isoformat()
        self.metadata = metadata or {}


# ============================================================================
# AGENT INTERFACE
# ============================================================================

class AgentInterface(ABC):
    """Abstract interface for CLI-based agents"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.is_running_flag = False
        self.waiting_for_input = False

    @abstractmethod
    async def start(self) -> bool:
        """Start the agent session"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the agent session"""
        pass

    @abstractmethod
    def send_command(self, command: str) -> str:
        """Send command to agent (synchronous)"""
        pass

    @abstractmethod
    async def get_output(self) -> Optional[Message]:
        """Get pending output from agent"""
        pass

    def is_running(self) -> bool:
        return self.is_running_flag


class PTYAgent(AgentInterface):
    """PTY-based CLI agent (Cline, Codex, etc)"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.command = config.get("command", ["cline"])
        self.name = config.get("name", "Agent")
        
        self.master_fd = None
        self.slave_fd = None
        self.process = None
        self.output_queue = deque(maxlen=100)
        self.output_queue_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.output_thread = None
        self.stop_reading = False
        self.input_prompt = ""
        self.last_prompt_time = 0

    def _output_reader(self):
        """Background thread to read PTY output"""
        debug_log(DEBUG_INFO, f"{self.name} output reader started")
        error_count = 0

        while not self.stop_reading and self.is_running_flag:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        output = data.decode("utf-8", errors="replace")
                        self._process_output(output)
                        error_count = 0
                    else:
                        debug_log(DEBUG_WARN, f"EOF from {self.name}")
                        break
                else:
                    time.sleep(0.05)
            except Exception as e:
                error_count += 1
                if error_count > 10:
                    debug_log(DEBUG_ERROR, f"{self.name} too many read errors: {e}")
                    break
                time.sleep(0.1)

        debug_log(DEBUG_INFO, f"{self.name} output reader stopped")

    def _process_output(self, output: str):
        """Process output and detect prompts"""
        clean_output = strip_ansi_codes(output)
        agent_config = self.config

        # Welcome screen detection (configurable)
        welcome_keywords = agent_config.get("welcome_keywords", ["cline cli"])
        is_welcome_screen = any(keyword in clean_output.lower() for keyword in welcome_keywords)

        # Mode switching detection (configurable)
        mode_keywords = agent_config.get("mode_keywords", ["switch to plan", "switch to act", "plan mode", "act mode"])
        is_mode_switch = any(keyword in clean_output.lower() for keyword in mode_keywords)

        # UI element filtering
        is_box_line = bool(re.match(r"^[\s│┃╭╰╮╯]+$", clean_output.strip()))
        is_mostly_empty_ui = (clean_output.strip() in ["╭", "╰", "│", "┃", "╮", "╯"] or is_box_line) and len(clean_output.strip()) <= 3

        if not is_welcome_screen and not is_mode_switch and is_mostly_empty_ui:
            debug_log(DEBUG_DEBUG, f"Filtered UI message: {repr(clean_output)}", reason="mostly_empty_ui")
            return

        # Detect interactive prompts
        prompt_patterns = [
            r"\[y/N\]\s*$", r"\[Y/n\]\s*$", r"\(y/n\)\s*$", r"\(Y/N\)\s*$",
            r"Continue\?\s*$", r"Proceed\?\s*$", r"Are you sure\?\s*$",
            r"Enter .*:\s*$", r"Password:\s*$",
            r"Press.*Enter.*to.*continue\s*$", r"Press.*any.*key\s*$",
            r"\[.*\]\s*$", r"Press .*to exit\s*$", r"Press .* to return\s*$",
        ]

        for pattern in prompt_patterns:
            if re.search(pattern, clean_output, re.IGNORECASE):
                with self.state_lock:
                    self.waiting_for_input = True
                    self.input_prompt = clean_output.strip()
                    self.last_prompt_time = time.time()
                debug_log(DEBUG_INFO, "Interactive prompt detected", pattern=pattern)
                break

        with self.output_queue_lock:
            self.output_queue.append(clean_output)

    async def start(self) -> bool:
        """Start the agent"""
        try:
            self.master_fd, self.slave_fd = pty.openpty()
            env = dict(os.environ, TERM="xterm-256color", COLUMNS="80", LINES="24")

            self.process = subprocess.Popen(
                self.command,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid,
                env=env,
            )

            time.sleep(0.5)
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.name} process died immediately")

            self.is_running_flag = True
            self.stop_reading = False
            self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
            self.output_thread.start()

            debug_log(DEBUG_INFO, f"{self.name} session started")
            return True
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to start {self.name}: {e}")
            return False

    async def stop(self) -> None:
        """Stop the agent"""
        self.stop_reading = True
        self.is_running_flag = False
        
        if self.process:
            try:
                self.process.terminate()
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
            except:
                pass

    def send_command(self, command: str) -> str:
        """Send command to agent (SYNCHRONOUS for telegram-bot compatibility)"""
        with self.command_lock:
            if not self.is_running_flag:
                return "Error: Agent not running"

            try:
                with self.state_lock:
                    self.waiting_for_input = False
                    self.input_prompt = ""

                os.write(self.master_fd, f"{command}\r\n".encode())
                time.sleep(0.2)
                return "Command sent"
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Failed to send command: {e}")
                return f"Error: {e}"

    async def get_output(self) -> Optional[Message]:
        """Get pending output"""
        with self.output_queue_lock:
            if not self.output_queue:
                return None

            combined = ""
            while self.output_queue and len(combined) < 4000:
                chunk = self.output_queue.popleft()
                if len(combined + chunk) > 4000:
                    self.output_queue.appendleft(chunk)
                    break
                combined += chunk

            if combined.strip():
                return Message(
                    MessageType.AGENT_OUTPUT,
                    combined.strip(),
                    sender=self.name
                )
            return None


# ============================================================================
# BRIDGE - Works WITH telegram-bot library
# ============================================================================

class AgentChatBridge:
    """Bridges agent and Telegram with minimal async complexity"""

    def __init__(self, agent: AgentInterface, app: Application, user_id: str):
        self.agent = agent
        self.app = app
        self.user_id = user_id
        self.output_monitor_task = None

    async def send_message(self, user_id: str, text: str) -> None:
        """Send message to user"""
        try:
            await self.app.bot.send_message(chat_id=int(user_id), text=text)
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to send message: {e}")

    async def handle_command(self, update, context):
        """Handle /start, /stop, /status commands"""
        if update.effective_user.id != int(self.user_id):
            await update.message.reply_text("❌ Unauthorized")
            return

        cmd = update.message.text

        if cmd == "/start":
            if self.agent.is_running():
                await update.message.reply_text("ℹ️ Agent already running")
                return

            if await self.agent.start():
                await update.message.reply_text(
                    "✅ Agent started\n\n"
                    "Commands:\n"
                    "• Natural language: `show me the current directory`\n"
                    "• CLI commands: `git status`, `ls`\n"
                    "• `/stop` - Stop agent\n"
                    "• `/status` - Check status"
                )
                if not self.output_monitor_task:
                    self.output_monitor_task = asyncio.create_task(self._output_monitor())
            else:
                await update.message.reply_text("❌ Failed to start agent")

        elif cmd == "/stop":
            await self.agent.stop()
            await update.message.reply_text("🛑 Agent stopped")

        elif cmd == "/status":
            status = "🟢 Running" if self.agent.is_running() else "🔴 Stopped"
            waiting = "\n⏸️ Waiting for input" if self.agent.waiting_for_input else ""
            await update.message.reply_text(f"Status: {status}{waiting}")

    async def handle_message(self, update, context):
        """Handle regular messages"""
        if update.effective_user.id != int(self.user_id):
            await update.message.reply_text("❌ Unauthorized")
            return

        if not self.agent.is_running():
            await update.message.reply_text("❌ Agent not running. Use /start")
            return

        message_text = update.message.text.strip()

        # Send command to agent (synchronous)
        self.agent.send_command(message_text)
        await update.message.reply_text(f"📤 Command sent: {message_text}")

        # Wait for output
        await asyncio.sleep(2.0)
        output = await self.agent.get_output()
        if output:
            await update.message.reply_text(output.content)

    async def _output_monitor(self) -> None:
        """Monitor for new output"""
        debug_log(DEBUG_INFO, "Output monitor started")
        recent_messages = deque(maxlen=10)

        while self.agent.is_running():
            try:
                output = await self.agent.get_output()
                if output:
                    clean_output = strip_ansi_codes(output.content)
                    lines = [line.strip() for line in clean_output.split("\n")]
                    lines = list(dict.fromkeys(lines))
                    clean_output = "\n".join(lines)

                    # Sophisticated UI filtering
                    agent_config = self.agent.config
                    ui_indicators = agent_config.get("ui_indicators", ["╭", "╰", "│", "┃", "/plan or /act"])
                    ui_score = sum(1 for indicator in ui_indicators if indicator in clean_output)

                    normalized = " ".join(clean_output.split())
                    msg_hash = hash(normalized)

                    response_markers = agent_config.get("response_markers", ["###"])
                    is_agent_response = any(marker in clean_output for marker in response_markers)

                    repetitive_ui_markers = agent_config.get("repetitive_ui_markers", ["/plan or /act"])
                    is_repetitive_ui = ui_score >= 1 and any(marker in clean_output for marker in repetitive_ui_markers)

                    ui_ratio = ui_score / max(1, len(clean_output.split()))
                    is_mostly_ui = ui_ratio > 0.3 or (ui_score >= 2 and len(clean_output.strip()) <= 100)

                    should_filter = (
                        msg_hash in recent_messages
                        or (is_repetitive_ui and not is_agent_response and is_mostly_ui)
                        or (ui_score >= 3 and len(clean_output.strip()) <= 50)
                    )

                    if should_filter:
                        if is_repetitive_ui:
                            recent_messages.append(msg_hash)
                        await asyncio.sleep(2)
                        continue

                    debug_log(DEBUG_INFO, "Sending output to user", output_length=len(clean_output))
                    await self.send_message(self.user_id, clean_output)
                    recent_messages.append(msg_hash)

                await asyncio.sleep(2)
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Output monitor error: {e}")
                await asyncio.sleep(2)


# ============================================================================
# MAIN
# ============================================================================

load_dotenv()


def main():
    """Main entry point"""
    debug_log(DEBUG_INFO, "Starting Agent-Chat Bridge")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    user_id = os.getenv("AUTHORIZED_USER_ID")

    if not token or not user_id:
        debug_log(DEBUG_ERROR, "TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_ID must be set in .env")
        return

    try:
        int(user_id)
    except ValueError:
        debug_log(DEBUG_ERROR, f"User ID '{user_id}' must be numeric")
        return

    # Initialize agent
    agent = PTYAgent({
        "command": ["cline"],
        "name": "Cline",
        "welcome_keywords": ["cline cli"],
        "mode_keywords": ["switch to plan", "switch to act", "plan mode", "act mode"],
        "ui_indicators": ["╭", "╰", "│", "┃", "/plan or /act"],
        "response_markers": ["###"],
        "repetitive_ui_markers": ["/plan or /act"]
    })

    # Initialize Telegram application
    application = Application.builder().token(token).build()

    # Create bridge
    bridge = AgentChatBridge(agent, application, user_id)

    # Add handlers
    application.add_handler(CommandHandler(["start", "stop", "status"], bridge.handle_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.handle_message))

    # Startup message
    async def post_init(app):
        try:
            await app.bot.send_message(
                chat_id=int(user_id),
                text="🤖 **Agent-Chat Bridge Started**\n\n"
                "Use /start to begin an agent session"
            )
            debug_log(DEBUG_INFO, "Startup message sent")
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to send startup message: {e}")

    application.post_init = post_init

    # Signal handling
    def signal_handler(signum, frame):
        debug_log(DEBUG_INFO, f"Received signal {signum}, shutting down")
        import sys
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    debug_log(DEBUG_INFO, "Starting polling")
    application.run_polling()


if __name__ == "__main__":
    main()