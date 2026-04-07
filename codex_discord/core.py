import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import discord
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
APP_SERVER_STREAM_LIMIT = 4 * 1024 * 1024
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
    checkout_path = Path(__file__).resolve().parent.parent
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
