"""
Additional unit tests for ClineTelegramBot
Covers areas not tested in the main test suite
"""

import asyncio  # noqa: F401
import os  # noqa: F401
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cline_telegram_bot import ClineTelegramBot, strip_ansi_codes


class TestPromptDetection:
    """Test various prompt detection patterns"""

    def test_detects_parenthesis_prompts(self):
        """Test detection of (y/n) style prompts at end of line"""
        test_cases = [
            ("Continue? (y/n)", True),
            ("Continue? (Y/N)", True),
            ("(y/n)", True),
            ("Choose (y/n) wisely", False),  # Not at end
        ]

        for input_text, should_detect in test_cases:
            bot = ClineTelegramBot()
            bot._process_output(input_text)
            assert bot.waiting_for_input == should_detect, \
                f"Failed for: {input_text}"

    def test_detects_question_prompts(self):
        """Test detection of question-style prompts"""
        test_cases = [
            ("Continue?", True),
            ("Proceed?", True),
            ("Are you sure?", True),
            ("Continue? And more text", False),  # Not at end
        ]

        for input_text, should_detect in test_cases:
            bot = ClineTelegramBot()
            bot._process_output(input_text)
            assert bot.waiting_for_input == should_detect, \
                f"Failed for: {input_text}"

    def test_detects_input_prompts(self):
        """Test detection of input-style prompts"""
        test_cases = [
            ("Password: ", True),
            ("Enter your name: ", True),
            ("Enter something: ", True),
            ("Password: in the text", False),  # Not at end
        ]

        for input_text, should_detect in test_cases:
            bot = ClineTelegramBot()
            bot._process_output(input_text)
            assert bot.waiting_for_input == should_detect, \
                f"Failed for: {input_text}"

    def test_detects_action_prompts(self):
        """Test detection of action-style prompts"""
        test_cases = [
            ("Press Enter to continue", True),
            ("Press any key", True),
            ("Press Enter to continue and more", False),  # Not at end
        ]

        for input_text, should_detect in test_cases:
            bot = ClineTelegramBot()
            bot._process_output(input_text)
            assert bot.waiting_for_input == should_detect, \
                f"Failed for: {input_text}"

    def test_detects_yes_no_bracket_prompts(self):
        """UPDATED: Prompts must be at end of line"""
        test_cases = [
            ("Continue? [y/N]", True),      # ✅ Detects - at end
            ("Continue? [y/N] ", True),     # ✅ Detects - trailing space ok
            ("[y/N] options", False),       # ✅ NOT detected - not at end
            ("Choose [y/N] now", False),    # ✅ NOT detected - not at end
        ]

        for input_text, should_detect in test_cases:
            bot = ClineTelegramBot()
            bot._process_output(input_text)
            assert bot.waiting_for_input == should_detect, \
                f"Failed for: {input_text} (expected {should_detect})"

    def test_detects_prompts_not_in_middle(self):
        """Test that prompts in middle of content don't trigger"""
        bot = ClineTelegramBot()

        # Prompt in middle - should NOT trigger
        bot._process_output("This explains [y/N] but doesn't ask")
        assert bot.waiting_for_input is False

        # Prompt at end - should trigger
        bot2 = ClineTelegramBot()
        bot2._process_output("This explains something - [y/N]")
        assert bot2.waiting_for_input is True

    def test_prompt_not_detected_in_content(self):
        """Test that prompts in middle of content don't trigger waiting state"""
        bot = ClineTelegramBot()

        bot._process_output("This is content about [y/N] options")
        # Should NOT detect because [y/N] is in middle of content, not at end
        assert bot.waiting_for_input is False

    def test_input_prompt_stored_correctly(self):
        """Test that the detected prompt is stored correctly"""
        bot = ClineTelegramBot()
        input_text = "Do something? [Y/n] "
        expected_prompt = input_text.strip()  # .strip() removes trailing space

        bot._process_output(input_text)
        assert bot.input_prompt == expected_prompt


