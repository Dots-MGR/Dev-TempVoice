import os
import json
import asyncio
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "voice_data.json"

MAX_CHANNEL_NAME_LENGTH = 100
CLAIM_DELAY = 10


# =========================================================
# INTENTS
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=".",
    intents=intents
)


# =========================================================
# DATA
# =========================================================

server_configs = {}

# channel_id -> {
#     owner_id,
#     guild_id,
#     locked,
#     hidden,
#     allowed_users,
#     denied_users,
#     moderators,
#     panel_message_id
# }
temporary_channels = {}

# channel_id -> asyncio.Task
claim_tasks = {}


# =========================================================
# PERSISTENCE
# =========================================================

def load_data():
    global server_configs, temporary_channels

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        server_configs = data.get("servers", {})
        saved_channels = data.get("temporary_channels", {})

        temporary_channels = {
            int(channel_id): channel_data
            for channel_id, channel_data in saved_channels.items()
        }

        # Make sure older saved channels get the new field.
        for channel_id, channel_data in temporary_channels.items():
            channel_data.setdefault("panel_message_id", None)
            channel_data.setdefault("locked", False)
            channel_data.setdefault("hidden", False)
            channel_data.setdefault("allowed_users", [])
            channel_data.setdefault("denied_users", [])
            channel_data.setdefault("moderators", [])

        print("✅ Voice configuration loaded.")

    except FileNotFoundError:
        server_configs = {}
        temporary_channels = {}

    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {DATA_FILE}: {e}")
        server_configs = {}
        temporary_channels = {}

    except Exception as e:
        print(f"❌ Failed to load voice configuration: {e}")
        server_configs = {}
        temporary_channels = {}


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "servers": server_configs,
                    "temporary_channels": {
                        str(channel_id): data
                        for channel_id, data in temporary_channels.items()
                    }
                },
                f,
                indent=2
            )

    except Exception as e:
        print(f"❌ Failed to save voice data: {e}")


# =========================================================
# HELPERS
# =========================================================

def get_config(guild_id: int):
    return server_configs.get(str(guild_id))


def get_temporary_channel(channel_id: int):
    return temporary_channels.get(channel_id)


def get_member_channel(
    interaction: discord.Interaction
) -> Optional[discord.VoiceChannel]:

    if interaction.user.voice is None:
        return None

    channel = interaction.user.voice.channel

    if not isinstance(channel, discord.VoiceChannel):
        return None

    return channel


def is_channel_owner(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):
    data = get_temporary_channel(channel.id)

    if not data:
        return False

    return data.get("owner_id") == interaction.user.id


def is_channel_manager(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):
    data = get_temporary_channel(channel.id)

    if not data:
        return False

    if data.get("owner_id") == interaction.user.id:
        return True

    return interaction.user.id in data.get("moderators", [])


def is_allowed_user(
    channel_id: int,
    user_id: int
):
    data = get_temporary_channel(channel_id)

    if not data:
        return False

    return user_id in data.get("allowed_users", [])


def is_denied_user(
    channel_id: int,
    user_id: int
):
    data = get_temporary_channel(channel_id)

    if not data:
        return False

    return user_id in data.get("denied_users", [])


def format_voice_name(
    template: str,
    member: discord.Member
):
    return template.replace(
        "{user}",
        member.display_name
    )[:MAX_CHANNEL_NAME_LENGTH]


def cancel_claim_task(channel_id: int):
    task = claim_tasks.pop(channel_id, None)

    if task and not task.done():
        task.cancel()


# =========================================================
# CHANNEL PERMISSIONS
# =========================================================

async def apply_channel_permissions(
    channel: discord.VoiceChannel
):
    data = get_temporary_channel(channel.id)

    if not data:
        return

    guild = channel.guild

    locked = data.get("locked", False)
    hidden = data.get("hidden", False)

    default_permissions = discord.PermissionOverwrite(
        connect=not locked,
        view_channel=not hidden
    )

    await channel.set_permissions(
        guild.default_role,
        overwrite=default_permissions,
        reason="TempVoice permission update"
    )

    # -----------------------------------------------------
    # OWNER
    # -----------------------------------------------------

    owner = guild.get_member(
        data["owner_id"]
    )

    if owner:
        await channel.set_permissions(
            owner,
            connect=True,
            view_channel=True,
            speak=True,
            reason="TempVoice owner permissions"
        )

    # -----------------------------------------------------
    # MODERATORS
    # -----------------------------------------------------

    for user_id in data.get("moderators", []):

        member = guild.get_member(user_id)

        if member:
            await channel.set_permissions(
                member,
                connect=True,
                view_channel=True,
                speak=True,
                reason="TempVoice moderator permissions"
            )

    # -----------------------------------------------------
    # ALLOWED USERS
    # -----------------------------------------------------

    for user_id in data.get("allowed_users", []):

        member = guild.get_member(user_id)

        if member:
            await channel.set_permissions(
                member,
                connect=True,
                view_channel=True,
                reason="TempVoice allowed user"
            )

    # -----------------------------------------------------
    # DENIED USERS
    # -----------------------------------------------------

    for user_id in data.get("denied_users", []):

        member = guild.get_member(user_id)

        if member:
            await channel.set_permissions(
                member,
                connect=False,
                view_channel=False,
                reason="TempVoice denied user"
            )


