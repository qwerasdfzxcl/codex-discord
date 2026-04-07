from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from codex_discord.bot import CodexDiscordBot


class AppServerApprovalView(discord.ui.View):
    def __init__(self, bot: CodexDiscordBot, approval_id: str) -> None:
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
    def __init__(self, bot: CodexDiscordBot, deletion_id: str) -> None:
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
