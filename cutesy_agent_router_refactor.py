"""
Multi-Agent, Multi-Chat Interface Architecture
Supports:
  - PTY-based agents (Cline, Codex CLI, etc) 
  - Agent Client Protocol (ACP) agents (Claude, etc)
  - Any chat service (Telegram, Discord, etc)
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
import httpx

import psutil
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Discord support (optional)
try:
    import discord
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    commands = None


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
# MESSAGE TYPES
# ============================================================================

class MessageType(Enum):
    USER_INPUT = "user_input"
    AGENT_OUTPUT = "agent_output"
    COMMAND = "command"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class Message:
    """Structured message"""
    def __init__(self, type: MessageType, content: str, sender: str = "unknown", metadata: Dict = None):
        self.type = type
        self.content = content
        self.sender = sender
        self.timestamp = datetime.now().isoformat()
        self.metadata = metadata or {}


# ============================================================================
# ABSTRACT AGENT INTERFACE
# ============================================================================

class AgentInterface(ABC):
    """Abstract interface for any AI agent"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.is_running_flag = False
        self.waiting_for_input = False
        self.command_timeout = config.get("command_timeout", 30.0)  # Default 30 seconds

    @abstractmethod
    async def start(self) -> bool:
        """Start the agent session"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the agent session"""
        pass

    @abstractmethod
    async def send_command(self, command: str) -> str:
        """Send command/message to agent"""
        pass

    @abstractmethod
    async def get_output(self) -> Optional[Message]:
        """Get pending output from agent"""
        pass

    def is_running(self) -> bool:
        return self.is_running_flag

    async def get_custom_commands(self) -> Dict[str, str]:
        """Return custom commands this agent supports

        Returns:
            Dict[command_name, description]
            Example: {"/plan": "Switch to plan mode", "/act": "Switch to act mode"}
        """
        return {}

    async def get_custom_help(self) -> str:
        """Return agent-specific help content to append to base help

        Returns:
            Additional help text specific to this agent, or empty string
        """
        return ""

    async def handle_custom_command(self, command: str, args: str) -> Optional[str]:
        """Handle a custom command

        Args:
            command: The command name (e.g., "/plan")
            args: Any arguments after the command

        Returns:
            Response message, or None to use default handling
        """
        return None


# ============================================================================
# PTY-BASED AGENTS (Cline, Codex CLI, etc)
# ============================================================================

class PTYAgent(AgentInterface):
    """Base class for PTY-based CLI agents"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.command = config.get("command", ["agent"])
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
        """Process output and detect prompts - override in subclasses for custom filtering"""
        clean_output = strip_ansi_codes(output)

        # Apply agent-specific filtering
        if self._should_filter_output(clean_output):
            return

        # Detect interactive prompts
        for pattern in self._get_prompt_patterns():
            if re.search(pattern, clean_output, re.IGNORECASE):
                with self.state_lock:
                    self.waiting_for_input = True
                    self.input_prompt = clean_output.strip()
                    self.last_prompt_time = time.time()
                debug_log(DEBUG_INFO, "Interactive prompt detected", pattern=pattern)
                break

        with self.output_queue_lock:
            self.output_queue.append(clean_output)

    def _should_filter_output(self, output: str) -> bool:
        """Override in subclasses to implement custom filtering. Return True to filter."""
        return False

    def _get_prompt_patterns(self) -> list:
        """Override in subclasses for custom prompt detection patterns"""
        return [
            r"\[y/N\]\s*$", r"\[Y/n\]\s*$", r"\(y/n\)\s*$", r"\(Y/N\)\s*$",
            r"Continue\?\s*$", r"Proceed\?\s*$", r"Are you sure\?\s*$",
            r"Enter .*:\s*$", r"Password:\s*$",
            r"Press.*Enter.*to.*continue\s*$", r"Press.*any.*key\s*$",
            r"\[.*\]\s*$", r"Press .*to exit\s*$", r"Press .* to return\s*$",
        ]

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

    async def send_command(self, command: str) -> str:
        """Send command to agent"""
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
                return Message(MessageType.AGENT_OUTPUT, combined.strip(), sender=self.name)
            return None


# ============================================================================
# CONCRETE PTY AGENTS
# ============================================================================

class ClineAgent(PTYAgent):
    """Cline CLI agent with custom filtering"""

    def __init__(self, config: Dict[str, Any]):
        config.setdefault("name", "Cline")
        config.setdefault("command", ["cline"])
        super().__init__(config)
        self._message_ui_scores = {}  # Track UI scores for filtering

    def _should_filter_output(self, output: str) -> bool:
        """Cline-specific output filtering at the PTY level"""
        welcome_keywords = self.config.get("welcome_keywords", ["cline cli"])
        is_welcome_screen = any(keyword in output.lower() for keyword in welcome_keywords)

        mode_keywords = self.config.get("mode_keywords", ["switch to plan", "switch to act", "plan mode", "act mode"])
        is_mode_switch = any(keyword in output.lower() for keyword in mode_keywords)

        is_box_line = bool(re.match(r"^[\s│┃╭╰╮╯]+$", output.strip()))
        is_mostly_empty_ui = (output.strip() in ["╭", "╰", "│", "┃", "╮", "╯"] or is_box_line) and len(output.strip()) <= 3

        return not is_welcome_screen and not is_mode_switch and is_mostly_empty_ui

    def _should_filter_message(self, content: str) -> bool:
        """Cline-specific message-level filtering for UI spam and duplicates"""
        # Count UI indicators
        ui_indicators = ["╭", "╰", "│", "┃", "/plan or /act"]
        ui_score = sum(1 for indicator in ui_indicators if indicator in content)

        # Check for Cline responses
        is_cline_response = "###" in content

        # Check for repetitive UI
        is_repetitive_ui = ui_score >= 1 and "/plan or /act" in content

        # Calculate if message is mostly UI
        ui_ratio = ui_score / max(1, len(content.split()))
        is_mostly_ui = ui_ratio > 0.3 or (ui_score >= 2 and len(content.strip()) <= 100)

        # High UI score filter
        high_ui_score = ui_score >= 3 and len(content.strip()) <= 50

        # Should filter if: repetitive UI that's mostly UI and not a response, OR high UI score
        should_filter = (
            (is_repetitive_ui and not is_cline_response and is_mostly_ui) or
            high_ui_score
        )

        if should_filter:
            debug_log(DEBUG_DEBUG, f"Filtered Cline message: UI score {ui_score}, ratio {ui_ratio:.2f}, mostly_ui: {is_mostly_ui}")

        return should_filter

    async def get_custom_commands(self) -> Dict[str, str]:
        """Cline-specific commands"""
        return {
            "/plan": "Switch to plan mode - Cline will plan before executing",
            "/act": "Switch to act mode - Cline will execute immediately",
        }

    async def get_custom_help(self) -> str:
        """Cline-specific help content"""
        return """