# =========================================================
# PANEL EMBED
# =========================================================

def build_panel_embed(
    channel: discord.VoiceChannel
):

    data = get_temporary_channel(
        channel.id
    )

    owner_text = "Unknown"

    if data:

        owner = channel.guild.get_member(
            data["owner_id"]
        )

        if owner:
            owner_text = owner.mention

    locked = False
    hidden = False

    if data:
        locked = data.get("locked", False)
        hidden = data.get("hidden", False)

    lock_status = (
        "🔒 Locked"
        if locked
        else
        "🔓 Unlocked"
    )

    visibility_status = (
        "👁️ Hidden"
        if hidden
        else
        "👀 Visible"
    )

    embed = discord.Embed(
        title="🎙️ Temporary Voice Controls",
        description=(
            f"Manage **{channel.name}** using the controls below.\n\n"
            f"👑 **Owner:** {owner_text}\n"
            f"📊 **Members:** {len(channel.members)} / "
            f"{channel.user_limit or '∞'}\n"
            f"⚙️ **Status:** "
            f"{lock_status} • {visibility_status}"
        ),
        color=(
            discord.Color.red()
            if locked or hidden
            else discord.Color.blurple()
        )
    )

    embed.add_field(
        name="Basic",
        value=(
            "✏️ Rename\n"
            "👥 User Limit\n"
            f"{'🔓 Unlock' if locked else '🔒 Lock'}\n"
            f"{'👀 Show' if hidden else '👁️ Hide'}"
        ),
        inline=True
    )

    embed.add_field(
        name="Members",
        value=(
            "➕ Allow\n"
            "➖ Deny\n"
            "👢 Kick\n"
            "🛡️ Moderators"
        ),
        inline=True
    )

    embed.add_field(
        name="Ownership",
        value=(
            "👑 Transfer\n"
            "🙋 Claim\n"
            "🗑️ Delete"
        ),
        inline=True
    )

    embed.set_footer(
        text="TempVoice • Self-hosted"
    )

    return embed


# =========================================================
# DYNAMIC CONTROL PANEL VIEW
# =========================================================

