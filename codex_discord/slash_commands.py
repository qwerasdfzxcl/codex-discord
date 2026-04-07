from __future__ import annotations

from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands

from codex_discord.core import command_name_for_role

if TYPE_CHECKING:
    from codex_discord.bot import CodexDiscordBot


def get_bot_from_interaction(interaction: discord.Interaction) -> CodexDiscordBot:
    return cast("CodexDiscordBot", interaction.client)


@app_commands.command(name="ping", description="봇이 살아 있는지 확인합니다. 채널 전용.")
async def ping_command(interaction: discord.Interaction) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_ping(interaction)


@app_commands.command(name="new-session", description="선택한 봇에 연결된 새 세션 스레드를 만듭니다. 채널 전용.")
@app_commands.describe(
    target_bot_role="새 세션을 맡을 봇입니다. 기본값은 main",
    title="새 세션 스레드 제목",
    danger="새 스레드 danger 모드. 기본값은 off",
)
@app_commands.choices(
    target_bot_role=[
        app_commands.Choice(name="메인", value="main"),
        app_commands.Choice(name="스테이징", value="staging"),
    ],
    danger=[
        app_commands.Choice(name="off (기본값)", value="off"),
        app_commands.Choice(name="on", value="on"),
    ],
)
async def new_session_command(
    interaction: discord.Interaction,
    title: str,
    target_bot_role: app_commands.Choice[str] | None = None,
    danger: app_commands.Choice[str] | None = None,
) -> None:
    bot = get_bot_from_interaction(interaction)
    await bot.handle_new_session(
        interaction,
        target_bot_role.value if target_bot_role else "main",
        title,
        danger_enabled=(danger.value == "on") if danger else False,
    )


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


async def configure_tree(bot: CodexDiscordBot) -> None:
    guild = discord.Object(id=bot.config.guild_id) if bot.config.guild_id else None
    bot.tree.on_error = bot.on_tree_error
    bot.tree.clear_commands(guild=guild)

    ping_command.name = command_name_for_role("ping", bot.config.bot_role)
    new_session_command.name = command_name_for_role("new-session", bot.config.bot_role)
    new_repo_command.name = command_name_for_role("new-repo", bot.config.bot_role)
    delete_repo_command.name = command_name_for_role("delete-repo", bot.config.bot_role)
    status_command.name = command_name_for_role("status", bot.config.bot_role)
    diff_command.name = command_name_for_role("diff", bot.config.bot_role)
    break_command.name = command_name_for_role("break", bot.config.bot_role)
    danger_on_command.name = command_name_for_role("danger-on", bot.config.bot_role)
    danger_off_command.name = command_name_for_role("danger-off", bot.config.bot_role)
    restart_command.name = "restart"
    restart_staging_command.name = "restart-staging"
    deploy_command.name = "deploy"

    bot.tree.add_command(ping_command, guild=guild)
    bot.tree.add_command(new_session_command, guild=guild)
    bot.tree.add_command(new_repo_command, guild=guild)
    bot.tree.add_command(delete_repo_command, guild=guild)
    bot.tree.add_command(status_command, guild=guild)
    bot.tree.add_command(diff_command, guild=guild)
    bot.tree.add_command(break_command, guild=guild)
    bot.tree.add_command(danger_on_command, guild=guild)
    bot.tree.add_command(danger_off_command, guild=guild)

    if bot.config.bot_role == "main":
        bot.tree.add_command(restart_command, guild=guild)
        bot.tree.add_command(restart_staging_command, guild=guild)
        bot.tree.add_command(deploy_command, guild=guild)

    if guild is not None:
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
