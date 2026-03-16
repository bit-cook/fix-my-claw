from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from string import Template
from typing import Any

try:
    import tomllib  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger("fix_my_claw")

AI_PROBE_TOKEN = "FIX_MY_CLAW_PROBE_OK"
AI_PROBE_PROMPT = (
    "This is a fix-my-claw dry-run capability probe.\n"
    "Do not modify files.\n"
    "Do not run commands.\n"
    "Do not inspect the workspace.\n"
    "Do not use any tools.\n"
    f"Reply with exactly: {AI_PROBE_TOKEN}\n"
)

DEFAULT_CONFIG_PATH = "~/.fix-my-claw/config.toml"

DEFAULT_CONFIG_TOML = """\
[monitor]
interval_seconds = 60
probe_timeout_seconds = 15
repair_cooldown_seconds = 300
state_dir = "~/.fix-my-claw"
log_file = "~/.fix-my-claw/fix-my-claw.log"
log_level = "INFO"

[openclaw]
command = "openclaw"
state_dir = "~/.openclaw"
workspace_dir = "~/.openclaw/workspace"
allow_remote_mode = false
health_args = ["gateway", "health", "--json"]
status_args = ["gateway", "status", "--json", "--require-rpc"]
logs_args = ["logs", "--tail", "200"]

[repair]
enabled = true
official_steps = [
  ["openclaw", "doctor", "--repair", "--non-interactive"],
  ["openclaw", "gateway", "restart"],
]
step_timeout_seconds = 600
post_step_wait_seconds = 2

[ai]
enabled = true
backend = "acpx"
provider = "auto"
command = "codex"
agent_id = "main"
local = false
agent_args = []
acpx_command = "acpx"
acpx_args = []
acpx_permissions = "approve-all"
acpx_non_interactive_permissions = "fail"
acpx_format = "json"
args = [
  "exec",
  "-s", "workspace-write",
  "-c", "approval_policy=\\"never\\"",
  "--skip-git-repo-check",
  "-C", "$workspace_dir",
  "--add-dir", "$openclaw_state_dir",
  "--add-dir", "$monitor_state_dir",
]
model = "gpt-5.2"
timeout_seconds = 1800
max_attempts_per_day = 2
cooldown_seconds = 3600
allow_code_changes = false
args_code = [
  "exec",
  "-s", "danger-full-access",
  "-c", "approval_policy=\\"never\\"",
  "--skip-git-repo-check",
  "-C", "$workspace_dir",
]
"""


def _expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _as_path(value: str) -> Path:
    return Path(_expand_path(value)).resolve()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def truncate_for_log(s: str, limit: int = 8000) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + f"\n...[truncated {len(s) - limit} chars]"


_SECRET_PATTERNS = [
    r"\bsk-[A-Za-z0-9]{16,}\b",
]


def redact_text(text: str) -> str:
    out = text
    out = re.sub(
        r'(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([^\s"\'`]+)',
        r"\1\2***",
        out,
    )
    out = re.sub(r"(?i)\b(Bearer)\s+([A-Za-z0-9._\\-]+)", r"\1 ***", out)
    for pat in _SECRET_PATTERNS:
        out = re.sub(pat, "sk-***", out)
    return out


def _supports_color(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") not in {"", "dumb", None}


def _format_duration_ms(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = seconds - (minutes * 60)
    return f"{minutes}m{rem:04.1f}s"


def _format_argv(argv: list[str], *, limit: int = 120) -> str:
    rendered = " ".join(argv)
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 3]}..."


class ConsoleFormatter(logging.Formatter):
    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _COLORS = {
        "START": "\033[94m",
        "WATCH": "\033[96m",
        "PROBE": "\033[92m",
        "REPAIR": "\033[93m",
        "AI": "\033[95m",
        "ERROR": "\033[91m",
    }

    def __init__(self, *, use_color: bool):
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        lane = self._lane(record)
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        ts_text = self._decorate(ts, self._DIM)
        lane_text = self._decorate(lane.ljust(6), self._COLORS.get(lane, ""))
        return f"{ts_text} | {lane_text} | {message}"

    def _lane(self, record: logging.LogRecord) -> str:
        name = record.name
        if record.levelno >= logging.ERROR:
            return "ERROR"
        if "startup" in name:
            return "START"
        if "watchdog" in name:
            return "WATCH"
        if ".openclaw" in name:
            return "PROBE"
        if ".ai" in name:
            return "AI"
        if ".repair" in name:
            return "REPAIR"
        return "LOG"

    def _decorate(self, text: str, prefix: str) -> str:
        if not self.use_color or not prefix:
            return text
        return f"{prefix}{text}{self._RESET}"