class TempVoiceView(discord.ui.View):

    def __init__(
        self,
        channel: Optional[discord.VoiceChannel] = None
    ):

        super().__init__(
            timeout=None
        )

        # -------------------------------------------------
        # If no channel was provided, create the complete
        # persistent fallback view.
        # -------------------------------------------------

        if channel is None:

            self.add_item(
                self.make_button(
                    "Rename",
                    "✏️",
                    discord.ButtonStyle.primary,
                    "tempvoice:rename",
                    self.rename_button
                )
            )

            self.add_item(
                self.make_button(
                    "Limit",
                    "👥",
                    discord.ButtonStyle.secondary,
                    "tempvoice:limit",
                    self.limit_button
                )
            )

            self.add_item(
                self.make_button(
                    "Lock",
                    "🔒",
                    discord.ButtonStyle.danger,
                    "tempvoice:lock",
                    self.lock_button
                )
            )

            self.add_item(
                self.make_button(
                    "Hide",
                    "👁️",
                    discord.ButtonStyle.danger,
                    "tempvoice:hide",
                    self.hide_button
                )
            )

            self.add_item(
                self.make_button(
                    "Allow",
                    "➕",
                    discord.ButtonStyle.success,
                    "tempvoice:allow",
                    self.allow_button
                )
            )

            self.add_item(
                self.make_button(
                    "Deny",
                    "➖",
                    discord.ButtonStyle.danger,
                    "tempvoice:deny",
                    self.deny_button
                )
            )

            self.add_item(
                self.make_button(
                    "Kick",
                    "👢",
                    discord.ButtonStyle.danger,
                    "tempvoice:kick",
                    self.kick_button
                )
            )

            self.add_item(
                self.make_button(
                    "Moderator",
                    "🛡️",
                    discord.ButtonStyle.secondary,
                    "tempvoice:moderator",
                    self.moderator_button
                )
            )

            self.add_item(
                self.make_button(
                    "Transfer",
                    "👑",
                    discord.ButtonStyle.primary,
                    "tempvoice:transfer",
                    self.transfer_button
                )
            )

            self.add_item(
                self.make_button(
                    "Claim",
                    "🙋",
                    discord.ButtonStyle.secondary,
                    "tempvoice:claim",
                    self.claim_button
                )
            )

            self.add_item(
                self.make_button(
                    "Delete",
                    "🗑️",
                    discord.ButtonStyle.danger,
                    "tempvoice:delete",
                    self.delete_button
                )
            )

            return

        # -------------------------------------------------
        # Dynamic state
        # -------------------------------------------------

        data = get_temporary_channel(
            channel.id
        )

        if not data:
            return

        locked = data.get(
            "locked",
            False
        )

        hidden = data.get(
            "hidden",
            False
        )

        # -------------------------------------------------
        # BASIC
        # -------------------------------------------------

        self.add_item(
            self.make_button(
                "Rename",
                "✏️",
                discord.ButtonStyle.primary,
                "tempvoice:rename",
                self.rename_button
            )
        )

        self.add_item(
            self.make_button(
                "Limit",
                "👥",
                discord.ButtonStyle.secondary,
                "tempvoice:limit",
                self.limit_button
            )
        )

        # -------------------------------------------------
        # LOCK / UNLOCK
        # Only ONE exists at a time.
        # -------------------------------------------------

        if locked:

            self.add_item(
                self.make_button(
                    "Unlock",
                    "🔓",
                    discord.ButtonStyle.success,
                    "tempvoice:unlock",
                    self.unlock_button
                )
            )

        else:

            self.add_item(
                self.make_button(
                    "Lock",
                    "🔒",
                    discord.ButtonStyle.danger,
                    "tempvoice:lock",
                    self.lock_button
                )
            )

        # -------------------------------------------------
        # HIDE / SHOW
        # Only ONE exists at a time.
        # -------------------------------------------------

        if hidden:

            self.add_item(
                self.make_button(
                    "Show",
                    "👀",
                    discord.ButtonStyle.success,
                    "tempvoice:show",
                    self.show_button
                )
            )

        else:

            self.add_item(
                self.make_button(
                    "Hide",
                    "👁️",
                    discord.ButtonStyle.danger,
                    "tempvoice:hide",
                    self.hide_button
                )
            )

        # -------------------------------------------------
        # MEMBERS
        # -------------------------------------------------

        self.add_item(
            self.make_button(
                "Allow",
                "➕",
                discord.ButtonStyle.success,
                "tempvoice:allow",
                self.allow_button
            )
        )

        self.add_item(
            self.make_button(
                "Deny",
                "➖",
                discord.ButtonStyle.danger,
                "tempvoice:deny",
                self.deny_button
            )
        )

        self.add_item(
            self.make_button(
                "Kick",
                "👢",
                discord.ButtonStyle.danger,
                "tempvoice:kick",
                self.kick_button
            )
        )

        self.add_item(
            self.make_button(
                "Moderator",
                "🛡️",
                discord.ButtonStyle.secondary,
                "tempvoice:moderator",
                self.moderator_button
            )
        )

        # -------------------------------------------------
        # OWNERSHIP
        # -------------------------------------------------

        self.add_item(
            self.make_button(
                "Transfer",
                "👑",
                discord.ButtonStyle.primary,
                "tempvoice:transfer",
                self.transfer_button
            )
        )

        self.add_item(
            self.make_button(
                "Claim",
                "🙋",
                discord.ButtonStyle.secondary,
                "tempvoice:claim",
                self.claim_button
            )
        )

        self.add_item(
            self.make_button(
                "Delete",
                "🗑️",
                discord.ButtonStyle.danger,
                "tempvoice:delete",
                self.delete_button
            )
        )

    # =====================================================
    # BUTTON FACTORY
    # =====================================================

    def make_button(
        self,
        label,
        emoji,
        style,
        custom_id,
        callback
    ):

        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=custom_id
        )

        button.callback = callback

        return button

    # =====================================================
    # PANEL UPDATE
    # =====================================================

    # -----------------------------------------------------
    # RENAME
    # -----------------------------------------------------

    async def rename_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if channel:

            await interaction.response.send_modal(
                RenameVoiceModal(channel)
            )

    # -----------------------------------------------------
    # LIMIT
    # -----------------------------------------------------

    async def limit_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if channel:

            await interaction.response.send_modal(
                LimitVoiceModal(channel)
            )

    # -----------------------------------------------------
    # LOCK
    # -----------------------------------------------------

    async def lock_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        if data.get("locked"):
            await interaction.response.send_message(
                "ℹ️ The channel is already locked.",
                ephemeral=True
            )
            return

        data["locked"] = True

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "🔒 Your channel is now locked.",
            ephemeral=True
        )

    # -----------------------------------------------------
    # UNLOCK
    # -----------------------------------------------------

    async def unlock_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        if not data.get("locked"):
            await interaction.response.send_message(
                "ℹ️ The channel is already unlocked.",
                ephemeral=True
            )
            return

        data["locked"] = False

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "🔓 Your channel is now unlocked.",
            ephemeral=True
        )

    # -----------------------------------------------------
    # HIDE
    # -----------------------------------------------------

    async def hide_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        if data.get("hidden"):
            await interaction.response.send_message(
                "ℹ️ The channel is already hidden.",
                ephemeral=True
            )
            return

        data["hidden"] = True

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "👁️ Your channel is now hidden.",
            ephemeral=True
        )

    # -----------------------------------------------------
    # SHOW
    # -----------------------------------------------------

    async def show_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        if not data.get("hidden"):
            await interaction.response.send_message(
                "ℹ️ The channel is already visible.",
                ephemeral=True
            )
            return

        data["hidden"] = False

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "👀 Your channel is visible again.",
            ephemeral=True
        )

    # -----------------------------------------------------
    # ALLOW
    # -----------------------------------------------------

    async def allow_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "Select a member to allow:",
                view=MemberActionView(
                    channel,
                    "allow"
                ),
                ephemeral=True
            )

    # -----------------------------------------------------
    # DENY
    # -----------------------------------------------------

    async def deny_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "Select a member to deny:",
                view=MemberActionView(
                    channel,
                    "deny"
                ),
                ephemeral=True
            )

    # -----------------------------------------------------
    # KICK
    # -----------------------------------------------------

    async def kick_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await self.check_owner(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "Select a member to kick:",
                view=MemberActionView(
                    channel,
                    "kick"
                ),
                ephemeral=True
            )

    # -----------------------------------------------------
    # MODERATOR
    # -----------------------------------------------------

    async def moderator_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if not channel:
            return

        if not is_channel_owner(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ Only the owner can manage moderators.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "Select a member to add/remove as a moderator:",
            view=MemberActionView(
                channel,
                "moderator"
            ),
            ephemeral=True
        )

    # -----------------------------------------------------
    # TRANSFER
    # -----------------------------------------------------

    async def transfer_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if not channel:
            return

        if not is_channel_owner(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ Only the owner can transfer ownership.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "Select the new owner:",
            view=TransferView(channel),
            ephemeral=True
        )

    # -----------------------------------------------------
    # CLAIM
    # -----------------------------------------------------

    async def claim_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if channel:

            await claim_channel(
                interaction,
                channel
            )

    # -----------------------------------------------------
    # DELETE
    # -----------------------------------------------------

    async def delete_button(
        self,
        interaction: discord.Interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if not channel:
            return

        if not is_channel_owner(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ Only the owner can delete the channel.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "🗑️ Deleting your temporary channel...",
            ephemeral=True
        )

        await delete_temporary_channel(
            channel,
            "Deleted by temporary channel owner."
        )

    # =====================================================
    # PERMISSION CHECK
    # =====================================================

    async def check_owner(
        self,
        interaction: discord.Interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if channel is None:
            return None

        if not is_channel_manager(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ You aren't the owner or a channel moderator.",
                ephemeral=True
            )

            return None

        return channel


# =========================================================
# UPDATE CONTROL PANEL
# =========================================================

async def update_control_panel(
    channel: discord.VoiceChannel
):
    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    message_id = data.get(
        "panel_message_id"
    )

    # -----------------------------------------------------
    # If we don't know the message ID, create a panel.
    # -----------------------------------------------------

    if not message_id:

        await send_control_panel(
            channel
        )

        return

    try:

        message = await channel.fetch_message(
            message_id
        )

        await message.edit(
            embed=build_panel_embed(channel),
            view=TempVoiceView(channel)
        )

    except discord.NotFound:

        print(
            f"⚠️ TempVoice panel disappeared in "
            f"{channel.name}; recreating it."
        )

        data["panel_message_id"] = None

        save_data()

        await send_control_panel(
            channel
        )

    except discord.Forbidden:

        print(
            f"❌ Cannot access TempVoice panel in "
            f"{channel.name}."
        )

    except discord.HTTPException as e:

        print(
            f"❌ Failed to update TempVoice panel "
            f"in {channel.name}: {e}"
        )


# =========================================================
# SEND CONTROL PANEL
# =========================================================

async def send_control_panel(
    channel: discord.VoiceChannel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    # -----------------------------------------------------
    # First try to update an existing panel.
    # -----------------------------------------------------

    if data.get("panel_message_id"):

        try:

            message = await channel.fetch_message(
                data["panel_message_id"]
            )

            await message.edit(
                embed=build_panel_embed(channel),
                view=TempVoiceView(channel)
            )

            return

        except discord.NotFound:
            data["panel_message_id"] = None

        except discord.HTTPException:
            pass

    # -----------------------------------------------------
    # Create new panel.
    # -----------------------------------------------------

    try:

        message = await channel.send(
            embed=build_panel_embed(channel),
            view=TempVoiceView(channel)
        )

        data["panel_message_id"] = message.id

        save_data()

        print(
            f"✅ TempVoice panel created in "
            f"{channel.name}"
        )

    except discord.Forbidden:

        print(
            f"❌ I don't have permission to send "
            f"messages in {channel.name}."
        )

    except discord.HTTPException as e:

        print(
            f"❌ Failed to send TempVoice panel "
            f"in {channel.name}: {e}"
        )


# =========================================================
# DELETE TEMP CHANNEL
# =========================================================

async def delete_temporary_channel(
    channel: discord.VoiceChannel,
    reason="Temporary voice channel cleanup."
):

    channel_id = channel.id

    cancel_claim_task(
        channel_id
    )

    temporary_channels.pop(
        channel_id,
        None
    )

    save_data()

    try:

        await channel.delete(
            reason=reason
        )

    except discord.NotFound:
        pass

    except discord.Forbidden:

        print(
            f"❌ No permission to delete "
            f"{channel.name}"
        )

    except discord.HTTPException as e:

        print(
            f"❌ Failed to delete "
            f"{channel.name}: {e}"
        )


# =========================================================
# CREATE TEMP CHANNEL
# =========================================================

async def create_temporary_channel(
    member: discord.Member,
    config: dict
):

    guild = member.guild

    category_id = config.get(
        "category_id"
    )

    category = guild.get_channel(
        category_id
    )

    if category_id and not isinstance(
        category,
        discord.CategoryChannel
    ):

        category = None

    if category is None:

        raise RuntimeError(
            "The configured voice category no longer exists."
        )

    template = config.get(
        "default_name",
        "{user}'s Room"
    )

    channel_name = format_voice_name(
        template,
        member
    )

    user_limit = config.get(
        "default_limit",
        0
    )

    overwrites = {

        guild.default_role:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True
            ),

        member:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                speak=True,
                stream=True,
                use_voice_activation=True
            ),

        guild.me:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                manage_channels=True,
                move_members=True,
                send_messages=True,
                read_message_history=True
            )
    }

    channel = await guild.create_voice_channel(
        name=channel_name,
        category=category,
        user_limit=user_limit,
        overwrites=overwrites,
        reason="TempVoice channel creation"
    )

    temporary_channels[channel.id] = {

        "owner_id":
            member.id,

        "guild_id":
            guild.id,

        "locked":
            False,

        "hidden":
            False,

        "allowed_users":
            [],

        "denied_users":
            [],

        "moderators":
            [],

        "panel_message_id":
            None
    }

    save_data()

    try:

        await member.move_to(
            channel
        )

    except discord.HTTPException:
        pass

    return channel


