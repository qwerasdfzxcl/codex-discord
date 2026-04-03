import asyncio
import io
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

DISCORD_MESSAGE_LIMIT = 1900
DISCORD_FILE_FALLBACK_LIMIT = 8000
DISCORD_THREAD_NAME_LIMIT = 100
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_HISTORY_MESSAGES = 20
DEFAULT_MAX_PROMPT_CHARS = 12000
THREAD_BINDING_PATTERN = re.compile(r"^\[bot:(main|staging)\]\s*")
STATE_DIRECTORY_NAME = ".codex-discord-state"
BACKGROUND_OPERATIONS_DIR_NAME = "background-operations"
BACKGROUND_OPERATION_POLL_SECONDS = 5


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AppConfig:
    bot_role: str
    token: str
    allowed_user_id: int
    checkout_path: Path
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
class PendingApproval:
    runtime: "ThreadSessionRuntime"
    request_id: int | str
    method: str
    params: dict[str, object]
    requested_at: str


@dataclass
class BackgroundOperation:
    operation_id: str
    command_name: str
    args: list[str]
    target_channel_id: int
    metadata_path: Path
    log_path: Path


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunk_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return ["(no output)"]

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


@dataclass
class AppServerTurnState:
    turn_id: str
    source_message_id: int
    completion_future: asyncio.Future[dict[str, object]]
    agent_messages_sent: int = 0