def setup_logging(cfg: "AppConfig") -> None:
    ensure_dir(cfg.monitor.state_dir)
    ensure_dir(cfg.monitor.log_file.parent)

    level = getattr(logging, cfg.monitor.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        cfg.monitor.log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(ConsoleFormatter(use_color=_supports_color(stream_handler.stream)))

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: int = 60
    probe_timeout_seconds: int = 15
    repair_cooldown_seconds: int = 300
    state_dir: Path = field(default_factory=lambda: _as_path("~/.fix-my-claw"))
    log_file: Path = field(default_factory=lambda: _as_path("~/.fix-my-claw/fix-my-claw.log"))
    log_level: str = "INFO"


@dataclass(frozen=True)
class OpenClawConfig:
    command: str = "openclaw"
    state_dir: Path = field(default_factory=lambda: _as_path("~/.openclaw"))
    workspace_dir: Path = field(default_factory=lambda: _as_path("~/.openclaw/workspace"))
    allow_remote_mode: bool = False
    health_args: list[str] = field(default_factory=lambda: ["gateway", "health", "--json"])
    status_args: list[str] = field(
        default_factory=lambda: ["gateway", "status", "--json", "--require-rpc"]
    )
    logs_args: list[str] = field(default_factory=lambda: ["logs", "--tail", "200"])


@dataclass(frozen=True)
class RepairConfig:
    enabled: bool = True
    official_steps: list[list[str]] = field(
        default_factory=lambda: [
            ["openclaw", "doctor", "--repair", "--non-interactive"],
            ["openclaw", "gateway", "restart"],
        ]
    )
    step_timeout_seconds: int = 600
    post_step_wait_seconds: int = 2


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = True
    backend: str = "acpx"  # direct | acpx
    provider: str = "auto"  # auto | codex | claude | openclaw
    command: str = "codex"
    agent_id: str | None = "main"
    local: bool = False
    agent_args: list[str] = field(default_factory=list)
    acpx_command: str = "acpx"
    acpx_args: list[str] = field(default_factory=list)
    acpx_permissions: str = "approve-all"  # approve-all | approve-reads | deny-all
    acpx_non_interactive_permissions: str = "fail"  # fail | deny
    acpx_format: str = "json"  # text | json | quiet
    # args supports placeholders: $workspace_dir, $openclaw_state_dir, $monitor_state_dir
    args: list[str] = field(
        default_factory=lambda: [
            "exec",
            "-s",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "--skip-git-repo-check",
            "-C",
            "$workspace_dir",
            "--add-dir",
            "$openclaw_state_dir",
            "--add-dir",
            "$monitor_state_dir",
        ]
    )
    model: str | None = None
    timeout_seconds: int = 1800
    max_attempts_per_day: int = 2
    cooldown_seconds: int = 3600
    allow_code_changes: bool = False
    args_code: list[str] = field(
        default_factory=lambda: [
            "exec",
            "-s",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            "--skip-git-repo-check",
            "-C",
            "$workspace_dir",
        ]
    )


@dataclass(frozen=True)
class AppConfig:
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    repair: RepairConfig = field(default_factory=RepairConfig)
    ai: AiConfig = field(default_factory=AiConfig)


def _get(d: dict[str, Any], key: str, default: Any) -> Any:
    v = d.get(key, default)
    return default if v is None else v


def _parse_monitor(raw: dict[str, Any]) -> MonitorConfig:
    return MonitorConfig(
        interval_seconds=int(_get(raw, "interval_seconds", 60)),
        probe_timeout_seconds=int(_get(raw, "probe_timeout_seconds", 15)),
        repair_cooldown_seconds=int(_get(raw, "repair_cooldown_seconds", 300)),
        state_dir=_as_path(str(_get(raw, "state_dir", "~/.fix-my-claw"))),
        log_file=_as_path(str(_get(raw, "log_file", "~/.fix-my-claw/fix-my-claw.log"))),
        log_level=str(_get(raw, "log_level", "INFO")),
    )


def _parse_openclaw(raw: dict[str, Any]) -> OpenClawConfig:
    return OpenClawConfig(
        command=str(_get(raw, "command", "openclaw")),
        state_dir=_as_path(str(_get(raw, "state_dir", "~/.openclaw"))),
        workspace_dir=_as_path(str(_get(raw, "workspace_dir", "~/.openclaw/workspace"))),
        allow_remote_mode=bool(_get(raw, "allow_remote_mode", False)),
        health_args=list(_get(raw, "health_args", ["gateway", "health", "--json"])),
        status_args=list(_get(raw, "status_args", ["gateway", "status", "--json", "--require-rpc"])),
        logs_args=list(_get(raw, "logs_args", ["logs", "--tail", "200"])),
    )


def _parse_repair(raw: dict[str, Any]) -> RepairConfig:
    return RepairConfig(
        enabled=bool(_get(raw, "enabled", True)),
        official_steps=[list(x) for x in _get(raw, "official_steps", RepairConfig().official_steps)],
        step_timeout_seconds=int(_get(raw, "step_timeout_seconds", 600)),
        post_step_wait_seconds=int(_get(raw, "post_step_wait_seconds", 2)),
    )


def _parse_ai(raw: dict[str, Any]) -> AiConfig:
    cfg = AiConfig()
    return AiConfig(
        enabled=bool(_get(raw, "enabled", cfg.enabled)),
        backend=str(_get(raw, "backend", cfg.backend)),
        provider=str(_get(raw, "provider", cfg.provider)),
        command=str(_get(raw, "command", cfg.command)),
        agent_id=_get(raw, "agent_id", cfg.agent_id),
        local=bool(_get(raw, "local", cfg.local)),
        agent_args=list(_get(raw, "agent_args", cfg.agent_args)),
        acpx_command=str(_get(raw, "acpx_command", cfg.acpx_command)),
        acpx_args=list(_get(raw, "acpx_args", cfg.acpx_args)),
        acpx_permissions=str(_get(raw, "acpx_permissions", cfg.acpx_permissions)),
        acpx_non_interactive_permissions=str(
            _get(raw, "acpx_non_interactive_permissions", cfg.acpx_non_interactive_permissions)
        ),
        acpx_format=str(_get(raw, "acpx_format", cfg.acpx_format)),
        args=list(_get(raw, "args", cfg.args)),
        model=_get(raw, "model", cfg.model),
        timeout_seconds=int(_get(raw, "timeout_seconds", cfg.timeout_seconds)),
        max_attempts_per_day=int(_get(raw, "max_attempts_per_day", cfg.max_attempts_per_day)),
        cooldown_seconds=int(_get(raw, "cooldown_seconds", cfg.cooldown_seconds)),
        allow_code_changes=bool(_get(raw, "allow_code_changes", cfg.allow_code_changes)),
        args_code=list(_get(raw, "args_code", cfg.args_code)),
    )


def load_config(path: str) -> AppConfig:
    p = _as_path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    monitor = _parse_monitor(dict(data.get("monitor", {})))
    openclaw = _parse_openclaw(dict(data.get("openclaw", {})))
    repair = _parse_repair(dict(data.get("repair", {})))
    ai = _parse_ai(dict(data.get("ai", {})))
    return AppConfig(monitor=monitor, openclaw=openclaw, repair=repair, ai=ai)


def write_default_config(path: str, *, overwrite: bool = False) -> Path:
    p = _as_path(path)
    if p.exists() and not overwrite:
        return p
    ensure_dir(p.parent)
    p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return p


@dataclass(frozen=True)
class CmdResult:
    argv: list[str]
    cwd: Path | None
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class UnsupportedOpenClawModeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AiProviderProbe:
    provider: str
    available: bool
    reason: str
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str

    def to_json(self) -> dict:
        return {
            "provider": self.provider,
            "available": self.available,
            "reason": self.reason,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def run_cmd(
    argv: list[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> CmdResult:
    started = time.monotonic()
    try:
        cp = subprocess.run(
            argv,
            input=stdin_text,
            text=True,
            capture_output=True,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            timeout=timeout_seconds,
        )
        code = cp.returncode
        out = cp.stdout or ""
        err = cp.stderr or ""
    except FileNotFoundError as e:
        code = 127
        out = ""
        err = f"[fix-my-claw] command not found: {argv[0]} ({e})"
    except subprocess.TimeoutExpired as e:
        code = 124
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        err = (err + "\n" if err else "") + f"[fix-my-claw] timeout after {timeout_seconds}s"
    except OSError as e:
        code = 1
        out = ""
        err = f"[fix-my-claw] os error running {argv!r}: {e}"
    duration_ms = int((time.monotonic() - started) * 1000)
    return CmdResult(
        argv=list(argv),
        cwd=cwd,
        exit_code=code,
        duration_ms=duration_ms,
        stdout=out,
        stderr=err,
    )


def _openclaw_cwd(cfg: "AppConfig") -> Path | None:
    return cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None


def _run_openclaw_config_cmd(cfg: "AppConfig", args: list[str]) -> CmdResult:
    argv = [cfg.openclaw.command, "config", *args]
    return run_cmd(argv, timeout_seconds=cfg.monitor.probe_timeout_seconds, cwd=_openclaw_cwd(cfg))


def _parse_json_scalar(stdout: str) -> Any | None:
    s = stdout.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _last_nonempty_line(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _ensure_supported_gateway_mode(cfg: "AppConfig") -> None:
    if cfg.openclaw.allow_remote_mode:
        return

    mode_res = _run_openclaw_config_cmd(cfg, ["get", "gateway.mode", "--json"])
    if not mode_res.ok:
        return

    mode = _parse_json_scalar(mode_res.stdout)
    if mode != "remote":
        return

    config_res = _run_openclaw_config_cmd(cfg, ["file"])
    config_path = _last_nonempty_line(config_res.stdout) if config_res.ok else None
    path_hint = f" active config: {config_path}." if config_path else ""
    raise UnsupportedOpenClawModeError(
        "fix-my-claw refuses to run when OpenClaw has gateway.mode=remote because probes may target "
        "a remote Gateway while repairs still modify this machine."
        f"{path_hint} Deploy fix-my-claw on the Gateway host, or set [openclaw].allow_remote_mode = true "
        "if you explicitly want this risk."
    )


def _log_startup(cfg: "AppConfig", *, mode: str, config_path: str) -> None:
    startup_log = logging.getLogger("fix_my_claw.startup")
    startup_log.info(
        "mode=%s config=%s",
        mode,
        _as_path(config_path),
    )
    startup_log.info(
        "openclaw=%s workspace=%s interval=%ss cooldown=%ss ai=%s/%s/%s",
        cfg.openclaw.command,
        cfg.openclaw.workspace_dir,
        cfg.monitor.interval_seconds,
        cfg.monitor.repair_cooldown_seconds,
        "on" if cfg.ai.enabled else "off",
        cfg.ai.backend,
        cfg.ai.provider,
    )


@dataclass
class FileLock:
    path: Path
    _fd: int | None = None

    def acquire(self, *, timeout_seconds: int = 0) -> bool:
        start = time.monotonic()
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                self._fd = fd
                return True
            except FileExistsError:
                if self._try_break_stale_lock():
                    continue
                if timeout_seconds <= 0:
                    return False
                if (time.monotonic() - start) >= timeout_seconds:
                    return False
                time.sleep(0.2)

    def _try_break_stale_lock(self) -> bool:
        try:
            pid_text = self.path.read_text(encoding="utf-8").strip()
            pid = int(pid_text) if pid_text else None
        except Exception:
            pid = None

        if pid is None:
            try:
                self.path.unlink(missing_ok=True)
                return True
            except Exception:
                return False

        try:
            os.kill(pid, 0)
            return False
        except Exception:
            try:
                self.path.unlink(missing_ok=True)
                return True
            except Exception:
                return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


def _now_ts() -> int:
    return int(time.time())


def _today_ymd() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


@dataclass
class State:
    last_ok_ts: int | None = None
    last_repair_ts: int | None = None
    last_ai_ts: int | None = None
    ai_attempts_day: str | None = None
    ai_attempts_count: int = 0

    def to_json(self) -> dict:
        return {
            "last_ok_ts": self.last_ok_ts,
            "last_repair_ts": self.last_repair_ts,
            "last_ai_ts": self.last_ai_ts,
            "ai_attempts_day": self.ai_attempts_day,
            "ai_attempts_count": self.ai_attempts_count,
        }

    @staticmethod
    def from_json(d: dict) -> "State":
        s = State()
        s.last_ok_ts = d.get("last_ok_ts")
        s.last_repair_ts = d.get("last_repair_ts")
        s.last_ai_ts = d.get("last_ai_ts")
        s.ai_attempts_day = d.get("ai_attempts_day")
        s.ai_attempts_count = int(d.get("ai_attempts_count", 0))
        return s


class StateStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.path = base_dir / "state.json"
        ensure_dir(base_dir)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return State.from_json(data if isinstance(data, dict) else {})
        except Exception:
            return State()

    def save(self, state: State) -> None:
        ensure_dir(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def mark_ok(self) -> None:
        s = self.load()
        s.last_ok_ts = _now_ts()
        self.save(s)

    def can_attempt_repair(self, cooldown_seconds: int, *, force: bool) -> bool:
        if force:
            return True
        s = self.load()
        if s.last_repair_ts is None:
            return True
        return (_now_ts() - s.last_repair_ts) >= cooldown_seconds

    def mark_repair_attempt(self) -> None:
        s = self.load()
        s.last_repair_ts = _now_ts()
        self.save(s)

    def can_attempt_ai(self, *, max_attempts_per_day: int, cooldown_seconds: int) -> bool:
        s = self.load()
        today = _today_ymd()
        if s.ai_attempts_day != today:
            s.ai_attempts_day = today
            s.ai_attempts_count = 0
            self.save(s)

        if s.ai_attempts_count >= max_attempts_per_day:
            return False
        if s.last_ai_ts is not None and (_now_ts() - s.last_ai_ts) < cooldown_seconds:
            return False
        return True

    def mark_ai_attempt(self) -> None:
        s = self.load()
        today = _today_ymd()
        if s.ai_attempts_day != today:
            s.ai_attempts_day = today
            s.ai_attempts_count = 0
        s.ai_attempts_count += 1
        s.last_ai_ts = _now_ts()
        self.save(s)


@dataclass(frozen=True)
class Probe:
    name: str
    cmd: CmdResult
    json_data: dict | list | None
    effective_ok: bool

    @property
    def ok(self) -> bool:
        return self.effective_ok

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "exit_code": self.cmd.exit_code,
            "duration_ms": self.cmd.duration_ms,
            "argv": self.cmd.argv,
            "stdout": self.cmd.stdout,
            "stderr": self.cmd.stderr,
            "json": self.json_data,
        }


def _parse_json_maybe(stdout: str) -> dict | list | None:
    s = stdout.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _probe_effective_ok(name: str, cmd: CmdResult, data: dict | list | None) -> bool:
    if not cmd.ok:
        return False
    if not isinstance(data, dict):
        return True

    if name == "health":
        ok = data.get("ok")
        if isinstance(ok, bool):
            return ok
        healthy = data.get("healthy")
        if isinstance(healthy, bool):
            return healthy
        nested_health = data.get("health")
        if isinstance(nested_health, dict):
            nested_healthy = nested_health.get("healthy")
            if isinstance(nested_healthy, bool):
                return nested_healthy
        return True

    if name == "status":
        rpc = data.get("rpc")
        if isinstance(rpc, dict):
            rpc_ok = rpc.get("ok")
            if isinstance(rpc_ok, bool) and not rpc_ok:
                return False
        nested_health = data.get("health")
        if isinstance(nested_health, dict):
            nested_healthy = nested_health.get("healthy")
            if isinstance(nested_healthy, bool):
                return nested_healthy
        healthy = data.get("healthy")
        if isinstance(healthy, bool):
            return healthy
        ok = data.get("ok")
        if isinstance(ok, bool):
            return ok
        return True

    return True


def probe_health(cfg: AppConfig, *, log_on_fail: bool = True) -> Probe:
    argv = [cfg.openclaw.command, *cfg.openclaw.health_args]
    cwd = cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None
    cmd = run_cmd(argv, timeout_seconds=cfg.monitor.probe_timeout_seconds, cwd=cwd)
    data = _parse_json_maybe(cmd.stdout)
    effective_ok = _probe_effective_ok("health", cmd, data)
    if log_on_fail and not effective_ok:
        logging.getLogger("fix_my_claw.openclaw").warning(
            "health probe failed: %s", truncate_for_log(cmd.stderr or cmd.stdout)
        )
    return Probe(name="health", cmd=cmd, json_data=data, effective_ok=effective_ok)


def probe_status(cfg: AppConfig, *, log_on_fail: bool = True) -> Probe:
    argv = [cfg.openclaw.command, *cfg.openclaw.status_args]
    cwd = cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None
    cmd = run_cmd(argv, timeout_seconds=cfg.monitor.probe_timeout_seconds, cwd=cwd)
    data = _parse_json_maybe(cmd.stdout)
    effective_ok = _probe_effective_ok("status", cmd, data)
    if log_on_fail and not effective_ok:
        logging.getLogger("fix_my_claw.openclaw").warning(
            "status probe failed: %s", truncate_for_log(cmd.stderr or cmd.stdout)
        )
    return Probe(name="status", cmd=cmd, json_data=data, effective_ok=effective_ok)


def probe_logs(cfg: AppConfig, *, timeout_seconds: int = 15) -> CmdResult:
    argv = [cfg.openclaw.command, *cfg.openclaw.logs_args]
    cwd = cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None
    return run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)


@dataclass(frozen=True)
class CheckResult:
    healthy: bool
    health: dict
    status: dict

    def to_json(self) -> dict:
        return {"healthy": self.healthy, "health": self.health, "status": self.status}


@dataclass(frozen=True)
class CapabilityCheck:
    name: str
    status: str  # ok | warn | fail | skip
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True)
class CapabilityReport:
    ok: bool
    checks: list[dict[str, Any]]
    summary: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "summary": self.summary,
        }


def run_check(cfg: AppConfig, store: StateStore) -> CheckResult:
    h = probe_health(cfg)
    s = probe_status(cfg)
    healthy = h.ok and s.ok
    if healthy:
        store.mark_ok()
    return CheckResult(healthy=healthy, health=h.to_json(), status=s.to_json())


def _probe_summary_counts(checks: list[CapabilityCheck]) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    counts["total"] = len(checks)
    return counts


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _probe_path_check(
    name: str,
    path: Path,
    *,
    can_create: bool,
    missing_status: str = "warn",
) -> CapabilityCheck:
    if path.exists():
        return CapabilityCheck(name=name, status="ok", summary=f"{path} exists", details={"path": str(path)})

    parent = _nearest_existing_parent(path)
    writable = bool(parent and os.access(parent, os.W_OK | os.X_OK))
    details = {
        "path": str(path),
        "nearest_existing_parent": str(parent) if parent is not None else None,
        "parent_writable": writable,
    }
    if can_create and writable:
        return CapabilityCheck(
            name=name,
            status="warn",
            summary=f"{path} does not exist yet; fix-my-claw can create it on first run",
            details=details,
        )
    return CapabilityCheck(
        name=name,
        status=missing_status,
        summary=f"{path} does not exist",
        details=details,
    )


def _render_runtime_probe(name: str, probe: Probe) -> CapabilityCheck:
    details = {
        "argv": probe.cmd.argv,
        "exit_code": probe.cmd.exit_code,
        "duration_ms": probe.cmd.duration_ms,
        "stderr": redact_text(probe.cmd.stderr),
        "stdout": redact_text(probe.cmd.stdout),
        "json": probe.json_data,
    }
    if probe.cmd.ok and probe.ok:
        return CapabilityCheck(name=name, status="ok", summary="probe succeeded", details=details)
    if probe.cmd.ok:
        return CapabilityCheck(name=name, status="warn", summary="probe ran but reported unhealthy", details=details)
    return CapabilityCheck(name=name, status="fail", summary="probe command failed", details=details)


def _probe_gateway_mode_check(cfg: AppConfig) -> CapabilityCheck:
    res = _run_openclaw_config_cmd(cfg, ["get", "gateway.mode", "--json"])
    details = {
        "argv": res.argv,
        "exit_code": res.exit_code,
        "duration_ms": res.duration_ms,
        "stdout": redact_text(res.stdout),
        "stderr": redact_text(res.stderr),
        "allow_remote_mode": cfg.openclaw.allow_remote_mode,
    }
    if not res.ok:
        return CapabilityCheck(
            name="config.gateway_mode",
            status="fail",
            summary="could not read OpenClaw gateway.mode",
            details=details,
        )

    mode = _parse_json_scalar(res.stdout)
    details["mode"] = mode
    if mode == "remote" and not cfg.openclaw.allow_remote_mode:
        return CapabilityCheck(
            name="config.gateway_mode",
            status="fail",
            summary="gateway.mode=remote is blocked by default",
            details=details,
        )
    if mode == "remote":
        return CapabilityCheck(
            name="config.gateway_mode",
            status="warn",
            summary="gateway.mode=remote is allowed by config; repairs may target the wrong host",
            details=details,
        )
    if isinstance(mode, str):
        return CapabilityCheck(
            name="config.gateway_mode",
            status="ok",
            summary=f"gateway.mode={mode}",
            details=details,
        )
    return CapabilityCheck(
        name="config.gateway_mode",
        status="warn",
        summary="gateway.mode returned an unexpected value",
        details=details,
    )


def _build_official_step_probe_argv(cfg: AppConfig, step: list[str]) -> tuple[list[str], list[str]]:
    argv = [cfg.openclaw.command if step and step[0] == "openclaw" else step[0], *step[1:]]
    if step and step[0] == "openclaw":
        return argv, [cfg.openclaw.command, *step[1:], "--help"]
    return argv, [argv[0], "--help"]


def _probe_official_step(cfg: AppConfig, step: list[str], idx: int) -> CapabilityCheck:
    argv, dry_run_argv = _build_official_step_probe_argv(cfg, step)
    res = run_cmd(
        dry_run_argv,
        timeout_seconds=min(max(5, cfg.monitor.probe_timeout_seconds), 15),
        cwd=_openclaw_cwd(cfg),
    )
    details = {
        "argv": argv,
        "dry_run_argv": dry_run_argv,
        "exit_code": res.exit_code,
        "duration_ms": res.duration_ms,
        "stdout": redact_text(res.stdout),
        "stderr": redact_text(res.stderr),
    }
    return CapabilityCheck(
        name=f"repair.official.{idx}",
        status="ok" if res.ok else "fail",
        summary="dry-run syntax check passed" if res.ok else "dry-run syntax check failed",
        details=details,
    )


def _extract_invocation_paths(argv: list[str]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"-C", "--cwd", "--add-dir"} and i + 1 < len(argv):
            out.append((arg, Path(argv[i + 1]).expanduser()))
            i += 2
            continue
        i += 1
    return out


def _validate_invocation_paths(argv: list[str]) -> list[str]:
    issues: list[str] = []
    for flag, path in _extract_invocation_paths(argv):
        if not path.exists():
            issues.append(f"{flag} path does not exist: {path}")
    return issues


def _resolve_probe_ai_targets(cfg: AppConfig) -> list[tuple[str, str]]:
    configured_backend = _normalize_ai_backend(cfg.ai.backend)
    configured = [(configured_backend, provider) for provider in _resolve_ai_provider_candidates(cfg)]
    supported = [
        ("acpx", "codex"),
        ("acpx", "claude"),
        ("acpx", "openclaw"),
        ("direct", "codex"),
        ("direct", "openclaw"),
    ]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*configured, *supported]:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _build_probe_cfg(cfg: AppConfig, backend: str, provider: str) -> AppConfig:
    return replace(
        cfg,
        ai=replace(
            cfg.ai,
            backend=backend,
            provider=provider,
        ),
    )


def _probe_ai_capability(
    cfg: AppConfig,
    *,
    backend: str,
    provider: str,
    live: bool,
    live_timeout_seconds: int,
) -> CapabilityCheck:
    probe_cfg = _build_probe_cfg(cfg, backend, provider)
    static_probe = _probe_ai_provider(probe_cfg, provider)
    argv, stdin_text = _build_ai_invocation(
        probe_cfg,
        AI_PROBE_PROMPT,
        code_stage=False,
        provider_override=provider,
    )
    path_issues = _validate_invocation_paths(argv)
    details: dict[str, Any] = {
        "backend": backend,
        "provider": provider,
        "argv": argv,
        "stdin_preview": AI_PROBE_PROMPT if stdin_text is not None else None,
        "path_issues": path_issues,
        "static_probe": static_probe.to_json(),
    }
    if not static_probe.available:
        return CapabilityCheck(
            name=f"ai.{backend}.{provider}",
            status="fail",
            summary=f"static probe failed: {static_probe.reason}",
            details=details,
        )
    if path_issues:
        return CapabilityCheck(
            name=f"ai.{backend}.{provider}",
            status="fail",
            summary="configured argv references missing paths",
            details=details,
        )
    if not live:
        return CapabilityCheck(
            name=f"ai.{backend}.{provider}",
            status="warn",
            summary="static probe passed; live AI dry-run skipped",
            details=details,
        )

    cmd = run_cmd(
        argv,
        timeout_seconds=max(5, live_timeout_seconds),
        cwd=probe_cfg.openclaw.workspace_dir if probe_cfg.openclaw.workspace_dir.exists() else None,
        stdin_text=stdin_text,
    )
    details["live_result"] = {
        "exit_code": cmd.exit_code,
        "duration_ms": cmd.duration_ms,
        "stdout": redact_text(cmd.stdout),
        "stderr": redact_text(cmd.stderr),
    }
    return CapabilityCheck(
        name=f"ai.{backend}.{provider}",
        status="ok" if cmd.ok else "fail",
        summary="live dry-run succeeded" if cmd.ok else "live dry-run failed",
        details=details,
    )


def run_probe(cfg: AppConfig, *, live_ai: bool, ai_timeout_seconds: int) -> CapabilityReport:
    checks: list[CapabilityCheck] = [
        _probe_gateway_mode_check(cfg),
        _probe_path_check("path.workspace_dir", cfg.openclaw.workspace_dir, can_create=False, missing_status="warn"),
        _probe_path_check("path.openclaw_state_dir", cfg.openclaw.state_dir, can_create=False, missing_status="warn"),
        _probe_path_check("path.monitor_state_dir", cfg.monitor.state_dir, can_create=True, missing_status="fail"),
        _render_runtime_probe("openclaw.health", probe_health(cfg, log_on_fail=False)),
        _render_runtime_probe("openclaw.status", probe_status(cfg, log_on_fail=False)),
    ]
    checks.extend(
        _probe_official_step(cfg, step, idx)
        for idx, step in enumerate(cfg.repair.official_steps, start=1)
        if step
    )
    checks.extend(
        _probe_ai_capability(
            cfg,
            backend=backend,
            provider=provider,
            live=live_ai,
            live_timeout_seconds=ai_timeout_seconds,
        )
        for backend, provider in _resolve_probe_ai_targets(cfg)
    )
    summary = _probe_summary_counts(checks)
    return CapabilityReport(
        ok=not any(check.failed for check in checks),
        checks=[check.to_json() for check in checks],
        summary=summary,
    )


def _render_probe_report(report: CapabilityReport) -> str:
    lines = [
        (
            "probe summary: "
            f"{report.summary.get('ok', 0)} ok, "
            f"{report.summary.get('warn', 0)} warn, "
            f"{report.summary.get('fail', 0)} fail, "
            f"{report.summary.get('skip', 0)} skip"
        )
    ]
    for check in report.checks:
        status = str(check["status"]).upper().ljust(4)
        lines.append(f"[{status}] {check['name']}: {check['summary']}")
    return "\n".join(lines)


@dataclass(frozen=True)
class RepairResult:
    attempted: bool
    fixed: bool
    used_ai: bool
    details: dict

    def to_json(self) -> dict:
        return {
            "attempted": self.attempted,
            "fixed": self.fixed,
            "used_ai": self.used_ai,
            "details": self.details,
        }


def _attempt_dir(cfg: AppConfig) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    d = cfg.monitor.state_dir / "attempts" / ts
    ensure_dir(d)
    return d


def _write_attempt_file(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.write_text(content, encoding="utf-8")
    return p


def _collect_context(cfg: AppConfig, attempt_dir: Path) -> dict:
    health = probe_health(cfg, log_on_fail=False)
    status = probe_status(cfg, log_on_fail=False)
    logs = probe_logs(cfg, timeout_seconds=cfg.monitor.probe_timeout_seconds)

    _write_attempt_file(attempt_dir, "health.stdout.txt", redact_text(health.cmd.stdout))
    _write_attempt_file(attempt_dir, "health.stderr.txt", redact_text(health.cmd.stderr))
    _write_attempt_file(attempt_dir, "status.stdout.txt", redact_text(status.cmd.stdout))
    _write_attempt_file(attempt_dir, "status.stderr.txt", redact_text(status.cmd.stderr))
    _write_attempt_file(attempt_dir, "openclaw.logs.txt", redact_text(logs.stdout + ("\n" + logs.stderr if logs.stderr else "")))

    return {
        "health": health.to_json(),
        "status": status.to_json(),
        "logs": {
            "ok": logs.ok,
            "exit_code": logs.exit_code,
            "duration_ms": logs.duration_ms,
            "argv": logs.argv,
            "stdout_path": str((attempt_dir / "openclaw.logs.txt").resolve()),
        },
        "attempt_dir": str(attempt_dir.resolve()),
    }


def _probe_is_healthy(cfg: AppConfig) -> bool:
    return probe_health(cfg, log_on_fail=False).ok and probe_status(cfg, log_on_fail=False).ok


def _run_official_steps(cfg: AppConfig, attempt_dir: Path) -> list[dict]:
    repair_log = logging.getLogger("fix_my_claw.repair")
    results: list[dict] = []
    total = len(cfg.repair.official_steps)
    for idx, step in enumerate(cfg.repair.official_steps, start=1):
        argv = [cfg.openclaw.command if step and step[0] == "openclaw" else step[0], *step[1:]]
        repair_log.warning("official %d/%d run=%s", idx, total, _format_argv(argv))
        cwd = cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None
        res = run_cmd(argv, timeout_seconds=cfg.repair.step_timeout_seconds, cwd=cwd)
        repair_log.warning(
            "official %d/%d exit=%s duration=%s",
            idx,
            total,
            res.exit_code,
            _format_duration_ms(res.duration_ms),
        )
        if res.stderr:
            repair_log.info("official %d/%d stderr=%s", idx, total, truncate_for_log(res.stderr))
        _write_attempt_file(attempt_dir, f"official.{idx}.stdout.txt", redact_text(res.stdout))
        _write_attempt_file(attempt_dir, f"official.{idx}.stderr.txt", redact_text(res.stderr))
        results.append(
            {
                "argv": res.argv,
                "exit_code": res.exit_code,
                "duration_ms": res.duration_ms,
                "stdout_path": str((attempt_dir / f"official.{idx}.stdout.txt").resolve()),
                "stderr_path": str((attempt_dir / f"official.{idx}.stderr.txt").resolve()),
            }
        )
        time.sleep(cfg.repair.post_step_wait_seconds)
        if _probe_is_healthy(cfg):
            repair_log.warning("official %d/%d restored health", idx, total)
            break
    return results


def _load_prompt_text(name: str) -> str:
    from importlib.resources import files

    return (files("fix_my_claw.prompts") / name).read_text(encoding="utf-8")


def _build_ai_cmd(cfg: AppConfig, *, code_stage: bool) -> list[str]:
    vars = {
        "workspace_dir": str(cfg.openclaw.workspace_dir),
        "openclaw_state_dir": str(cfg.openclaw.state_dir),
        "monitor_state_dir": str(cfg.monitor.state_dir),
    }
    args = cfg.ai.args_code if code_stage else cfg.ai.args
    rendered = [Template(x).safe_substitute(vars) for x in args]
    argv = [cfg.ai.command]
    if cfg.ai.model:
        argv += ["-m", cfg.ai.model]
    argv += rendered
    return argv


def _normalize_ai_provider(provider: str) -> str:
    return provider.strip().lower().replace("_", "-")


def _normalize_ai_backend(backend: str) -> str:
    return backend.strip().lower().replace("_", "-")


def _resolve_codex_ai_command(cfg: AppConfig) -> str:
    command = cfg.ai.command.strip()
    return command or "codex"


def _resolve_acpx_ai_command(cfg: AppConfig) -> str:
    command = cfg.ai.acpx_command.strip()
    return command or "acpx"


def _resolve_openclaw_ai_command(cfg: AppConfig) -> str:
    command = cfg.ai.command.strip()
    if (
        _normalize_ai_provider(cfg.ai.provider) in {"openclaw", "openclaw-agent"}
        and command
        and command != "codex"
    ):
        return command
    if not command or command == "codex":
        return cfg.openclaw.command
    return command


def _unique_preserving_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _acpx_working_dir(cfg: AppConfig) -> Path:
    candidates = [
        cfg.openclaw.workspace_dir,
        cfg.openclaw.state_dir,
        cfg.monitor.state_dir,
    ]
    try:
        common = Path(os.path.commonpath([str(path.resolve()) for path in candidates]))
    except ValueError:
        common = cfg.openclaw.workspace_dir

    if str(common) in {"", os.sep}:
        return cfg.openclaw.workspace_dir
    return common


def _build_acpx_invocation(
    cfg: AppConfig,
    provider: str,
    *,
    one_shot: bool,
) -> list[str]:
    backend_args = list(cfg.ai.acpx_args)
    argv = [
        _resolve_acpx_ai_command(cfg),
        *backend_args,
        "--cwd",
        str(_acpx_working_dir(cfg)),
    ]

    permissions = _normalize_ai_provider(cfg.ai.acpx_permissions)
    if permissions == "approve-all":
        argv.append("--approve-all")
    elif permissions == "deny-all":
        argv.append("--deny-all")
    else:
        argv.append("--approve-reads")

    output_format = _normalize_ai_provider(cfg.ai.acpx_format)
    argv += ["--format", output_format if output_format in {"text", "json", "quiet"} else "json"]

    non_interactive = _normalize_ai_provider(cfg.ai.acpx_non_interactive_permissions)
    argv += ["--non-interactive-permissions", non_interactive if non_interactive in {"fail", "deny"} else "fail"]

    if cfg.ai.timeout_seconds > 0:
        argv += ["--timeout", str(cfg.ai.timeout_seconds)]

    argv.append(provider)
    if one_shot:
        argv.append("exec")
    argv += ["--file", "-"]
    return argv


def _resolve_ai_provider_candidates(cfg: AppConfig) -> list[str]:
    backend = _normalize_ai_backend(cfg.ai.backend)
    provider = _normalize_ai_provider(cfg.ai.provider)
    if backend == "acpx":
        if provider in {"", "auto"}:
            return ["codex", "claude"]
        if provider == "claude":
            return _unique_preserving_order(["claude", "codex"])
        if provider == "codex":
            return _unique_preserving_order(["codex", "claude"])
        if provider in {"openclaw", "openclaw-agent"}:
            return _unique_preserving_order(["openclaw", "codex", "claude"])
        return [provider]

    if provider in {"", "auto"}:
        return ["codex", "openclaw"]
    if provider in {"openclaw", "openclaw-agent"}:
        return _unique_preserving_order(["openclaw", "codex"])
    if provider == "codex":
        return _unique_preserving_order(["codex", "openclaw"])
    return [provider]


def _probe_ai_provider(cfg: AppConfig, provider: str) -> AiProviderProbe:
    timeout_seconds = min(max(5, cfg.monitor.probe_timeout_seconds), 15)
    backend = _normalize_ai_backend(cfg.ai.backend)
    provider = _normalize_ai_provider(provider)
    cwd = _openclaw_cwd(cfg)

    if backend == "acpx":
        base_argv = [_resolve_acpx_ai_command(cfg), *cfg.ai.acpx_args, "--help"]
        base_cmd = run_cmd(base_argv, timeout_seconds=timeout_seconds, cwd=cwd)
        if not base_cmd.ok:
            return AiProviderProbe(
                provider=provider,
                available=False,
                reason="acpx-command-unavailable",
                argv=base_argv,
                exit_code=base_cmd.exit_code,
                stdout=base_cmd.stdout,
                stderr=base_cmd.stderr,
            )

        if provider == "claude":
            argv = ["claude", "--help"]
            cmd = run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)
            return AiProviderProbe(
                provider=provider,
                available=cmd.ok,
                reason="command-ok" if cmd.ok else "command-unavailable",
                argv=argv,
                exit_code=cmd.exit_code,
                stdout=cmd.stdout,
                stderr=cmd.stderr,
            )

        if provider == "codex":
            argv = [_resolve_codex_ai_command(cfg), "exec", "--help"]
            cmd = run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)
            return AiProviderProbe(
                provider=provider,
                available=cmd.ok,
                reason="command-ok" if cmd.ok else "command-unavailable",
                argv=argv,
                exit_code=cmd.exit_code,
                stdout=cmd.stdout,
                stderr=cmd.stderr,
            )

        if provider == "openclaw":
            argv = [cfg.openclaw.command, "acp", "--help"]
            cmd = run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)
            gateway_ready = probe_status(cfg, log_on_fail=False).ok
            available = cmd.ok and gateway_ready
            reason = "gateway-rpc-ok" if available else "gateway-rpc-unavailable" if cmd.ok else "command-unavailable"
            return AiProviderProbe(
                provider=provider,
                available=available,
                reason=reason,
                argv=argv,
                exit_code=cmd.exit_code,
                stdout=cmd.stdout,
                stderr=cmd.stderr,
            )

        return AiProviderProbe(
            provider=provider,
            available=False,
            reason="unsupported-provider",
            argv=[],
            exit_code=1,
            stdout="",
            stderr=f"unsupported acpx provider: {provider}",
        )

    if provider == "openclaw":
        argv = [cfg.openclaw.command, "models", "status", "--check", "--json"]
        cmd = run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)
        available = cmd.exit_code in {0, 2}
        reason = "models-status-ok" if cmd.exit_code == 0 else "models-status-expiring-auth" if cmd.exit_code == 2 else "models-status-unavailable"
        return AiProviderProbe(
            provider=provider,
            available=available,
            reason=reason,
            argv=argv,
            exit_code=cmd.exit_code,
            stdout=cmd.stdout,
            stderr=cmd.stderr,
        )

    if provider == "codex":
        argv = [_resolve_codex_ai_command(cfg), "exec", "--help"]
        cmd = run_cmd(argv, timeout_seconds=timeout_seconds, cwd=cwd)
        return AiProviderProbe(
            provider=provider,
            available=cmd.ok,
            reason="command-ok" if cmd.ok else "command-unavailable",
            argv=argv,
            exit_code=cmd.exit_code,
            stdout=cmd.stdout,
            stderr=cmd.stderr,
        )

    return AiProviderProbe(
        provider=provider,
        available=False,
        reason="unsupported-provider",
        argv=[],
        exit_code=1,
        stdout="",
        stderr=f"unsupported AI provider: {provider}",
    )