# =========================================================
# CHANNEL ACCESS CHECK
# =========================================================

async def require_managed_channel(
    interaction: discord.Interaction
):

    channel = get_member_channel(
        interaction
    )

    if channel is None:

        await interaction.response.send_message(
            "❌ You aren't currently in a voice channel.",
            ephemeral=True
        )

        return None

    if not get_temporary_channel(
        channel.id
    ):

        await interaction.response.send_message(
            "❌ This isn't a temporary voice channel.",
            ephemeral=True
        )

        return None

    return channel


async def require_manager(
    interaction: discord.Interaction
):

    channel = await require_managed_channel(
        interaction
    )

    if channel is None:
        return None

    if not is_channel_manager(
        interaction,
        channel
    ):

        await interaction.response.send_message(
            "❌ You aren't the owner or a channel moderator.",
            ephemeral=True
        )

        return None

    return channel


# =========================================================
# VOICE STATE
# =========================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):

    # -----------------------------------------------------
    # CREATE CHANNEL
    # -----------------------------------------------------

    if after.channel is not None:

        config = get_config(
            member.guild.id
        )

        if config:

            create_channel_id = config.get(
                "create_channel_id"
            )

            if after.channel.id == create_channel_id:

                try:

                    channel = await create_temporary_channel(
                        member,
                        config
                    )

                    # Give Discord a moment to finish creating
                    # the text-chat portion of the voice channel.
                    await asyncio.sleep(0.5)

                    await send_control_panel(
                        channel
                    )

                except Exception as e:

                    print(
                        f"❌ Failed to create temporary "
                        f"channel: {repr(e)}"
                    )

    # -----------------------------------------------------
    # EMPTY CHANNEL
    # -----------------------------------------------------

    if before.channel is not None:

        channel = before.channel

        if channel.id in temporary_channels:

            if len(channel.members) == 0:

                await delete_temporary_channel(
                    channel,
                    "Temporary voice channel became empty."
                )

            else:

                data = temporary_channels.get(
                    channel.id
                )

                if data:

                    # Owner left.
                    # Channel stays alive and can be claimed.
                    if data["owner_id"] == member.id:

                        await update_control_panel(
                            channel
                        )


