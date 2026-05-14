import asyncio
import io
import json
import logging
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from codex_discord.core import (
    APP_SERVER_STREAM_LIMIT,
    BACKGROUND_OPERATION_POLL_SECONDS,
    BACKGROUND_OPERATIONS_DIR_NAME,
    BREAK_CONFIRM_TIMEOUT_SECONDS,
    DISCORD_FILE_FALLBACK_LIMIT,
    EMBED_DESCRIPTION_LIMIT,
    EMBED_FIELD_VALUE_LIMIT,
    EMBED_COLOR_ERROR,
    EMBED_COLOR_INFO,
    EMBED_COLOR_SUCCESS,
    EMBED_COLOR_WARNING,
    STATE_DIRECTORY_NAME,
    AppConfig,
    ConfigError,
    DiffSummary,
    ExecutionRecord,
    GitSummary,
    ProcessResult,
    SessionStore,
    ThreadSessionRecord,
    ThreadPolicyStore,
    build_codex_subprocess_env,
    chunk_text,
    command_name_for_role,
    configure_logging,
    format_code_block,
    format_code_field,
    format_thread_binding_name,
    is_relative_to,
    load_app_config,
    message_to_prompt,
    normalize_github_clone_url,
    normalize_repo_name,
    now_utc_iso,
    output_title_for_prefix,
    truncate_text,
    thread_target_role,
)
from codex_discord.runtime import ThreadSessionRuntime
from codex_discord.slash_commands import configure_tree
from codex_discord.views import AppServerApprovalView, DeleteRepoConfirmationView


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
        await configure_tree(self)
        if self.config.guild_id is not None:
            logging.info("Synced slash commands to guild %s", self.config.guild_id)
        else:
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

    def resolve_checkout_path_for_role(self, bot_role: str) -> Path | None:
        if bot_role == self.config.bot_role:
            return self.config.checkout_path.resolve()
        candidate = (self.config.checkout_path.parent / bot_role).resolve()
        if not candidate.is_dir():
            return None
        return candidate

    def resolve_policy_store_path_for_role(self, bot_role: str) -> Path | None:
        checkout_path = self.resolve_checkout_path_for_role(bot_role)
        if checkout_path is None:
            return None
        return checkout_path / STATE_DIRECTORY_NAME / f"{bot_role}-thread-policies.json"

    def set_thread_danger_mode_for_role(self, thread_id: int, bot_role: str, enabled: bool) -> bool:
        if bot_role == self.config.bot_role:
            self.thread_policy_store.set_danger_mode(thread_id, enabled)
            return True

        policy_store_path = self.resolve_policy_store_path_for_role(bot_role)
        if policy_store_path is None:
            return False

        ThreadPolicyStore(policy_store_path).set_danger_mode(thread_id, enabled)
        return True

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

    async def handle_new_session(
        self,
        interaction: discord.Interaction,
        target_bot_role: str,
        title: str,
        danger_enabled: bool = False,
    ) -> None:
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
        if self.resolve_checkout_path_for_role(resolved_target_role) is None:
            await self.send_interaction_embed(
                interaction,
                title="세션 생성 실패",
                description=(
                    f"대상 봇 역할 `{resolved_target_role}` 의 체크아웃 경로를 찾지 못했어요.\n"
                    "BOT_ROLE별 checkout 구조와 실행 경로를 확인해 주세요."
                ),
                tone="error",
                ephemeral=True,
            )
            return

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

        if not self.set_thread_danger_mode_for_role(thread.id, resolved_target_role, danger_enabled):
            try:
                await thread.delete(reason=f"세션 생성 롤백: thread policy 저장 실패 ({resolved_target_role})")
            except discord.HTTPException:
                logging.exception("Failed to roll back thread after policy write failure: thread=%s", thread.id)
            await self.send_interaction_embed(
                interaction,
                title="세션 생성 실패",
                description=(
                    "새 스레드는 만들었지만 danger 설정을 저장하지 못해서 생성 작업을 취소했어요.\n"
                    f"대상 봇 역할: `{resolved_target_role}`"
                ),
                tone="error",
                ephemeral=True,
            )
            return

        await self.send_interaction_embed(
            interaction,
            title="세션 생성 완료",
            description=(
                f"대상 봇: `{resolved_target_role}`\n"
                f"위험 모드: `{'on' if danger_enabled else 'off'}`\n"
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