def _build_ai_invocation(
    cfg: AppConfig,
    prompt: str,
    *,
    code_stage: bool,
    provider_override: str | None = None,
) -> tuple[list[str], str | None]:
    backend = _normalize_ai_backend(cfg.ai.backend)
    provider = _normalize_ai_provider(provider_override or cfg.ai.provider)
    if backend == "acpx":
        return _build_acpx_invocation(cfg, provider, one_shot=True), prompt

    if provider in {"openclaw", "openclaw-agent"}:
        vars = {
            "workspace_dir": str(cfg.openclaw.workspace_dir),
            "openclaw_state_dir": str(cfg.openclaw.state_dir),
            "monitor_state_dir": str(cfg.monitor.state_dir),
        }
        agent_args = [Template(x).safe_substitute(vars) for x in cfg.ai.agent_args]
        argv = [_resolve_openclaw_ai_command(cfg), "agent", "--json"]
        if cfg.ai.local:
            argv.append("--local")
        agent_id = (cfg.ai.agent_id or "").strip()
        if agent_id:
            argv += ["--agent", agent_id]
        argv += agent_args
        argv += ["--timeout", str(max(0, cfg.ai.timeout_seconds)), "--message", prompt]
        return argv, None

    return _build_ai_cmd(cfg, code_stage=code_stage), prompt