**Usage Examples:**
• "Show me all Python files in this directory"
• "Create a README.md with project description"
• "Fix any syntax errors in src/main.py"
• "What's the current git status?"

**Tips:**
• Cline works best with clear, specific instructions
• Use full context: "In the api/ directory, create..."
• Chain requests: "First check git status, then commit my changes"
• Send shell commands directly: `git status`, `ls -la`, `pwd`

**Cline will execute commands in your project directory**
"""

    async def handle_custom_command(self, command: str, args: str) -> Optional[str]:
        """Handle Cline-specific commands"""

        if command == "/plan":
            # Send the plan command to Cline
            result = await self.send_command("/plan")
            await asyncio.sleep(0.5)
            output = await self.get_output()
            response = f"📋 Switched to Plan Mode\n{result}"
            if output:
                response += f"\n{output.content}"
            return response

        elif command == "/act":
            # Send the act command to Cline
            result = await self.send_command("/act")
            await asyncio.sleep(0.5)
            output = await self.get_output()
            response = f"⚡ Switched to Act Mode\n{result}"
            if output:
                response += f"\n{output.content}"
            return response

        else:
            return None  # Use default handling


class CodexCLIAgent(PTYAgent):
    """Codex CLI agent - PTY-based (runs locally as subprocess)"""
    
    def __init__(self, config: Dict[str, Any]):
        config.setdefault("name", "Codex CLI")
        config.setdefault("command", ["codex"])
        super().__init__(config)

    def _should_filter_output(self, output: str) -> bool:
        """Codex-specific filtering if needed"""
        # Implement Codex-specific filtering here
        return False


# ============================================================================
# ACP-BASED AGENTS
# ============================================================================

class ACPAgent(AgentInterface):
    """Agent Client Protocol (ACP) based agent"""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_url = config.get("api_url")
        self.api_key = config.get("api_key")
        self.model = config.get("model", "claude-3-5-sonnet-20241022")
        self.name = config.get("name", "ACP Agent")
        
        self.conversation_history = []
        self.output_queue = deque(maxlen=50)
        self.output_queue_lock = threading.Lock()
        
    async def start(self) -> bool:
        """Start ACP agent (no process to start)"""
        self.is_running_flag = True
        self.conversation_history = []
        debug_log(DEBUG_INFO, f"{self.name} ACP session started")
        return True

    async def stop(self) -> None:
        """Stop ACP agent"""
        self.is_running_flag = False
        self.conversation_history = []
        debug_log(DEBUG_INFO, f"{self.name} ACP session stopped")

    async def send_command(self, command: str) -> str:
        """Send message to ACP agent via API"""
        if not self.is_running_flag:
            return "Error: Agent not running"

        try:
            # Add user message to history
            self.conversation_history.append({
                "role": "user",
                "content": command
            })

            # Call ACP API
            response = await self._call_api(self.conversation_history)
            
            if response:
                # Add assistant response to history
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response
                })
                
                # Queue output
                with self.output_queue_lock:
                    self.output_queue.append(response)
                
                return "Message sent"
            else:
                return "Error: No response from agent"
                
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to send message to {self.name}: {e}")
            return f"Error: {e}"

    async def _call_api(self, messages: list) -> Optional[str]:
        """Call the ACP API endpoint"""
        raise NotImplementedError("Subclasses must implement _call_api")

    async def get_output(self) -> Optional[Message]:
        """Get pending output"""
        with self.output_queue_lock:
            if not self.output_queue:
                return None

            combined = ""
            while self.output_queue and len(combined) < 4000:
                chunk = self.output_queue.popleft()
                combined += chunk + "\n"

            if combined.strip():
                return Message(MessageType.AGENT_OUTPUT, combined.strip(), sender=self.name)
            return None


# ============================================================================
# CONCRETE ACP AGENTS
# ============================================================================

class ClaudeAAPIAgent(ACPAgent):
    """Claude via Anthropic API (ACP-compatible)"""
    
    def __init__(self, config: Dict[str, Any]):
        config.setdefault("name", "Claude API")
        config.setdefault("api_url", "https://api.anthropic.com/v1/messages")
        super().__init__(config)

    async def _call_api(self, messages: list) -> Optional[str]:
        """Call Claude API"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 1024,
                        "messages": messages,
                    },
                    timeout=30.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("content"):
                        return data["content"][0]["text"]
                else:
                    debug_log(DEBUG_ERROR, f"API error: {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to call Claude API: {e}")
            return None