class TestUIFiltering:
    """Test UI element filtering"""

    def test_filters_single_box_character(self):
        """Test filtering of single box characters"""
        # Only these characters are actually filtered by the current logic
        filtered_chars = ["╭", "╰", "│", "┃", "╮", "╯"]

        for char in filtered_chars:
            bot = ClineTelegramBot()
            bot._process_output(char)
            # Single box chars should be filtered
            assert len(bot.output_queue) == 0, f"Failed to filter: {char}"

    def test_filters_box_lines(self):
        """Test filtering of pure box character lines"""
        bot = ClineTelegramBot()

        # This line has length 13 > 3, so it doesn't get filtered
        bot._process_output("╭───────────╮")
        assert len(bot.output_queue) == 1  # Not filtered

        # But short box lines do get filtered
        bot2 = ClineTelegramBot()
        bot2._process_output("│││")  # length 3
        assert len(bot2.output_queue) == 0  # Filtered

    def test_filters_multiple_box_chars(self):
        """Test filtering of multiple box characters"""
        bot = ClineTelegramBot()

        # This has length 6 > 3, so it doesn't get filtered
        bot._process_output("╭╰│┃╮╯")
        assert len(bot.output_queue) == 1  # Not filtered

        # But short sequences do get filtered
        bot2 = ClineTelegramBot()
        bot2._process_output("╭╰│")  # length 3
        assert len(bot2.output_queue) == 0  # Filtered

    def test_keeps_box_with_text(self):
        """Test that box characters with text are kept"""
        bot = ClineTelegramBot()

        bot._process_output("┃ Important information ┃")
        # Should keep because it has text content
        assert len(bot.output_queue) > 0


class TestLongRunningCommands:
    """Test handling of long-running commands"""

    def test_detect_long_running_keywords(self):
        """Test identification of long-running command keywords"""
        keywords = ["run", "build", "install", "download", "clone", "test", "compile"]

        # This would be used in the handle_message for visual feedback
        for keyword in keywords:
            command = f"please {keyword} the project"
            has_keyword = any(kw in command.lower() for kw in keywords)
            assert has_keyword, f"Failed to detect: {keyword}"


class TestProcessOutputRobustness:
    """Test _process_output robustness with various inputs"""

    def test_handles_very_long_output(self):
        """Test processing of very long output lines"""
        bot = ClineTelegramBot()

        long_output = "x" * 10000
        bot._process_output(long_output)

        # Should not crash and should queue the output
        assert len(bot.output_queue) > 0

    def test_handles_unicode_output(self):
        """Test processing of unicode content"""
        bot = ClineTelegramBot()

        unicode_output = "Hello 世界 🌍 مرحبا"
        bot._process_output(unicode_output)

        assert len(bot.output_queue) > 0
        assert "世界" in bot.output_queue[0]

    def test_handles_mixed_line_endings(self):
        """Test processing of output with mixed line endings"""
        bot = ClineTelegramBot()

        bot._process_output("Line1\nLine2\r\nLine3\rLine4")

        assert len(bot.output_queue) > 0

    def test_handles_null_bytes(self):
        """Test processing of output with null bytes"""
        bot = ClineTelegramBot()

        # This might come from binary data in the output
        output_with_null = "valid\x00invalid"

        # Should not crash
        try:
            bot._process_output(output_with_null)
            assert True
        except Exception as e:
            pytest.fail(f"Failed to handle null bytes: {e}")

    def test_handles_control_characters(self):
        """Test processing of control characters"""
        bot = ClineTelegramBot()

        output_with_control = "text\x01\x02\x03more"
        bot._process_output(output_with_control)

        # Should not crash
        assert True