def _run_ai_repair(
    cfg: AppConfig,
    attempt_dir: Path,
    *,
    code_stage: bool,
    provider_override: str | None = None,
) -> CmdResult:
    prompt_name = "repair_code.md" if code_stage else "repair.md"
    prompt = Template(_load_prompt_text(prompt_name)).safe_substitute(
        {
            "attempt_dir": str(attempt_dir.resolve()),
            "workspace_dir": str(cfg.openclaw.workspace_dir),
            "openclaw_state_dir": str(cfg.openclaw.state_dir),
            "monitor_state_dir": str(cfg.monitor.state_dir),
            "health_cmd": " ".join([cfg.openclaw.command, *cfg.openclaw.health_args]),
            "status_cmd": " ".join([cfg.openclaw.command, *cfg.openclaw.status_args]),
            "logs_cmd": " ".join([cfg.openclaw.command, *cfg.openclaw.logs_args]),
        }
    )

    provider_name = _normalize_ai_provider(provider_override or cfg.ai.provider)
    argv, stdin_text = _build_ai_invocation(
        cfg,
        prompt,
        code_stage=code_stage,
        provider_override=provider_name,
    )
    logging.getLogger("fix_my_claw.repair").warning(
        "AI %s/%s/%s run=%s",
        "code" if code_stage else "config",
        _normalize_ai_backend(cfg.ai.backend),
        provider_name,
        _format_argv(argv),
    )
    res = run_cmd(
        argv,
        timeout_seconds=cfg.ai.timeout_seconds,
        cwd=cfg.openclaw.workspace_dir if cfg.openclaw.workspace_dir.exists() else None,
        stdin_text=stdin_text,
    )
    suffix = provider_name.replace("/", "-")
    _write_attempt_file(attempt_dir, f"ai.{suffix}.argv.txt", " ".join(argv))
    _write_attempt_file(attempt_dir, f"ai.{suffix}.stdout.txt", redact_text(res.stdout))
    _write_attempt_file(attempt_dir, f"ai.{suffix}.stderr.txt", redact_text(res.stderr))
    logging.getLogger("fix_my_claw.repair").warning(
        "AI %s/%s/%s exit=%s duration=%s",
        "code" if code_stage else "config",
        _normalize_ai_backend(cfg.ai.backend),
        provider_name,
        res.exit_code,
        _format_duration_ms(res.duration_ms),
    )
    if res.stderr:
        logging.getLogger("fix_my_claw.repair").warning(
            "AI %s/%s stderr=%s",
            _normalize_ai_backend(cfg.ai.backend),
            provider_name,
            truncate_for_log(res.stderr),
        )
    return res


