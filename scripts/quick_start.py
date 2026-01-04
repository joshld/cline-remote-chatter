#!/usr/bin/env python3
"""
Quick daemon starter for Cline Telegram Bot
Usage: python3 quick_start.py
"""
import os
import sys
import time
import signal
import subprocess
from pathlib import Path

class BotDaemon:
    def __init__(self):
        self.project_root = Path(__file__).parent.absolute()
        self.bot_script = self.project_root / "../cline_telegram_bot.py"
        self.pid_file = self.project_root / "bot.pid"
        self.log_file = self.project_root / "bot.log"
        self.monitor_pid_file = self.project_root / "monitor.pid"
        self.monitor_log_file = self.project_root / "monitor.log"
        self.process = None

        # Monitoring configuration
        self.monitoring = False
        self.restart_count = 0
        self.max_restarts = 5
        self.restart_delay = 5  # seconds between restart attempts
        self.health_check_interval = 3  # seconds between health checks

    def is_running(self):
        """Check if bot is already running"""
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                return True
            except (OSError, ValueError):
                self.pid_file.unlink(missing_ok=True)
        return False

    def start(self):
        """Start the bot as a daemon"""
        if self.is_running():
            print("❌ Bot is already running!")
            return False

        print("🚀 Starting Cline Telegram Bot...")

        try:
            # Start the bot process
            self.process = subprocess.Popen(
                [sys.executable, str(self.bot_script)],
                cwd=self.project_root,
                stdout=open(self.log_file, 'a'),
                stderr=subprocess.STDOUT,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            # Save PID
            self.pid_file.write_text(str(self.process.pid))

            # Wait a moment to check if it started successfully
            time.sleep(2)

            if self.process.poll() is None:
                print(f"✅ Bot started successfully (PID: {self.process.pid})")
                print(f"📝 Log file: {self.log_file}")
                print("💡 Use 'python3 quick_start.py stop' to stop the bot")
                return True
            else:
                print("❌ Bot failed to start")
                print(f"📝 Check log file: {self.log_file}")
                return False

        except Exception as e:
            print(f"❌ Error starting bot: {e}")
            return False

    def stop(self):
        """Stop the bot"""
        if not self.is_running():
            print("❌ Bot is not running!")
            return False

        try:
            pid = int(self.pid_file.read_text().strip())
            print(f"🛑 Stopping bot (PID: {pid})...")

            # Try graceful shutdown first
            os.kill(pid, signal.SIGTERM)

            # Wait up to 10 seconds
            for _ in range(10):
                time.sleep(1)
                if not self.is_running():
                    break

            # Force kill if still running
            if self.is_running():
                print("💪 Force killing...")
                os.kill(pid, signal.SIGKILL)
                time.sleep(1)

            if not self.is_running():
                self.pid_file.unlink(missing_ok=True)
                print("✅ Bot stopped successfully")
                return True
            else:
                print("❌ Failed to stop bot")
                return False

        except Exception as e:
            print(f"❌ Error stopping bot: {e}")
            return False

    def status(self):
        """Check bot status"""
        if self.is_running():
            pid = int(self.pid_file.read_text().strip())
            print(f"✅ Bot is running (PID: {pid})")
            print(f"📝 Log file: {self.log_file}")
        else:
            print("❌ Bot is not running")

        # Show monitor status
        if self.is_monitor_running():
            monitor_pid = int(self.monitor_pid_file.read_text().strip())
            print(f"👁️  Monitor running (PID: {monitor_pid})")
            print(f"🔄 Auto-restart: enabled (max {self.max_restarts} attempts)")
            print(f"📝 Monitor log: {self.monitor_log_file}")
        else:
            print("👁️  No monitor running (manual management only)")

    def logs(self, follow=False):
        """Show bot logs"""
        if not self.log_file.exists():
            print("❌ No log file found")
            return

        if follow:
            print("📜 Following log file (Ctrl+C to stop)...")
            os.system(f"tail -f {self.log_file}")
        else:
            print("📜 Last 20 lines of bot log file:")
            os.system(f"tail -20 {self.log_file}")

    def monitor_logs(self, follow=False):
        """Show monitor logs"""
        if not self.monitor_log_file.exists():
            print("❌ No monitor log file found")
            return

        if follow:
            print("📜 Following monitor log file (Ctrl+C to stop)...")
            os.system(f"tail -f {self.monitor_log_file}")
        else:
            print("📜 Last 20 lines of monitor log file:")
            os.system(f"tail -20 {self.monitor_log_file}")

    def monitor(self):
        """Monitor bot process and restart if needed"""
        self.monitoring = True

        # Initialize monitor log
        with open(self.monitor_log_file, 'a') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 👁️ Monitor started (max_restarts={self.max_restarts}, check_interval={self.health_check_interval}s)\n")

        try:
            while self.monitoring:
                if not self.is_running():
                    self.restart_count += 1
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

                    with open(self.monitor_log_file, 'a') as f:
                        f.write(f"[{timestamp}] ⚠️ Bot not running, restarting... (attempt {self.restart_count}/{self.max_restarts})\n")

                    if self.restart_count > self.max_restarts:
                        with open(self.monitor_log_file, 'a') as f:
                            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Max restart attempts reached, stopping monitor\n")
                        break

                    if self.start():
                        with open(self.monitor_log_file, 'a') as f:
                            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Bot restarted successfully\n")
                        self.restart_count = 0  # Reset counter on success
                    else:
                        with open(self.monitor_log_file, 'a') as f:
                            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Failed to restart bot, will try again in {self.restart_delay}s\n")

                time.sleep(self.health_check_interval)

        except Exception as e:
            with open(self.monitor_log_file, 'a') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Monitor error: {e}\n")
        finally:
            self.monitoring = False
            with open(self.monitor_log_file, 'a') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 👁️ Monitor stopped\n")
            if self.monitor_pid_file.exists():
                self.monitor_pid_file.unlink(missing_ok=True)

    def daemonize(self):
        """Properly daemonize the process (Unix double-fork technique)"""
        # First fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent exits
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"Fork #1 failed: {e}\n")
            sys.exit(1)

        # Decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent exits
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"Fork #2 failed: {e}\n")
            sys.exit(1)

        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        with open(os.devnull, 'r') as dev_null:
            os.dup2(dev_null.fileno(), sys.stdin.fileno())
        with open(os.devnull, 'a') as dev_null:
            os.dup2(dev_null.fileno(), sys.stdout.fileno())
            os.dup2(dev_null.fileno(), sys.stderr.fileno())

    def start_monitor(self):
        """Start the monitoring daemon with auto-restart"""
        if self.is_monitor_running():
            print("❌ Monitor is already running!")
            return False

        print("🚀 Starting bot with auto-restart monitoring...")

        # Start the bot first
        if not self.start():
            return False

        # Double-fork to create proper daemon
        try:
            # First fork
            pid = os.fork()
            if pid > 0:
                # Parent waits briefly then exits
                time.sleep(0.1)
                print(f"✅ Monitor daemon started (PID: {pid})")
                print("💡 Monitor will survive terminal close")
                print("💡 Use 'python3 quick_start.py stop-monitor' to stop")
                return True

            # Child process - become daemon
            self.daemonize()

            # This code runs in the daemon process
            self.monitor_pid_file.write_text(str(os.getpid()))
            self.monitor()

        except Exception as e:
            print(f"❌ Failed to start monitor daemon: {e}")
            return False

    def stop_monitor(self):
        """Stop the monitoring daemon and bot"""
        if not self.is_monitor_running():
            print("❌ Monitor is not running!")
            return False

        try:
            monitor_pid = int(self.monitor_pid_file.read_text().strip())
            print(f"🛑 Stopping monitor (PID: {monitor_pid})...")
            os.kill(monitor_pid, signal.SIGTERM)

            # Wait for monitor to stop
            for _ in range(10):
                if not self.is_monitor_running():
                    break
                time.sleep(1)

            if self.is_monitor_running():
                print("💪 Force killing monitor...")
                os.kill(monitor_pid, signal.SIGKILL)

            # Also stop the bot
            self.stop()

            self.monitor_pid_file.unlink(missing_ok=True)
            print("✅ Monitor and bot stopped")
            return True

        except Exception as e:
            print(f"❌ Error stopping monitor: {e}")
            return False

    def is_monitor_running(self):
        """Check if monitor is running"""
        if self.monitor_pid_file.exists():
            try:
                pid = int(self.monitor_pid_file.read_text().strip())
                os.kill(pid, 0)
                return True
            except (OSError, ValueError):
                self.monitor_pid_file.unlink(missing_ok=True)
        return False

