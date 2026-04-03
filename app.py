import asyncio
import io
import json
import logging
import os
import re
import tempfile
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
    codex_exec_args: list[str]
    restart_staging_args: list[str]
    deploy_args: list[str]


@dataclass
class ProcessResult:
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass
class ExecutionRecord:
    command_name: str
    status: str
    target_path: Path | None
    started_at: str
    duration_seconds: float
    summary: str


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
        default=["/usr/bin/systemctl", "restart", "codex-discord-staging"],
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
        codex_exec_args=codex_exec_args,
        restart_staging_args=restart_staging_args,
        deploy_args=deploy_args,
    )


class CodexDiscordBot(commands.Bot):
    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.thread_locks: dict[int, asyncio.Lock] = {}
        self.last_execution: ExecutionRecord | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id) if self.config.guild_id else None
        self.tree.on_error = self.on_tree_error
        self.tree.clear_commands(guild=guild)

        ping_command.name = command_name_for_role("ping", self.config.bot_role)
        new_session_command.name = command_name_for_role("new-session", self.config.bot_role)
        status_command.name = command_name_for_role("status", self.config.bot_role)
        diff_command.name = command_name_for_role("diff", self.config.bot_role)
        restart_staging_command.name = "restart-staging"
        deploy_command.name = "deploy"

        self.tree.add_command(ping_command, guild=guild)
        self.tree.add_command(new_session_command, guild=guild)
        self.tree.add_command(status_command, guild=guild)
        self.tree.add_command(diff_command, guild=guild)

        if self.config.bot_role == "main":
            self.tree.add_command(restart_staging_command, guild=guild)
            self.tree.add_command(deploy_command, guild=guild)

        if guild is not None:
            await self.tree.sync(guild=guild)
            logging.info("Synced slash commands to guild %s", guild.id)
        else:
            await self.tree.sync()
            logging.info("Synced global slash commands")

    async def on_ready(self) -> None:
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

        lock = self.get_thread_lock(thread.id)
        if lock.locked():
            await thread.send("A Codex task is already running in this thread. Wait for it to finish.")
            return

        async with lock:
            async with thread.typing():
                full_prompt = await self.build_thread_prompt(thread)
                response_text = await self.run_codex(full_prompt, workspace)
            await self.send_message_output(message, response_text, filename_prefix=f"message-{thread.id}")

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

    async def run_process(self, args: list[str], cwd: Path, timeout_seconds: int | None = None) -> ProcessResult:
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

    async def build_thread_prompt(self, thread: discord.Thread) -> str:
        lines = [
            "You are running inside a Discord-driven Codex wrapper.",
            "The working directory is already set to the mapped development workspace.",
            "Only operate inside that workspace.",
            "Respond to the latest user message in this thread while using prior thread messages as context.",
            "",
            f"Thread title: {thread.name}",
            f"Thread id: {thread.id}",
            "",
            "Previous conversation:",
        ]

        async for item in thread.history(limit=self.config.history_messages, oldest_first=True):
            if item.type != discord.MessageType.default:
                continue

            is_bot_message = self.user is not None and item.author.id == self.user.id
            is_allowed_user_message = item.author.id == self.config.allowed_user_id
            if not is_bot_message and not is_allowed_user_message:
                continue

            content = item.content.strip()
            if not content and not item.attachments:
                continue

            if item.attachments:
                attachments = [f"attachment: {attachment.filename} ({attachment.url})" for attachment in item.attachments]
                content = "\n".join(filter(None, [content, *attachments]))

            role = "assistant" if is_bot_message else "user"
            lines.append(f"[{role}] {item.author.display_name}:")
            lines.append(content)
            lines.append("")

        full_prompt = "\n".join(lines).strip()
        if len(full_prompt) > self.config.max_prompt_chars:
            full_prompt = (
                "The earlier thread context was truncated to fit the configured prompt size.\n\n"
                + full_prompt[-self.config.max_prompt_chars :]
            )
        return full_prompt

    async def run_codex(self, prompt: str, workspace: Path) -> str:
        temp_output_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="codex-discord-", suffix=".txt", delete=False) as temp_file:
                temp_output_path = temp_file.name

            args = [
                self.config.codex_bin,
                "exec",
                "-C",
                str(workspace),
                "-o",
                temp_output_path,
                *self.config.codex_exec_args,
                prompt,
            ]

            result = await self.run_process(args=args, cwd=workspace)

            output_text = ""
            if temp_output_path and Path(temp_output_path).is_file():
                output_text = Path(temp_output_path).read_text(encoding="utf-8", errors="replace").strip()

            sections = [f"workspace: {workspace}"]
            if result.timed_out:
                sections.append(f"codex exec timed out after {self.config.timeout_seconds} seconds.")
            elif result.returncode == 0:
                sections.append(output_text or result.stdout or "codex exec finished without output.")
            else:
                sections.append(f"codex exec failed with exit code {result.returncode}.")
                if output_text:
                    sections.append(output_text)
                elif result.stdout:
                    sections.append(f"stdout:\n{result.stdout}")

            if result.stderr:
                sections.append(f"stderr:\n{result.stderr}")

            status = "success" if result.returncode == 0 and not result.timed_out else "failed"
            summary = f"{status} in {result.duration_seconds:.1f}s"
            self.record_execution("ask", status, workspace, result.duration_seconds, summary)

            return "\n\n".join(sections)
        except FileNotFoundError:
            summary = f"missing executable: {self.config.codex_bin}"
            self.record_execution("ask", "failed", workspace, 0.0, summary)
            return f"Unable to start `{self.config.codex_bin}`. Check that Codex CLI is installed and on PATH."
        except Exception as exc:
            logging.exception("Unexpected error while running codex exec")
            summary = f"unexpected error: {exc}"
            self.record_execution("ask", "failed", workspace, 0.0, summary)
            return f"Unexpected error while running codex exec: {exc}"
        finally:
            if temp_output_path:
                try:
                    Path(temp_output_path).unlink(missing_ok=True)
                except OSError:
                    logging.warning("Failed to remove temporary output file: %s", temp_output_path)

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
        if thread is not None:
            current_thread_busy = self.get_thread_lock(thread.id).locked()

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
        if not await self.ensure_thread_only(interaction, "restart-staging"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        await interaction.response.defer(thinking=True)
        result_text = await self.run_main_operation("restart_staging", self.config.restart_staging_args)
        await self.send_output(interaction, result_text, filename_prefix="restart-staging")

    async def handle_deploy(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_allowed_or_reply(interaction):
            return
        if self.config.bot_role != "main":
            await interaction.response.send_message("`/deploy` is only available on the main bot.", ephemeral=True)
            return
        if not await self.ensure_thread_only(interaction, "deploy"):
            return
        if not await self.ensure_thread_owned(interaction):
            return

        await interaction.response.defer(thinking=True)
        result_text = await self.run_main_operation("deploy", self.config.deploy_args)
        await self.send_output(interaction, result_text, filename_prefix="deploy")


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


@app_commands.command(name="restart-staging", description="Restart the staging bot service. Main bot only. Thread only.")
async def restart_staging_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_restart_staging(interaction)


@app_commands.command(name="deploy", description="Run the configured deploy script. Main bot only. Thread only.")
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