def _attempt_ai_stage(cfg: AppConfig, attempt_dir: Path, *, code_stage: bool) -> tuple[bool, bool, dict]:
    repair_log = logging.getLogger("fix_my_claw.ai")
    providers = _resolve_ai_provider_candidates(cfg)
    probes = [_probe_ai_provider(cfg, provider) for provider in providers]
    _write_attempt_file(
        attempt_dir,
        f"ai.{'code' if code_stage else 'config'}.providers.json",
        json.dumps([probe.to_json() for probe in probes], ensure_ascii=False, indent=2),
    )

    stage_details: dict[str, Any] = {
        "backend": _normalize_ai_backend(cfg.ai.backend),
        "provider_order": providers,
        "provider_probes": [probe.to_json() for probe in probes],
        "attempts": [],
    }
    used_any = False
    repair_log.warning(
        "%s stage backend=%s providers=%s",
        "code" if code_stage else "config",
        _normalize_ai_backend(cfg.ai.backend),
        ", ".join(
            f"{probe.provider}:{'ok' if probe.available else 'skip'}"
            for probe in probes
        ),
    )

    for probe in probes:
        if not probe.available:
            repair_log.warning(
                "%s stage skip provider=%s reason=%s",
                "code" if code_stage else "config",
                probe.provider,
                probe.reason,
            )
            continue

        used_any = True
        repair_log.warning(
            "%s stage try provider=%s reason=%s",
            "code" if code_stage else "config",
            probe.provider,
            probe.reason,
        )
        res = _run_ai_repair(
            cfg,
            attempt_dir,
            code_stage=code_stage,
            provider_override=probe.provider,
        )
        context_after = _collect_context(cfg, attempt_dir)
        fixed = _probe_is_healthy(cfg)
        stage_details["attempts"].append(
            {
                "provider": probe.provider,
                "reason": probe.reason,
                "result": res.__dict__,
                "context_after": context_after,
                "fixed": fixed,
            }
        )
        if fixed:
            stage_details["selected_provider"] = probe.provider
            repair_log.warning(
                "%s stage restored health via provider=%s",
                "code" if code_stage else "config",
                probe.provider,
            )
            return used_any, True, stage_details

    if used_any:
        repair_log.warning("%s stage completed without restoring health", "code" if code_stage else "config")
    else:
        repair_log.warning("%s stage had no usable providers", "code" if code_stage else "config")
    return used_any, False, stage_details


