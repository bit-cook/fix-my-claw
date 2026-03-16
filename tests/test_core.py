import logging
import unittest
from unittest.mock import patch

from fix_my_claw.core import (
    AiConfig,
    AiProviderProbe,
    AppConfig,
    CapabilityCheck,
    CapabilityReport,
    CmdResult,
    ConsoleFormatter,
    MonitorConfig,
    OpenClawConfig,
    Probe,
    UnsupportedOpenClawModeError,
    _build_ai_invocation,
    _ensure_supported_gateway_mode,
    _probe_ai_capability,
    _probe_ai_provider,
    _probe_effective_ok,
    _render_probe_report,
    _resolve_ai_provider_candidates,
    _resolve_probe_ai_targets,
    run_probe,
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
    def test_ai_defaults_enabled(self) -> None:
        cfg = AiConfig()

        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.backend, "acpx")

    def test_codex_provider_uses_stdin_prompt(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="direct", provider="codex"))

        argv, stdin_text = _build_ai_invocation(cfg, "repair prompt", code_stage=False)

        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(stdin_text, "repair prompt")

    def test_openclaw_provider_uses_agent_message_and_openclaw_command(self) -> None:
        cfg = AppConfig(
            monitor=MonitorConfig(),
            openclaw=OpenClawConfig(command="openclaw"),
            ai=AiConfig(backend="direct", provider="openclaw", local=True, command="codex", agent_id="main"),
        )

        argv, stdin_text = _build_ai_invocation(cfg, "repair prompt", code_stage=False)

        self.assertEqual(argv[:5], ["openclaw", "agent", "--json", "--local", "--agent"])
        self.assertIn("repair prompt", argv)
        self.assertIsNone(stdin_text)

    def test_acpx_provider_uses_exec_with_stdin_prompt(self) -> None:
        cfg = AppConfig(
            ai=AiConfig(
                backend="acpx",
                provider="claude",
                acpx_command="acpx",
                acpx_permissions="approve-all",
                acpx_non_interactive_permissions="fail",
                acpx_format="json",
            )
        )

        argv, stdin_text = _build_ai_invocation(cfg, "repair prompt", code_stage=False)

        self.assertEqual(argv[0], "acpx")
        self.assertIn("--cwd", argv)
        self.assertIn("--approve-all", argv)
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertIn("--non-interactive-permissions", argv)
        self.assertEqual(argv[-4:], ["claude", "exec", "--file", "-"])
        self.assertEqual(stdin_text, "repair prompt")


