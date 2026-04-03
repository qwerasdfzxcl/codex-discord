import asyncio
import io
import json
import logging
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

DISCORD_MESSAGE_LIMIT = 1900
DISCORD_FILE_FALLBACK_LIMIT = 8000
DISCORD_THREAD_NAME_LIMIT = 100
DEFAULT_TIMEOUT_SECONDS = 900
BREAK_CONFIRM_TIMEOUT_SECONDS = 10
DEFAULT_HISTORY_MESSAGES = 20
DEFAULT_MAX_PROMPT_CHARS = 12000
EMBED_DESCRIPTION_LIMIT = 4000
EMBED_FIELD_VALUE_LIMIT = 1024
THREAD_BINDING_PATTERN = re.compile(r"^\[bot:(main|staging)\]\s*")
STATE_DIRECTORY_NAME = ".codex-discord-state"
BACKGROUND_OPERATIONS_DIR_NAME = "background-operations"
BACKGROUND_OPERATION_POLL_SECONDS = 5
EMBED_COLOR_INFO = 0x5865F2
EMBED_COLOR_SUCCESS = 0x57F287
EMBED_COLOR_WARNING = 0xFEE75C
EMBED_COLOR_ERROR = 0xED4245
NVM_NODE_BIN_PATH = Path.home() / ".nvm" / "versions" / "node" / "v22.22.2" / "bin"


class ConfigError(Exception):
    pass


def build_codex_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if NVM_NODE_BIN_PATH.exists():
        current_path = env.get("PATH", "")
        env["PATH"] = f"{NVM_NODE_BIN_PATH}{os.pathsep}{current_path}" if current_path else str(NVM_NODE_BIN_PATH)
    return env


@dataclass(frozen=True)
class AppConfig:
    bot_role: str
    token: str
    allowed_user_id: int
    checkout_path: Path
    config_path: Path
    guild_id: int | None
    channel_workspaces: dict[int, Path]
    timeout_seconds: int
    history_messages: int
    max_prompt_chars: int
    codex_bin: str
    codex_global_args: list[str]
    codex_exec_args: list[str]
    restart_args: list[str]
    restart_staging_args: list[str]
    deploy_args: list[str]


@dataclass
class ExecutionRecord:
    command_name: str
    status: str
    target_path: Path | None
    started_at: str
    duration_seconds: float
    summary: str


@dataclass
class ProcessResult:
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass
class ThreadSessionRecord:
    session_id: str
    workspace: Path
    updated_at: str


@dataclass
class ThreadPolicyRecord:
    dangerously_bypass_approvals_and_sandbox: bool
    updated_at: str


@dataclass
class PendingApproval:
    runtime: "ThreadSessionRuntime"
    request_id: int | str
    method: str
    params: dict[str, object]
    requested_at: str


@dataclass
class PendingRepoDeletion:
    channel_id: int
    workspace: Path
    requested_at: str
    requested_by_user_id: int


@dataclass
class BackgroundOperation:
    operation_id: str
    command_name: str
    args: list[str]
    target_channel_id: int
    metadata_path: Path
    log_path: Path


@dataclass
class GitSummary:
    path: Path
    is_repo: bool
    branch: str = "(알 수 없음)"
    commit: str = "(알 수 없음)"
    changed_files: int = 0
    status_lines: list[str] = field(default_factory=list)
    stderr: str = ""


@dataclass
class DiffSummary:
    path: Path
    git: GitSummary
    diff_stat: str
    status_stderr: str = ""
    diff_stderr: str = ""
    staged_count: int = 0
    unstaged_count: int = 0
    untracked_count: int = 0


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunk_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return ["(출력 없음)"]

    chunks: list[str] = []
    remaining = stripped
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


def split_thread_binding(name: str) -> tuple[str | None, str]:
    match = THREAD_BINDING_PATTERN.match(name)
    if match is None:
        return None, name.strip()
    bound_role = match.group(1)
    base_name = name[match.end() :].strip()
    return bound_role, base_name


def thread_target_role(name: str) -> str:
    bound_role, _ = split_thread_binding(name)
    if bound_role == "staging":
        return "staging"
    return "main"


def format_thread_binding_name(bot_role: str, current_name: str) -> str:
    _, base_name = split_thread_binding(current_name)
    prefix = "[bot:staging] " if bot_role == "staging" else ""
    normalized_base_name = base_name or "session"
    remaining = DISCORD_THREAD_NAME_LIMIT - len(prefix)
    return prefix + normalized_base_name[:remaining].rstrip()


def command_name_for_role(base_name: str, bot_role: str) -> str:
    if bot_role == "staging":
        return f"{base_name}-staging"
    return base_name


def format_code_block(text: str) -> str:
    normalized = text.strip() or "(출력 없음)"
    safe = normalized.replace("```", "'''")
    return f"```text\n{safe}\n```"


def output_title_for_prefix(filename_prefix: str) -> str:
    if filename_prefix == "status":
        return "현재 상태"
    if filename_prefix == "diff":
        return "변경 사항"
    if filename_prefix == "restart-staging":
        return "스테이징 재시작 결과"
    if filename_prefix == "restart":
        return "재시작 결과"
    if filename_prefix == "deploy":
        return "배포 결과"
    return "실행 결과"


def normalize_repo_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower())
    slug = slug.strip("-.")
    return slug[:90]


def normalize_github_clone_url(url: str) -> str | None:
    candidate = url.strip()
    if not candidate:
        return None

    scp_style_match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", candidate)
    if scp_style_match is not None:
        owner, repo = scp_style_match.groups()
        return f"https://github.com/{owner}/{repo}.git"

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https", "ssh"}:
        return None
    if parsed.hostname != "github.com":
        return None

    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\.git)?", repo):
        return None

    normalized_repo = repo[:-4] if repo.endswith(".git") else repo
    return f"https://github.com/{owner}/{normalized_repo}.git"


def truncate_text(text: str, limit: int) -> str:
    normalized = text.strip() or "없음"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 4].rstrip() + "\n..."


def format_code_field(text: str, limit: int = EMBED_FIELD_VALUE_LIMIT) -> str:
    wrapped_limit = max(32, limit - 12)
    return format_code_block(truncate_text(text, wrapped_limit))