def attempt_repair(cfg: AppConfig, store: StateStore, *, force: bool) -> RepairResult:
    repair_log = logging.getLogger("fix_my_claw.repair")
    if _probe_is_healthy(cfg):
        repair_log.info("repair skipped: already healthy")
        return RepairResult(attempted=False, fixed=True, used_ai=False, details={"already_healthy": True})

    if not cfg.repair.enabled:
        repair_log.warning("repair skipped: disabled by config")
        return RepairResult(attempted=False, fixed=False, used_ai=False, details={"repair_disabled": True})

    if not store.can_attempt_repair(cfg.monitor.repair_cooldown_seconds, force=force):
        details: dict[str, object] = {"cooldown": True}
        state = store.load()
        if state.last_repair_ts is not None:
            elapsed = _now_ts() - state.last_repair_ts
            remaining = max(0, cfg.monitor.repair_cooldown_seconds - elapsed)
            details["cooldown_remaining_seconds"] = remaining
            repair_log.info("repair skipped: cooldown (%ss remaining)", remaining)
        else:
            repair_log.info("repair skipped: cooldown")
        return RepairResult(attempted=False, fixed=False, used_ai=False, details=details)

    store.mark_repair_attempt()
    attempt_dir = _attempt_dir(cfg)
    details: dict = {"attempt_dir": str(attempt_dir.resolve())}
    repair_log.warning(
        "attempt=%s dir=%s",
        attempt_dir.name,
        attempt_dir.resolve(),
    )

    details["context_before"] = _collect_context(cfg, attempt_dir)
    details["official"] = _run_official_steps(cfg, attempt_dir)
    details["context_after_official"] = _collect_context(cfg, attempt_dir)

    if _probe_is_healthy(cfg):
        repair_log.warning("recovered by official steps: dir=%s", attempt_dir.resolve())
        return RepairResult(attempted=True, fixed=True, used_ai=False, details=details)

    used_ai = False
    if not cfg.ai.enabled:
        repair_log.info("AI-assisted remediation disabled; leaving OpenClaw unhealthy")
    elif not store.can_attempt_ai(
        max_attempts_per_day=cfg.ai.max_attempts_per_day,
        cooldown_seconds=cfg.ai.cooldown_seconds,
    ):
        repair_log.warning("AI-assisted remediation skipped (rate limit / cooldown)")
    else:
        store.mark_ai_attempt()
        details["ai_stage"] = "config"
        used_ai, fixed_by_ai, stage_details = _attempt_ai_stage(cfg, attempt_dir, code_stage=False)
        details["ai_config"] = stage_details
        if fixed_by_ai:
            repair_log.warning("recovered by AI-assisted remediation: dir=%s", attempt_dir.resolve())
            return RepairResult(attempted=True, fixed=True, used_ai=True, details=details)

        if used_ai and cfg.ai.allow_code_changes:
            details["ai_stage"] = "code"
            used_ai_code, fixed_by_ai_code, stage_details_code = _attempt_ai_stage(
                cfg,
                attempt_dir,
                code_stage=True,
            )
            details["ai_code"] = stage_details_code
            used_ai = used_ai or used_ai_code
            if fixed_by_ai_code:
                repair_log.warning("recovered by AI-assisted code remediation: dir=%s", attempt_dir.resolve())
                return RepairResult(attempted=True, fixed=True, used_ai=True, details=details)

    fixed = _probe_is_healthy(cfg)
    repair_log.warning(
        "repair attempt finished: fixed=%s used_ai=%s dir=%s",
        fixed,
        used_ai,
        attempt_dir.resolve(),
    )
    return RepairResult(attempted=True, fixed=fixed, used_ai=used_ai, details=details)


