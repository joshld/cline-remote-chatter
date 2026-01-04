"""
Unit tests for cutesy_agent_router_refactor.py
Tests the multi-agent, multi-chat architecture
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Dict, Any, Optional

# Import the refactored module
import cutesy_agent_router_refactor as car

# Agent configuration matching the main function
TEST_AGENT_CONFIG = {
    "cline": {
        "command": ["cline"],
        "name": "Cline",
        "welcome_keywords": ["cline cli"],
        "mode_keywords": ["switch to plan", "switch to act", "plan mode", "act mode"],
        "command_timeout": 30.0,
    },
    "codex-cli": {
        "command": ["codex"],
        "name": "Codex CLI",
        "command_timeout": 45.0,
    },
    "codex-api": {
        "api_url": "http://localhost:8000/v1/messages",
        "name": "Codex API",
        "command_timeout": 60.0,
    },
    "claude-api": {
        "api_key": "test-anthropic-key",
        "model": "claude-3-5-sonnet-20241022",
        "name": "Claude API",
        "command_timeout": 45.0,
    },
    "openai-api": {
        "api_key": "test-openai-key",
        "model": "gpt-4-turbo",
        "name": "OpenAI",
        "command_timeout": 45.0,
    },
}


class TestMessageTypes:
    """Test Message and MessageType classes"""

    def test_message_type_enum_values(self):
        """Test that MessageType has expected values"""
        assert car.MessageType.USER_INPUT.value == "user_input"
        assert car.MessageType.AGENT_OUTPUT.value == "agent_output"
        assert car.MessageType.COMMAND.value == "command"
        assert car.MessageType.ERROR.value == "error"
        assert car.MessageType.TOOL_CALL.value == "tool_call"
        assert car.MessageType.TOOL_RESULT.value == "tool_result"

    def test_message_creation(self):
        """Test Message object creation and properties"""
        msg = car.Message(
            type=car.MessageType.USER_INPUT,
            content="Hello world",
            sender="user123",
            metadata={"timestamp": "2024-01-01"}
        )

        assert msg.type == car.MessageType.USER_INPUT
        assert msg.content == "Hello world"
        assert msg.sender == "user123"
        assert msg.metadata == {"timestamp": "2024-01-01"}

    def test_message_default_metadata(self):
        """Test Message with default metadata"""
        msg = car.Message(
            type=car.MessageType.COMMAND,
            content="/start",
            sender="user456"
        )

        assert msg.metadata == {}


class TestAgentInterface:
    """Test the AgentInterface abstract base class"""

    def test_agent_interface_is_abstract(self):
        """Test that AgentInterface cannot be instantiated directly"""
        with pytest.raises(TypeError):
            car.AgentInterface({})

    def test_agent_interface_abstract_methods(self):
        """Test that AgentInterface methods are implemented in concrete classes"""
        # PTYAgent should have concrete implementations of abstract methods
        agent = car.PTYAgent({"command": ["test"]})

        # These should not raise NotImplementedError (concrete implementations)
        result = asyncio.run(agent.get_custom_commands())
        assert isinstance(result, dict)

        result = asyncio.run(agent.get_custom_help())
        assert isinstance(result, str)

        result = asyncio.run(agent.handle_custom_command("/test", ""))
        assert result is None


class TestPTYAgent:
    """Test PTY-based agent implementation"""

    def test_pty_agent_initialization(self):
        """Test PTYAgent constructor"""
        config = {
            "command": ["python3", "-c", "print('hello')"],
            "name": "TestAgent",
            "welcome_keywords": ["ready"],
            "command_timeout": 30.0
        }

        agent = car.PTYAgent(config)

        assert agent.command == ["python3", "-c", "print('hello')"]
        assert agent.name == "TestAgent"
        assert agent.command_timeout == 30.0
        assert not agent.is_running_flag

    @patch('cutesy_agent_router_refactor.pty.openpty')
    @patch('cutesy_agent_router_refactor.os.write')
    @patch('cutesy_agent_router_refactor.subprocess.Popen')
    def test_pty_agent_start(self, mock_popen, mock_write, mock_openpty):
        """Test PTYAgent start method"""
        mock_openpty.return_value = (10, 11)  # master_fd, slave_fd
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process is still running
        mock_popen.return_value = mock_process

        agent = car.PTYAgent({"command": ["sleep", "10"]})  # Long-running command

        result = asyncio.run(agent.start())

        assert result is True
        assert agent.is_running_flag is True
        assert agent.master_fd == 10
        assert agent.process == mock_process
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_pty_agent_send_command(self):
        """Test PTYAgent send_command method"""
        agent = car.PTYAgent({"command": ["test"]})

        # Should return error when not running
        result = await agent.send_command("test command")
        assert "not running" in result

        # Mock running state
        agent.is_running_flag = True
        agent.master_fd = 5

        with patch('cutesy_agent_router_refactor.os.write', return_value=12) as mock_write:
            result = await agent.send_command("test command")
            assert result == "Command sent"  # Success
            mock_write.assert_called_with(5, b"test command\r\n")

    def test_pty_agent_get_output(self):
        """Test PTYAgent get_output method"""
        agent = car.PTYAgent({"command": ["test"]})

        # Should return None when not running
        result = asyncio.run(agent.get_output())
        assert result is None

        # Mock running state with output
        agent.is_running_flag = True
        agent.master_fd = 5
        from collections import deque
        agent.output_queue = deque(["Hello"])

        result = asyncio.run(agent.get_output())
        assert result is not None
        assert result.content == "Hello"


class TestClineAgent:
    """Test ClineAgent specific functionality"""

    def test_cline_agent_initialization(self):
        """Test ClineAgent constructor"""
        config = TEST_AGENT_CONFIG["cline"]
        agent = car.ClineAgent(config)

        assert agent.command == ["cline"]
        assert agent.name == "Cline"
        # Check that config is properly stored
        assert agent.config["welcome_keywords"] == ["cline cli"]
        assert agent.config["mode_keywords"] == ["switch to plan", "switch to act", "plan mode", "act mode"]

    def test_cline_agent_custom_commands(self):
        """Test ClineAgent custom commands"""
        agent = car.ClineAgent({"command": ["cline"]})

        commands = asyncio.run(agent.get_custom_commands())

        assert "/plan" in commands
        assert "/act" in commands
        assert "plan mode" in commands["/plan"]

    def test_cline_agent_custom_help(self):
        """Test ClineAgent custom help"""
        agent = car.ClineAgent(TEST_AGENT_CONFIG["cline"])

        help_text = asyncio.run(agent.get_custom_help())

        assert "Usage Examples:" in help_text
        assert "Show me all Python files" in help_text
        assert "Cline will execute commands" in help_text

    @patch('cutesy_agent_router_refactor.PTYAgent.send_command')
    @patch('cutesy_agent_router_refactor.PTYAgent.get_output')
    def test_cline_agent_handle_plan_command(self, mock_get_output, mock_send_command):
        """Test ClineAgent /plan command handling"""
        agent = car.ClineAgent({"command": ["cline"]})

        mock_send_command.return_value = "Switched to plan mode"
        mock_output = MagicMock()
        mock_output.content = "Plan mode active"
        mock_get_output.return_value = mock_output

        result = asyncio.run(agent.handle_custom_command("/plan", ""))

        assert result is not None
        assert "Plan Mode" in result
        mock_send_command.assert_called_with("/plan")

    @patch('cutesy_agent_router_refactor.PTYAgent.send_command')
    @patch('cutesy_agent_router_refactor.PTYAgent.get_output')
    def test_cline_agent_handle_act_command(self, mock_get_output, mock_send_command):
        """Test ClineAgent /act command handling"""
        agent = car.ClineAgent({"command": ["cline"]})

        mock_send_command.return_value = "Switched to act mode"
        mock_output = MagicMock()
        mock_output.content = "Act mode active"
        mock_get_output.return_value = mock_output

        result = asyncio.run(agent.handle_custom_command("/act", ""))

        assert result is not None
        assert "Act Mode" in result
        mock_send_command.assert_called_with("/act")


class TestACPAgents:
    """Test Agent Client Protocol based agents"""

    def test_acp_agent_initialization(self):
        """Test ACPAgent constructor"""
        config = {
            "api_url": "http://localhost:8000/v1/messages",
            "name": "TestACP",
            "command_timeout": 60.0
        }

        agent = car.ACPAgent(config)

        assert agent.api_url == "http://localhost:8000/v1/messages"
        assert agent.name == "TestACP"
        assert agent.command_timeout == 60.0

    @patch('cutesy_agent_router_refactor.httpx.AsyncClient')
    def test_claude_api_agent_initialization(self, mock_client):
        """Test ClaudeAAPIAgent constructor"""
        config = {
            "api_key": "test-key",
            "model": "claude-3-sonnet"
        }

        agent = car.ClaudeAAPIAgent(config)

        assert agent.api_key == "test-key"
        assert agent.model == "claude-3-sonnet"
        assert agent.name == "Claude API"

    def test_claude_api_agent_initialization(self):
        """Test ClaudeAAPIAgent constructor - simplified test"""
        config = TEST_AGENT_CONFIG["claude-api"]
        agent = car.ClaudeAAPIAgent(config)

        assert agent.api_key == "test-anthropic-key"
        assert agent.model == "claude-3-5-sonnet-20241022"
        assert agent.name == "Claude API"


class TestChatServiceInterface:
    """Test ChatServiceInterface abstract base class"""

    def test_chat_service_interface_is_abstract(self):
        """Test that ChatServiceInterface cannot be instantiated directly"""
        with pytest.raises(TypeError):
            car.ChatServiceInterface({})

    def test_chat_service_interface_abstract_methods(self):
        """Test that ChatServiceInterface defines required abstract methods"""
        # Should have abstract methods
        methods = [method for method in dir(car.ChatServiceInterface) if not method.startswith('_')]
        assert 'set_message_handler' in methods
        assert 'send_message' in methods
        assert 'start' in methods
        assert 'stop' in methods


@pytest.mark.skipif(not car.DISCORD_AVAILABLE, reason="Discord.py not available")
class TestDiscordChatService:
    """Test Discord chat service implementation"""

    def test_discord_chat_service_initialization(self):
        """Test DiscordChatService constructor"""
        config = {
            "token": "discord-token",
            "authorized_user_id": "123456",
            "channel_id": "789"
        }

        service = car.DiscordChatService(config)

        assert service.token == "discord-token"
        assert service.authorized_user_id == "123456"
        assert service.channel_id == "789"

    @patch('cutesy_agent_router_refactor.discord.Intents.default')
    @patch('cutesy_agent_router_refactor.commands.Bot')
    def test_discord_chat_service_start(self, mock_bot_class, mock_intents):
        """Test DiscordChatService start method"""
        mock_bot = AsyncMock()
        mock_bot_class.return_value = mock_bot
        mock_bot.start = AsyncMock(side_effect=KeyboardInterrupt)  # Exit immediately

        service = car.DiscordChatService({
            "token": "test-token",
            "authorized_user_id": "123"
        })

        # Should be able to create and start service without error
        # The KeyboardInterrupt prevents the actual Discord connection
        try:
            asyncio.run(service.start())
        except KeyboardInterrupt:
            pass  # Expected

        # Verify bot was created
        mock_bot_class.assert_called_once()
        mock_bot.start.assert_called_once()


class TestAgentChatBridge:
    """Test AgentChatBridge functionality"""

    def test_agent_chat_bridge_initialization(self):
        """Test AgentChatBridge constructor"""
        mock_agent = MagicMock()
        mock_chat_service = MagicMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        assert bridge.agent == mock_agent
        assert bridge.chat_service == mock_chat_service
        assert bridge.user_id == "user123"
        assert bridge.custom_commands == {}
        assert bridge._rate_limit_ms == 500
        assert bridge._max_message_length == 10000

    @pytest.mark.asyncio
    async def test_agent_chat_bridge_send_message_telegram(self):
        """Test send_message with Telegram interface"""
        mock_agent = MagicMock()
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_app, "123")

        await bridge.send_message("456", "Hello")

        mock_app.bot.send_message.assert_called_once_with(
            chat_id=456, text="Hello"
        )

    def test_agent_chat_bridge_send_message_chat_service(self):
        """Test send_message with ChatServiceInterface"""
        mock_agent = MagicMock()
        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()
        # Mock as non-Telegram (no run_polling method)
        del mock_chat_service.run_polling

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        asyncio.run(bridge.send_message("user456", "Hello"))

        mock_chat_service.send_message.assert_called_once_with("user456", "Hello")

    def test_agent_chat_bridge_process_message_commands(self):
        """Test process_message with various commands"""
        mock_agent = MagicMock()
        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        # Test unauthorized user
        asyncio.run(bridge.process_message("/start", "wrong_user"))
        mock_chat_service.send_message.assert_called_with("wrong_user", "❌ Unauthorized")

    def test_agent_chat_bridge_start_command(self):
        """Test /start command handling"""
        mock_agent = MagicMock()
        mock_agent.is_running.return_value = False
        mock_agent.start = AsyncMock(return_value=True)
        mock_agent.name = "TestAgent"

        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        asyncio.run(bridge.process_message("/start", "user123"))

        mock_agent.start.assert_called_once()
        mock_chat_service.send_message.assert_called()

    @pytest.mark.asyncio
    async def test_agent_chat_bridge_stop_command(self):
        """Test /stop command handling"""
        mock_agent = AsyncMock()
        mock_agent.is_running.return_value = True
        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        await bridge.process_message("/stop", "user123")

        mock_agent.stop.assert_called_once()
        mock_chat_service.send_message.assert_called_with("user123", "🛑 Agent stopped")

    def test_agent_chat_bridge_status_command(self):
        """Test /status command handling"""
        mock_agent = MagicMock()
        mock_agent.is_running.return_value = True
        mock_agent.waiting_for_input = False
        mock_agent.name = "TestAgent"

        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        asyncio.run(bridge.process_message("/status", "user123"))

        mock_chat_service.send_message.assert_called_with(
            "user123", "Status: 🟢 Running\nAgent: TestAgent"
        )

    @pytest.mark.asyncio
    async def test_agent_chat_bridge_regular_message(self):
        """Test regular message handling"""
        mock_agent = AsyncMock()
        mock_agent.is_running.return_value = True
        mock_agent.send_command = AsyncMock(return_value=None)
        mock_agent.get_output = AsyncMock(return_value=MagicMock(content="Agent response"))
        mock_agent.command_timeout = 30.0

        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")

        await bridge.process_message("Hello agent", "user123")

        mock_agent.send_command.assert_called_once_with("Hello agent")
        mock_chat_service.send_message.assert_has_calls([
            call("user123", "📤 Message sent..."),
            call("user123", "Agent response")
        ])

    def test_agent_chat_bridge_message_size_limit(self):
        """Test message size limiting"""
        mock_agent = MagicMock()
        mock_agent.is_running.return_value = True

        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")
        bridge._max_message_length = 100

        long_message = "x" * 101
        asyncio.run(bridge.process_message(long_message, "user123"))

        mock_chat_service.send_message.assert_called_with(
            "user123", "❌ Message too long (max 100 characters)"
        )
        mock_agent.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_chat_bridge_rate_limiting(self):
        """Test rate limiting functionality"""
        mock_agent = AsyncMock()
        mock_agent.is_running.return_value = True
        mock_agent.send_command = AsyncMock(return_value=None)
        mock_agent.get_output = AsyncMock(return_value=MagicMock(content="Response"))
        mock_agent.command_timeout = 30.0

        mock_chat_service = MagicMock()
        mock_chat_service.send_message = AsyncMock()

        bridge = car.AgentChatBridge(mock_agent, mock_chat_service, "user123")
        bridge._rate_limit_ms = 1000  # 1 second

        # First message should go through
        await bridge.process_message("First message", "user123")

        # Immediate second message should be rate limited
        await bridge.process_message("Second message", "user123")

        # Should have been rate limited
        assert mock_chat_service.send_message.call_count >= 2
        rate_limited_calls = [call for call in mock_chat_service.send_message.call_args_list
                            if "Please wait" in str(call)]
        assert len(rate_limited_calls) > 0


class TestConfiguration:
    """Test configuration and factory functions"""

    def test_create_agent_cline(self):
        """Test create_agent factory for Cline"""
        agent = car.create_agent("cline", TEST_AGENT_CONFIG["cline"])

        assert isinstance(agent, car.ClineAgent)
        assert agent.name == "Cline"

    def test_create_agent_codex_cli(self):
        """Test create_agent factory for Codex CLI"""
        agent = car.create_agent("codex-cli", TEST_AGENT_CONFIG["codex-cli"])

        assert isinstance(agent, car.CodexCLIAgent)
        assert agent.name == "Codex CLI"

    def test_create_agent_claude_api(self):
        """Test create_agent factory for Claude API"""
        config = TEST_AGENT_CONFIG["claude-api"].copy()

        agent = car.create_agent("claude-api", config)

        assert isinstance(agent, car.ClaudeAAPIAgent)
        assert agent.name == "Claude API"

    def test_create_agent_openai_api(self):
        """Test create_agent factory for OpenAI API"""
        config = TEST_AGENT_CONFIG["openai-api"].copy()

        agent = car.create_agent("openai-api", config)

        assert isinstance(agent, car.OpenAIAPIAgent)
        assert agent.name == "OpenAI"

    def test_create_agent_unknown_type(self):
        """Test create_agent with unknown agent type"""
        with pytest.raises(ValueError):
            car.create_agent("unknown-agent", {})


class TestMainFunction:
    """Test main function and environment handling"""

    @patch.dict(os.environ, {
        "CHAT_SERVICE": "telegram",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "AUTHORIZED_USER_ID": "123",
        "AGENT_TYPE": "cline"
    })
    @patch('cutesy_agent_router_refactor.Application.builder')
    @patch('cutesy_agent_router_refactor.AgentChatBridge')
    def test_main_telegram_mode(self, mock_bridge, mock_builder):
        """Test main function with Telegram configuration"""
        mock_app = MagicMock()
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        mock_app.run_polling = MagicMock(side_effect=KeyboardInterrupt)  # Exit immediately

        # Should not raise exception during setup
        car.main()

        # Verify components were created
        mock_builder.assert_called_once()
        mock_bridge.assert_called_once()
        mock_app.run_polling.assert_called_once()

    @patch.dict(os.environ, {
        "CHAT_SERVICE": "discord",
        "DISCORD_BOT_TOKEN": "discord-token",
        "AUTHORIZED_USER_ID": "123",
        "AGENT_TYPE": "cline"
    })
    @patch('cutesy_agent_router_refactor.DiscordChatService')
    @patch('cutesy_agent_router_refactor.AgentChatBridge')
    @patch('asyncio.run')
    def test_main_discord_mode(self, mock_asyncio_run, mock_bridge, mock_discord_service):
        """Test main function with Discord configuration"""
        mock_service = MagicMock()
        mock_discord_service.return_value = mock_service
        mock_service.start = AsyncMock(side_effect=KeyboardInterrupt)  # Exit immediately

        # Should not raise exception
        car.main()

        # Verify components were created
        mock_discord_service.assert_called_once()
        mock_bridge.assert_called_once()
        mock_asyncio_run.assert_called_once()

    @patch.dict(os.environ, {"CHAT_SERVICE": "unknown", "AGENT_TYPE": "cline"})
    def test_main_unknown_chat_service(self):
        """Test main function with unknown chat service"""
        # Should print error and exit
        with pytest.raises(SystemExit):
            car.main()

    @patch.dict(os.environ, {"CHAT_SERVICE": "telegram", "AGENT_TYPE": "cline"})
    def test_main_missing_telegram_token(self):
        """Test main function with missing Telegram token"""
        # Should print error and exit
        with pytest.raises(SystemExit):
            car.main()


class TestIntegrationScenarios:
    """Integration tests for complete workflows"""

    @pytest.mark.asyncio
    async def test_full_telegram_workflow(self):
        """Test complete Telegram workflow"""
        # Mock Telegram components
        mock_update = MagicMock()
        mock_update.effective_user.id = 123
        mock_update.message.text = "/start"
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()

        # Create real agent and bridge
        agent = car.ClineAgent({"command": ["echo", "test"]})
        bridge = car.AgentChatBridge(agent, mock_update._mock_name, "123")

        # Test command handling
        await bridge.handle_command(mock_update, mock_context)

        # Verify response was sent
        mock_update.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_agent_switching_workflow(self):
        """Test switching between different agents"""
        # Test that different agent types can be created
        agents = []

        # PTY agents
        agents.append(car.create_agent("cline", TEST_AGENT_CONFIG["cline"]))
        agents.append(car.create_agent("codex-cli", TEST_AGENT_CONFIG["codex-cli"]))

        # API agents
        agents.append(car.create_agent("claude-api", TEST_AGENT_CONFIG["claude-api"]))
        agents.append(car.create_agent("openai-api", TEST_AGENT_CONFIG["openai-api"]))

        # All should be valid agent instances
        for agent in agents:
            assert hasattr(agent, 'start')
            assert hasattr(agent, 'stop')
            assert hasattr(agent, 'send_command')
            assert hasattr(agent, 'get_output')


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])