class OpenAIAPIAgent(ACPAgent):
    """OpenAI via OpenAI API (ACP-compatible)"""
    
    def __init__(self, config: Dict[str, Any]):
        config.setdefault("name", "OpenAI API")
        config.setdefault("api_url", "https://api.openai.com/v1/chat/completions")
        config.setdefault("model", "gpt-4-turbo")
        super().__init__(config)

    async def _call_api(self, messages: list) -> Optional[str]:
        """Call OpenAI API"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": 1024,
                    },
                    timeout=30.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("choices"):
                        return data["choices"][0]["message"]["content"]
                else:
                    debug_log(DEBUG_ERROR, f"API error: {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to call OpenAI API: {e}")
            return None


class CodexAPIAgent(ACPAgent):
    """Codex via API (ACP-compatible) - if running with --api-server"""
    
    def __init__(self, config: Dict[str, Any]):
        config.setdefault("name", "Codex API")
        config.setdefault("api_url", "http://localhost:8000/v1/messages")
        super().__init__(config)

    async def _call_api(self, messages: list) -> Optional[str]:
        """Call Codex API endpoint"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "Content-Type": "application/json",
                    },
                    json={
                        "messages": messages,
                        "max_tokens": 2048,
                    },
                    timeout=60.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    # Parse based on Codex API response format
                    if data.get("content"):
                        return data["content"][0]["text"]
                    elif data.get("message"):
                        return data["message"]
                    else:
                        return str(data)
                else:
                    debug_log(DEBUG_ERROR, f"Codex API error: {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to call Codex API: {e}")
            return None


# ============================================================================
# BRIDGE
# ============================================================================