def monitor_loop(cfg: AppConfig, store: StateStore) -> None:
    wd_log = logging.getLogger("fix_my_claw.watchdog")
    wd_log.info(
        "watching every %ss log=%s attempts=%s",
        cfg.monitor.interval_seconds,
        cfg.monitor.log_file,
        cfg.monitor.state_dir / "attempts",
    )
    while True:
        try:
            result = run_check(cfg, store)
            if not result.healthy:
                wd_log.warning(
                    "unhealthy health_exit=%s status_exit=%s -> repair",
                    result.health.get("exit_code"),
                    result.status.get("exit_code"),
                )
                rr = attempt_repair(cfg, store, force=False)
                if rr.attempted:
                    wd_log.warning(
                        "repair fixed=%s used_ai=%s dir=%s",
                        rr.fixed,
                        rr.used_ai,
                        rr.details.get("attempt_dir"),
                    )
                elif rr.details.get("cooldown"):
                    remaining = rr.details.get("cooldown_remaining_seconds")
                    wd_log.info("repair skipped: cooldown (%ss remaining)", remaining if remaining is not None else "?")
                else:
                    wd_log.info("repair skipped: %s", rr.details)
        except Exception as e:
            wd_log.exception("monitor loop error: %s", e)
        time.sleep(cfg.monitor.interval_seconds)