def parse_command_args(name: str, value: object, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{name} must be a JSON array of non-empty strings")
    return list(value)


def message_to_prompt(message: discord.Message) -> str:
    parts: list[str] = []
    content = message.content.strip()
    if content:
        parts.append(content)

    if message.attachments:
        attachment_lines = [f"- {attachment.filename}: {attachment.url}" for attachment in message.attachments]
        parts.append("Attachments:\n" + "\n".join(attachment_lines))

    return "\n\n".join(parts).strip()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def configure_logging() -> None:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def infer_checkout_path(bot_role: str) -> Path:
    checkout_path = Path(__file__).resolve().parent
    container_root = checkout_path.parent
    expected_checkout_name = "main" if bot_role == "main" else "staging"
    expected_checkout_path = (container_root / expected_checkout_name).resolve()

    if checkout_path != expected_checkout_path:
        raise ConfigError(
            f"BOT_ROLE={bot_role} expects app.py under {expected_checkout_path}, got {checkout_path}"
        )
    return checkout_path


def read_env_settings() -> dict[str, object]:
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_BOT_TOKEN is missing from .env")

    allowed_user_raw = os.getenv("DISCORD_ALLOWED_USER_ID", "").strip()
    if not allowed_user_raw:
        raise ConfigError("DISCORD_ALLOWED_USER_ID is missing from .env")

    bot_role = os.getenv("BOT_ROLE", "").strip().lower()
    if bot_role not in {"main", "staging"}:
        raise ConfigError("BOT_ROLE must be either 'main' or 'staging'")

    config_path_raw = os.getenv("CODEX_DISCORD_CONFIG", "config/config.json").strip()
    if not config_path_raw:
        raise ConfigError("CODEX_DISCORD_CONFIG must not be empty")

    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    checkout_path = infer_checkout_path(bot_role)
    raw_config_path = Path(config_path_raw).expanduser()
    if raw_config_path.is_absolute():
        config_path = raw_config_path.resolve()
    else:
        config_path = (checkout_path / raw_config_path).resolve()

    if not checkout_path.is_dir():
        raise ConfigError(f"Derived checkout path does not exist: {checkout_path}")

    guild_id = int(guild_id_raw) if guild_id_raw else None

    return {
        "token": token,
        "allowed_user_id": int(allowed_user_raw),
        "bot_role": bot_role,
        "checkout_path": checkout_path,
        "config_path": config_path,
        "guild_id": guild_id,
    }


def load_app_config() -> AppConfig:
    env_settings = read_env_settings()
    config_path = env_settings["config_path"]
    assert isinstance(config_path, Path)

    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    raw_channels = raw.get("channels", {})
    if not raw_channels:
        raise ConfigError("channels must contain at least one channel_id -> workspace mapping")

    checkout_path = env_settings["checkout_path"]
    assert isinstance(checkout_path, Path)
    config_dir = config_path.parent

    channel_workspaces: dict[int, Path] = {}
    for channel_id_raw, workspace_raw in raw_channels.items():
        channel_id = int(channel_id_raw)
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            raise ConfigError(f"workspace path missing for channel {channel_id}")

        raw_workspace_path = Path(workspace_raw).expanduser()
        if raw_workspace_path.is_absolute():
            workspace_path = raw_workspace_path.resolve()
        else:
            workspace_path = (config_dir / raw_workspace_path).resolve()
        if not workspace_path.is_dir():
            raise ConfigError(f"workspace directory not found for channel {channel_id}: {workspace_path}")
        if env_settings["bot_role"] == "main" and is_relative_to(workspace_path, checkout_path):
            raise ConfigError(
                f"main bot cannot map channel {channel_id} to its own checkout: {workspace_path}"
            )

        channel_workspaces[channel_id] = workspace_path

    timeout_seconds = int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    history_messages = int(raw.get("history_messages", DEFAULT_HISTORY_MESSAGES))
    max_prompt_chars = int(raw.get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))
    codex_bin = str(raw.get("codex_bin", "codex"))
    codex_global_args = parse_command_args(
        "codex_global_args",
        raw.get("codex_global_args"),
        default=[],
    )
    codex_exec_args = parse_command_args(
        "codex_exec_args",
        raw.get("codex_exec_args"),
        default=["--model", "gpt-5.4", "--skip-git-repo-check", "--color", "never"],
    )

    main_commands = raw.get("main_commands", {})
    if main_commands is None:
        main_commands = {}
    if not isinstance(main_commands, dict):
        raise ConfigError("main_commands must be a JSON object")

    restart_staging_args = parse_command_args(
        "main_commands.restart_staging",
        main_commands.get("restart_staging"),
        default=["./scripts/systemd-restart-service.sh", "codex-discord-staging"],
    )
    restart_args = parse_command_args(
        "main_commands.restart",
        main_commands.get("restart"),
        default=["./scripts/systemd-restart-service.sh", "codex-discord-main"],
    )
    deploy_args = parse_command_args(
        "main_commands.deploy",
        main_commands.get("deploy"),
        default=["./scripts/deploy-prod.sh"],
    )

    if timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be greater than 0")
    if history_messages <= 0:
        raise ConfigError("history_messages must be greater than 0")
    if max_prompt_chars <= 0:
        raise ConfigError("max_prompt_chars must be greater than 0")

    return AppConfig(
        bot_role=str(env_settings["bot_role"]),
        token=str(env_settings["token"]),
        allowed_user_id=int(env_settings["allowed_user_id"]),
        checkout_path=env_settings["checkout_path"],
        config_path=config_path,
        guild_id=env_settings["guild_id"],
        channel_workspaces=channel_workspaces,
        timeout_seconds=timeout_seconds,
        history_messages=history_messages,
        max_prompt_chars=max_prompt_chars,
        codex_bin=codex_bin,
        codex_global_args=codex_global_args,
        codex_exec_args=codex_exec_args,
        restart_args=restart_args,
        restart_staging_args=restart_staging_args,
        deploy_args=deploy_args,
    )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, ThreadSessionRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.exception("Failed to load session store from %s", self.path)
            return

        raw_threads = payload.get("threads", {})
        if not isinstance(raw_threads, dict):
            return

        for thread_id, raw_record in raw_threads.items():
            if not isinstance(raw_record, dict):
                continue
            session_id = raw_record.get("session_id")
            workspace = raw_record.get("workspace")
            updated_at = raw_record.get("updated_at")
            if not isinstance(session_id, str) or not session_id:
                continue
            if not isinstance(workspace, str) or not workspace:
                continue
            if not isinstance(updated_at, str) or not updated_at:
                updated_at = now_utc_iso()
            self._records[thread_id] = ThreadSessionRecord(
                session_id=session_id,
                workspace=Path(workspace),
                updated_at=updated_at,
            )

    def _save(self) -> None:
        payload = {
            "threads": {
                thread_id: {
                    "session_id": record.session_id,
                    "workspace": str(record.workspace),
                    "updated_at": record.updated_at,
                }
                for thread_id, record in self._records.items()
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, thread_id: int) -> ThreadSessionRecord | None:
        return self._records.get(str(thread_id))

    def set(self, thread_id: int, session_id: str, workspace: Path) -> None:
        self._records[str(thread_id)] = ThreadSessionRecord(
            session_id=session_id,
            workspace=workspace.resolve(),
            updated_at=now_utc_iso(),
        )
        self._save()

    def delete(self, thread_id: int) -> None:
        if str(thread_id) not in self._records:
            return
        self._records.pop(str(thread_id), None)
        self._save()


class ThreadPolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, ThreadPolicyRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.exception("Failed to load thread policy store from %s", self.path)
            return

        raw_threads = payload.get("threads", {})
        if not isinstance(raw_threads, dict):
            return

        for thread_id, raw_record in raw_threads.items():
            if not isinstance(raw_record, dict):
                continue
            dangerously_bypass = raw_record.get("dangerously_bypass_approvals_and_sandbox")
            updated_at = raw_record.get("updated_at")
            if not isinstance(dangerously_bypass, bool):
                continue
            if not isinstance(updated_at, str) or not updated_at:
                updated_at = now_utc_iso()
            self._records[thread_id] = ThreadPolicyRecord(
                dangerously_bypass_approvals_and_sandbox=dangerously_bypass,
                updated_at=updated_at,
            )

    def _save(self) -> None:
        payload = {
            "threads": {
                thread_id: {
                    "dangerously_bypass_approvals_and_sandbox": record.dangerously_bypass_approvals_and_sandbox,
                    "updated_at": record.updated_at,
                }
                for thread_id, record in self._records.items()
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, thread_id: int) -> ThreadPolicyRecord | None:
        return self._records.get(str(thread_id))

    def set_danger_mode(self, thread_id: int, enabled: bool) -> None:
        if enabled:
            self._records[str(thread_id)] = ThreadPolicyRecord(
                dangerously_bypass_approvals_and_sandbox=True,
                updated_at=now_utc_iso(),
            )
        else:
            self._records.pop(str(thread_id), None)
        self._save()


@dataclass
class AppServerTurnState:
    turn_id: str
    source_message_id: int
    completion_future: asyncio.Future[dict[str, object]]
    agent_messages_sent: int = 0
    started_at: str = field(default_factory=now_utc_iso)
    last_event_at: str = field(default_factory=now_utc_iso)
    last_event: str = "turn created"


class AppServerApprovalView(discord.ui.View):
    def __init__(self, bot: "CodexDiscordBot", approval_id: str) -> None:
        super().__init__(timeout=3600)
        self.bot = bot
        self.approval_id = approval_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.config.allowed_user_id:
            await self.bot.send_interaction_embed(
                interaction,
                title="승인 권한 없음",
                description="이 승인 요청을 처리할 권한이 없어요.",
                tone="error",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="승인", style=discord.ButtonStyle.danger)
    async def approve(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_app_server_approval_click(interaction, self.approval_id, approved=True, view=self)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.secondary)
    async def deny(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_app_server_approval_click(interaction, self.approval_id, approved=False, view=self)


class DeleteRepoConfirmationView(discord.ui.View):
    def __init__(self, bot: "CodexDiscordBot", deletion_id: str) -> None:
        super().__init__(timeout=1800)
        self.bot = bot
        self.deletion_id = deletion_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        pending = self.bot.pending_repo_deletions.get(self.deletion_id)
        if pending is None:
            await self.bot.send_interaction_embed(
                interaction,
                title="삭제 요청 만료",
                description="이 삭제 요청은 더 이상 유효하지 않아요.",
                tone="warning",
                ephemeral=True,
            )
            return False
        if interaction.user.id != pending.requested_by_user_id:
            await self.bot.send_interaction_embed(
                interaction,
                title="삭제 권한 없음",
                description="이 삭제 요청을 시작한 사용자만 최종 삭제를 진행할 수 있어요.",
                tone="error",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="최종 삭제", style=discord.ButtonStyle.danger)
    async def confirm(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_delete_repo_confirmation(interaction, self.deletion_id, confirmed=True, view=self)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_delete_repo_confirmation(interaction, self.deletion_id, confirmed=False, view=self)


class ThreadSessionRuntime:
    def __init__(self, bot: "CodexDiscordBot", discord_thread_id: int, workspace: Path) -> None:
        self.bot = bot
        self.discord_thread_id = discord_thread_id
        self.workspace = workspace.resolve()
        self.process: asyncio.subprocess.Process | None = None
        self.stdout_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.pending_requests: dict[int | str, asyncio.Future[dict[str, object]]] = {}
        self.pending_turn_state: AppServerTurnState | None = None
        self.current_turns: dict[str, AppServerTurnState] = {}
        self.next_request_id = 1
        self.started = False
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()

    def get_active_turn_state(self) -> AppServerTurnState | None:
        if self.pending_turn_state is not None:
            return self.pending_turn_state
        if not self.current_turns:
            return None
        return max(self.current_turns.values(), key=lambda state: state.last_event_at)

    def note_turn_event(self, event: str, *, turn_id: str | None = None) -> None:
        state: AppServerTurnState | None = None
        if turn_id is not None:
            state = self.current_turns.get(turn_id)
        if state is None:
            state = self.get_active_turn_state()
        if state is None:
            return
        state.last_event = event
        state.last_event_at = now_utc_iso()

    def get_active_turn_id(self) -> str | None:
        state = self.get_active_turn_state()
        if state is None or not state.turn_id:
            return None
        return state.turn_id

    async def ensure_started(self) -> None:
        if self.process is not None and self.process.returncode is None and self.started:
            return

        async with self.start_lock:
            if self.process is not None and self.process.returncode is None and self.started:
                return

            self.process = await asyncio.create_subprocess_exec(
                self.bot.config.codex_bin,
                "app-server",
                "--listen",
                "stdio://",
                cwd=str(self.workspace),
                env=build_codex_subprocess_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.stdout_task = asyncio.create_task(self.read_stdout_loop())
            self.stderr_task = asyncio.create_task(self.read_stderr_loop())
            request_id = self.next_request_id
            self.next_request_id += 1
            future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
            self.pending_requests[request_id] = future
            await self.write_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": f"codex-discord-{self.bot.config.bot_role}",
                            "version": "0.1",
                        },
                        "capabilities": None,
                    },
                }
            )
            await future
            self.started = True

    async def read_stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stream = self.process.stdout
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                logging.warning("app-server non-JSON stdout thread=%s line=%s", self.discord_thread_id, text)
                continue
            await self.handle_rpc_message(message)

        self.started = False
        self.fail_pending(RuntimeError("app-server process exited"))

    async def read_stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        stream = self.process.stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logging.warning("app-server stderr thread=%s %s", self.discord_thread_id, text)

    def fail_pending(self, error: Exception) -> None:
        for future in self.pending_requests.values():
            if not future.done():
                future.set_exception(error)
        self.pending_requests.clear()

        if self.pending_turn_state and not self.pending_turn_state.completion_future.done():
            self.pending_turn_state.completion_future.set_exception(error)
        for state in self.current_turns.values():
            if not state.completion_future.done():
                state.completion_future.set_exception(error)
        self.current_turns.clear()
        self.pending_turn_state = None

    async def shutdown(self) -> None:
        self.started = False
        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        for task in (self.stdout_task, self.stderr_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("Failed while shutting down app-server task thread=%s", self.discord_thread_id)

        self.process = None
        self.stdout_task = None
        self.stderr_task = None
        self.fail_pending(RuntimeError("app-server runtime shut down"))

    async def handle_rpc_message(self, message: dict[str, object]) -> None:
        message_id = message.get("id")
        method = message.get("method")

        if method is not None and message_id is not None:
            params = message.get("params")
            if isinstance(method, str) and isinstance(params, dict):
                await self.bot.handle_app_server_request(self, message_id, method, params)
            return

        if message_id is not None:
            future = self.pending_requests.pop(message_id, None)
            if future is None:
                return
            if "error" in message:
                future.set_exception(RuntimeError(str(message["error"])))
                return
            result = message.get("result")
            if isinstance(result, dict):
                future.set_result(result)
            else:
                future.set_result({})
            return

        if isinstance(method, str):
            params = message.get("params")
            if isinstance(params, dict):
                await self.handle_notification(method, params)

    async def handle_notification(self, method: str, params: dict[str, object]) -> None:
        if method == "turn/started":
            turn = params.get("turn")
            if isinstance(turn, dict):
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and self.pending_turn_state is not None:
                    self.pending_turn_state.turn_id = turn_id
                    self.pending_turn_state.last_event = "turn/started received"
                    self.pending_turn_state.last_event_at = now_utc_iso()
                    self.current_turns[turn_id] = self.pending_turn_state
                    self.pending_turn_state = None
                    logging.info("app-server turn started thread=%s turn=%s", self.discord_thread_id, turn_id)
            return

        if method == "item/completed":
            turn_id = params.get("turnId")
            item = params.get("item")
            if not isinstance(turn_id, str) or not isinstance(item, dict):
                return
            turn_state = self.current_turns.get(turn_id)
            if turn_state is None:
                return

            item_type = item.get("type")
            self.note_turn_event(f"item/completed:{item_type}", turn_id=turn_id)
            if item_type == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    logging.info(
                        "app-server agent message thread=%s turn=%s chars=%s",
                        self.discord_thread_id,
                        turn_id,
                        len(text.strip()),
                    )
                    await self.bot.post_agent_message(
                        self.discord_thread_id,
                        turn_state.source_message_id,
                        text.strip(),
                        reply_to_source=turn_state.agent_messages_sent == 0,
                    )
                    turn_state.agent_messages_sent += 1
            return

        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            turn_id = turn.get("id")
            if not isinstance(turn_id, str):
                return
            turn_state = self.current_turns.pop(turn_id, None)
            if turn_state is None:
                return
            turn_state.last_event = "turn/completed received"
            turn_state.last_event_at = now_utc_iso()
            if not turn_state.completion_future.done():
                turn_state.completion_future.set_result(turn)

            status = turn.get("status")
            error = turn.get("error")
            logging.info(
                "app-server turn completed thread=%s turn=%s status=%s",
                self.discord_thread_id,
                turn_id,
                status,
            )
            if turn_state.agent_messages_sent == 0:
                if status == "failed" and isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message.strip():
                        await self.bot.post_agent_message(
                            self.discord_thread_id,
                            turn_state.source_message_id,
                            f"작업이 실패했어요: {message}",
                            reply_to_source=True,
                        )
                elif status == "interrupted":
                    await self.bot.post_agent_message(
                        self.discord_thread_id,
                        turn_state.source_message_id,
                        "작업이 중단됐어요.",
                        reply_to_source=True,
                    )

    async def write_json(self, payload: dict[str, object]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("app-server is not running")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        async with self.write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def send_request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
        request_id, future = await self.start_request(method, params)
        return await future

    async def start_request(
        self,
        method: str,
        params: dict[str, object] | None,
    ) -> tuple[int, asyncio.Future[dict[str, object]]]:
        await self.ensure_started()
        request_id = self.next_request_id
        self.next_request_id += 1
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending_requests[request_id] = future
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self.write_json(payload)
        return request_id, future

    def abandon_request(self, request_id: int, future: asyncio.Future[dict[str, object]]) -> None:
        current = self.pending_requests.get(request_id)
        if current is future:
            self.pending_requests.pop(request_id, None)

    async def send_response(self, request_id: int | str, result: dict[str, object]) -> None:
        if self.process is None or self.process.returncode is not None or not self.started:
            raise RuntimeError("app-server is not running")
        await self.write_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def ensure_codex_thread(self) -> str:
        model = self.bot.resolve_codex_model()
        resume_params: dict[str, object] = {
            "threadId": "",
            "cwd": str(self.workspace),
            "approvalPolicy": self.bot.resolve_approval_policy(self.discord_thread_id),
            "approvalsReviewer": "user",
            "sandbox": self.bot.resolve_sandbox_mode(self.discord_thread_id),
            "persistExtendedHistory": False,
        }
        start_params: dict[str, object] = {
            "cwd": str(self.workspace),
            "approvalPolicy": self.bot.resolve_approval_policy(self.discord_thread_id),
            "approvalsReviewer": "user",
            "sandbox": self.bot.resolve_sandbox_mode(self.discord_thread_id),
            "serviceName": f"codex-discord-{self.bot.config.bot_role}",
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
        }
        if model:
            resume_params["model"] = model
            start_params["model"] = model

        record = self.bot.get_thread_session(self.discord_thread_id, self.workspace)
        if record is not None:
            try:
                resume_params["threadId"] = record.session_id
                response = await self.send_request("thread/resume", resume_params)
            except Exception:
                logging.exception("Failed to resume app-server thread for Discord thread %s", self.discord_thread_id)
                self.bot.session_store.delete(self.discord_thread_id)
                response = await self.send_request("thread/start", start_params)
        else:
            response = await self.send_request("thread/start", start_params)

        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("app-server did not return a thread object")
        codex_thread_id = thread.get("id")
        if not isinstance(codex_thread_id, str) or not codex_thread_id:
            raise RuntimeError("app-server thread response is missing id")
        self.bot.session_store.set(self.discord_thread_id, codex_thread_id, self.workspace)
        return codex_thread_id

    async def run_turn(self, prompt: str, source_message: discord.Message) -> dict[str, object]:
        codex_thread_id = await self.ensure_codex_thread()
        completion_future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending_turn_state = AppServerTurnState(
            turn_id="",
            source_message_id=source_message.id,
            completion_future=completion_future,
            last_event="turn/start requested",
        )
        logging.info(
            "starting turn thread=%s source_message=%s session=%s",
            self.discord_thread_id,
            source_message.id,
            codex_thread_id,
        )
        response = await self.send_request(
            "turn/start",
            {
                "threadId": codex_thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": prompt,
                        "text_elements": [],
                    }
                ],
                "cwd": str(self.workspace),
                "sandboxPolicy": self.bot.resolve_turn_sandbox_policy(self.discord_thread_id),
            },
        )
        turn = response.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and self.pending_turn_state is not None:
                self.pending_turn_state.turn_id = turn_id
                self.pending_turn_state.last_event = "turn/start response received"
                self.pending_turn_state.last_event_at = now_utc_iso()
                self.current_turns[turn_id] = self.pending_turn_state
                self.pending_turn_state = None
                logging.info("turn/start response thread=%s turn=%s", self.discord_thread_id, turn_id)

        return await asyncio.wait_for(completion_future, timeout=self.bot.config.timeout_seconds)

    async def interrupt_active_turn(self) -> tuple[bool, str]:
        record = self.bot.get_thread_session(self.discord_thread_id, self.workspace)
        if record is None:
            return False, "현재 스레드에 연결된 Codex 세션이 없어요."

        state = self.get_active_turn_state()
        if state is None or not state.turn_id:
            return False, "중단할 활성 turn이 없어요."
        if state.completion_future.done():
            return False, "현재 turn은 이미 끝난 상태예요."

        turn_id = state.turn_id

        self.note_turn_event("turn/interrupt requested", turn_id=turn_id)
        request_id, request_future = await self.start_request(
            "turn/interrupt",
            {
                "threadId": record.session_id,
                "turnId": turn_id,
            },
        )
        logging.warning(
            "turn interrupt dispatched thread=%s turn=%s request=%s",
            self.discord_thread_id,
            turn_id,
            request_id,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + BREAK_CONFIRM_TIMEOUT_SECONDS
        wait_targets: set[asyncio.Future[dict[str, object]]] = {request_future, state.completion_future}
        turn: dict[str, object] | None = None
        while turn is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.abandon_request(request_id, request_future)
                logging.warning(
                    "turn interrupt confirmation timed out thread=%s turn=%s timeout=%ss",
                    self.discord_thread_id,
                    turn_id,
                    BREAK_CONFIRM_TIMEOUT_SECONDS,
                )
                return (
                    False,
                    f"중단 요청은 보냈지만 {BREAK_CONFIRM_TIMEOUT_SECONDS}초 안에 실제 중단을 확인하지 못했어요.",
                )

            done, _ = await asyncio.wait(wait_targets, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                continue

            if request_future in done:
                try:
                    request_future.result()
                except Exception as exc:
                    self.abandon_request(request_id, request_future)
                    logging.exception(
                        "turn interrupt request failed thread=%s turn=%s request=%s",
                        self.discord_thread_id,
                        turn_id,
                        request_id,
                    )
                    return False, f"중단 요청 전달 자체가 실패했어요.\n`{exc}`"
                wait_targets.discard(request_future)

            if state.completion_future in done:
                turn = state.completion_future.result()

        self.abandon_request(request_id, request_future)
        status = str(turn.get("status", "unknown")) if isinstance(turn, dict) else "unknown"
        if status == "interrupted":
            logging.warning("turn interrupt confirmed thread=%s turn=%s", self.discord_thread_id, turn_id)
            return True, f"현재 작업 중이던 turn `{turn_id}`이 실제로 중단됐어요."
        if status == "completed":
            return False, f"중단 요청 전에 turn `{turn_id}`이 이미 완료됐어요."
        if status == "failed":
            return False, f"turn `{turn_id}`이 중단 대신 실패 상태로 끝났어요."
        return False, f"turn `{turn_id}`이 예상과 다른 상태 `{status}`로 끝났어요."


class CodexDiscordBot(commands.Bot):
    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.thread_locks: dict[int, asyncio.Lock] = {}
        self.session_runtimes: dict[int, ThreadSessionRuntime] = {}
        self.pending_approvals: dict[str, PendingApproval] = {}
        self.pending_repo_deletions: dict[str, PendingRepoDeletion] = {}
        self.background_notifications_task: asyncio.Task[None] | None = None
        self.background_notifications_lock = asyncio.Lock()
        self.last_execution: ExecutionRecord | None = None
        state_path = self.config.checkout_path / STATE_DIRECTORY_NAME / f"{self.config.bot_role}-app-server-threads.json"
        self.session_store = SessionStore(state_path)
        policy_state_path = self.config.checkout_path / STATE_DIRECTORY_NAME / f"{self.config.bot_role}-thread-policies.json"
        self.thread_policy_store = ThreadPolicyStore(policy_state_path)

    def build_embed(self, title: str, description: str | None = None, tone: str = "info") -> discord.Embed:
        color = EMBED_COLOR_INFO
        if tone == "success":
            color = EMBED_COLOR_SUCCESS
        elif tone == "warning":
            color = EMBED_COLOR_WARNING
        elif tone == "error":
            color = EMBED_COLOR_ERROR

        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_footer(text=f"codex-discord · {self.config.bot_role}")
        return embed

    def add_embed_field(self, embed: discord.Embed, name: str, value: str, *, inline: bool = False, code: bool = False) -> None:
        if code:
            embed.add_field(name=name, value=format_code_field(value), inline=inline)
            return
        embed.add_field(name=name, value=truncate_text(value, EMBED_FIELD_VALUE_LIMIT), inline=inline)

    async def send_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        kwargs: dict[str, object] = {"embed": embed}
        if ephemeral:
            kwargs["ephemeral"] = True
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def send_channel_message(
        self,
        channel: discord.abc.Messageable,
        *,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        kwargs: dict[str, object] = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        await channel.send(**kwargs)

    async def send_interaction_embed(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str | None = None,
        *,
        tone: str = "info",
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        embed = self.build_embed(title, description, tone=tone)
        await self.send_interaction_message(
            interaction,
            embed=embed,
            ephemeral=ephemeral,
            view=view,
            file=file,
        )

    async def send_channel_embed(
        self,
        channel: discord.abc.Messageable,
        title: str,
        description: str | None = None,
        *,
        tone: str = "info",
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        embed = self.build_embed(title, description, tone=tone)
        await self.send_channel_message(channel, embed=embed, view=view, file=file)

    async def reply_with_output_file(
        self,
        interaction: discord.Interaction,
        title: str,
        filename_prefix: str,
        text: str,
        *,
        tone: str = "info",
    ) -> None:
        filename = f"{filename_prefix}.txt"
        file_data = io.BytesIO(text.encode("utf-8", errors="replace"))
        file = discord.File(file_data, filename=filename)
        await self.send_interaction_embed(
            interaction,
            title=title,
            description=f"내용이 길어서 `{filename}` 파일로 첨부했어요.",
            tone=tone,
            file=file,
        )

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id) if self.config.guild_id else None
        self.tree.on_error = self.on_tree_error
        self.tree.clear_commands(guild=guild)

        ping_command.name = command_name_for_role("ping", self.config.bot_role)
        new_session_command.name = command_name_for_role("new-session", self.config.bot_role)
        new_repo_command.name = command_name_for_role("new-repo", self.config.bot_role)
        delete_repo_command.name = command_name_for_role("delete-repo", self.config.bot_role)
        status_command.name = command_name_for_role("status", self.config.bot_role)
        diff_command.name = command_name_for_role("diff", self.config.bot_role)
        break_command.name = command_name_for_role("break", self.config.bot_role)
        danger_on_command.name = command_name_for_role("danger-on", self.config.bot_role)
        danger_off_command.name = command_name_for_role("danger-off", self.config.bot_role)
        restart_command.name = "restart"
        restart_staging_command.name = "restart-staging"
        deploy_command.name = "deploy"

        self.tree.add_command(ping_command, guild=guild)
        self.tree.add_command(new_session_command, guild=guild)
        self.tree.add_command(new_repo_command, guild=guild)
        self.tree.add_command(delete_repo_command, guild=guild)
        self.tree.add_command(status_command, guild=guild)
        self.tree.add_command(diff_command, guild=guild)
        self.tree.add_command(break_command, guild=guild)
        self.tree.add_command(danger_on_command, guild=guild)
        self.tree.add_command(danger_off_command, guild=guild)

        if self.config.bot_role == "main":
            self.tree.add_command(restart_command, guild=guild)
            self.tree.add_command(restart_staging_command, guild=guild)
            self.tree.add_command(deploy_command, guild=guild)

        if guild is not None:
            await self.tree.sync(guild=guild)
            logging.info("Synced slash commands to guild %s", guild.id)
        else:
            await self.tree.sync()
            logging.info("Synced global slash commands")

    async def on_ready(self) -> None:
        if self.background_notifications_task is None or self.background_notifications_task.done():
            self.background_notifications_task = asyncio.create_task(self.background_notification_loop())
        logging.info(
            "Logged in as %s role=%s checkout=%s",
            self.user,
            self.config.bot_role,
            self.config.checkout_path,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None or message.author.id == self.user.id:
            return
        if message.author.id != self.config.allowed_user_id:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        workspace = self.config.channel_workspaces.get(thread.parent_id or 0)
        if workspace is None:
            return
        if thread_target_role(thread.name) != self.config.bot_role:
            return
        prompt = message_to_prompt(message)
        if not prompt:
            return
        if len(prompt) > self.config.max_prompt_chars:
            await self.send_channel_embed(
                thread,
                title="입력이 너무 깁니다",
                description=(
                    f"현재 입력 길이: `{len(prompt)}`자\n"
                    f"허용 최대 길이: `{self.config.max_prompt_chars}`자"
                ),
                tone="warning",
            )
            return

        lock = self.get_thread_lock(thread.id)
        if lock.locked():
            await self.send_channel_embed(
                thread,
                title="작업 진행 중",
                description="이 스레드에서는 이미 Codex 작업이 실행 중이에요. 현재 작업이 끝난 뒤 다시 시도해 주세요.",
                tone="warning",
            )
            return

        async with lock:
            async with thread.typing():
                runtime = self.get_or_create_runtime(thread.id, workspace)
                started = asyncio.get_running_loop().time()
                try:
                    turn = await runtime.run_turn(prompt, message)
                except asyncio.TimeoutError:
                    duration = asyncio.get_running_loop().time() - started
                    pending_approvals = self.get_pending_approvals_for_thread(thread.id)
                    runtime_state = self.describe_runtime_state(thread.id)
                    self.record_execution("ask", "failed", workspace, duration, "turn timeout")
                    logging.error(
                        "turn timed out thread=%s source_message=%s approvals=%s runtime_state=%s",
                        thread.id,
                        message.id,
                        len(pending_approvals),
                        runtime_state,
                    )
                    await self.send_channel_embed(
                        thread,
                        title="Codex 응답 시간 초과",
                        description=(
                            f"{self.config.timeout_seconds}초 안에 turn이 끝나지 않았어요.\n"
                            f"대기 중인 승인 수: `{len(pending_approvals)}`\n"
                            f"런타임 상태: `{runtime_state}`\n"
                            f"`/{command_name_for_role('status', self.config.bot_role)}`로 상세 상태를 확인해 보세요."
                        ),
                        tone="warning",
                    )
                    return
                except Exception as exc:
                    duration = asyncio.get_running_loop().time() - started
                    self.record_execution("ask", "failed", workspace, duration, f"런타임 오류: {exc}")
                    await self.send_channel_embed(
                        thread,
                        title="Codex 실행 오류",
                        description=f"작업을 처리하던 중 오류가 발생했어요.\n`{exc}`",
                        tone="error",
                    )
                    return
                duration = asyncio.get_running_loop().time() - started
                turn_status = str(turn.get("status", "unknown"))
                turn_status_label = "완료" if turn_status == "completed" else turn_status
                summary = f"응답 상태: {turn_status_label} ({duration:.1f}s)"
                record_status = "success" if turn_status == "completed" else "failed"
                self.record_execution("ask", record_status, workspace, duration, summary)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logging.exception("Unhandled app command error", exc_info=error)
        await self.send_interaction_embed(
            interaction,
            title="명령 실행 실패",
            description=f"슬래시 커맨드를 처리하지 못했어요.\n`{error}`",
            tone="error",
            ephemeral=True,
        )

    def is_allowed_user(self, user_id: int) -> bool:
        return user_id == self.config.allowed_user_id

    def record_execution(self, command_name: str, status: str, target_path: Path | None, duration_seconds: float, summary: str) -> None:
        self.last_execution = ExecutionRecord(
            command_name=command_name,
            status=status,
            target_path=target_path,
            started_at=now_utc_iso(),
            duration_seconds=duration_seconds,
            summary=summary,
        )

    def format_execution_status(self, status: str) -> str:
        if status == "success":
            return "성공"
        if status == "failed":
            return "실패"
        if status == "requested":
            return "요청됨"
        return status

    def format_background_status(self, status: str) -> str:
        if status == "succeeded":
            return "성공"
        if status == "failed":
            return "실패"
        if status == "pending":
            return "대기 중"
        return status

    def resolve_repo_creation_base(self) -> Path:
        return self.config.checkout_path.resolve().parent.parent

    def resolve_config_sync_paths(self) -> list[Path]:
        paths = [self.config.config_path.resolve()]
        if is_relative_to(self.config.config_path, self.config.checkout_path):
            relative = self.config.config_path.resolve().relative_to(self.config.checkout_path.resolve())
            for role in ("main", "staging"):
                candidate = (self.config.checkout_path.parent / role / relative).resolve()
                if candidate not in paths and candidate.is_file():
                    paths.append(candidate)
        return paths

    def persist_channel_mapping(self, channel_id: int, workspace: Path) -> list[Path]:
        updated_paths: list[Path] = []
        for config_path in self.resolve_config_sync_paths():
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            channels = raw.get("channels")
            if not isinstance(channels, dict):
                raise ConfigError(f"`channels` 항목이 올바르지 않습니다: {config_path}")
            channels[str(channel_id)] = str(workspace)
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
            updated_paths.append(config_path)
        self.config.channel_workspaces[channel_id] = workspace
        return updated_paths

    def remove_channel_mapping(self, channel_id: int) -> list[Path]:
        updated_paths: list[Path] = []
        for config_path in self.resolve_config_sync_paths():
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            channels = raw.get("channels")
            if not isinstance(channels, dict):
                raise ConfigError(f"`channels` 항목이 올바르지 않습니다: {config_path}")
            channels.pop(str(channel_id), None)
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
            updated_paths.append(config_path)
        self.config.channel_workspaces.pop(channel_id, None)
        return updated_paths

    def is_protected_workspace(self, workspace: Path) -> bool:
        resolved = workspace.resolve()
        project_root = self.config.checkout_path.resolve().parent
        if resolved == project_root:
            return True
        if is_relative_to(resolved, project_root):
            return True
        return False

    def background_operations_dir(self) -> Path:
        return self.config.checkout_path / STATE_DIRECTORY_NAME / BACKGROUND_OPERATIONS_DIR_NAME

    def create_background_operation(self, command_name: str, args: list[str], target_channel_id: int) -> BackgroundOperation:
        operations_dir = self.background_operations_dir()
        operations_dir.mkdir(parents=True, exist_ok=True)
        operation_id = f"{command_name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        metadata_path = operations_dir / f"{operation_id}.json"
        log_path = operations_dir / f"{operation_id}.log"
        payload = {
            "operation_id": operation_id,
            "command_name": command_name,
            "args": list(args),
            "target_channel_id": target_channel_id,
            "status": "pending",
            "requested_at": now_utc_iso(),
            "notified": False,
            "log_path": str(log_path),
        }
        metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return BackgroundOperation(
            operation_id=operation_id,
            command_name=command_name,
            args=list(args),
            target_channel_id=target_channel_id,
            metadata_path=metadata_path,
            log_path=log_path,
        )

    def get_thread_lock(self, thread_id: int) -> asyncio.Lock:
        return self.thread_locks.setdefault(thread_id, asyncio.Lock())

    def active_thread_count(self) -> int:
        return sum(1 for lock in self.thread_locks.values() if lock.locked())

    def resolve_thread_workspace(self, interaction: discord.Interaction, require_thread: bool) -> tuple[discord.Thread | None, Path | None]:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            if require_thread:
                return None, None
            return None, None

        workspace = self.config.channel_workspaces.get(channel.parent_id or 0)
        if workspace is None and require_thread:
            return channel, None
        return channel, workspace

    async def ensure_thread_owned(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            return True

        owner_role = thread_target_role(channel.name)
        if owner_role == self.config.bot_role:
            return True

        await self.send_interaction_embed(
            interaction,
            title="다른 봇 전용 스레드",
            description=(
                f"이 스레드는 `{owner_role}` 봇이 담당하는 세션이에요.\n"
                f"이 스레드에서는 `{owner_role}` 봇용 명령을 사용해 주세요."
            ),
            tone="warning",
            ephemeral=True,
        )
        return False

    def resolve_session_parent_channel(self, interaction: discord.Interaction) -> tuple[discord.TextChannel | None, Path | None]:
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            if not isinstance(parent, discord.TextChannel):
                return None, None
            workspace = self.config.channel_workspaces.get(parent.id)
            return parent, workspace

        if isinstance(channel, discord.TextChannel):
            workspace = self.config.channel_workspaces.get(channel.id)
            return channel, workspace

        return None, None

    async def ensure_allowed_or_reply(self, interaction: discord.Interaction) -> bool:
        if self.is_allowed_user(interaction.user.id):
            return True

        await self.send_interaction_embed(
            interaction,
            title="사용 권한 없음",
            description="이 봇은 현재 허용된 사용자만 사용할 수 있어요.",
            tone="error",
            ephemeral=True,
        )
        return False

    async def ensure_channel_only(self, interaction: discord.Interaction, command_name: str) -> bool:
        if isinstance(interaction.channel, discord.Thread):
            await self.send_interaction_embed(
                interaction,
                title="채널에서만 사용 가능",
                description=f"`/{command_name}` 명령은 부모 채널에서만 사용할 수 있어요. 스레드 안에서는 실행할 수 없습니다.",
                tone="warning",
                ephemeral=True,
            )
            return False
        return True

    async def ensure_thread_only(self, interaction: discord.Interaction, command_name: str) -> bool:
        if not isinstance(interaction.channel, discord.Thread):
            await self.send_interaction_embed(
                interaction,
                title="스레드에서만 사용 가능",
                description=f"`/{command_name}` 명령은 세션 스레드 안에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return False
        return True

    async def ensure_session_context(self, interaction: discord.Interaction, command_name: str) -> bool:
        parent_channel, workspace = self.resolve_session_parent_channel(interaction)
        if parent_channel is None or workspace is None:
            await self.send_interaction_embed(
                interaction,
                title="워크스페이스 연결 필요",
                description=f"`/{command_name}` 명령은 워크스페이스가 연결된 채널 또는 해당 스레드에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return False
        return True

    async def send_output(self, interaction: discord.Interaction, text: str, filename_prefix: str) -> None:
        title = output_title_for_prefix(filename_prefix)
        output_block = format_code_block(text)
        if len(output_block) > EMBED_DESCRIPTION_LIMIT or len(text) > DISCORD_FILE_FALLBACK_LIMIT:
            await self.reply_with_output_file(interaction, title, filename_prefix, text)
            return

        await self.send_interaction_embed(interaction, title=title, description=output_block)

    async def send_message_output(self, source_message: discord.Message, text: str, filename_prefix: str) -> None:
        if len(text) > DISCORD_FILE_FALLBACK_LIMIT:
            filename = f"{filename_prefix}.txt"
            file_data = io.BytesIO(text.encode("utf-8", errors="replace"))
            file = discord.File(file_data, filename=filename)
            await source_message.reply(
                f"응답이 길어서 `{filename}` 파일로 첨부했어요.",
                file=file,
                mention_author=False,
            )
            return

        chunks = chunk_text(text)
        await source_message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await source_message.channel.send(chunk)

    def build_approval_id(self, thread_id: int, request_id: int | str) -> str:
        return f"{thread_id}:{request_id}"

    def disable_view_buttons(self, view: discord.ui.View) -> None:
        for item in view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    def get_or_create_runtime(self, thread_id: int, workspace: Path) -> ThreadSessionRuntime:
        runtime = self.session_runtimes.get(thread_id)
        resolved_workspace = workspace.resolve()
        if runtime is not None and runtime.workspace == resolved_workspace:
            return runtime
        runtime = ThreadSessionRuntime(self, thread_id, resolved_workspace)
        self.session_runtimes[thread_id] = runtime
        return runtime

    def get_pending_approvals_for_thread(self, thread_id: int) -> list[PendingApproval]:
        return [
            pending
            for pending in self.pending_approvals.values()
            if pending.runtime.discord_thread_id == thread_id
        ]

    def describe_runtime_state(self, thread_id: int) -> str:
        runtime = self.session_runtimes.get(thread_id)
        if runtime is None:
            return "런타임 없음"

        state = runtime.get_active_turn_state()
        if state is None:
            return "활성 turn 없음"

        turn_id = state.turn_id or "(미할당)"
        return (
            f"turn={turn_id}, last_event={state.last_event}, last_event_at={state.last_event_at}, "
            f"agent_messages_sent={state.agent_messages_sent}"
        )

    def build_approval_response_payload(
        self,
        method: str,
        params: dict[str, object],
        approved: bool,
    ) -> dict[str, object]:
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept" if approved else "cancel"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept" if approved else "cancel"}
        if method == "item/permissions/requestApproval":
            if approved:
                return {"permissions": params.get("permissions", {}), "scope": "turn"}
            return {"permissions": {}, "scope": "turn"}
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "approved" if approved else "abort"}
        return {"decision": "accept" if approved else "cancel"}

    def clear_pending_approvals_for_thread(self, thread_id: int) -> int:
        approval_ids = [
            approval_id
            for approval_id, pending in self.pending_approvals.items()
            if pending.runtime.discord_thread_id == thread_id
        ]
        for approval_id in approval_ids:
            self.pending_approvals.pop(approval_id, None)
        return len(approval_ids)

    def extract_arg_value(self, args: list[str], names: tuple[str, ...]) -> str | None:
        for index, arg in enumerate(args):
            if arg in names and index + 1 < len(args):
                return args[index + 1]
            for name in names:
                prefix = f"{name}="
                if arg.startswith(prefix):
                    return arg[len(prefix) :]
        return None

    def resolve_codex_model(self) -> str | None:
        return self.extract_arg_value(self.config.codex_exec_args, ("--model", "-m"))

    def is_thread_danger_mode_enabled(self, thread_id: int | None) -> bool:
        if thread_id is None:
            return False
        record = self.thread_policy_store.get(thread_id)
        return record is not None and record.dangerously_bypass_approvals_and_sandbox

    def describe_thread_danger_mode(self, thread_id: int | None) -> str:
        if thread_id is None:
            return "해당 없음"
        if self.is_thread_danger_mode_enabled(thread_id):
            return "켜짐 (스레드 override)"
        return "꺼짐"

    def resolve_approval_policy(self, thread_id: int | None = None) -> object:
        if self.is_thread_danger_mode_enabled(thread_id):
            return "never"
        return self.extract_arg_value(self.config.codex_global_args, ("--ask-for-approval", "-a")) or "on-request"

    def resolve_sandbox_mode(self, thread_id: int | None = None) -> str:
        if self.is_thread_danger_mode_enabled(thread_id):
            return "danger-full-access"
        if "--dangerously-bypass-approvals-and-sandbox" in self.config.codex_global_args:
            return "danger-full-access"
        if "--full-auto" in self.config.codex_global_args:
            return "workspace-write"
        sandbox = self.extract_arg_value(
            self.config.codex_global_args + self.config.codex_exec_args,
            ("--sandbox", "-s"),
        )
        return sandbox or "workspace-write"

    def resolve_turn_sandbox_policy(self, thread_id: int | None = None) -> dict[str, object] | None:
        sandbox_mode = self.resolve_sandbox_mode(thread_id)
        if sandbox_mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if sandbox_mode == "workspace-write":
            return {
                "type": "workspaceWrite",
                "networkAccess": True,
                "readOnlyAccess": {"type": "fullAccess"},
                "writableRoots": [str(Path.home() / ".codex" / "memories")],
            }
        return None

    async def reset_thread_runtime(self, thread_id: int) -> None:
        runtime = self.session_runtimes.pop(thread_id, None)
        if runtime is not None:
            await runtime.shutdown()

    async def handle_danger_mode_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        command_name = command_name_for_role("danger-on" if enabled else "danger-off", self.config.bot_role)
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_thread_only(interaction, command_name):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=True)
        if thread is None or workspace is None:
            await self.send_interaction_embed(
                interaction,
                title="워크스페이스 연결 필요",
                description=f"`/{command_name}` 명령은 워크스페이스가 연결된 세션 스레드에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return

        runtime = self.session_runtimes.get(thread.id)
        active_turn = runtime.get_active_turn_state() if runtime is not None else None
        pending_approvals = self.get_pending_approvals_for_thread(thread.id)
        if self.get_thread_lock(thread.id).locked() or active_turn is not None or pending_approvals:
            await self.send_interaction_embed(
                interaction,
                title="지금은 변경할 수 없음",
                description=(
                    "현재 이 스레드에서 진행 중인 Codex turn 또는 승인 요청이 있어요.\n"
                    f"먼저 `/{command_name_for_role('break', self.config.bot_role)}` 로 작업을 중단한 뒤 다시 시도해 주세요."
                ),
                tone="warning",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        already_enabled = self.is_thread_danger_mode_enabled(thread.id)
        if already_enabled == enabled:
            await self.send_interaction_embed(
                interaction,
                title="설정 변경 없음",
                description=(
                    f"이 스레드 위험 모드는 이미 `{self.describe_thread_danger_mode(thread.id)}` 상태예요.\n"
                    f"유효 승인 정책: `{self.resolve_approval_policy(thread.id)}`\n"
                    f"유효 샌드박스: `{self.resolve_sandbox_mode(thread.id)}`"
                ),
                tone="info",
                ephemeral=True,
            )
            return

        self.thread_policy_store.set_danger_mode(thread.id, enabled)
        self.session_store.delete(thread.id)
        await self.reset_thread_runtime(thread.id)

        action_label = "켜짐" if enabled else "꺼짐"
        summary = f"스레드 위험 모드 {action_label}"
        self.record_execution(command_name, "success", workspace, 0.0, summary)
        await self.send_interaction_embed(
            interaction,
            title=f"위험 모드 {action_label}",
            description=(
                f"적용 대상 스레드: {thread.mention}\n"
                f"유효 승인 정책: `{self.resolve_approval_policy(thread.id)}`\n"
                f"유효 샌드박스: `{self.resolve_sandbox_mode(thread.id)}`\n"
                "기존 session/runtime은 정리했고, 다음 요청부터 이 스레드에만 새 설정이 적용됩니다."
            ),
            tone="warning" if enabled else "success",
            ephemeral=True,
        )

    async def post_agent_message(
        self,
        thread_id: int,
        source_message_id: int,
        text: str,
        reply_to_source: bool,
    ) -> None:
        channel = self.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            fetched = await self.fetch_channel(thread_id)
            channel = fetched if isinstance(fetched, discord.Thread) else None
        if not isinstance(channel, discord.Thread):
            logging.warning("Discord thread not found for runtime thread_id=%s", thread_id)
            return

        if reply_to_source:
            try:
                source_message = await channel.fetch_message(source_message_id)
                await self.send_message_output(source_message, text, filename_prefix=f"message-{thread_id}")
                return
            except discord.HTTPException:
                pass

        if len(text) > DISCORD_FILE_FALLBACK_LIMIT:
            filename = f"message-{thread_id}.txt"
            file_data = io.BytesIO(text.encode("utf-8", errors="replace"))
            file = discord.File(file_data, filename=filename)
            await self.send_channel_embed(
                channel,
                title="응답 첨부",
                description=f"응답이 길어서 `{filename}` 파일로 첨부했어요.",
                file=file,
            )
            return

        chunks = chunk_text(text)
        await channel.send(chunks[0])
        for chunk in chunks[1:]:
            await channel.send(chunk)

    def format_app_server_approval_message(self, method: str, params: dict[str, object]) -> tuple[str, str]:
        reason = params.get("reason")
        reason_line = f"사유: {reason}" if isinstance(reason, str) and reason.strip() else "사유: 없음"

        if method == "item/commandExecution/requestApproval":
            command = params.get("command")
            cwd = params.get("cwd")
            lines = ["Codex가 명령 실행 승인을 요청했어요.", reason_line]
            if isinstance(command, str) and command.strip():
                lines.append(f"명령어: `{command}`")
            if isinstance(cwd, str) and cwd.strip():
                lines.append(f"작업 경로: `{cwd}`")
            return "승인 요청", "\n".join(lines)

        if method == "item/fileChange/requestApproval":
            grant_root = params.get("grantRoot")
            lines = ["Codex가 파일 변경 승인을 요청했어요.", reason_line]
            if isinstance(grant_root, str) and grant_root.strip():
                lines.append(f"허용 루트: `{grant_root}`")
            return "승인 요청", "\n".join(lines)

        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            return (
                "권한 요청",
                "Codex가 추가 권한을 요청했어요.\n"
                + reason_line
                + f"\n권한: `{json.dumps(permissions, ensure_ascii=False)}`",
            )

        if method == "execCommandApproval":
            command = params.get("command")
            return "승인 요청", "Codex가 명령 실행 승인을 요청했어요.\n" + reason_line + f"\n명령어: `{command}`"

        if method == "applyPatchApproval":
            return "승인 요청", "Codex가 패치 적용 승인을 요청했어요.\n" + reason_line

        return "승인 요청", f"Codex가 `{method}` 작업에 대한 승인을 요청했어요.\n{reason_line}"

    async def handle_app_server_request(
        self,
        runtime: ThreadSessionRuntime,
        request_id: int | str,
        method: str,
        params: dict[str, object],
    ) -> None:
        approval_id = self.build_approval_id(runtime.discord_thread_id, request_id)
        self.pending_approvals[approval_id] = PendingApproval(
            runtime=runtime,
            request_id=request_id,
            method=method,
            params=params,
            requested_at=now_utc_iso(),
        )
        runtime.note_turn_event(f"approval requested:{method}")
        logging.warning(
            "app-server approval requested thread=%s request=%s method=%s",
            runtime.discord_thread_id,
            request_id,
            method,
        )

        channel = self.get_channel(runtime.discord_thread_id)
        if not isinstance(channel, discord.Thread):
            fetched = await self.fetch_channel(runtime.discord_thread_id)
            channel = fetched if isinstance(fetched, discord.Thread) else None
        if not isinstance(channel, discord.Thread):
            logging.warning("Unable to find Discord thread for approval request %s", approval_id)
            self.pending_approvals.pop(approval_id, None)
            await runtime.send_response(
                request_id,
                self.build_approval_response_payload(method, params, approved=False),
            )
            return

        view = AppServerApprovalView(self, approval_id)
        title, description = self.format_app_server_approval_message(method, params)
        try:
            await self.send_channel_embed(channel, title=title, description=description, tone="warning", view=view)
        except discord.HTTPException:
            logging.exception(
                "Failed to send approval request message thread=%s request=%s",
                runtime.discord_thread_id,
                request_id,
            )
            self.pending_approvals.pop(approval_id, None)
            await runtime.send_response(
                request_id,
                self.build_approval_response_payload(method, params, approved=False),
            )

    async def handle_app_server_approval_click(
        self,
        interaction: discord.Interaction,
        approval_id: str,
        approved: bool,
        view: AppServerApprovalView,
    ) -> None:
        pending = self.pending_approvals.pop(approval_id, None)
        if pending is None:
            self.disable_view_buttons(view)
            embed = self.build_embed("승인 요청 만료", "이 승인 요청은 더 이상 유효하지 않아요.", tone="warning")
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        self.disable_view_buttons(view)
        response_payload = self.build_approval_response_payload(pending.method, pending.params, approved)

        try:
            await pending.runtime.send_response(pending.request_id, response_payload)
        except Exception as exc:
            logging.exception("Failed to send approval response")
            embed = self.build_embed(
                "승인 응답 실패",
                f"승인 결과를 Codex에 전달하지 못했어요.\n`{exc}`",
                tone="error",
            )
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        embed = self.build_embed(
            "승인 처리 완료" if approved else "승인 거절됨",
            "요청을 승인했어요." if approved else "요청을 거절했어요.",
            tone="success" if approved else "warning",
        )
        await interaction.response.edit_message(embed=embed, view=view, content=None)

    async def run_process(self, args: list[str], cwd: Path, timeout_seconds: int | None = None) -> "ProcessResult":
        timeout = timeout_seconds or self.config.timeout_seconds
        start = asyncio.get_running_loop().time()
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
            duration = asyncio.get_running_loop().time() - start
            return ProcessResult(
                args=args,
                returncode=process.returncode,
                stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
                duration_seconds=duration,
                timed_out=False,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            duration = asyncio.get_running_loop().time() - start
            return ProcessResult(
                args=args,
                returncode=None,
                stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
                duration_seconds=duration,
                timed_out=True,
            )

    def get_thread_session(self, thread_id: int, workspace: Path) -> ThreadSessionRecord | None:
        record = self.session_store.get(thread_id)
        if record is None:
            return None
        if record.workspace.resolve() != workspace.resolve():
            self.session_store.delete(thread_id)
            return None
        return record

    async def collect_git_summary(self, target_path: Path) -> GitSummary:
        inside_repo = await self.run_process(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_path,
            timeout_seconds=15,
        )
        if inside_repo.timed_out or inside_repo.returncode != 0 or inside_repo.stdout != "true":
            return GitSummary(path=target_path, is_repo=False)

        branch_result = await self.run_process(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target_path,
            timeout_seconds=15,
        )
        commit_result = await self.run_process(
            ["git", "--no-pager", "log", "-1", "--oneline"],
            cwd=target_path,
            timeout_seconds=15,
        )
        status_result = await self.run_process(
            ["git", "status", "--short"],
            cwd=target_path,
            timeout_seconds=15,
        )

        status_lines = status_result.stdout.splitlines() if status_result.stdout else []
        return GitSummary(
            path=target_path,
            is_repo=True,
            branch=branch_result.stdout or "(알 수 없음)",
            commit=commit_result.stdout or "(알 수 없음)",
            changed_files=len(status_lines),
            status_lines=status_lines,
            stderr=status_result.stderr,
        )

    def render_git_summary_text(self, summary: GitSummary) -> str:
        if not summary.is_repo:
            return f"경로: {summary.path}\nGit: 저장소가 아닙니다"

        lines = [
            f"경로: {summary.path}",
            f"브랜치: {summary.branch}",
            f"최근 커밋: {summary.commit}",
            f"변경 파일 수: {summary.changed_files}",
        ]
        if summary.stderr:
            lines.append(f"Git stderr:\n{summary.stderr}")
        return "\n".join(lines)

    def summarize_git_changes(self, status_lines: list[str]) -> tuple[int, int, int]:
        staged_count = 0
        unstaged_count = 0
        untracked_count = 0
        for line in status_lines:
            if not line:
                continue
            if line.startswith("??"):
                untracked_count += 1
                continue
            staged_code = line[0]
            unstaged_code = line[1] if len(line) > 1 else " "
            if staged_code not in {" ", "?"}:
                staged_count += 1
            if unstaged_code not in {" ", "?"}:
                unstaged_count += 1
        return staged_count, unstaged_count, untracked_count

    async def build_status_text(self, interaction: discord.Interaction) -> str:
        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=False)
        current_thread_busy = False
        thread_session: ThreadSessionRecord | None = None
        thread_id = thread.id if thread is not None else None
        if thread is not None:
            current_thread_busy = self.get_thread_lock(thread.id).locked()
            if workspace is not None:
                thread_session = self.get_thread_session(thread.id, workspace)

        lines = [
            f"봇 역할: {self.config.bot_role}",
            f"체크아웃 경로: {self.config.checkout_path}",
            f"현재 작업 중인 스레드 수: {self.active_thread_count()}",
            f"이 스레드 실행 중 여부: {'예' if current_thread_busy else '아니요'}",
            f"모델: {self.resolve_codex_model() or '기본값'}",
            f"승인 정책: {self.resolve_approval_policy(thread_id)}",
            f"샌드박스: {self.resolve_sandbox_mode(thread_id)}",
            f"이 스레드 위험 모드: {self.describe_thread_danger_mode(thread_id)}",
        ]

        if workspace is not None:
            lines.append(f"스레드 워크스페이스: {workspace}")
        else:
            lines.append("스레드 워크스페이스: 연결된 스레드가 아닙니다")

        if thread_session is not None:
            lines.append(f"세션 ID: {thread_session.session_id}")
            lines.append(f"세션 갱신 시각: {thread_session.updated_at}")
        else:
            lines.append("세션 ID: 없음")
        if thread is not None:
            pending_approvals = self.get_pending_approvals_for_thread(thread.id)
            lines.append(f"이 스레드 대기 승인 수: {len(pending_approvals)}")
            lines.append(f"이 스레드 런타임: {self.describe_runtime_state(thread.id)}")

        if self.last_execution is None:
            lines.append("최근 실행 기록: 없음")
        else:
            lines.extend(
                [
                    f"최근 실행 명령: {self.last_execution.command_name}",
                    f"최근 실행 상태: {self.format_execution_status(self.last_execution.status)}",
                    f"최근 실행 대상: {self.last_execution.target_path or '없음'}",
                    f"최근 실행 시각: {self.last_execution.started_at}",
                    f"최근 실행 시간: {self.last_execution.duration_seconds:.1f}s",
                    f"최근 실행 요약: {self.last_execution.summary}",
                ]
            )

        checkout_git = await self.collect_git_summary(self.config.checkout_path)
        lines.append("")
        lines.append("[체크아웃 Git 상태]")
        lines.append(self.render_git_summary_text(checkout_git))

        if workspace is not None and workspace != self.config.checkout_path:
            workspace_git = await self.collect_git_summary(workspace)
            lines.append("")
            lines.append("[워크스페이스 Git 상태]")
            lines.append(self.render_git_summary_text(workspace_git))

        return "\n".join(lines)

    async def send_status_embed(self, interaction: discord.Interaction) -> None:
        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=False)
        current_thread_busy = False
        thread_session: ThreadSessionRecord | None = None
        thread_id = thread.id if thread is not None else None
        if thread is not None:
            current_thread_busy = self.get_thread_lock(thread.id).locked()
            if workspace is not None:
                thread_session = self.get_thread_session(thread.id, workspace)

        checkout_git = await self.collect_git_summary(self.config.checkout_path)
        workspace_git = None
        if workspace is not None and workspace != self.config.checkout_path:
            workspace_git = await self.collect_git_summary(workspace)

        tone = "info"
        if self.last_execution is not None and self.last_execution.status == "failed":
            tone = "warning"
        embed = self.build_embed(
            "현재 상태",
            None,
            tone=tone,
        )

        self.add_embed_field(
            embed,
            "세션",
            (
                f"역할: {self.config.bot_role}\n"
                f"스레드 작업 중: {'예' if current_thread_busy else '아니요'}\n"
                f"활성 스레드 수: {self.active_thread_count()}\n"
                f"워크스페이스: {workspace or '연결 없음'}\n"
                f"세션 ID: {thread_session.session_id if thread_session else '없음'}\n"
                f"대기 승인 수: {len(self.get_pending_approvals_for_thread(thread.id)) if thread is not None else 0}\n"
                f"런타임: {self.describe_runtime_state(thread.id) if thread is not None else '런타임 없음'}"
            ),
            inline=False,
        )
        self.add_embed_field(
            embed,
            "실행 환경",
            (
                f"체크아웃: {self.config.checkout_path}\n"
                f"모델: {self.resolve_codex_model() or '기본값'}\n"
                f"승인 정책: {self.resolve_approval_policy(thread_id)}\n"
                f"샌드박스: {self.resolve_sandbox_mode(thread_id)}\n"
                f"이 스레드 위험 모드: {self.describe_thread_danger_mode(thread_id)}"
            ),
            inline=False,
        )

        if self.last_execution is not None:
            self.add_embed_field(
                embed,
                "최근 실행",
                (
                    f"명령: {self.last_execution.command_name}\n"
                    f"상태: {self.format_execution_status(self.last_execution.status)}\n"
                    f"대상: {self.last_execution.target_path or '없음'}\n"
                    f"시각: {self.last_execution.started_at}\n"
                    f"소요 시간: {self.last_execution.duration_seconds:.1f}s\n"
                    f"요약: {self.last_execution.summary}"
                ),
                inline=False,
            )

        self.add_embed_field(
            embed,
            "체크아웃 Git",
            (
                f"브랜치: {checkout_git.branch}\n"
                f"최근 커밋: {checkout_git.commit}\n"
                f"변경 파일 수: {checkout_git.changed_files}\n"
                f"경로: {checkout_git.path}"
            )
            if checkout_git.is_repo
            else f"경로: {checkout_git.path}\nGit 저장소가 아닙니다.",
            inline=False,
        )
        if workspace_git is not None:
            self.add_embed_field(
                embed,
                "워크스페이스 Git",
                (
                    f"브랜치: {workspace_git.branch}\n"
                    f"최근 커밋: {workspace_git.commit}\n"
                    f"변경 파일 수: {workspace_git.changed_files}\n"
                    f"경로: {workspace_git.path}"
                )
                if workspace_git.is_repo
                else f"경로: {workspace_git.path}\nGit 저장소가 아닙니다.",
                inline=False,
            )

        raw_text = await self.build_status_text(interaction)
        if len(embed) > 5900:
            await self.reply_with_output_file(interaction, "현재 상태", "status", raw_text)
            return
        await self.send_interaction_message(interaction, embed=embed)
    async def collect_diff_summary(self, target_path: Path) -> DiffSummary:
        git_summary = await self.collect_git_summary(target_path)
        if not git_summary.is_repo:
            self.record_execution("diff", "failed", target_path, 0.0, "Git 저장소가 아님")
            return DiffSummary(path=target_path, git=git_summary, diff_stat="")

        status_result = await self.run_process(
            ["git", "status", "--short"],
            cwd=target_path,
            timeout_seconds=15,
        )
        diff_stat_result = await self.run_process(
            ["git", "--no-pager", "diff", "--stat"],
            cwd=target_path,
            timeout_seconds=15,
        )

        summary = "작업 트리가 깨끗함"
        if status_result.stdout or diff_stat_result.stdout:
            summary = "변경 사항 감지됨"

        status = "success" if not status_result.timed_out and not diff_stat_result.timed_out else "failed"
        self.record_execution(
            "diff",
            status,
            target_path,
            max(status_result.duration_seconds, diff_stat_result.duration_seconds),
            summary,
        )
        status_lines = status_result.stdout.splitlines() if status_result.stdout else []
        staged_count, unstaged_count, untracked_count = self.summarize_git_changes(status_lines)
        return DiffSummary(
            path=target_path,
            git=GitSummary(
                path=git_summary.path,
                is_repo=True,
                branch=git_summary.branch,
                commit=git_summary.commit,
                changed_files=len(status_lines),
                status_lines=status_lines,
                stderr=git_summary.stderr,
            ),
            diff_stat=diff_stat_result.stdout,
            status_stderr=status_result.stderr,
            diff_stderr=diff_stat_result.stderr,
            staged_count=staged_count,
            unstaged_count=unstaged_count,
            untracked_count=untracked_count,
        )

    def render_diff_text(self, summary: DiffSummary) -> str:
        if not summary.git.is_repo:
            return f"경로: {summary.path}\nGit: 저장소가 아닙니다"

        lines = [
            f"경로: {summary.path}",
            f"브랜치: {summary.git.branch}",
            f"최근 커밋: {summary.git.commit}",
            "",
            "[요약]",
            f"변경 파일 수: {summary.git.changed_files}",
            f"스테이지됨: {summary.staged_count}",
            f"작업 디렉토리 변경: {summary.unstaged_count}",
            f"추적 안 된 파일: {summary.untracked_count}",
            "",
            "[변경 파일]",
            "\n".join(summary.git.status_lines) or "(변경 파일 없음)",
            "",
            "[Diff 통계]",
            summary.diff_stat or "(Diff 통계 없음)",
        ]
        if summary.status_stderr:
            lines.extend(["", f"상태 확인 오류 출력:\n{summary.status_stderr}"])
        if summary.diff_stderr:
            lines.extend(["", f"Diff 오류 출력:\n{summary.diff_stderr}"])
        return "\n".join(lines)

    async def send_diff_embed(self, interaction: discord.Interaction, target_path: Path) -> None:
        summary = await self.collect_diff_summary(target_path)
        raw_text = self.render_diff_text(summary)
        if not summary.git.is_repo:
            await self.send_interaction_embed(
                interaction,
                title="변경 사항",
                description=f"`{target_path}` 경로는 Git 저장소가 아니에요.",
                tone="error",
            )
            return

        if summary.git.changed_files == 0 and not summary.diff_stat:
            tone = "success"
        else:
            tone = "warning"

        embed = self.build_embed("변경 사항", None, tone=tone)
        self.add_embed_field(
            embed,
            "작업 위치",
            (
                f"경로: {summary.path}\n"
                f"브랜치: {summary.git.branch}\n"
                f"최근 커밋: {summary.git.commit}"
            ),
            inline=False,
        )
        self.add_embed_field(
            embed,
            "요약",
            (
                f"변경 파일 수: {summary.git.changed_files}\n"
                f"스테이지됨: {summary.staged_count}\n"
                f"작업 디렉토리 변경: {summary.unstaged_count}\n"
                f"추적 안 된 파일: {summary.untracked_count}"
            ),
            inline=False,
        )
        self.add_embed_field(
            embed,
            "변경 파일",
            "\n".join(summary.git.status_lines) or "(변경 파일 없음)",
            inline=False,
            code=True,
        )
        self.add_embed_field(
            embed,
            "Diff 통계",
            summary.diff_stat or "(Diff 통계 없음)",
            inline=False,
            code=True,
        )

        if summary.status_stderr:
            self.add_embed_field(embed, "상태 확인 오류", summary.status_stderr, inline=False, code=True)
        if summary.diff_stderr:
            self.add_embed_field(embed, "Diff 오류", summary.diff_stderr, inline=False, code=True)

        if len(embed) > 5900 or len(raw_text) > DISCORD_FILE_FALLBACK_LIMIT:
            await self.reply_with_output_file(interaction, "변경 사항", "diff", raw_text, tone=tone)
            return
        await self.send_interaction_message(interaction, embed=embed)

    async def run_main_operation(self, command_name: str, args: list[str]) -> str:
        try:
            result = await self.run_process(args=args, cwd=self.config.checkout_path)
        except FileNotFoundError:
            summary = f"실행 파일 없음: {args[0]}"
            self.record_execution(command_name, "failed", self.config.checkout_path, 0.0, summary)
            return f"`{args[0]}` 실행 파일을 찾지 못했어요."
        except Exception as exc:
            logging.exception("Unexpected error while running %s", command_name)
            summary = f"예기치 않은 오류: {exc}"
            self.record_execution(command_name, "failed", self.config.checkout_path, 0.0, summary)
            return f"`{command_name}` 실행 중 예기치 않은 오류가 발생했어요.\n{exc}"

        if result.timed_out:
            self.record_execution(
                command_name,
                "failed",
                self.config.checkout_path,
                result.duration_seconds,
                f"{self.config.timeout_seconds}초 초과로 중단됨",
            )
            return f"`{command_name}` 작업이 {self.config.timeout_seconds}초 안에 끝나지 않아 중단됐어요."

        status = "success" if result.returncode == 0 else "failed"
        self.record_execution(
            command_name,
            status,
            self.config.checkout_path,
            result.duration_seconds,
            f"종료 코드 {result.returncode}",
        )

        lines = [f"명령어: {' '.join(args)}", f"종료 코드: {result.returncode}"]
        if result.stdout:
            lines.append("")
            lines.append(f"표준 출력:\n{result.stdout}")
        if result.stderr:
            lines.append("")
            lines.append(f"표준 오류:\n{result.stderr}")
        if not result.stdout and not result.stderr:
            lines.append("")
            lines.append("(출력 없음)")
        return "\n".join(lines)

    async def spawn_main_operation(self, operation: BackgroundOperation) -> None:
        await asyncio.sleep(1)
        runner_script = self.config.checkout_path / "scripts" / "background_operation_runner.py"
        unit_name = f"codex-discord-bg-{operation.operation_id}"
        try:
            process = await asyncio.create_subprocess_exec(
                "systemd-run",
                "--user",
                "--unit",
                unit_name,
                "--collect",
                "--no-block",
                "--quiet",
                "--working-directory",
                str(self.config.checkout_path),
                sys.executable,
                str(runner_script),
                str(operation.metadata_path),
                str(operation.log_path),
                str(self.config.checkout_path),
                *operation.args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logging.info(
                "Started background %s pid=%s unit=%s log=%s",
                operation.command_name,
                process.pid,
                unit_name,
                operation.log_path,
            )
        except FileNotFoundError:
            logging.exception("Failed to start %s: missing executable %s", operation.command_name, operation.args[0])
        except Exception:
            logging.exception("Unexpected error while starting %s", operation.command_name)

    def format_background_operation_message(self, operation: BackgroundOperation) -> str:
        lines = [
            f"`{operation.command_name}` 작업을 백그라운드로 시작했어요.",
            f"명령어: {' '.join(operation.args)}",
            f"로그: {operation.log_path}",
        ]
        if operation.command_name == "deploy":
            lines.append("배포 중에는 main 봇이 먼저 재시작되어 최종 완료 메시지가 늦게 오거나 생략될 수 있어요.")
        return "\n".join(lines)

    async def background_notification_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.process_completed_background_operations()
            except Exception:
                logging.exception("Failed while processing background operation notifications")
            await asyncio.sleep(BACKGROUND_OPERATION_POLL_SECONDS)

    async def process_completed_background_operations(self) -> None:
        async with self.background_notifications_lock:
            operations_dir = self.background_operations_dir()
            if not operations_dir.is_dir():
                return
            for metadata_path in sorted(operations_dir.glob("*.json")):
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    logging.exception("Failed to read background operation metadata %s", metadata_path)
                    continue

                if payload.get("notified") is True:
                    continue
                if payload.get("status") not in {"succeeded", "failed"}:
                    continue

                channel_id = payload.get("target_channel_id")
                if not isinstance(channel_id, int):
                    logging.warning("Background operation metadata missing target_channel_id: %s", metadata_path)
                    continue

                channel = self.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        logging.exception("Failed to fetch background operation target channel %s", channel_id)
                        continue
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    logging.warning("Unsupported background operation target channel type: %s", channel_id)
                    continue

                command_name = payload.get("command_name", "operation")
                status = payload.get("status", "failed")
                returncode = payload.get("returncode")
                log_path = payload.get("log_path", str(metadata_path.with_suffix(".log")))
                command_args = payload.get("args", [])
                error_message = payload.get("error_message")
                lines = [
                    f"`{command_name}` 작업이 완료됐어요.",
                    f"상태: {self.format_background_status(str(status))}",
                ]
                if isinstance(returncode, int):
                    lines.append(f"종료 코드: {returncode}")
                if isinstance(command_args, list) and all(isinstance(item, str) for item in command_args):
                    lines.append(f"명령어: {' '.join(command_args)}")
                lines.append(f"로그: {log_path}")
                if isinstance(error_message, str) and error_message.strip():
                    lines.append(f"오류: {error_message}")

                try:
                    await self.send_channel_embed(
                        channel,
                        title="백그라운드 작업 완료",
                        description="\n".join(lines),
                        tone="success" if status == "succeeded" else "error",
                    )
                except discord.HTTPException:
                    logging.exception("Failed to send background operation completion message to channel %s", channel_id)
                    continue

                payload["notified"] = True
                payload["notified_at"] = now_utc_iso()
                metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    async def handle_ping(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_channel_only(interaction, command_name_for_role("ping", self.config.bot_role)):
            return

        latency_ms = round(self.latency * 1000)
        await self.send_interaction_embed(
            interaction,
            title="봇 응답 정상",
            description=(
                f"역할: `{self.config.bot_role}`\n"
                f"지연 시간: `{latency_ms}ms`"
            ),
            tone="success",
        )

    async def handle_new_session(self, interaction: discord.Interaction, target_bot_role: str, title: str) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_channel_only(interaction, command_name_for_role("new-session", self.config.bot_role)):
            return

        parent_channel, workspace = self.resolve_session_parent_channel(interaction)
        if parent_channel is None:
            await self.send_interaction_embed(
                interaction,
                title="채널 연결 필요",
                description=f"`/{command_name_for_role('new-session', self.config.bot_role)}` 명령은 워크스페이스가 연결된 부모 채널에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return
        if workspace is None:
            await self.send_interaction_embed(
                interaction,
                title="워크스페이스 없음",
                description="이 채널에는 아직 워크스페이스가 연결되어 있지 않아요.",
                tone="warning",
                ephemeral=True,
            )
            return

        resolved_target_role = target_bot_role or "main"
        thread_name = format_thread_binding_name(resolved_target_role, title)
        try:
            thread = await parent_channel.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
                type=discord.ChannelType.public_thread,
                reason=f"새 Codex 세션 생성: {resolved_target_role}",
            )
        except discord.Forbidden:
            await self.send_interaction_embed(
                interaction,
                title="스레드 생성 실패",
                description="이 채널에서 스레드를 만들 권한이 없어요. 봇의 스레드 권한을 확인해 주세요.",
                tone="error",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await self.send_interaction_embed(
                interaction,
                title="세션 생성 실패",
                description=f"새 세션 스레드를 만들지 못했어요.\n`{exc}`",
                tone="error",
                ephemeral=True,
            )
            return

        await self.send_interaction_embed(
            interaction,
            title="세션 생성 완료",
            description=(
                f"대상 봇: `{resolved_target_role}`\n"
                f"스레드: {thread.mention}\n"
                f"워크스페이스: `{workspace}`"
            ),
            tone="success",
        )

    async def handle_new_repo(self, interaction: discord.Interaction, name: str, github_url: str | None = None) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_channel_only(interaction, command_name_for_role("new-repo", self.config.bot_role)):
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.send_interaction_embed(
                interaction,
                title="채널에서만 사용 가능",
                description="새 레포는 일반 텍스트 채널에서만 만들 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return

        repo_name = normalize_repo_name(name)
        if not repo_name:
            await self.send_interaction_embed(
                interaction,
                title="이름 형식 오류",
                description="레포 이름에는 영문, 숫자, `.`, `_`, `-`만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return

        normalized_github_url: str | None = None
        if github_url:
            normalized_github_url = normalize_github_clone_url(github_url)
            if normalized_github_url is None:
                await self.send_interaction_embed(
                    interaction,
                    title="GitHub URL 형식 오류",
                    description=(
                        "비어 있는 레포를 만들거나, `https://github.com/owner/repo` 또는 "
                        "`git@github.com:owner/repo.git` 형식의 GitHub 레포 URL을 입력해 주세요."
                    ),
                    tone="warning",
                    ephemeral=True,
                )
                return

        guild = interaction.guild
        if guild is None:
            await self.send_interaction_embed(
                interaction,
                title="Guild 정보 없음",
                description="서버 안에서만 새 채널을 만들 수 있어요.",
                tone="error",
                ephemeral=True,
            )
            return

        if discord.utils.get(guild.text_channels, name=repo_name) is not None:
            await self.send_interaction_embed(
                interaction,
                title="채널 이름 중복",
                description=f"`#{repo_name}` 채널이 이미 존재해요. 다른 이름을 사용해 주세요.",
                tone="warning",
                ephemeral=True,
            )
            return

        repo_root = self.resolve_repo_creation_base()
        repo_path = (repo_root / repo_name).resolve()
        if repo_path.exists():
            await self.send_interaction_embed(
                interaction,
                title="레포 경로 중복",
                description=f"`{repo_path}` 경로가 이미 존재해요.",
                tone="warning",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        created_channel: discord.TextChannel | None = None
        created_repo = False
        branch_name = "main"
        try:
            if normalized_github_url is None:
                repo_path.mkdir(parents=True, exist_ok=False)
                created_repo = True

                init_result = await self.run_process(
                    ["git", "init", "-b", "main", str(repo_path)],
                    cwd=repo_root,
                    timeout_seconds=30,
                )
                if init_result.timed_out or init_result.returncode != 0:
                    raise RuntimeError(init_result.stderr or init_result.stdout or "git init failed")
            else:
                clone_result = await self.run_process(
                    ["git", "clone", normalized_github_url, str(repo_path)],
                    cwd=repo_root,
                    timeout_seconds=120,
                )
                if clone_result.timed_out or clone_result.returncode != 0:
                    raise RuntimeError(clone_result.stderr or clone_result.stdout or "git clone failed")
                created_repo = repo_path.exists()

                branch_result = await self.run_process(
                    ["git", "-C", str(repo_path), "branch", "--show-current"],
                    cwd=repo_root,
                    timeout_seconds=15,
                )
                if not branch_result.timed_out and branch_result.returncode == 0 and branch_result.stdout.strip():
                    branch_name = branch_result.stdout.strip()

            created_channel = await guild.create_text_channel(
                name=repo_name,
                category=channel.category,
                topic=f"workspace: {repo_path}",
                overwrites=channel.overwrites,
                reason=f"새 Git 레포/채널 생성: {repo_name}",
            )

            updated_paths = self.persist_channel_mapping(created_channel.id, repo_path)
        except Exception as exc:
            if created_channel is not None:
                try:
                    await created_channel.delete(reason=f"새 Git 레포 생성 롤백: {repo_name}")
                except discord.HTTPException:
                    logging.exception("Failed to roll back created channel %s", created_channel.id)
            if created_repo and repo_path.exists():
                shutil.rmtree(repo_path, ignore_errors=True)
            logging.exception("Failed to create repo/channel for %s", repo_name)
            await self.send_interaction_embed(
                interaction,
                title="레포 생성 실패",
                description=f"새 레포와 채널을 만들지 못했어요.\n`{exc}`",
                tone="error",
            )
            return

        await self.send_interaction_embed(
            interaction,
            title="레포 생성 완료",
            description=(
                f"채널: {created_channel.mention}\n"
                f"레포 경로: `{repo_path}`\n"
                f"브랜치: `{branch_name}`\n"
                + (f"원본: `{normalized_github_url}`\n" if normalized_github_url else "")
                + ("생성 방식: GitHub clone\n" if normalized_github_url else "생성 방식: 빈 레포 초기화\n")
                + f"설정 반영: {', '.join(str(path) for path in updated_paths)}\n"
                + "이미 실행 중인 다른 봇 프로세스는 재시작 전까지 새 채널 매핑을 바로 인식하지 못할 수 있어요."
            ),
            tone="success",
        )

    async def handle_delete_repo(self, interaction: discord.Interaction, confirm_name: str) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_channel_only(interaction, command_name_for_role("delete-repo", self.config.bot_role)):
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.send_interaction_embed(
                interaction,
                title="채널에서만 사용 가능",
                description="삭제는 일반 텍스트 채널에서만 시작할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return

        workspace = self.config.channel_workspaces.get(channel.id)
        if workspace is None:
            await self.send_interaction_embed(
                interaction,
                title="삭제 대상 없음",
                description="이 채널은 레포 워크스페이스에 매핑되어 있지 않아요.",
                tone="warning",
                ephemeral=True,
            )
            return

        if self.is_protected_workspace(workspace):
            await self.send_interaction_embed(
                interaction,
                title="보호된 워크스페이스",
                description=(
                    f"`{workspace}` 는 bot 운영용 경로라서 `/delete-repo`로 삭제할 수 없어요.\n"
                    "codex-discord 자체 checkout과 그 내부 경로는 보호됩니다."
                ),
                tone="error",
                ephemeral=True,
            )
            return

        expected = channel.name.strip()
        if confirm_name.strip() != expected:
            await self.send_interaction_embed(
                interaction,
                title="확인 이름 불일치",
                description=(
                    "삭제 확인을 위해 현재 채널 이름을 정확히 다시 입력해야 해요.\n"
                    f"입력값: `{confirm_name.strip() or '(비어 있음)'}`\n"
                    f"기대값: `{expected}`"
                ),
                tone="warning",
                ephemeral=True,
            )
            return

        deletion_id = f"{channel.id}:{uuid.uuid4().hex[:10]}"
        self.pending_repo_deletions[deletion_id] = PendingRepoDeletion(
            channel_id=channel.id,
            workspace=workspace.resolve(),
            requested_at=now_utc_iso(),
            requested_by_user_id=interaction.user.id,
        )
        view = DeleteRepoConfirmationView(self, deletion_id)
        await self.send_interaction_embed(
            interaction,
            title="레포 삭제 최종 확인",
            description=(
                "이 작업은 되돌릴 수 없어요.\n"
                f"삭제 대상 채널: {channel.mention}\n"
                f"삭제 대상 경로: `{workspace.resolve()}`\n"
                f"확인 일치: `{expected}`\n"
                "아래 `최종 삭제` 버튼을 눌러야 실제로 삭제됩니다."
            ),
            tone="error",
            ephemeral=True,
            view=view,
        )

    async def handle_delete_repo_confirmation(
        self,
        interaction: discord.Interaction,
        deletion_id: str,
        confirmed: bool,
        view: DeleteRepoConfirmationView,
    ) -> None:
        pending = self.pending_repo_deletions.pop(deletion_id, None)
        if pending is None:
            self.disable_view_buttons(view)
            embed = self.build_embed("삭제 요청 만료", "이 삭제 요청은 더 이상 유효하지 않아요.", tone="warning")
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        self.disable_view_buttons(view)
        if not confirmed:
            embed = self.build_embed("삭제 취소됨", "레포와 채널 삭제를 취소했어요.", tone="success")
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        channel = self.get_channel(pending.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(pending.channel_id)
            except discord.HTTPException:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            embed = self.build_embed("채널 조회 실패", "삭제 대상 채널을 찾지 못했어요.", tone="error")
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        workspace = pending.workspace.resolve()
        if self.is_protected_workspace(workspace):
            embed = self.build_embed(
                "보호된 워크스페이스",
                f"`{workspace}` 는 보호된 경로라 삭제할 수 없어요.",
                tone="error",
            )
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        try:
            updated_paths = self.remove_channel_mapping(channel.id)
            shutil.rmtree(workspace)
        except Exception as exc:
            logging.exception("Failed to delete repo/channel for %s", workspace)
            try:
                self.persist_channel_mapping(channel.id, workspace)
            except Exception:
                logging.exception("Failed to restore channel mapping for %s", workspace)
            embed = self.build_embed(
                "삭제 실패",
                f"레포 삭제 또는 설정 정리 중 오류가 발생했어요.\n`{exc}`",
                tone="error",
            )
            await interaction.response.edit_message(embed=embed, view=view, content=None)
            return

        embed = self.build_embed(
            "삭제 완료",
            (
                f"레포 경로를 삭제했고 채널도 곧 정리합니다.\n"
                f"삭제된 경로: `{workspace}`\n"
                f"설정 반영: {', '.join(str(path) for path in updated_paths)}"
            ),
            tone="success",
        )
        await interaction.response.edit_message(embed=embed, view=view, content=None)
        await asyncio.sleep(1)
        try:
            await channel.delete(reason=f"레포 삭제 완료: {workspace.name}")
        except discord.HTTPException:
            logging.exception("Failed to delete Discord channel %s after repo deletion", channel.id)

    async def handle_status(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_thread_only(interaction, command_name_for_role("status", self.config.bot_role)):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        await interaction.response.defer(thinking=True)
        await self.send_status_embed(interaction)

    async def handle_diff(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_thread_only(interaction, command_name_for_role("diff", self.config.bot_role)):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=False)
        target_path = workspace or self.config.checkout_path

        await interaction.response.defer(thinking=True)
        await self.send_diff_embed(interaction, target_path)

    async def handle_break(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_thread_only(interaction, command_name_for_role("break", self.config.bot_role)):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await self.send_interaction_embed(
                interaction,
                title="스레드에서만 사용 가능",
                description=f"`/{command_name_for_role('break', self.config.bot_role)}` 명령은 세션 스레드 안에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return

        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=False)
        if thread is None or workspace is None:
            await self.send_interaction_embed(
                interaction,
                title="워크스페이스 연결 필요",
                description="이 스레드는 워크스페이스에 연결되어 있지 않아요.",
                tone="warning",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        runtime = self.get_or_create_runtime(thread.id, workspace)
        try:
            interrupted, detail = await runtime.interrupt_active_turn()
        except Exception as exc:
            logging.exception("Failed to interrupt active turn thread=%s", thread.id)
            await self.send_interaction_embed(
                interaction,
                title="중단 요청 실패",
                description=f"현재 작업을 중단하지 못했어요.\n`{exc}`",
                tone="error",
                ephemeral=True,
            )
            return

        if not interrupted:
            await self.send_interaction_embed(
                interaction,
                title="중단할 작업 없음",
                description=detail,
                tone="warning",
                ephemeral=True,
            )
            return

        cleared = self.clear_pending_approvals_for_thread(thread.id)
        self.record_execution("break", "success", workspace, 0.0, detail)
        await self.send_interaction_embed(
            interaction,
            title="중단 요청 전송",
            description=(
                f"{detail}\n"
                f"정리한 승인 요청 수: `{cleared}`\n"
                f"잠시 뒤 turn 상태가 `interrupted`로 바뀌면 정상입니다."
            ),
            tone="success",
            ephemeral=True,
        )

    async def handle_danger_on(self, interaction: discord.Interaction) -> None:
        await self.handle_danger_mode_toggle(interaction, enabled=True)

    async def handle_danger_off(self, interaction: discord.Interaction) -> None:
        await self.handle_danger_mode_toggle(interaction, enabled=False)

    async def handle_restart_staging(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await self.send_interaction_embed(
                interaction,
                title="main 봇 전용 명령",
                description="`/restart-staging` 명령은 main 봇에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return
        if not await self.ensure_session_context(interaction, "restart-staging"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        await interaction.response.defer(thinking=True)
        result_text = await self.run_main_operation("restart_staging", self.config.restart_staging_args)
        await self.send_output(interaction, result_text, filename_prefix="restart-staging")

    async def handle_restart(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await self.send_interaction_embed(
                interaction,
                title="main 봇 전용 명령",
                description="`/restart` 명령은 main 봇에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return
        if not await self.ensure_session_context(interaction, "restart"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        self.record_execution("restart", "requested", self.config.checkout_path, 0.0, "재시작 요청 접수")
        operation = self.create_background_operation("restart", self.config.restart_args, interaction.channel_id)
        await self.send_interaction_embed(
            interaction,
            title="재시작 요청 접수",
            description=self.format_background_operation_message(operation),
            tone="warning",
        )
        asyncio.create_task(self.spawn_main_operation(operation))

    async def handle_deploy(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await self.send_interaction_embed(
                interaction,
                title="main 봇 전용 명령",
                description="`/deploy` 명령은 main 봇에서만 사용할 수 있어요.",
                tone="warning",
                ephemeral=True,
            )
            return
        if not await self.ensure_session_context(interaction, "deploy"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        self.record_execution("deploy", "requested", self.config.checkout_path, 0.0, "배포 요청 접수")
        operation = self.create_background_operation("deploy", self.config.deploy_args, interaction.channel_id)
        await self.send_interaction_embed(
            interaction,
            title="배포 요청 접수",
            description=self.format_background_operation_message(operation),
            tone="warning",
        )
        asyncio.create_task(self.spawn_main_operation(operation))


def get_bot_from_interaction(interaction: discord.Interaction) -> CodexDiscordBot:
    client = interaction.client
    if not isinstance(client, CodexDiscordBot):
        raise RuntimeError("unexpected bot instance")
    return client


@app_commands.command(name="ping", description="봇이 살아 있는지 확인합니다. 채널 전용.")
async def ping_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_ping(interaction)


@app_commands.command(name="new-session", description="선택한 봇에 연결된 새 세션 스레드를 만듭니다. 채널 전용.")
@app_commands.describe(target_bot_role="새 세션을 맡을 봇입니다. 기본값은 main", title="새 세션 스레드 제목")
@app_commands.choices(
    target_bot_role=[
        app_commands.Choice(name="메인", value="main"),
        app_commands.Choice(name="스테이징", value="staging"),
    ]
)
async def new_session_command(
    interaction: discord.Interaction,
    title: str,
    target_bot_role: app_commands.Choice[str] | None = None,
) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_new_session(interaction, target_bot_role.value if target_bot_role else "main", title)


@app_commands.command(name="new-repo", description="새 Git 레포와 대응 채널을 함께 만듭니다. 채널 전용.")
@app_commands.describe(
    name="새 레포와 채널에 사용할 이름",
    github_url="선택 사항: 이 GitHub 레포를 clone해서 시작",
)
async def new_repo_command(interaction: discord.Interaction, name: str, github_url: str | None = None) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_new_repo(interaction, name, github_url)


@app_commands.command(name="delete-repo", description="현재 채널에 연결된 Git 레포와 채널을 삭제합니다. 매우 위험합니다. 채널 전용.")
@app_commands.describe(confirm_name="확인용으로 현재 채널 이름을 정확히 다시 입력")
async def delete_repo_command(interaction: discord.Interaction, confirm_name: str) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_delete_repo(interaction, confirm_name)


@app_commands.command(name="status", description="봇 역할, 체크아웃, 최근 상태를 보여줍니다. 스레드 전용.")
async def status_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_status(interaction)


@app_commands.command(name="diff", description="현재 워크스페이스 또는 체크아웃의 변경 파일과 diff 통계를 보여줍니다. 스레드 전용.")
async def diff_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_diff(interaction)


@app_commands.command(name="break", description="현재 스레드에서 실행 중인 Codex 작업을 강제 중단합니다. 스레드 전용.")
async def break_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_break(interaction)


@app_commands.command(name="danger-on", description="현재 스레드에만 위험 모드(승인/샌드박스 우회)를 켭니다. 스레드 전용.")
async def danger_on_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_danger_on(interaction)


@app_commands.command(name="danger-off", description="현재 스레드의 위험 모드를 끄고 기본 설정으로 되돌립니다. 스레드 전용.")
async def danger_off_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_danger_off(interaction)


@app_commands.command(name="restart", description="main 봇 서비스를 재시작합니다. main 봇 전용. 채널/스레드 사용 가능.")
async def restart_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_restart(interaction)


@app_commands.command(name="restart-staging", description="staging 봇 서비스를 재시작합니다. main 봇 전용. 채널/스레드 사용 가능.")
async def restart_staging_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_restart_staging(interaction)


@app_commands.command(name="deploy", description="설정된 배포 스크립트를 실행합니다. main 봇 전용. 채널/스레드 사용 가능.")
async def deploy_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_deploy(interaction)


def main() -> None:
    configure_logging()
    config = load_app_config()
    bot = CodexDiscordBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
