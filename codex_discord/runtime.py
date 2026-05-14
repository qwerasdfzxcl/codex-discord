from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from codex_discord.core import (
    APP_SERVER_STREAM_LIMIT,
    BREAK_CONFIRM_TIMEOUT_SECONDS,
    build_codex_subprocess_env,
    now_utc_iso,
)

if TYPE_CHECKING:
    from codex_discord.bot import CodexDiscordBot


@dataclass
class AppServerTurnState:
    turn_id: str
    source_message_id: int
    completion_future: asyncio.Future[dict[str, object]]
    agent_messages_sent: int = 0
    started_at: str = field(default_factory=now_utc_iso)
    last_event_at: str = field(default_factory=now_utc_iso)
    last_event: str = "turn created"


class ThreadSessionRuntime:
    def __init__(self, bot: CodexDiscordBot, discord_thread_id: int, workspace: Path) -> None:
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
                limit=APP_SERVER_STREAM_LIMIT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.stdout_task = asyncio.create_task(self.read_stdout_loop())
            self.stderr_task = asyncio.create_task(self.read_stderr_loop())
            self.attach_reader_task(self.stdout_task, "stdout")
            self.attach_reader_task(self.stderr_task, "stderr")
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
            try:
                line = await stream.readline()
            except ValueError as exc:
                raise RuntimeError(
                    f"app-server stdout line exceeded stream limit {APP_SERVER_STREAM_LIMIT} bytes"
                ) from exc
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
            try:
                line = await stream.readline()
            except ValueError as exc:
                raise RuntimeError(
                    f"app-server stderr line exceeded stream limit {APP_SERVER_STREAM_LIMIT} bytes"
                ) from exc
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logging.warning("app-server stderr thread=%s %s", self.discord_thread_id, text)

    def attach_reader_task(self, task: asyncio.Task[None], stream_name: str) -> None:
        task.add_done_callback(lambda completed_task: self.handle_reader_task_done(completed_task, stream_name))

    def handle_reader_task_done(self, task: asyncio.Task[None], stream_name: str) -> None:
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is None:
            return

        logging.exception(
            "app-server %s reader failed thread=%s",
            stream_name,
            self.discord_thread_id,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        self.started = False
        self.fail_pending(RuntimeError(f"app-server {stream_name} reader failed: {exception}"))

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

    async def ensure_codex_thread(self) -> tuple[str, bool]:
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
        started_fresh_after_resume_failure = False
        if record is not None:
            try:
                resume_params["threadId"] = record.session_id
                response = await self.send_request("thread/resume", resume_params)
            except Exception:
                logging.exception("Failed to resume app-server thread for Discord thread %s", self.discord_thread_id)
                self.bot.session_store.delete(self.discord_thread_id)
                started_fresh_after_resume_failure = True
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
        return codex_thread_id, started_fresh_after_resume_failure

    async def run_turn(self, prompt: str, source_message: discord.Message) -> dict[str, object]:
        codex_thread_id, started_fresh_after_resume_failure = await self.ensure_codex_thread()
        if started_fresh_after_resume_failure:
            await self.bot.post_agent_message(
                self.discord_thread_id,
                source_message.id,
                "기존 Codex 세션 복원에 실패해서 새 세션으로 전환했어요.",
                reply_to_source=True,
            )
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

        return await completion_future

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
                await self.shutdown()
                return (
                    True,
                    (
                        f"중단 요청은 보냈지만 {BREAK_CONFIRM_TIMEOUT_SECONDS}초 안에 실제 중단을 확인하지 못했어요.\n"
                        "앱서버 런타임을 강제로 재시작해서 이 스레드의 잠금 상태를 해제했어요."
                    ),
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
