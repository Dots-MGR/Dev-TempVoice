import os
import json
import asyncio

import discord
from discord.ext import commands
from discord import app_commands

from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

DATA_FILE = "voice_data.json"


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
temporary_channels = {}


def load_data():
    global server_configs

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        server_configs = data.get("servers", {})

        print("✅ Voice configuration loaded.")

    except FileNotFoundError:
        server_configs = {}

    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {DATA_FILE}: {e}")
        server_configs = {}

    except Exception as e:
        print(f"❌ Failed to load voice configuration: {e}")
        server_configs = {}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "servers": server_configs
            },
            f,
            indent=2
        )


# =========================================================
# HELPERS
# =========================================================

def get_config(guild_id: int):
    return server_configs.get(str(guild_id))


def get_temporary_channel(channel_id: int):
    return temporary_channels.get(channel_id)


def is_channel_owner(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):
    data = get_temporary_channel(channel.id)

    if not data:
        return False

    return data["owner_id"] == interaction.user.id


async def delete_temporary_channel(
    channel: discord.VoiceChannel
):
    temporary_channels.pop(channel.id, None)

    try:
        await channel.delete(
            reason="Temporary voice channel became empty."
        )
    except discord.NotFound:
        pass
    except discord.Forbidden:
        print(
            f"❌ No permission to delete {channel.name}"
        )
    except discord.HTTPException as e:
        print(
            f"❌ Failed to delete {channel.name}: {e}"
        )


# =========================================================
# TEMPORARY VOICE CREATION
# =========================================================

async def create_temporary_channel(
    member: discord.Member,
    config: dict
):
    guild = member.guild

    category_id = config.get("category_id")

    category = guild.get_channel(category_id)

    if category_id and not isinstance(
        category,
        discord.CategoryChannel
    ):
        category = None

    if category is None:
        raise RuntimeError(
            "The configured voice category no longer exists."
        )

    channel_name = config.get(
        "default_name",
        f"{member.display_name}'s Room"
    )

    user_limit = config.get(
        "default_limit",
        0
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            connect=True,
            view_channel=True
        ),

        member: discord.PermissionOverwrite(
            connect=True,
            view_channel=True,
            speak=True,
            stream=True,
            use_voice_activation=True
        ),

        guild.me: discord.PermissionOverwrite(
            connect=True,
            view_channel=True,
            manage_channels=True,
            move_members=True
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
        "owner_id": member.id,
        "guild_id": guild.id
    }

    try:
        await member.move_to(channel)
    except discord.HTTPException:
        pass

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
    # USER JOINED A CREATE CHANNEL
    # -----------------------------------------------------

    if after.channel is not None:

        config = get_config(member.guild.id)

        if config:
            create_channel_id = config.get(
                "create_channel_id"
            )

            if after.channel.id == create_channel_id:

                try:
                    await create_temporary_channel(
                        member,
                        config
                    )

                except Exception as e:
                    print(
                        f"❌ Failed to create temporary "
                        f"channel: {repr(e)}"
                    )

    # -----------------------------------------------------
    # EMPTY TEMPORARY CHANNEL
    # -----------------------------------------------------

    if before.channel is not None:

        channel = before.channel

        if channel.id in temporary_channels:

            if len(channel.members) == 0:

                await delete_temporary_channel(
                    channel
                )


# =========================================================
# TEMPVOICE CONTROL PANEL
# =========================================================

class TempVoiceView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Rename",
        emoji="✏️",
        style=discord.ButtonStyle.primary,
        custom_id="tempvoice:rename"
    )
    async def rename_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        channel = interaction.user.voice.channel \
            if interaction.user.voice else None

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):
            await interaction.response.send_message(
                "❌ You aren't in a voice channel.",
                ephemeral=True
            )
            return

        if not is_channel_owner(
            interaction,
            channel
        ):
            await interaction.response.send_message(
                "❌ You aren't the owner of this channel.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            RenameVoiceModal(channel)
        )

    @discord.ui.button(
        label="Lock",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        custom_id="tempvoice:lock"
    )
    async def lock_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        channel = interaction.user.voice.channel \
            if interaction.user.voice else None

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):
            await interaction.response.send_message(
                "❌ You aren't in a voice channel.",
                ephemeral=True
            )
            return

        if not is_channel_owner(
            interaction,
            channel
        ):
            await interaction.response.send_message(
                "❌ You aren't the owner of this channel.",
                ephemeral=True
            )
            return

        await channel.set_permissions(
            interaction.guild.default_role,
            connect=False,
            reason="TempVoice owner locked channel"
        )

        await interaction.response.send_message(
            "🔒 Your voice channel is now locked.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Unlock",
        emoji="🔓",
        style=discord.ButtonStyle.secondary,
        custom_id="tempvoice:unlock"
    )
    async def unlock_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        channel = interaction.user.voice.channel \
            if interaction.user.voice else None

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):
            await interaction.response.send_message(
                "❌ You aren't in a voice channel.",
                ephemeral=True
            )
            return

        if not is_channel_owner(
            interaction,
            channel
        ):
            await interaction.response.send_message(
                "❌ You aren't the owner of this channel.",
                ephemeral=True
            )
            return

        await channel.set_permissions(
            interaction.guild.default_role,
            connect=True,
            reason="TempVoice owner unlocked channel"
        )

        await interaction.response.send_message(
            "🔓 Your voice channel is now unlocked.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="tempvoice:delete"
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        channel = interaction.user.voice.channel \
            if interaction.user.voice else None

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):
            await interaction.response.send_message(
                "❌ You aren't in a voice channel.",
                ephemeral=True
            )
            return

        if not is_channel_owner(
            interaction,
            channel
        ):
            await interaction.response.send_message(
                "❌ You aren't the owner of this channel.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🗑️ Deleting your temporary channel...",
            ephemeral=True
        )

        await delete_temporary_channel(
            channel
        )


# =========================================================
# RENAME MODAL
# =========================================================

class RenameVoiceModal(discord.ui.Modal):

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
                reason="TempVoice owner renamed channel"
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
        "create_channel_id": create_channel.id,
        "category_id": category.id,
        "default_name": "{user}'s Room",
        "default_limit": 0
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
    description="Show the TempVoice control panel."
)
async def tempvoice_panel(
    interaction: discord.Interaction
):
    embed = discord.Embed(
        title="🎙️ Temporary Voice Controls",
        description=(
            "Join your temporary voice channel and use "
            "the buttons below to manage it.\n\n"
            "Only the owner of a temporary channel can "
            "use these controls."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="✏️ Rename",
        value="Change your channel's name.",
        inline=True
    )

    embed.add_field(
        name="🔒 Lock",
        value="Prevent other members from joining.",
        inline=True
    )

    embed.add_field(
        name="🔓 Unlock",
        value="Allow members to join again.",
        inline=True
    )

    embed.add_field(
        name="🗑️ Delete",
        value="Delete your temporary channel.",
        inline=True
    )

    embed.set_footer(
        text="TempVoice • Self-hosted"
    )

    await interaction.response.send_message(
        embed=embed,
        view=TempVoiceView()
    )


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
            f"Command error: {repr(error)}"
        )

        message = (
            "❌ Something went wrong while "
            "executing that command."
        )

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


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    load_data()

    if not hasattr(
        bot,
        "tempvoice_view_added"
    ):
        bot.add_view(
            TempVoiceView()
        )

        bot.tempvoice_view_added = True

    try:
        synced = await bot.tree.sync()

        print(
            f"✅ TempVoice is online as {bot.user}"
        )

        print(
            f"🔧 Synced {len(synced)} slash commands"
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
