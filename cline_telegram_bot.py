import os
import pty
import select
import subprocess
import threading
import time
import asyncio
import re
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configuration
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
AUTHORIZED_USER_ID = 123456789
CLINE_COMMAND = ["cline"]

class ClineTelegramBot:
    def __init__(self):
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

    def start_pty_session(self):
        """Start PTY session with background output reader"""
        try:
            self.master_fd, self.slave_fd = pty.openpty()

            self.process = subprocess.Popen(
                CLINE_COMMAND,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid,
                env=dict(os.environ, TERM='xterm-256color')
            )

            self.is_running = True
            self.session_active = True

            # Start background output reader
            self.stop_reading = False
            self.output_thread = threading.Thread(target=self._output_reader, daemon=True)
            self.output_thread.start()

            print("PTY session started with background reader")
            return True
        except Exception as e:
            print(f"Failed to start PTY session: {e}")
            return False

    def stop_pty_session(self):
        """Stop PTY session and cleanup"""
        self.stop_reading = True
        self.session_active = False

        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), 15)
                time.sleep(1)
                if self.process.poll() is None:
                    os.killpg(os.getpgid(self.process.pid), 9)
            except:
                pass

        if self.master_fd:
            os.close(self.master_fd)
        if self.slave_fd:
            os.close(self.slave_fd)

        self.is_running = False
        print("PTY session stopped")

    def _output_reader(self):
        """Background thread to continuously read PTY output"""
        while not self.stop_reading and self.is_running:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        output = data.decode('utf-8', errors='replace')
                        self._process_output(output)
                    else:
                        # EOF reached
                        break
                else:
                    time.sleep(0.05)
            except Exception as e:
                print(f"Output reader error: {e}")
                break

        print("Output reader thread stopped")

    def _process_output(self, output):
        """Process incoming output from Cline"""
        # Check for interactive prompts
        prompt_patterns = [
            r'\[y/N\]', r'\[Y/n\]', r'\(y/n\)', r'\(Y/N\)',
            r'Continue\?', r'Proceed\?', r'Are you sure\?',
            r'Enter .*:\s*$', r'Password:\s*$'
        ]

        for pattern in prompt_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                self.waiting_for_input = True
                self.input_prompt = output.strip()
                break

        # Add to output queue
        self.output_queue.append(output)

        # Keep queue size manageable
        if len(self.output_queue) > 100:
            self.output_queue.popleft()

    def send_command(self, command):
        """Send command to Cline"""
        if not self.is_running:
            return "Error: PTY session not running"

        try:
            # Reset input waiting state
            self.waiting_for_input = False
            self.input_prompt = ""

            os.write(self.master_fd, f"{command}\n".encode())
            self.current_command = command
            return "Command sent"
        except Exception as e:
            return f"Error sending command: {e}"

    def get_pending_output(self, max_length=4000):
        """Get accumulated output, formatted for Telegram"""
        if not self.output_queue:
            return None

        # Combine available output
        combined = ""
        while self.output_queue and len(combined) < max_length:
            chunk = self.output_queue.popleft()
            if len(combined + chunk) > max_length:
                # Put back the chunk that would exceed limit
                self.output_queue.appendleft(chunk)
                break
            combined += chunk

        return combined.strip() if combined else None

    def is_waiting_for_input(self):
        """Check if Cline is waiting for user input"""
        return self.waiting_for_input

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming Telegram messages"""
        user_id = update.effective_user.id

        if user_id != AUTHORIZED_USER_ID:
            await update.message.reply_text("❌ Unauthorized access")
            return

        message_text = update.message.text.strip()

        # Special commands
        if message_text == "/start":
            if not self.session_active:
                if self.start_pty_session():
                    await update.message.reply_text("✅ Cline session started\n\nUse commands like:\n• `show me the current directory`\n• `edit file.py`\n• `git status`")
                else:
                    await update.message.reply_text("❌ Failed to start Cline session")
            else:
                await update.message.reply_text("ℹ️ Cline session already running")
            return

        if message_text == "/stop":
            self.stop_pty_session()
            await update.message.reply_text("🛑 Cline session stopped")
            return

        if message_text == "/status":
            status = "🟢 Running" if self.session_active else "🔴 Stopped"
            waiting = " (waiting for input)" if self.is_waiting_for_input() else ""
            await update.message.reply_text(f"Status: {status}{waiting}")
            return

        # Handle interactive input
        if self.is_waiting_for_input():
            # Send user input directly
            result = self.send_command(message_text)
            await update.message.reply_text(f"📤 Input sent: {message_text}")

            # Wait a moment for response
            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            if output:
                await update.message.reply_text(f"```\n{output}\n```")
            return

        # Regular commands
        if self.session_active:
            result = self.send_command(message_text)
            await update.message.reply_text(f"📤 {result}")

            # For long-running tasks, acknowledge and let background reader handle output
            if any(keyword in message_text.lower() for keyword in ['run', 'build', 'install', 'download', 'clone']):
                await update.message.reply_text("⏳ Long-running task started. Output will be sent as it becomes available...")

            # Check for immediate output
            await asyncio.sleep(0.5)
            output = self.get_pending_output()
            if output:
                # Send output in chunks if needed
                chunks = [output[i:i+4000] for i in range(0, len(output), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(f"```\n{chunk}\n```")
            else:
                await update.message.reply_text("ℹ️ Command sent. Waiting for output...")
        else:
            await update.message.reply_text("❌ Cline session not running. Use /start first")

async def output_monitor(bot_instance, application):
    """Monitor for new output and send to user"""
    while True:
        if bot_instance.session_active and bot_instance.output_queue:
            output = bot_instance.get_pending_output()
            if output:
                try:
                    # Send output to authorized user
                    await application.bot.send_message(
                        chat_id=AUTHORIZED_USER_ID,
                        text=f"```\n{output}\n```",
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    print(f"Error sending output: {e}")

        await asyncio.sleep(2)  # Check every 2 seconds

def main():
    bot = ClineTelegramBot()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", bot.handle_message))
    application.add_handler(CommandHandler("stop", bot.handle_message))
    application.add_handler(CommandHandler("status", bot.handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    # Start output monitoring task
    loop = asyncio.get_event_loop()
    loop.create_task(output_monitor(bot, application))

    print("Bot started with long-running task support")
    application.run_polling()

if __name__ == "__main__":
    main()