class TestStateTransitions:
    """Test state transitions and consistency"""

    def test_waiting_for_input_transitions(self):
        """Test transitions of waiting_for_input flag"""
        bot = ClineTelegramBot()

        assert bot.waiting_for_input is False

        # Transition to waiting
        bot._process_output("Enter value: ")
        assert bot.waiting_for_input is True

        # Reset on command send
        bot.is_running = True
        bot.master_fd = 99
        with patch("os.write", return_value=5):
            bot.send_command("test")
        assert bot.waiting_for_input is False

    def test_session_state_consistency(self):
        """Test that session state remains consistent"""
        bot = ClineTelegramBot()

        # Start state
        assert bot.session_active is False
        assert bot.is_running is False

        # Both should be set together
        bot.session_active = True
        bot.is_running = True

        assert bot.session_active is True
        assert bot.is_running is True

    def test_lock_state_under_concurrent_access(self):
        """Test that locks maintain state consistency under concurrent access"""
        bot = ClineTelegramBot()
        final_values = []

        def increment_and_read():
            for _ in range(10):
                with bot.state_lock:
                    # Simulate state change and read
                    bot.session_active = not bot.session_active
                    final_values.append(bot.session_active)

        threads = [threading.Thread(target=increment_and_read) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have collected all values without corruption
        assert len(final_values) == 50


class TestErrorRecovery:
    """Test error recovery mechanisms"""

    def test_stale_prompt_timeout_reset(self):
        """Test that stale prompts are reset after timeout"""
        bot = ClineTelegramBot()
        bot.is_running = True
        bot.master_fd = 99
        bot.waiting_for_input = True
        bot.last_prompt_time = time.time() - 35  # 35 seconds ago

        with patch("os.write", return_value=5):
            bot.send_command("test")

        # Should reset stale state
        assert bot.waiting_for_input is False
        assert bot.input_prompt == ""

    def test_output_queue_recovery_from_overflow(self):
        """Test recovery after queue overflow"""
        bot = ClineTelegramBot()

        # Fill queue past limit
        for i in range(150):
            bot._process_output(f"Message {i}")

        # Queue should be capped at 100
        assert len(bot.output_queue) <= 100

        # Should still be able to retrieve output
        output = bot.get_pending_output()
        assert output is not None

    def test_process_survives_bad_file_descriptor(self):
        """Test handling of bad file descriptor"""
        bot = ClineTelegramBot()
        bot.is_running = True
        bot.master_fd = -1  # Invalid FD

        result = bot.send_command("test")

        # Should return error, not crash
        assert "Error" in result


class TestOutputRetrievalEdgeCases:
    """Test edge cases in output retrieval"""

    def test_get_pending_output_multiple_calls(self):
        """Test multiple consecutive calls to get_pending_output"""
        bot = ClineTelegramBot()

        bot._process_output("Message 1")
        bot._process_output("Message 2")
        bot._process_output("Message 3")

        # First call
        output1 = bot.get_pending_output()
        assert output1 is not None

        # Second call (queue empty)
        output2 = bot.get_pending_output()
        assert output2 is None

    def test_get_pending_output_exact_max_length(self):
        """Test get_pending_output when content exactly matches max_length"""
        bot = ClineTelegramBot()

        exact_length = "x" * 1000
        bot.output_queue.append(exact_length)

        result = bot.get_pending_output(max_length=1000)

        assert result == exact_length
        assert len(bot.output_queue) == 0

    def test_get_pending_output_one_byte_over_limit(self):
        """Test get_pending_output when chunk is larger than limit"""
        bot = ClineTelegramBot()

        bot.output_queue.append("x" * 1001)

        result = bot.get_pending_output(max_length=1000)

        # Chunk is too large, so nothing is returned and chunk stays in queue
        assert result is None
        assert len(bot.output_queue) == 1


class TestAnsiCodeEdgeCases:
    """Test ANSI code stripping edge cases"""

    def test_strips_256_color_codes(self):
        """Test stripping of 256-color ANSI codes"""
        colored = "\x1b[38;5;196mRed\x1b[0m"
        result = strip_ansi_codes(colored)
        assert result == "Red"

    def test_strips_rgb_color_codes(self):
        """Test stripping of RGB ANSI codes"""
        colored = "\x1b[38;2;255;0;0mRed\x1b[0m"
        result = strip_ansi_codes(colored)
        assert result == "Red"

    def test_strips_multiple_sequential_codes(self):
        """Test stripping of multiple sequential codes"""
        colored = "\x1b[1m\x1b[32m\x1b[4mBold Green Underline\x1b[0m"
        result = strip_ansi_codes(colored)
        assert result == "Bold Green Underline"

    def test_preserves_text_between_codes(self):
        """Test that text between codes is preserved"""
        colored = "\x1b[32mGreen\x1b[0m Normal \x1b[31mRed\x1b[0m"
        result = strip_ansi_codes(colored)
        assert "Green" in result
        assert "Normal" in result
        assert "Red" in result


class TestConcurrencyStress:
    """Stress tests for concurrent operations"""

    def test_high_frequency_output_processing(self):
        """Test handling of high-frequency output"""
        bot = ClineTelegramBot()
        errors = []

        def rapid_output():
            try:
                for i in range(1000):
                    bot._process_output(f"Output {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=rapid_output) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Queue should have some output (capped at 100)
        with bot.output_queue_lock:
            assert len(bot.output_queue) > 0

    def test_interleaved_read_write(self):
        """Test interleaved reads and writes"""
        bot = ClineTelegramBot()
        read_count = [0]
        errors = []

        def writer():
            try:
                for i in range(100):
                    bot._process_output(f"msg_{i}")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    output = bot.get_pending_output()
                    if output:
                        read_count[0] += 1
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.asyncio
class TestAsyncIntegration:
    """Test async/await patterns"""

    async def test_send_message_error_handling(self):
        """Test error handling in _send_message"""
        bot = ClineTelegramBot()
        bot.application = MagicMock()
        bot.application.bot.send_message = AsyncMock(side_effect=Exception("Network error"))

        # Should not raise, should handle internally
        try:
            await bot._send_message(123, "test")
            assert True
        except Exception as e:
            pytest.fail(f"_send_message raised: {e}")

    async def test_send_notification_formats_correctly(self):
        """Test that notifications are formatted correctly"""
        bot = ClineTelegramBot()
        bot.application = MagicMock()
        bot.application.bot.send_message = AsyncMock()

        await bot._send_notification(123, "Test message", "success", "error")

        bot.application.bot.send_message.assert_called_once()


class TestMemoryManagement:
    """Test memory usage and cleanup"""

    def test_output_queue_memory_bounded(self):
        """Test that output queue memory usage is bounded"""
        bot = ClineTelegramBot()

        # Add lots of output
        for i in range(10000):
            bot._process_output("x" * 100)

        # Queue should still be at max 100 items
        with bot.output_queue_lock:
            assert len(bot.output_queue) <= 100

    def test_cleanup_resources_empties_queue(self):
        """Test that cleanup empties the queue"""
        bot = ClineTelegramBot()

        bot._process_output("test1")
        bot._process_output("test2")
        bot._process_output("test3")

        with bot.output_queue_lock:
            assert len(bot.output_queue) == 3

        bot._cleanup_resources()

        assert len(bot.output_queue) == 0

    def test_repeated_start_stop_no_leak(self):
        """Test that repeated start/stop doesn't leak resources"""
        # This is more of an integration test
        bot = ClineTelegramBot()

        for i in range(3):
            bot.session_active = True
            bot._process_output(f"Output {i}")

            with patch("cline_telegram_bot.ClineTelegramBot._kill_process_tree"):
                with patch("cline_telegram_bot.ClineTelegramBot._ensure_session_clean"):
                    bot.stop_pty_session()

            assert len(bot.output_queue) == 0
            assert bot.session_active is False

    def test_ui_ratio_calculation(self):
        """Test UI ratio calculation"""
        bot = ClineTelegramBot()
        bot._process_output("hello world")
        assert len(bot.output_queue) > 0  # Should be allowed (no UI elements)

        bot2 = ClineTelegramBot()
        bot2._process_output("╭")  # Single box character should be filtered
        assert len(bot2.output_queue) == 0  # Should be filtered (mostly empty UI)

    def test_ui_score_threshold(self):
        """Test UI score threshold"""
        bot = ClineTelegramBot()
        # Long informative message - should pass through _process_output
        # This tests that substantial content is preserved
        bot._process_output(
            "Here is a long informative message with lots of content " "that explains something useful about the project"
        )
        assert len(bot.output_queue) > 0  # Should be allowed (processed normally)

        bot2 = ClineTelegramBot()
        bot2._process_output("│││")  # Multiple box characters, short length - should be filtered
        assert len(bot2.output_queue) == 0  # Should be filtered (mostly empty UI)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