class AgentChatBridge:
    """Bridges agent and chat service (Telegram/Discord)"""

    def __init__(self, agent: AgentInterface, chat_service, user_id: str):
        self.agent = agent
        self.chat_service = chat_service
        self.user_id = user_id
        self.output_monitor_task = None
        self._recent_hashes = deque(maxlen=10)  # For duplicate filtering
        self.custom_commands = {}  # Store custom commands

        # Security: Rate limiting and size limits
        self._last_message_time = {}  # user_id -> timestamp
        self._rate_limit_ms = 500  # Minimum 500ms between messages
        self._max_message_length = 10000  # Max 10,000 characters
        self._rate_limit_cleanup_interval = 3600  # Clean every hour
        self._last_cleanup = time.time()

    async def initialize(self) -> None:
        """Initialize bridge - get custom commands from agent"""
        self.custom_commands = await self.agent.get_custom_commands()
        custom_count = len(self.custom_commands)
        debug_log(DEBUG_INFO,
            f"Loaded custom commands for {self.agent.name}",
            count=custom_count,
            commands=list(self.custom_commands.keys())
        )

    async def send_message(self, user_id: str, text: str) -> None:
        """Send message to user"""
        try:
            # Check if it's a Telegram Application (old interface) or ChatServiceInterface (new)
            if hasattr(self.chat_service, 'run_polling'):
                # Telegram Application - old interface
                await self.chat_service.bot.send_message(chat_id=int(user_id), text=text)
            else:
                # ChatServiceInterface - new interface (Discord, etc.)
                await self.chat_service.send_message(user_id, text)
        except Exception as e:
            debug_log(DEBUG_ERROR, f"Failed to send message: {e}")

    async def handle_command(self, update, context):
        """Handle commands - both built-in and custom"""
        if update.effective_user.id != int(self.user_id):
            await update.message.reply_text("❌ Unauthorized")
            return

        cmd_text = update.message.text

        # Parse command and arguments
        parts = cmd_text.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Handle built-in commands
        if cmd == "/start":
            if self.agent.is_running():
                await update.message.reply_text("ℹ️ Agent already running")
                return

            if await self.agent.start():
                # Show a concise startup message
                agent_type = "CLI Agent" if hasattr(self.agent, 'command') else "API Agent"
                startup_msg = f"✅ {self.agent.name} session started ({agent_type})\n\n"
                startup_msg += "Send messages to interact with the agent\n"
                startup_msg += "• `/help` - Show all available commands\n"
                startup_msg += "• `/status` - Check current status"

                await update.message.reply_text(startup_msg)
                if not self.output_monitor_task:
                    self.output_monitor_task = asyncio.create_task(self._output_monitor())
            else:
                await update.message.reply_text(f"❌ Failed to start {self.agent.name}")

        elif cmd == "/stop":
            await self.agent.stop()
            await update.message.reply_text("🛑 Agent stopped")

        elif cmd == "/status":
            status = "🟢 Running" if self.agent.is_running() else "🔴 Stopped"
            waiting = "\n⏸️ Waiting for input" if self.agent.waiting_for_input else ""
            await update.message.reply_text(f"Status: {status}{waiting}\nAgent: {self.agent.name}")

        elif cmd == "/cancel":
            if not self.agent.is_running():
                await update.message.reply_text(f"❌ {self.agent.name} not running. Use /start")
                return

            # Cancel operation - behavior depends on agent type
            cancel_result = await self._cancel_operation()
            await update.message.reply_text(cancel_result)

        elif cmd == "/reset":
            # Reset agent - behavior depends on agent type
            reset_result = await self._reset_agent()
            await update.message.reply_text(reset_result)

        elif cmd == "/help":
            # Get agent-specific help content
            custom_help = await self.agent.get_custom_help()

            help_text = f"""🤖 **Agent-Chat Bridge Help**

**Getting Started:**
• `/stop` - Stop the current session
• `/start` - Start a new {self.agent.name} session
• `/reset` - Reset agent state and start fresh

**Commands:**
• `/status` - Check bot and session status
• `/cancel` - Cancel current operation

**Available Commands:**
{self._format_commands()}{custom_help}
"""
            await update.message.reply_text(help_text)

        # Handle custom commands
        elif cmd in self.custom_commands:
            if not self.agent.is_running():
                await update.message.reply_text(f"❌ Agent not running. Use /start")
                return

            # Let agent handle the custom command
            response = await self.agent.handle_custom_command(cmd, args)

            if response:
                # Custom command returned a response
                await update.message.reply_text(response)
            else:
                # Agent returned None, use default handling
                await self.agent.send_command(cmd_text)
                await asyncio.sleep(1.0)
                output = await self.agent.get_output()
                if output:
                    await update.message.reply_text(output.content)

        # Handle unknown commands
        else:
            available = self._format_commands()
            await update.message.reply_text(
                f"❌ Unknown command: {cmd}\n\n"
                f"Available commands:\n{available}"
            )

    async def _cancel_operation(self) -> str:
        """Cancel current operation - implementation varies by agent type"""
        # For PTY agents (like Cline), send Ctrl+C
        if isinstance(self.agent, PTYAgent):
            with self.agent.command_lock:
                try:
                    if not self.agent.is_running_flag:
                        return "❌ Agent not running"
                    # Send Ctrl+C (0x03) directly to PTY
                    bytes_written = os.write(self.agent.master_fd, b"\x03")
                    debug_log(DEBUG_INFO, "Ctrl+C sent to PTY", bytes_written=bytes_written)
                    return "🛑 Sent cancel signal (Ctrl+C) to agent"
                except Exception as e:
                    debug_log(DEBUG_ERROR, f"Failed to send Ctrl+C: {e}")
                    return f"❌ Failed to send cancel signal: {e}"
        else:
            # For API agents, cancel pending operations/state
            # For now, just indicate cancellation attempted
            # Future: could cancel pending HTTP requests, clear queues, etc.
            return f"🛑 Cancelled current {self.agent.name} operation"

    async def _reset_agent(self) -> str:
        """Reset agent - behavior depends on agent type"""
        debug_log(DEBUG_INFO, f"Resetting agent: {self.agent.name}")

        # For PTY agents (like Cline), restart the entire session
        if isinstance(self.agent, PTYAgent):
            try:
                # Stop the current output monitor if running
                if self.output_monitor_task and not self.output_monitor_task.done():
                    debug_log(DEBUG_INFO, "Cancelling output monitor task")
                    self.output_monitor_task.cancel()
                    try:
                        await self.output_monitor_task
                    except asyncio.CancelledError:
                        debug_log(DEBUG_INFO, "Output monitor task cancelled")
                    self.output_monitor_task = None

                was_running = self.agent.is_running()
                debug_log(DEBUG_INFO, f"Agent was running: {was_running}")

                if was_running:
                    debug_log(DEBUG_INFO, "Stopping agent")
                    await self.agent.stop()
                    await asyncio.sleep(1.0)  # Longer pause for cleanup

                # Clear duplicate message cache to allow fresh welcome messages
                debug_log(DEBUG_INFO, "Clearing duplicate message cache")
                self._recent_hashes.clear()

                # Force cleanup even if agent thinks it's not running
                debug_log(DEBUG_INFO, "Ensuring session clean before restart")
                if hasattr(self.agent, '_ensure_session_clean'):
                    self.agent._ensure_session_clean()

                debug_log(DEBUG_INFO, "Starting fresh session")
                # Start fresh session
                if await self.agent.start():
                    # Restart the output monitor
                    debug_log(DEBUG_INFO, "Restarting output monitor")
                    self.output_monitor_task = asyncio.create_task(self._output_monitor())
                    debug_log(DEBUG_INFO, "Agent reset successful")
                    return "🔄 **Agent Reset Complete**\n\n✅ Fresh session started\n✅ All state cleared\n✅ Duplicate cache cleared\n✅ Output monitoring restarted\n✅ Ready for new commands"
                else:
                    debug_log(DEBUG_ERROR, "Failed to start agent after reset")
                    return "❌ Reset failed - could not start new session"
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Exception during agent reset: {e}")
                return f"❌ Reset failed with error: {e}"
        else:
            # For API agents, clear conversation history and reset state
            if hasattr(self.agent, 'conversation_history'):
                self.agent.conversation_history = []
            # Future: could also cancel pending requests, reset other state
            return f"🔄 **{self.agent.name} Reset Complete**\n\n✅ Conversation history cleared\n✅ Agent state reset"

    async def handle_all_text(self, update, context):
        """Handle all text messages (commands and regular messages)"""
        if update.effective_user.id != int(self.user_id):
            await update.message.reply_text("❌ Unauthorized")
            return

        message_text = update.message.text.strip()

        # Check if it's a custom command (starts with / and is in custom commands)
        if message_text.startswith("/") and message_text.split()[0] in self.custom_commands:
            await self.handle_custom_command_message(update, message_text)
        else:
            await self.handle_regular_message(update, message_text)

    async def handle_custom_command_message(self, update, message_text):
        """Handle custom command messages"""
        if not self.agent.is_running():
            await update.message.reply_text(f"❌ {self.agent.name} not running. Use /start")
            return

        # Parse command and args
        parts = message_text.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Handle the custom command
        response = await self.agent.handle_custom_command(cmd, args)

        if response:
            await update.message.reply_text(response)
        else:
            # Fallback: send as regular command
            await self.agent.send_command(message_text)
            await asyncio.sleep(1.0)
            output = await self.agent.get_output()
            if output:
                await update.message.reply_text(output.content)

    async def handle_regular_message(self, update, message_text):
        """Handle regular (non-command) messages"""
        if not self.agent.is_running():
            await update.message.reply_text(f"❌ {self.agent.name} not running. Use /start")
            return

        # Security: Check message size
        if len(message_text) > self._max_message_length:
            await update.message.reply_text(f"❌ Message too long (max {self._max_message_length} characters)")
            return

        # Security: Rate limiting
        user_id = str(update.effective_user.id)
        current_time = time.time()

        # Periodic cleanup of stale rate limit entries (prevents memory leak)
        if current_time - self._last_cleanup > self._rate_limit_cleanup_interval:
            stale_users = [
                uid for uid, ts in self._last_message_time.items()
                if current_time - ts > 86400  # Older than 24 hours
            ]
            for uid in stale_users:
                del self._last_message_time[uid]
            self._last_cleanup = current_time
            debug_log(DEBUG_INFO, f"Cleaned up {len(stale_users)} stale rate limit entries")

        if user_id in self._last_message_time:
            time_diff = current_time - self._last_message_time[user_id]
            if time_diff < (self._rate_limit_ms / 1000):  # Convert ms to seconds
                await update.message.reply_text("⏱️ Please wait before sending another message")
                return

        self._last_message_time[user_id] = current_time

        await update.message.reply_text(f"📤 Message sent...")

        await self.agent.send_command(message_text)

        try:
            # Wait for output with agent-specific timeout to prevent Telegram timeout
            timeout_seconds = getattr(self.agent, 'command_timeout', 30.0)
            output = await asyncio.wait_for(
                self.agent.get_output(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timeout_seconds = getattr(self.agent, 'command_timeout', 30.0)
            await update.message.reply_text(
                f"⏱️ Response timeout (>{timeout_seconds}s)\n\n"
                "Command is still running in background.\n"
                "Output will arrive via continuous monitoring."
            )
            return

        if output:
            await update.message.reply_text(output.content)

    async def _output_monitor(self) -> None:
        """Monitor for new output with sophisticated filtering"""
        debug_log(DEBUG_INFO, "Output monitor started")
        recent_messages = deque(maxlen=10)

        while self.agent.is_running():
            try:
                output = await self.agent.get_output()
                if output:
                    # Apply agent-specific filtering
                    filtered_output = self._filter_output(output)
                    if filtered_output:
                        debug_log(DEBUG_INFO, "Sending filtered output to user", output_length=len(filtered_output.content))
                        await self.send_message(self.user_id, filtered_output.content)
                await asyncio.sleep(2)
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Output monitor error: {e}")
                await asyncio.sleep(2)

    def _format_commands(self) -> str:
        """Format available commands for display"""
        commands_text = ""

        if self.custom_commands:
            for cmd, description in self.custom_commands.items():
                commands_text += f"• `{cmd}` - {description.split(' - ')[0]}\n"

        return commands_text

    def _filter_output(self, output: Message) -> Optional[Message]:
        """Apply sophisticated filtering to prevent duplicates and UI spam"""
        clean_content = strip_ansi_codes(output.content)

        # Remove duplicate lines within message
        lines = [line.strip() for line in clean_content.split("\n")]
        lines = list(dict.fromkeys(lines))  # Remove duplicates
        clean_content = "\n".join(lines)

        # Agent-specific UI filtering
        if hasattr(self.agent, '_should_filter_message'):
            if self.agent._should_filter_message(clean_content):
                return None

        # Global duplicate message filtering
        msg_hash = hash(clean_content)
        if msg_hash in self._recent_hashes:
            debug_log(DEBUG_DEBUG, "Filtered duplicate message")
            return None
        self._recent_hashes.append(msg_hash)

        return Message(output.type, clean_content, output.sender, output.metadata)

    # Methods for new chat service interface (used by Discord)
    async def process_message(self, message_text: str, sender_id: str):
        """Process incoming message from chat service (for Discord)"""
        if sender_id != self.user_id:
            await self.chat_service.send_message(sender_id, "❌ Unauthorized")
            return

        message_text = message_text.strip()

        # Handle commands vs regular messages
        if message_text.startswith("/"):
            await self.handle_command_from_chat(message_text, sender_id)
        else:
            await self.handle_regular_message_from_chat(message_text, sender_id)

    async def handle_command_from_chat(self, message_text: str, sender_id: str):
        """Handle commands from chat service (simplified version)"""
        parts = message_text.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Handle built-in commands
        if cmd == "/start":
            if self.agent.is_running():
                await self.chat_service.send_message(sender_id, "ℹ️ Agent already running")
                return

            if await self.agent.start():
                startup_msg = f"✅ {self.agent.name} session started\n\n{self._format_commands()}"
                await self.chat_service.send_message(sender_id, startup_msg)
                if not self.output_monitor_task:
                    self.output_monitor_task = asyncio.create_task(self._output_monitor())
            else:
                await self.chat_service.send_message(sender_id, f"❌ Failed to start {self.agent.name}")

        elif cmd == "/stop":
            await self.agent.stop()
            await self.chat_service.send_message(sender_id, "🛑 Agent stopped")

        elif cmd == "/status":
            status = "🟢 Running" if self.agent.is_running() else "🔴 Stopped"
            waiting = "\n⏸️ Waiting for input" if self.agent.waiting_for_input else ""
            await self.chat_service.send_message(sender_id, f"Status: {status}{waiting}\nAgent: {self.agent.name}")

        elif cmd == "/help":
            custom_help = await self.agent.get_custom_help()
            help_text = f"""🤖 **Agent-Chat Bridge Help**

**Getting Started:**
• `/start` - Start a new {self.agent.name} session
• `/stop` - Stop the current session
• `/reset` - Reset agent state and start fresh

**Commands:**
• `/status` - Check bot and session status
• `/cancel` - Cancel current operation

**Available Commands:**
{self._format_commands()}{custom_help}"""
            await self.chat_service.send_message(sender_id, help_text)

        elif cmd == "/cancel":
            if not self.agent.is_running():
                await self.chat_service.send_message(sender_id, f"❌ {self.agent.name} not running. Use /start")
                return

            cancel_result = await self._cancel_operation()
            await self.chat_service.send_message(sender_id, cancel_result)

        elif cmd == "/reset":
            reset_result = await self._reset_agent()
            await self.chat_service.send_message(sender_id, reset_result)

        # Handle custom commands
        elif cmd in self.custom_commands:
            if not self.agent.is_running():
                await self.chat_service.send_message(sender_id, f"❌ {self.agent.name} not running. Use /start")
                return

            response = await self.agent.handle_custom_command(cmd, args)
            if response:
                await self.chat_service.send_message(sender_id, response)
            else:
                # Send as regular command to agent
                await self.agent.send_command(message_text)
                await asyncio.sleep(1.0)
                output = await self.agent.get_output()
                if output:
                    await self.chat_service.send_message(sender_id, output.content)

        else:
            available = self._format_commands()
            await self.chat_service.send_message(sender_id,
                f"❌ Unknown command: {cmd}\n\n**Available Commands:**\n{available}")

    async def handle_regular_message_from_chat(self, message_text: str, sender_id: str):
        """Handle regular messages from chat service"""
        if not self.agent.is_running():
            await self.chat_service.send_message(sender_id, f"❌ {self.agent.name} not running. Use /start")
            return

        # Security: Check message size
        if len(message_text) > self._max_message_length:
            await self.chat_service.send_message(sender_id, f"❌ Message too long (max {self._max_message_length} characters)")
            return

        # Security: Rate limiting
        current_time = time.time()

        # Periodic cleanup of stale rate limit entries (prevents memory leak)
        if current_time - self._last_cleanup > self._rate_limit_cleanup_interval:
            stale_users = [
                uid for uid, ts in self._last_message_time.items()
                if current_time - ts > 86400  # Older than 24 hours
            ]
            for uid in stale_users:
                del self._last_message_time[uid]
            self._last_cleanup = current_time
            debug_log(DEBUG_INFO, f"Cleaned up {len(stale_users)} stale rate limit entries")

        if sender_id in self._last_message_time:
            time_diff = current_time - self._last_message_time[sender_id]
            if time_diff < (self._rate_limit_ms / 1000):  # Convert ms to seconds
                await self.chat_service.send_message(sender_id, "⏱️ Please wait before sending another message")
                return

        self._last_message_time[sender_id] = current_time

        await self.chat_service.send_message(sender_id, f"📤 Message sent...")

        await self.agent.send_command(message_text)

        try:
            # Wait for output with agent-specific timeout
            timeout_seconds = getattr(self.agent, 'command_timeout', 30.0)
            output = await asyncio.wait_for(
                self.agent.get_output(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timeout_seconds = getattr(self.agent, 'command_timeout', 30.0)
            await self.chat_service.send_message(sender_id,
                f"⏱️ Response timeout (>{timeout_seconds}s)\n\n"
                "Command is still running in background.\n"
                "Output will arrive via continuous monitoring."
            )
            return

        if output:
            await self.chat_service.send_message(sender_id, output.content)


# ============================================================================
# CHAT SERVICES
# ============================================================================

class ChatServiceInterface(ABC):
    """Abstract interface for chat services (Telegram, Discord, etc.)"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def set_message_handler(self, handler: Callable) -> None:
        """Set handler for incoming messages"""
        pass

    @abstractmethod
    async def send_message(self, user_id: str, text: str) -> None:
        """Send message to user"""
        pass

    @abstractmethod
    async def start(self) -> None:
        """Start the chat service"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the chat service"""
        pass


if DISCORD_AVAILABLE:
    class DiscordChatService(ChatServiceInterface):
        """Discord chat service"""

        def __init__(self, config: Dict[str, Any]):
            super().__init__(config)
            self.token = config.get("token")
            self.channel_id = config.get("channel_id")
            self.authorized_user_id = config.get("authorized_user_id")
            self.bot = None
            self.message_handler = None

        def set_message_handler(self, handler: Callable) -> None:
            """Set handler for incoming messages"""
            self.message_handler = handler

        async def send_message(self, user_id: str, text: str) -> None:
            """Send message to Discord user or channel"""
            try:
                if self.channel_id:
                    # Send to specific channel
                    channel = self.bot.get_channel(int(self.channel_id))
                    if channel:
                        await channel.send(text)
                else:
                    # DM the user
                    user = await self.bot.fetch_user(int(user_id))
                    await user.send(text)
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Failed to send Discord message: {e}")

        async def start(self) -> None:
            """Start Discord bot"""
            self.bot = commands.Bot(command_prefix="/", intents=discord.Intents.default())

            @self.bot.event
            async def on_ready():
                debug_log(DEBUG_INFO, f"Discord bot logged in as {self.bot.user}")

            @self.bot.event
            async def on_message(message):
                # Ignore bot's own messages
                if message.author == self.bot.user:
                    return

                # Check authorization
                if str(message.author.id) != self.authorized_user_id:
                    await message.reply("❌ Unauthorized")
                    return

                # Handle commands and messages
                if message.content.startswith("/"):
                    cmd = message.content
                    if self.message_handler:
                        await self.message_handler(cmd, str(message.author.id))
                else:
                    if self.message_handler:
                        await self.message_handler(message.content, str(message.author.id))

            await self.bot.start(self.token)

        async def stop(self) -> None:
            """Stop Discord bot"""
            if self.bot:
                await self.bot.close()
else:
    class DiscordChatService(ChatServiceInterface):
        """Discord placeholder when discord.py not available"""
        def __init__(self, config):
            raise ImportError("discord.py not installed. Run: pip install discord.py")

        def set_message_handler(self, handler): pass
        async def send_message(self, user_id, text): pass
        async def start(self): pass
        async def stop(self): pass


# ============================================================================
# MAIN
# ============================================================================

load_dotenv()


def create_agent(agent_type: str, config: Dict[str, Any]) -> AgentInterface:
    """Factory function to create agents"""
    agents = {
        # PTY agents (run locally as subprocess)
        "cline": ClineAgent,
        "codex-cli": CodexCLIAgent,
        
        # ACP agents (call remote API)
        "claude-api": ClaudeAAPIAgent,
        "openai-api": OpenAIAPIAgent,
        "codex-api": CodexAPIAgent,
    }
    
    if agent_type not in agents:
        raise ValueError(f"Unknown agent type: {agent_type}")
    
    return agents[agent_type](config)


def main():
    """Main entry point"""
    debug_log(DEBUG_INFO, "Starting Agent-Chat Bridge")

    # Choose chat service
    chat_service_type = os.getenv("CHAT_SERVICE", "telegram").lower()

    if chat_service_type == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            debug_log(DEBUG_ERROR, "TELEGRAM_BOT_TOKEN required for Telegram service")
            return
    elif chat_service_type == "discord":
        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            debug_log(DEBUG_ERROR, "DISCORD_BOT_TOKEN required for Discord service")
            return
        if not DISCORD_AVAILABLE:
            debug_log(DEBUG_ERROR, "Discord service requested but discord.py not installed. Run: pip install discord.py")
            return
    else:
        debug_log(DEBUG_ERROR, f"Unknown chat service: {chat_service_type}. Use 'telegram' or 'discord'")
        return

    user_id = os.getenv("AUTHORIZED_USER_ID")
    agent_type = os.getenv("AGENT_TYPE", "cline")

    if not token or not user_id:
        debug_log(DEBUG_ERROR, "TELEGRAM_BOT_TOKEN and AUTHORIZED_USER_ID must be set in .env")
        return

    try:
        int(user_id)
    except ValueError:
        debug_log(DEBUG_ERROR, f"User ID '{user_id}' must be numeric")
        return

    # Create agent based on type
    agent_config = {
        "cline": {
            "command": ["cline"],
            "name": "Cline",
            "welcome_keywords": ["cline cli"],
            "mode_keywords": ["switch to plan", "switch to act", "plan mode", "act mode"],
            "command_timeout": 30.0,  # Allow time for file operations
        },
        "codex-cli": {
            "command": ["codex"],
            "name": "Codex CLI",
            "command_timeout": 45.0,  # May need more time
        },
        "codex-api": {
            "api_url": os.getenv("CODEX_API_URL", "http://localhost:8000/v1/messages"),
            "name": "Codex API",
            "command_timeout": 60.0,  # Network + processing
        },
        "claude-api": {
            "api_key": os.getenv("ANTHROPIC_API_KEY"),
            "model": "claude-3-5-sonnet-20241022",
            "name": "Claude API",
            "command_timeout": 45.0,  # Network + complex reasoning
        },
        "openai-api": {
            "api_key": os.getenv("OPENAI_API_KEY"),
            "model": "gpt-4-turbo",
            "name": "OpenAI",
            "command_timeout": 45.0,  # Network + complex reasoning
        },
    }

    if agent_type not in agent_config:
        debug_log(DEBUG_ERROR, f"Agent type '{agent_type}' not configured. Available: {list(agent_config.keys())}")
        return

    agent = create_agent(agent_type, agent_config[agent_type])

    if chat_service_type == "telegram":
        # Original Telegram implementation
        application = Application.builder().token(token).build()
        bridge = AgentChatBridge(agent, application, user_id)

        # Add handlers - built-in commands only (custom commands handled via messages)
        application.add_handler(CommandHandler(["start", "stop", "status", "help", "cancel", "reset"], bridge.handle_command))

        # Handle all text messages (including custom commands)
        application.add_handler(MessageHandler(filters.TEXT, bridge.handle_all_text))

        # Startup message and bridge initialization
        async def post_init(app):
            try:
                # Initialize bridge asynchronously
                await bridge.initialize()

                # Send startup message
                await app.bot.send_message(
                    chat_id=int(user_id),
                    text=f"🤖 **Agent-Chat Bridge Started**\n\nAgent: {agent.name}\nUse /start to begin"
                )
                debug_log(DEBUG_INFO, "Startup message sent")
            except Exception as e:
                debug_log(DEBUG_ERROR, f"Failed to send startup message: {e}")

        application.post_init = post_init

        debug_log(DEBUG_INFO, f"Starting with {agent.name} ({agent_type}) on Telegram")
        application.run_polling()

    elif chat_service_type == "discord":
        # Discord implementation
        chat_config = {
            "token": token,
            "authorized_user_id": user_id,
            "channel_id": os.getenv("DISCORD_CHANNEL_ID"),  # Optional
        }
        chat_service = DiscordChatService(chat_config)

        # Create bridge with chat service
        bridge = AgentChatBridge(agent, chat_service, user_id)

        # Initialize custom commands
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bridge.initialize())

        # Set up message routing
        async def message_handler(message_text: str, sender_id: str):
            await bridge.process_message(message_text, sender_id)

        chat_service.set_message_handler(message_handler)

        debug_log(DEBUG_INFO, f"Starting with {agent.name} ({agent_type}) on Discord")
        asyncio.run(chat_service.start())


if __name__ == "__main__":
    main()