import logging
import unittest
from unittest.mock import patch

from fix_my_claw.core import (
    AiConfig,
    AiProviderProbe,
    AppConfig,
    CmdResult,
    ConsoleFormatter,
    MonitorConfig,
    OpenClawConfig,
    UnsupportedOpenClawModeError,
    _build_ai_invocation,
    _ensure_supported_gateway_mode,
    _probe_ai_provider,
    _probe_effective_ok,
    _resolve_ai_provider_candidates,
)


class ProbeEffectiveOkTests(unittest.TestCase):
    def test_health_probe_uses_json_ok_flag(self) -> None:
        cmd = CmdResult(argv=["openclaw"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr="")
        self.assertFalse(_probe_effective_ok("health", cmd, {"ok": False}))

    def test_status_probe_requires_rpc_when_json_reports_failure(self) -> None:
        cmd = CmdResult(argv=["openclaw"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr="")
        self.assertFalse(_probe_effective_ok("status", cmd, {"rpc": {"ok": False}}))

    def test_status_probe_accepts_nested_health_flag(self) -> None:
        cmd = CmdResult(argv=["openclaw"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr="")
        self.assertTrue(_probe_effective_ok("status", cmd, {"rpc": {"ok": True}, "health": {"healthy": True}}))


class BuildAiInvocationTests(unittest.TestCase):
    def test_codex_provider_uses_stdin_prompt(self) -> None:
        cfg = AppConfig()

        argv, stdin_text = _build_ai_invocation(cfg, "repair prompt", code_stage=False)

        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(stdin_text, "repair prompt")

    def test_openclaw_provider_uses_agent_message_and_openclaw_command(self) -> None:
        cfg = AppConfig(
            monitor=MonitorConfig(),
            openclaw=OpenClawConfig(command="openclaw"),
            ai=AiConfig(provider="openclaw", local=True, command="codex", agent_id="main"),
        )

        argv, stdin_text = _build_ai_invocation(cfg, "repair prompt", code_stage=False)

        self.assertEqual(argv[:5], ["openclaw", "agent", "--json", "--local", "--agent"])
        self.assertIn("repair prompt", argv)
        self.assertIsNone(stdin_text)


class AiProviderSelectionTests(unittest.TestCase):
    def test_auto_provider_prefers_codex_then_openclaw(self) -> None:
        cfg = AppConfig(ai=AiConfig(provider="auto"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["codex", "openclaw"])

    def test_openclaw_provider_falls_back_to_codex(self) -> None:
        cfg = AppConfig(ai=AiConfig(provider="openclaw"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["openclaw", "codex"])

    def test_openclaw_probe_treats_expiring_auth_as_available(self) -> None:
        cfg = AppConfig()
        result = CmdResult(
            argv=["openclaw", "models", "status", "--check", "--json"],
            cwd=None,
            exit_code=2,
            duration_ms=1,
            stdout="{}",
            stderr="",
        )
        with patch("fix_my_claw.core.run_cmd", return_value=result):
            probe = _probe_ai_provider(cfg, "openclaw")

        self.assertIsInstance(probe, AiProviderProbe)
        self.assertTrue(probe.available)
        self.assertEqual(probe.reason, "models-status-expiring-auth")


class GatewayModeGuardTests(unittest.TestCase):
    def test_remote_mode_is_blocked_by_default(self) -> None:
        cfg = AppConfig(openclaw=OpenClawConfig(command="openclaw", allow_remote_mode=False))
        responses = [
            CmdResult(
                argv=["openclaw", "config", "get", "gateway.mode", "--json"],
                cwd=None,
                exit_code=0,
                duration_ms=1,
                stdout='"remote"\n',
                stderr="",
            ),
            CmdResult(
                argv=["openclaw", "config", "file"],
                cwd=None,
                exit_code=0,
                duration_ms=1,
                stdout="~/.openclaw/openclaw.json\n",
                stderr="",
            ),
        ]

        with patch("fix_my_claw.core.run_cmd", side_effect=responses):
            with self.assertRaises(UnsupportedOpenClawModeError):
                _ensure_supported_gateway_mode(cfg)

    def test_remote_mode_can_be_explicitly_allowed(self) -> None:
        cfg = AppConfig(openclaw=OpenClawConfig(command="openclaw", allow_remote_mode=True))

        with patch("fix_my_claw.core.run_cmd") as mocked_run_cmd:
            _ensure_supported_gateway_mode(cfg)

        mocked_run_cmd.assert_not_called()


class ConsoleFormatterTests(unittest.TestCase):
    def test_startup_logger_uses_start_lane_without_color_codes(self) -> None:
        formatter = ConsoleFormatter(use_color=False)
        record = logging.LogRecord(
            name="fix_my_claw.startup",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="mode=up config=/tmp/config.toml",
            args=(),
            exc_info=None,
        )

        rendered = formatter.format(record)

        self.assertIn(" | START  | mode=up config=/tmp/config.toml", rendered)

    def test_error_level_forces_error_lane(self) -> None:
        formatter = ConsoleFormatter(use_color=False)
        record = logging.LogRecord(
            name="fix_my_claw.repair",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="repair failed",
            args=(),
            exc_info=None,
        )

        rendered = formatter.format(record)

        self.assertIn(" | ERROR  | repair failed", rendered)


if __name__ == "__main__":
    unittest.main()