class AppServerApprovalView(discord.ui.View):
    def __init__(self, bot: "CodexDiscordBot", approval_id: str) -> None:
        super().__init__(timeout=3600)
        self.bot = bot
        self.approval_id = approval_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.config.allowed_user_id:
            await interaction.response.send_message("You are not allowed to approve this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.danger)
    async def approve(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_app_server_approval_click(interaction, self.approval_id, approved=True, view=self)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary)
    async def deny(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.bot.handle_app_server_approval_click(interaction, self.approval_id, approved=False, view=self)


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
                    self.current_turns[turn_id] = self.pending_turn_state
                    self.pending_turn_state = None
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
            if item_type == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
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
            if not turn_state.completion_future.done():
                turn_state.completion_future.set_result(turn)

            status = turn.get("status")
            error = turn.get("error")
            if turn_state.agent_messages_sent == 0:
                if status == "failed" and isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message.strip():
                        await self.bot.post_agent_message(
                            self.discord_thread_id,
                            turn_state.source_message_id,
                            f"Turn failed: {message}",
                            reply_to_source=True,
                        )
                elif status == "interrupted":
                    await self.bot.post_agent_message(
                        self.discord_thread_id,
                        turn_state.source_message_id,
                        "Turn interrupted.",
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
        await self.ensure_started()
        request_id = self.next_request_id
        self.next_request_id += 1
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending_requests[request_id] = future
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self.write_json(payload)
        return await future

    async def send_response(self, request_id: int | str, result: dict[str, object]) -> None:
        if self.process is None or self.process.returncode is not None or not self.started:
            raise RuntimeError("app-server is not running")
        await self.write_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def ensure_codex_thread(self) -> str:
        model = self.bot.resolve_codex_model()
        resume_params: dict[str, object] = {
            "threadId": "",
            "cwd": str(self.workspace),
            "approvalPolicy": self.bot.resolve_approval_policy(),
            "approvalsReviewer": "user",
            "sandbox": self.bot.resolve_sandbox_mode(),
            "persistExtendedHistory": False,
        }
        start_params: dict[str, object] = {
            "cwd": str(self.workspace),
            "approvalPolicy": self.bot.resolve_approval_policy(),
            "approvalsReviewer": "user",
            "sandbox": self.bot.resolve_sandbox_mode(),
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
            },
        )
        turn = response.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and self.pending_turn_state is not None:
                self.pending_turn_state.turn_id = turn_id
                self.current_turns[turn_id] = self.pending_turn_state
                self.pending_turn_state = None

        return await asyncio.wait_for(completion_future, timeout=self.bot.config.timeout_seconds)


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
        self.background_notifications_task: asyncio.Task[None] | None = None
        self.background_notifications_lock = asyncio.Lock()
        self.last_execution: ExecutionRecord | None = None
        state_path = self.config.checkout_path / STATE_DIRECTORY_NAME / f"{self.config.bot_role}-app-server-threads.json"
        self.session_store = SessionStore(state_path)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id) if self.config.guild_id else None
        self.tree.on_error = self.on_tree_error
        self.tree.clear_commands(guild=guild)

        ping_command.name = command_name_for_role("ping", self.config.bot_role)
        new_session_command.name = command_name_for_role("new-session", self.config.bot_role)
        status_command.name = command_name_for_role("status", self.config.bot_role)
        diff_command.name = command_name_for_role("diff", self.config.bot_role)
        restart_command.name = "restart"
        restart_staging_command.name = "restart-staging"
        deploy_command.name = "deploy"

        self.tree.add_command(ping_command, guild=guild)
        self.tree.add_command(new_session_command, guild=guild)
        self.tree.add_command(status_command, guild=guild)
        self.tree.add_command(diff_command, guild=guild)

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
            await thread.send(
                f"Prompt is too long ({len(prompt)} chars). "
                f"Limit: {self.config.max_prompt_chars} chars."
            )
            return

        lock = self.get_thread_lock(thread.id)
        if lock.locked():
            await thread.send("A Codex task is already running in this thread. Wait for it to finish.")
            return

        async with lock:
            async with thread.typing():
                runtime = self.get_or_create_runtime(thread.id, workspace)
                started = asyncio.get_running_loop().time()
                try:
                    turn = await runtime.run_turn(prompt, message)
                except Exception as exc:
                    duration = asyncio.get_running_loop().time() - started
                    self.record_execution("ask", "failed", workspace, duration, f"runtime error: {exc}")
                    await thread.send(f"Codex runtime error: {exc}")
                    return
                duration = asyncio.get_running_loop().time() - started
                turn_status = str(turn.get("status", "unknown"))
                summary = f"turn {turn_status} in {duration:.1f}s"
                record_status = "success" if turn_status == "completed" else "failed"
                self.record_execution("ask", record_status, workspace, duration, summary)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logging.exception("Unhandled app command error", exc_info=error)
        message = f"Command failed: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

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

        expected_command_prefix = "" if owner_role == "main" else "-staging"
        message = (
            f"This thread belongs to the `{owner_role}` bot. "
            f"Use the `{owner_role}` bot commands{expected_command_prefix} in this thread."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
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

        message = "You are not allowed to use this bot."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def ensure_channel_only(self, interaction: discord.Interaction, command_name: str) -> bool:
        if isinstance(interaction.channel, discord.Thread):
            message = f"`/{command_name}` can only be used in a parent channel, not inside a thread."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False
        return True

    async def ensure_thread_only(self, interaction: discord.Interaction, command_name: str) -> bool:
        if not isinstance(interaction.channel, discord.Thread):
            message = f"`/{command_name}` can only be used inside a thread."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False
        return True

    async def ensure_session_context(self, interaction: discord.Interaction, command_name: str) -> bool:
        parent_channel, workspace = self.resolve_session_parent_channel(interaction)
        if parent_channel is None or workspace is None:
            message = f"`/{command_name}` must be used in a mapped parent channel or one of its threads."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False
        return True

    async def send_output(self, interaction: discord.Interaction, text: str, filename_prefix: str) -> None:
        if len(text) > DISCORD_FILE_FALLBACK_LIMIT:
            filename = f"{filename_prefix}.txt"
            file_data = io.BytesIO(text.encode("utf-8", errors="replace"))
            file = discord.File(file_data, filename=filename)
            summary = f"Output was too long, attached as `{filename}`."
            if interaction.response.is_done():
                await interaction.followup.send(summary, file=file)
            else:
                await interaction.response.send_message(summary, file=file)
            return

        chunks = chunk_text(text)
        if interaction.response.is_done():
            await interaction.followup.send(chunks[0])
        else:
            await interaction.response.send_message(chunks[0])

        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    async def send_message_output(self, source_message: discord.Message, text: str, filename_prefix: str) -> None:
        if len(text) > DISCORD_FILE_FALLBACK_LIMIT:
            filename = f"{filename_prefix}.txt"
            file_data = io.BytesIO(text.encode("utf-8", errors="replace"))
            file = discord.File(file_data, filename=filename)
            await source_message.reply(f"Output was too long, attached as `{filename}`.", file=file, mention_author=False)
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

    def resolve_approval_policy(self) -> object:
        return self.extract_arg_value(self.config.codex_global_args, ("--ask-for-approval", "-a")) or "on-request"

    def resolve_sandbox_mode(self) -> str:
        if "--dangerously-bypass-approvals-and-sandbox" in self.config.codex_global_args:
            return "danger-full-access"
        if "--full-auto" in self.config.codex_global_args:
            return "workspace-write"
        sandbox = self.extract_arg_value(
            self.config.codex_global_args + self.config.codex_exec_args,
            ("--sandbox", "-s"),
        )
        return sandbox or "workspace-write"

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
            await channel.send(f"Output was too long, attached as `{filename}`.", file=file)
            return

        chunks = chunk_text(text)
        await channel.send(chunks[0])
        for chunk in chunks[1:]:
            await channel.send(chunk)

    def format_app_server_approval_message(self, method: str, params: dict[str, object]) -> str:
        reason = params.get("reason")
        reason_line = f"reason: {reason}" if isinstance(reason, str) and reason.strip() else "reason: (none)"

        if method == "item/commandExecution/requestApproval":
            command = params.get("command")
            cwd = params.get("cwd")
            lines = ["Codex wants to run a command.", reason_line]
            if isinstance(command, str) and command.strip():
                lines.append(f"command: `{command}`")
            if isinstance(cwd, str) and cwd.strip():
                lines.append(f"cwd: `{cwd}`")
            return "\n".join(lines)

        if method == "item/fileChange/requestApproval":
            grant_root = params.get("grantRoot")
            lines = ["Codex wants to apply file changes.", reason_line]
            if isinstance(grant_root, str) and grant_root.strip():
                lines.append(f"grant root: `{grant_root}`")
            return "\n".join(lines)

        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            return "Codex requested additional permissions.\n" + reason_line + f"\npermissions: `{json.dumps(permissions, ensure_ascii=False)}`"

        if method == "execCommandApproval":
            command = params.get("command")
            return "Codex wants to run a command.\n" + reason_line + f"\ncommand: `{command}`"

        if method == "applyPatchApproval":
            return "Codex wants to apply a patch.\n" + reason_line

        return f"Codex requested approval for `{method}`.\n{reason_line}"

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

        channel = self.get_channel(runtime.discord_thread_id)
        if not isinstance(channel, discord.Thread):
            fetched = await self.fetch_channel(runtime.discord_thread_id)
            channel = fetched if isinstance(fetched, discord.Thread) else None
        if not isinstance(channel, discord.Thread):
            logging.warning("Unable to find Discord thread for approval request %s", approval_id)
            return

        view = AppServerApprovalView(self, approval_id)
        await channel.send(self.format_app_server_approval_message(method, params), view=view)

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
            await interaction.response.edit_message(content="This approval request is no longer available.", view=view)
            return

        self.disable_view_buttons(view)
        response_payload: dict[str, object]
        if pending.method == "item/commandExecution/requestApproval":
            response_payload = {"decision": "accept" if approved else "cancel"}
        elif pending.method == "item/fileChange/requestApproval":
            response_payload = {"decision": "accept" if approved else "cancel"}
        elif pending.method == "item/permissions/requestApproval":
            if approved:
                response_payload = {"permissions": pending.params.get("permissions", {}), "scope": "turn"}
            else:
                response_payload = {"permissions": {}, "scope": "turn"}
        elif pending.method in {"execCommandApproval", "applyPatchApproval"}:
            response_payload = {"decision": "approved" if approved else "abort"}
        else:
            response_payload = {"decision": "accept" if approved else "cancel"}

        try:
            await pending.runtime.send_response(pending.request_id, response_payload)
        except Exception as exc:
            logging.exception("Failed to send approval response")
            await interaction.response.edit_message(content=f"Failed to send approval response: {exc}", view=view)
            return

        status_text = "Approved." if approved else "Denied."
        await interaction.response.edit_message(content=status_text, view=view)

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

    async def get_git_summary(self, target_path: Path) -> str:
        inside_repo = await self.run_process(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_path,
            timeout_seconds=15,
        )
        if inside_repo.timed_out or inside_repo.returncode != 0 or inside_repo.stdout != "true":
            return f"path: {target_path}\ngit: not a repository"

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

        branch = branch_result.stdout or "(unknown)"
        commit = commit_result.stdout or "(unknown)"
        changed_files = 0
        if status_result.stdout:
            changed_files = len(status_result.stdout.splitlines())

        lines = [
            f"path: {target_path}",
            f"branch: {branch}",
            f"commit: {commit}",
            f"changed files: {changed_files}",
        ]
        if status_result.stderr:
            lines.append(f"git stderr:\n{status_result.stderr}")
        return "\n".join(lines)

    async def build_status_text(self, interaction: discord.Interaction) -> str:
        thread, workspace = self.resolve_thread_workspace(interaction, require_thread=False)
        current_thread_busy = False
        thread_session: ThreadSessionRecord | None = None
        if thread is not None:
            current_thread_busy = self.get_thread_lock(thread.id).locked()
            if workspace is not None:
                thread_session = self.get_thread_session(thread.id, workspace)

        lines = [
            f"role: {self.config.bot_role}",
            f"checkout: {self.config.checkout_path}",
            f"active threads: {self.active_thread_count()}",
            f"current thread busy: {'yes' if current_thread_busy else 'no'}",
        ]

        if workspace is not None:
            lines.append(f"thread workspace: {workspace}")
        else:
            lines.append("thread workspace: (not in a mapped thread)")

        if thread_session is not None:
            lines.append(f"thread session id: {thread_session.session_id}")
            lines.append(f"thread session updated: {thread_session.updated_at}")
        else:
            lines.append("thread session id: (none)")

        if self.last_execution is None:
            lines.append("last execution: none")
        else:
            lines.extend(
                [
                    f"last execution command: {self.last_execution.command_name}",
                    f"last execution status: {self.last_execution.status}",
                    f"last execution target: {self.last_execution.target_path or '(none)'}",
                    f"last execution time: {self.last_execution.started_at}",
                    f"last execution duration: {self.last_execution.duration_seconds:.1f}s",
                    f"last execution summary: {self.last_execution.summary}",
                ]
            )

        lines.append("")
        lines.append("checkout git:")
        lines.append(await self.get_git_summary(self.config.checkout_path))

        if workspace is not None and workspace != self.config.checkout_path:
            lines.append("")
            lines.append("workspace git:")
            lines.append(await self.get_git_summary(workspace))

        return "\n".join(lines)

    async def build_diff_text(self, target_path: Path) -> str:
        inside_repo = await self.run_process(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_path,
            timeout_seconds=15,
        )
        if inside_repo.timed_out or inside_repo.returncode != 0 or inside_repo.stdout != "true":
            self.record_execution("diff", "failed", target_path, inside_repo.duration_seconds, "not a git repository")
            return f"path: {target_path}\ngit: not a repository"

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

        summary = "clean working tree"
        if status_result.stdout or diff_stat_result.stdout:
            summary = "changes detected"

        status = "success" if not status_result.timed_out and not diff_stat_result.timed_out else "failed"
        self.record_execution(
            "diff",
            status,
            target_path,
            max(status_result.duration_seconds, diff_stat_result.duration_seconds),
            summary,
        )

        lines = [f"path: {target_path}", "", "changed files:"]
        lines.append(status_result.stdout or "(no changed files)")
        lines.append("")
        lines.append("diff stat:")
        lines.append(diff_stat_result.stdout or "(no diff stat)")

        if status_result.stderr:
            lines.append("")
            lines.append(f"status stderr:\n{status_result.stderr}")
        if diff_stat_result.stderr:
            lines.append("")
            lines.append(f"diff stderr:\n{diff_stat_result.stderr}")

        return "\n".join(lines)

    async def run_main_operation(self, command_name: str, args: list[str]) -> str:
        try:
            result = await self.run_process(args=args, cwd=self.config.checkout_path)
        except FileNotFoundError:
            summary = f"missing executable: {args[0]}"
            self.record_execution(command_name, "failed", self.config.checkout_path, 0.0, summary)
            return f"Unable to start `{args[0]}`."
        except Exception as exc:
            logging.exception("Unexpected error while running %s", command_name)
            summary = f"unexpected error: {exc}"
            self.record_execution(command_name, "failed", self.config.checkout_path, 0.0, summary)
            return f"Unexpected error while running {command_name}: {exc}"

        if result.timed_out:
            self.record_execution(
                command_name,
                "failed",
                self.config.checkout_path,
                result.duration_seconds,
                f"timed out after {self.config.timeout_seconds}s",
            )
            return f"{command_name} timed out after {self.config.timeout_seconds} seconds."

        status = "success" if result.returncode == 0 else "failed"
        self.record_execution(
            command_name,
            status,
            self.config.checkout_path,
            result.duration_seconds,
            f"exit code {result.returncode}",
        )

        lines = [f"command: {' '.join(args)}", f"exit code: {result.returncode}"]
        if result.stdout:
            lines.append("")
            lines.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            lines.append("")
            lines.append(f"stderr:\n{result.stderr}")
        if not result.stdout and not result.stderr:
            lines.append("")
            lines.append("(no output)")
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
            f"{operation.command_name} requested.",
            f"command: {' '.join(operation.args)}",
            f"log: {operation.log_path}",
        ]
        if operation.command_name == "deploy":
            lines.append("The main bot may restart before posting a final follow-up message.")
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
                    f"{command_name} completed.",
                    f"status: {status}",
                ]
                if isinstance(returncode, int):
                    lines.append(f"exit code: {returncode}")
                if isinstance(command_args, list) and all(isinstance(item, str) for item in command_args):
                    lines.append(f"command: {' '.join(command_args)}")
                lines.append(f"log: {log_path}")
                if isinstance(error_message, str) and error_message.strip():
                    lines.append(f"error: {error_message}")

                try:
                    await channel.send("\n".join(lines))
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
        await interaction.response.send_message(
            f"pong\nrole: {self.config.bot_role}\nlatency_ms: {latency_ms}"
        )

    async def handle_new_session(self, interaction: discord.Interaction, target_bot_role: str, title: str) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_channel_only(interaction, command_name_for_role("new-session", self.config.bot_role)):
            return

        parent_channel, workspace = self.resolve_session_parent_channel(interaction)
        if parent_channel is None:
            await interaction.response.send_message(
                f"`/{command_name_for_role('new-session', self.config.bot_role)}` must be used in a mapped parent channel.",
                ephemeral=True,
            )
            return
        if workspace is None:
            await interaction.response.send_message(
                "This channel is not mapped to a workspace.",
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
                reason=f"New Codex session for {resolved_target_role}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Unable to create a thread in this channel. Check the bot's thread permissions.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Unable to create a new session thread: {exc}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Created session for `{resolved_target_role}`: {thread.mention}\nworkspace: {workspace}"
        )

    async def handle_status(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if not await self.ensure_thread_only(interaction, command_name_for_role("status", self.config.bot_role)):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        await interaction.response.defer(thinking=True)
        status_text = await self.build_status_text(interaction)
        await self.send_output(interaction, status_text, filename_prefix="status")

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
        diff_text = await self.build_diff_text(target_path)
        await self.send_output(interaction, diff_text, filename_prefix="diff")

    async def handle_restart_staging(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await interaction.response.send_message("`/restart-staging` is only available on the main bot.", ephemeral=True)
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
            await interaction.response.send_message("`/restart` is only available on the main bot.", ephemeral=True)
            return
        if not await self.ensure_session_context(interaction, "restart"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        self.record_execution("restart", "requested", self.config.checkout_path, 0.0, "restart requested")
        operation = self.create_background_operation("restart", self.config.restart_args, interaction.channel_id)
        await interaction.response.send_message(self.format_background_operation_message(operation))
        asyncio.create_task(self.spawn_main_operation(operation))

    async def handle_deploy(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await interaction.response.send_message("`/deploy` is only available on the main bot.", ephemeral=True)
            return
        if not await self.ensure_session_context(interaction, "deploy"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        self.record_execution("deploy", "requested", self.config.checkout_path, 0.0, "deploy requested")
        operation = self.create_background_operation("deploy", self.config.deploy_args, interaction.channel_id)
        await interaction.response.send_message(self.format_background_operation_message(operation))
        asyncio.create_task(self.spawn_main_operation(operation))


def get_bot_from_interaction(interaction: discord.Interaction) -> CodexDiscordBot:
    client = interaction.client
    if not isinstance(client, CodexDiscordBot):
        raise RuntimeError("unexpected bot instance")
    return client


@app_commands.command(name="ping", description="Check whether the bot is alive. Channel only.")
async def ping_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_ping(interaction)


@app_commands.command(name="new-session", description="Create a new session thread bound to a selected bot. Channel only.")
@app_commands.describe(target_bot_role="Which bot should own the new session. Defaults to main", title="Thread title for the new session")
@app_commands.choices(
    target_bot_role=[
        app_commands.Choice(name="main", value="main"),
        app_commands.Choice(name="staging", value="staging"),
    ]
)
async def new_session_command(
    interaction: discord.Interaction,
    title: str,
    target_bot_role: app_commands.Choice[str] | None = None,
) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_new_session(interaction, target_bot_role.value if target_bot_role else "main", title)


@app_commands.command(name="status", description="Show bot role, checkout, and recent status. Thread only.")
async def status_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_status(interaction)


@app_commands.command(name="diff", description="Show changed files and diff stat for the current workspace or checkout. Thread only.")
async def diff_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_diff(interaction)


@app_commands.command(name="restart", description="Restart the main bot service. Main bot only. Channel or thread.")
async def restart_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_restart(interaction)


@app_commands.command(name="restart-staging", description="Restart the staging bot service. Main bot only. Channel or thread.")
async def restart_staging_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_restart_staging(interaction)


@app_commands.command(name="deploy", description="Run the configured deploy script. Main bot only. Channel or thread.")
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