# =========================================================
# RENAME MODAL
# =========================================================

class RenameVoiceModal(
    discord.ui.Modal
):

    def __init__(
        self,
        channel: discord.VoiceChannel
    ):

        super().__init__(
            title="Rename Voice Channel"
        )

        self.channel = channel

        self.name_input = discord.ui.TextInput(
            label="Channel name",
            placeholder="Enter a new channel name...",
            max_length=100
        )

        self.add_item(
            self.name_input
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            await self.channel.edit(
                name=self.name_input.value,
                reason="TempVoice channel rename"
            )

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                f"✏️ Channel renamed to "
                f"**{self.name_input.value}**.",
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I don't have permission to rename this channel.",
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "❌ Discord rejected the channel rename.",
                ephemeral=True
            )


# =========================================================
# LIMIT MODAL
# =========================================================

class LimitVoiceModal(
    discord.ui.Modal
):

    def __init__(
        self,
        channel: discord.VoiceChannel
    ):

        super().__init__(
            title="Change User Limit"
        )

        self.channel = channel

        self.limit_input = discord.ui.TextInput(
            label="User limit",
            placeholder="0 = unlimited, maximum 99",
            max_length=2
        )

        self.add_item(
            self.limit_input
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            limit = int(
                self.limit_input.value
            )

            if limit < 0 or limit > 99:
                raise ValueError

        except ValueError:

            await interaction.response.send_message(
                "❌ Enter a number between 0 and 99.",
                ephemeral=True
            )

            return

        try:

            await self.channel.edit(
                user_limit=limit,
                reason="TempVoice user limit change"
            )

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                (
                    "👥 User limit removed."
                    if limit == 0
                    else
                    f"👥 User limit set to **{limit}**."
                ),
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "❌ Failed to change the user limit.",
                ephemeral=True
            )