class AiProviderSelectionTests(unittest.TestCase):
    def test_default_auto_provider_prefers_codex_then_claude(self) -> None:
        cfg = AppConfig()

        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["codex", "claude"])

    def test_direct_auto_provider_prefers_codex_then_openclaw(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="direct", provider="auto"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["codex", "openclaw"])

    def test_acpx_auto_provider_prefers_codex_then_claude(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="acpx", provider="auto"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["codex", "claude"])

    def test_openclaw_provider_falls_back_to_codex(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="direct", provider="openclaw"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["openclaw", "codex"])

    def test_acpx_openclaw_provider_falls_back_to_codex_and_claude(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="acpx", provider="openclaw"))
        self.assertEqual(_resolve_ai_provider_candidates(cfg), ["openclaw", "codex", "claude"])

    def test_openclaw_probe_treats_expiring_auth_as_available(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="direct"))
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

    def test_acpx_claude_probe_requires_acpx_and_claude_command(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="acpx", provider="claude", acpx_command="acpx"))
        responses = [
            CmdResult(argv=["acpx", "--help"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr=""),
            CmdResult(argv=["claude", "--help"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr=""),
        ]

        with patch("fix_my_claw.core.run_cmd", side_effect=responses):
            probe = _probe_ai_provider(cfg, "claude")

        self.assertTrue(probe.available)
        self.assertEqual(probe.reason, "command-ok")

    def test_acpx_openclaw_probe_requires_gateway_rpc(self) -> None:
        cfg = AppConfig(ai=AiConfig(backend="acpx", provider="openclaw", acpx_command="acpx"))
        responses = [
            CmdResult(argv=["acpx", "--help"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr=""),
            CmdResult(argv=["openclaw", "acp", "--help"], cwd=None, exit_code=0, duration_ms=1, stdout="", stderr=""),
            CmdResult(
                argv=["openclaw", "gateway", "status", "--json", "--require-rpc"],
                cwd=None,
                exit_code=1,
                duration_ms=1,
                stdout='{"rpc":{"ok":false}}',
                stderr="rpc unavailable",
            ),
        ]

        with patch("fix_my_claw.core.run_cmd", side_effect=responses):
            probe = _probe_ai_provider(cfg, "openclaw")

        self.assertFalse(probe.available)
        self.assertEqual(probe.reason, "gateway-rpc-unavailable")


class ProbeCapabilityTests(unittest.TestCase):
    def test_probe_targets_prioritize_configured_backend_then_supported_methods(self) -> None:
        cfg = AppConfig()

        self.assertEqual(
            _resolve_probe_ai_targets(cfg),
            [
                ("acpx", "codex"),
                ("acpx", "claude"),
                ("acpx", "openclaw"),
                ("direct", "codex"),
                ("direct", "openclaw"),
            ],
        )

    def test_ai_capability_fails_when_invocation_references_missing_path(self) -> None:
        cfg = AppConfig()
        static_probe = AiProviderProbe(
            provider="codex",
            available=True,
            reason="command-ok",
            argv=["acpx", "--help"],
            exit_code=0,
            stdout="",
            stderr="",
        )

        with patch("fix_my_claw.core._probe_ai_provider", return_value=static_probe), patch(
            "fix_my_claw.core._build_ai_invocation",
            return_value=(["acpx", "--cwd", "/missing-dir", "codex", "exec", "--file", "-"], "probe"),
        ):
            check = _probe_ai_capability(cfg, backend="acpx", provider="codex", live=True, live_timeout_seconds=10)

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.summary, "configured argv references missing paths")

    def test_ai_capability_runs_live_probe(self) -> None:
        cfg = AppConfig()
        static_probe = AiProviderProbe(
            provider="codex",
            available=True,
            reason="command-ok",
            argv=["acpx", "--help"],
            exit_code=0,
            stdout="",
            stderr="",
        )
        live_result = CmdResult(
            argv=["acpx", "codex", "exec", "--file", "-"],
            cwd=None,
            exit_code=0,
            duration_ms=12,
            stdout="ok",
            stderr="",
        )

        with patch("fix_my_claw.core._probe_ai_provider", return_value=static_probe), patch(
            "fix_my_claw.core._build_ai_invocation",
            return_value=(["acpx", "codex", "exec", "--file", "-"], "probe"),
        ), patch("fix_my_claw.core.run_cmd", return_value=live_result):
            check = _probe_ai_capability(cfg, backend="acpx", provider="codex", live=True, live_timeout_seconds=10)

        self.assertEqual(check.status, "ok")
        self.assertEqual(check.summary, "live dry-run succeeded")

    def test_run_probe_collects_checks_and_warns_for_unhealthy_runtime(self) -> None:
        cfg = AppConfig()

        health_probe = CmdResult(
            argv=["openclaw", "gateway", "health", "--json"],
            cwd=None,
            exit_code=0,
            duration_ms=1,
            stdout='{"ok": false}',
            stderr="",
        )
        status_probe = CmdResult(
            argv=["openclaw", "gateway", "status", "--json", "--require-rpc"],
            cwd=None,
            exit_code=0,
            duration_ms=1,
            stdout='{"rpc": {"ok": true}, "health": {"healthy": true}}',
            stderr="",
        )
        config_mode = CmdResult(
            argv=["openclaw", "config", "get", "gateway.mode", "--json"],
            cwd=None,
            exit_code=0,
            duration_ms=1,
            stdout='"local"\n',
            stderr="",
        )

        with patch("fix_my_claw.core._run_openclaw_config_cmd", return_value=config_mode), patch(
            "fix_my_claw.core.probe_health",
            return_value=Probe(name="health", cmd=health_probe, json_data={"ok": False}, effective_ok=False),
        ), patch(
            "fix_my_claw.core.probe_status",
            return_value=Probe(
                name="status",
                cmd=status_probe,
                json_data={"rpc": {"ok": True}, "health": {"healthy": True}},
                effective_ok=True,
            ),
        ), patch(
            "fix_my_claw.core._probe_official_step",
            side_effect=[
                CapabilityCheck(
                    name="repair.official.1",
                    status="ok",
                    summary="dry-run syntax check passed",
                    details={},
                ),
                CapabilityCheck(
                    name="repair.official.2",
                    status="ok",
                    summary="dry-run syntax check passed",
                    details={},
                ),
            ],
        ), patch(
            "fix_my_claw.core._resolve_probe_ai_targets",
            return_value=[],
        ):
            report = run_probe(cfg, live_ai=False, ai_timeout_seconds=10)

        self.assertIsInstance(report, CapabilityReport)
        self.assertTrue(report.ok)
        self.assertTrue(any(check["name"] == "openclaw.health" and check["status"] == "warn" for check in report.checks))

    def test_render_probe_report_summarizes_statuses(self) -> None:
        report = CapabilityReport(
            ok=False,
            summary={"ok": 1, "warn": 1, "fail": 1, "skip": 0, "total": 3},
            checks=[
                {"name": "config.gateway_mode", "status": "ok", "summary": "gateway.mode=local", "details": {}},
                {"name": "openclaw.health", "status": "warn", "summary": "probe ran but reported unhealthy", "details": {}},
                {"name": "ai.acpx.codex", "status": "fail", "summary": "live dry-run failed", "details": {}},
            ],
        )

        rendered = _render_probe_report(report)

        self.assertIn("probe summary: 1 ok, 1 warn, 1 fail, 0 skip", rendered)
        self.assertIn("[FAIL] ai.acpx.codex: live dry-run failed", rendered)


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