def main():
    daemon = BotDaemon()

    if len(sys.argv) < 2:
        command = "start"
    else:
        command = sys.argv[1].lower()

    if command == "start":
        daemon.start()
    elif command == "stop":
        daemon.stop()
    elif command == "restart":
        daemon.stop()
        time.sleep(2)
        daemon.start()
    elif command == "monitor":
        daemon.start_monitor()
    elif command == "stop-monitor":
        daemon.stop_monitor()
    elif command == "status":
        daemon.status()
    elif command == "logs":
        daemon.logs()
    elif command == "monitor-logs":
        daemon.monitor_logs()
    elif command == "tail":
        daemon.logs(follow=True)
    elif command == "monitor-tail":
        daemon.monitor_logs(follow=True)
    else:
        print("Usage: python3 quick_start.py {start|stop|restart|monitor|stop-monitor|status|logs|monitor-logs|tail|monitor-tail}")
        print("  start         - Start bot once (no monitoring)")
        print("  stop          - Stop bot")
        print("  restart       - Restart bot once")
        print("  monitor       - Start bot with auto-restart monitoring")
        print("  stop-monitor  - Stop monitoring and bot")
        print("  status        - Check bot and monitor status")
        print("  logs          - Show last 20 bot log lines")
        print("  monitor-logs  - Show last 20 monitor log lines")
        print("  tail          - Follow bot log file")
        print("  monitor-tail  - Follow monitor log file")

if __name__ == "__main__":
    main()