# =========================================================
# MEMBER SELECT
# =========================================================

class MemberActionView(
    discord.ui.View
):

    def __init__(
        self,
        channel: discord.VoiceChannel,
        action: str
    ):

        super().__init__(
            timeout=60
        )

        self.channel = channel
        self.action = action

        self.member_select = discord.ui.UserSelect(
            placeholder="Select a member...",
            min_values=1,
            max_values=1
        )

        self.member_select.callback = (
            self.member_selected
        )

        self.add_item(
            self.member_select
        )

    async def member_selected(
        self,
        interaction: discord.Interaction
    ):

        member = self.member_select.values[0]

        if not isinstance(
            member,
            discord.Member
        ):

            await interaction.response.send_message(
                "❌ Invalid member.",
                ephemeral=True
            )

            return

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:

            await interaction.response.send_message(
                "❌ This channel no longer exists.",
                ephemeral=True
            )

            return

        if member.id == data["owner_id"]:

            await interaction.response.send_message(
                "❌ You cannot apply this action to the owner.",
                ephemeral=True
            )

            return

        # -------------------------------------------------
        # ALLOW
        # -------------------------------------------------

        if self.action == "allow":

            if member.id not in data["allowed_users"]:

                data["allowed_users"].append(
                    member.id
                )

            if member.id in data["denied_users"]:

                data["denied_users"].remove(
                    member.id
                )

            await self.channel.set_permissions(
                member,
                connect=True,
                view_channel=True,
                reason="TempVoice allowed member"
            )

            message = (
                f"➕ Allowed {member.mention}."
            )

        # -------------------------------------------------
        # DENY
        # -------------------------------------------------

        elif self.action == "deny":

            if member.id not in data["denied_users"]:

                data["denied_users"].append(
                    member.id
                )

            if member.id in data["allowed_users"]:

                data["allowed_users"].remove(
                    member.id
                )

            await self.channel.set_permissions(
                member,
                connect=False,
                view_channel=False,
                reason="TempVoice denied member"
            )

            if (
                member.voice
                and member.voice.channel == self.channel
            ):

                try:

                    await member.move_to(
                        None,
                        reason="TempVoice denied member"
                    )

                except discord.HTTPException:
                    pass

            message = (
                f"➖ Denied {member.mention}."
            )

        # -------------------------------------------------
        # KICK
        # -------------------------------------------------

        elif self.action == "kick":

            if (
                member.voice
                and member.voice.channel == self.channel
            ):

                try:

                    await member.move_to(
                        None,
                        reason="TempVoice owner kicked member"
                    )

                    message = (
                        f"👢 Kicked {member.mention}."
                    )

                except discord.Forbidden:

                    message = (
                        "❌ I don't have permission "
                        "to move that member."
                    )

            else:

                message = (
                    "❌ That member isn't in your channel."
                )

        # -------------------------------------------------
        # MODERATOR
        # -------------------------------------------------

        elif self.action == "moderator":

            moderators = data["moderators"]

            if member.id in moderators:

                moderators.remove(
                    member.id
                )

                message = (
                    f"🛡️ Removed {member.mention} "
                    "from channel moderators."
                )

            else:

                moderators.append(
                    member.id
                )

                message = (
                    f"🛡️ Added {member.mention} "
                    "as a channel moderator."
                )

            await apply_channel_permissions(
                self.channel
            )

        else:

            message = (
                "❌ Unknown action."
            )

        save_data()

        await update_control_panel(
            self.channel
        )

        await interaction.response.send_message(
            message,
            ephemeral=True
        )