def _default_config_path() -> str:
    return DEFAULT_CONFIG_PATH


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=_default_config_path(),
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_PATH}).",
    )


def _load_or_init_config(path: str, *, init_if_missing: bool) -> AppConfig:
    p = _as_path(path)
    if not p.exists():
        if init_if_missing:
            write_default_config(str(p), overwrite=False)
        else:
            raise FileNotFoundError(f"config not found: {p} (run `fix-my-claw init` or `fix-my-claw up`)")
    return load_config(str(p))


def cmd_init(args: argparse.Namespace) -> int:
    p = write_default_config(args.config, overwrite=args.force)
    print(str(p))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load_or_init_config(args.config, init_if_missing=False)
    setup_logging(cfg)
    _log_startup(cfg, mode="check", config_path=args.config)
    try:
        _ensure_supported_gateway_mode(cfg)
    except UnsupportedOpenClawModeError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = StateStore(cfg.monitor.state_dir)
    result = run_check(cfg, store)
    logging.getLogger("fix_my_claw.openclaw").info(
        "check result=%s health_exit=%s status_exit=%s",
        "healthy" if result.healthy else "unhealthy",
        result.health.get("exit_code"),
        result.status.get("exit_code"),
    )
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False))
    return 0 if result.healthy else 1


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _load_or_init_config(args.config, init_if_missing=False)
    setup_logging(cfg)
    _log_startup(cfg, mode="probe", config_path=args.config)
    report = run_probe(
        cfg,
        live_ai=not args.no_live_ai,
        ai_timeout_seconds=args.ai_timeout_seconds,
    )
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False))
    else:
        print(_render_probe_report(report))
    return 0 if report.ok else 1


def cmd_repair(args: argparse.Namespace) -> int:
    cfg = _load_or_init_config(args.config, init_if_missing=False)
    setup_logging(cfg)
    _log_startup(cfg, mode="repair", config_path=args.config)
    try:
        _ensure_supported_gateway_mode(cfg)
    except UnsupportedOpenClawModeError as e:
        print(str(e), file=sys.stderr)
        return 2
    lock = FileLock(cfg.monitor.state_dir / "fix-my-claw.lock")
    if not lock.acquire(timeout_seconds=0):
        print("another fix-my-claw instance is running", file=sys.stderr)
        return 2
    store = StateStore(cfg.monitor.state_dir)
    try:
        result = attempt_repair(cfg, store, force=args.force)
    finally:
        lock.release()
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False))
    return 0 if result.fixed else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    cfg = _load_or_init_config(args.config, init_if_missing=False)
    setup_logging(cfg)
    _log_startup(cfg, mode="monitor", config_path=args.config)
    try:
        _ensure_supported_gateway_mode(cfg)
    except UnsupportedOpenClawModeError as e:
        print(str(e), file=sys.stderr)
        return 2
    lock = FileLock(cfg.monitor.state_dir / "fix-my-claw.lock")
    if not lock.acquire(timeout_seconds=0):
        print("another fix-my-claw instance is running", file=sys.stderr)
        return 2
    store = StateStore(cfg.monitor.state_dir)
    try:
        monitor_loop(cfg, store)
    finally:
        lock.release()
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    cfg = _load_or_init_config(args.config, init_if_missing=True)
    setup_logging(cfg)
    _log_startup(cfg, mode="up", config_path=args.config)
    try:
        _ensure_supported_gateway_mode(cfg)
    except UnsupportedOpenClawModeError as e:
        print(str(e), file=sys.stderr)
        return 2
    lock = FileLock(cfg.monitor.state_dir / "fix-my-claw.lock")
    if not lock.acquire(timeout_seconds=0):
        print("another fix-my-claw instance is running", file=sys.stderr)
        return 2
    store = StateStore(cfg.monitor.state_dir)
    try:
        monitor_loop(cfg, store)
    finally:
        lock.release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fix-my-claw")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("up", help="One-command start: init default config (if missing) then monitor.")
    _add_config_arg(p_up)
    p_up.set_defaults(func=cmd_up)

    p_init = sub.add_parser("init", help="Write default config (prints config path).")
    _add_config_arg(p_init)
    p_init.add_argument("--force", action="store_true", help="Overwrite config if it already exists.")
    p_init.set_defaults(func=cmd_init)

    p_check = sub.add_parser("check", help="Probe OpenClaw health/status once.")
    _add_config_arg(p_check)
    p_check.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_check.set_defaults(func=cmd_check)

    p_probe = sub.add_parser(
        "probe",
        help="Dry-run capability probe for repair paths, commands, and AI backends.",
    )
    _add_config_arg(p_probe)
    p_probe.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_probe.add_argument(
        "--no-live-ai",
        action="store_true",
        help="Skip live AI dry-runs; only run static command/path/config checks.",
    )
    p_probe.add_argument(
        "--ai-timeout-seconds",
        type=int,
        default=45,
        help="Outer timeout for each live AI dry-run (default: 45).",
    )
    p_probe.set_defaults(func=cmd_probe)

    p_repair = sub.add_parser("repair", help="Run official repair (and optional AI repair) once if unhealthy.")
    _add_config_arg(p_repair)
    p_repair.add_argument("--force", action="store_true", help="Ignore cooldown and attempt repair.")
    p_repair.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_repair.set_defaults(func=cmd_repair)

    p_mon = sub.add_parser("monitor", help="Run 24/7 monitor loop (requires config to exist).")
    _add_config_arg(p_mon)
    p_mon.set_defaults(func=cmd_monitor)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    raise SystemExit(code)