# =========================================================
# CLAIM
# =========================================================

async def claim_channel(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:

        await interaction.response.send_message(
            "❌ This channel is no longer managed.",
            ephemeral=True
        )

        return

    if data["owner_id"] == interaction.user.id:

        await interaction.response.send_message(
            "👑 You already own this channel.",
            ephemeral=True
        )

        return

    owner = channel.guild.get_member(
        data["owner_id"]
    )

    if (
        owner
        and owner.voice
        and owner.voice.channel == channel
    ):

        await interaction.response.send_message(
            "❌ The current owner is still in the channel.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        f"🙋 Claiming **{channel.name}** in "
        f"**{CLAIM_DELAY} seconds**...\n"
        "Stay in the channel to complete the claim.",
        ephemeral=True
    )

    async def finish_claim():

        try:

            await asyncio.sleep(
                CLAIM_DELAY
            )

            if not interaction.user.voice:
                return

            if interaction.user.voice.channel != channel:
                return

            current_data = get_temporary_channel(
                channel.id
            )

            if not current_data:
                return

            current_data["owner_id"] = (
                interaction.user.id
            )

            save_data()

            await apply_channel_permissions(
                channel
            )

            await update_control_panel(
                channel
            )

            try:

                await interaction.followup.send(
                    "👑 You now own this temporary voice channel.",
                    ephemeral=True
                )

            except discord.HTTPException:
                pass

        except asyncio.CancelledError:
            pass

    cancel_claim_task(
        channel.id
    )

    task = asyncio.create_task(
        finish_claim()
    )

    claim_tasks[channel.id] = task


# =========================================================
# TRANSFER VIEW
# =========================================================

class TransferView(
    discord.ui.View
):

    def __init__(
        self,
        channel: discord.VoiceChannel
    ):

        super().__init__(
            timeout=60
        )

        self.channel = channel

        self.select = discord.ui.UserSelect(
            placeholder="Select the new owner...",
            min_values=1,
            max_values=1
        )

        self.select.callback = (
            self.callback
        )

        self.add_item(
            self.select
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        member = self.select.values[0]

        if not isinstance(
            member,
            discord.Member
        ):

            await interaction.response.send_message(
                "❌ Invalid member.",
                ephemeral=True
            )

            return

        if member.id == interaction.user.id:

            await interaction.response.send_message(
                "❌ You already own this channel.",
                ephemeral=True
            )

            return

        if member.voice is None:

            await interaction.response.send_message(
                "❌ The new owner must be in a voice channel.",
                ephemeral=True
            )

            return

        # The selected person must actually be in
        # the temporary channel being transferred.

        if member.voice.channel != self.channel:

            await interaction.response.send_message(
                "❌ The new owner must be in this temporary voice channel.",
                ephemeral=True
            )

            return

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:

            await interaction.response.send_message(
                "❌ This channel no longer exists.",
                ephemeral=True
            )

            return

        data["owner_id"] = member.id

        save_data()

        await apply_channel_permissions(
            self.channel
        )

        await update_control_panel(
            self.channel
        )

        await interaction.response.send_message(
            f"👑 Ownership transferred to "
            f"{member.mention}.",
            ephemeral=True
        )


# =========================================================
# /TEMPVOICE SETUP
# =========================================================

@bot.tree.command(
    name="tempvoice_setup",
    description="Set up the TempVoice system."
)
@app_commands.describe(
    create_channel="Voice channel users join to create a room",
    category="Category where temporary rooms will be created"
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def tempvoice_setup(
    interaction: discord.Interaction,
    create_channel: discord.VoiceChannel,
    category: discord.CategoryChannel
):

    guild = interaction.guild

    if guild is None:
        return

    server_configs[str(guild.id)] = {

        "create_channel_id":
            create_channel.id,

        "category_id":
            category.id,

        "default_name":
            "{user}'s Room",

        "default_limit":
            0
    }

    save_data()

    await interaction.response.send_message(
        "✅ **TempVoice has been configured!**\n\n"
        f"🎙️ Create channel: {create_channel.mention}\n"
        f"📁 Category: **{category.name}**\n\n"
        "Users who join the create channel will automatically "
        "receive their own temporary voice channel.",
        ephemeral=True
    )


# =========================================================
# /TEMPVOICE PANEL
# =========================================================

@bot.tree.command(
    name="tempvoice_panel",
    description="Show or refresh the TempVoice control panel."
)
async def tempvoice_panel(
    interaction: discord.Interaction
):

    channel = get_member_channel(
        interaction
    )

    if (
        channel
        and get_temporary_channel(channel.id)
    ):

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "✅ The TempVoice panel has been refreshed in your voice channel.",
            ephemeral=True
        )

        return

    embed = discord.Embed(
        title="🎙️ Temporary Voice Controls",
        description=(
            "Join your temporary voice channel and "
            "use the control panel there.\n\n"
            "Only the owner or channel moderators can "
            "manage a temporary channel."
        ),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /TEMPVOICE CONFIG
# =========================================================

@bot.tree.command(
    name="tempvoice_config",
    description="View the current TempVoice configuration."
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def tempvoice_config(
    interaction: discord.Interaction
):

    config = get_config(
        interaction.guild.id
    )

    if not config:

        await interaction.response.send_message(
            "❌ TempVoice hasn't been configured yet.",
            ephemeral=True
        )

        return

    create_channel = interaction.guild.get_channel(
        config.get("create_channel_id")
    )

    category = interaction.guild.get_channel(
        config.get("category_id")
    )

    embed = discord.Embed(
        title="⚙️ TempVoice Configuration",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Create Channel",
        value=(
            create_channel.mention
            if create_channel
            else "Missing"
        ),
        inline=False
    )

    embed.add_field(
        name="Category",
        value=(
            category.name
            if category
            else "Missing"
        ),
        inline=False
    )

    embed.add_field(
        name="Default Name",
        value=config.get(
            "default_name",
            "{user}'s Room"
        ),
        inline=False
    )

    embed.add_field(
        name="Default Limit",
        value=str(
            config.get(
                "default_limit",
                0
            )
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# CLEANUP TASK
# =========================================================

@tasks.loop(minutes=5)
async def cleanup_channels():

    changed = False

    for channel_id in list(
        temporary_channels.keys()
    ):

        channel = bot.get_channel(
            channel_id
        )

        if channel is None:

            temporary_channels.pop(
                channel_id,
                None
            )

            changed = True

            continue

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):

            temporary_channels.pop(
                channel_id,
                None
            )

            changed = True

            continue

        if len(channel.members) == 0:

            await delete_temporary_channel(
                channel,
                "Automatic TempVoice cleanup."
            )

            changed = True

    if changed:
        save_data()


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ You don't have the required "
            "Discord permission."
        )

    else:

        print(
            f"❌ Command error: {repr(error)}"
        )

        message = (
            "❌ Something went wrong while "
            "executing that command."
        )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except discord.HTTPException:
        pass


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    load_data()

    # -----------------------------------------------------
    # Register persistent fallback callbacks.
    # -----------------------------------------------------

    if not hasattr(
        bot,
        "tempvoice_view_added"
    ):

        bot.add_view(
            TempVoiceView()
        )

        bot.tempvoice_view_added = True

    # -----------------------------------------------------
    # Cleanup
    # -----------------------------------------------------

    if not cleanup_channels.is_running():

        cleanup_channels.start()

    try:

        synced = await bot.tree.sync()

        print(
            f"✅ TempVoice is online as {bot.user}"
        )

        print(
            f"🔧 Synced {len(synced)} slash commands"
        )

        print(
            f"🎙️ Tracking "
            f"{len(temporary_channels)} temporary channels"
        )

        # -------------------------------------------------
        # Refresh existing panels after startup.
        # -------------------------------------------------

        for channel_id in list(
            temporary_channels.keys()
        ):

            channel = bot.get_channel(
                channel_id
            )

            if (
                channel
                and isinstance(
                    channel,
                    discord.VoiceChannel
                )
            ):

                try:

                    await update_control_panel(
                        channel
                    )

                except Exception as e:

                    print(
                        f"⚠️ Failed to refresh panel "
                        f"for {channel.name}: {repr(e)}"
                    )

    except Exception as e:

        print(
            f"❌ Failed to sync commands: {repr(e)}"
        )


# =========================================================
# START
# =========================================================

if not BOT_TOKEN:

    raise RuntimeError(
        "BOT_TOKEN environment variable is missing!"
    )

bot.run(BOT_TOKEN)
