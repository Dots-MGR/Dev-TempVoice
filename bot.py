import os
import json
import asyncio
import base64
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---------------------------------------------------------
# GitHub persistence
# ---------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

GITHUB_VOICE_DATA_PATH = os.getenv(
    "GITHUB_VOICE_DATA_PATH",
    "voice_data.json"
)

GITHUB_BRANCH = os.getenv(
    "GITHUB_BRANCH",
    "main"
)

# ---------------------------------------------------------
# Local fallback/cache
# ---------------------------------------------------------

DATA_DIR = "/app/data"

DATA_FILE = os.path.join(
    DATA_DIR,
    "voice_data.json"
)

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

temporary_channels = {}

claim_tasks = {}

data_loaded = False

github_file_sha = None

github_save_lock = asyncio.Lock()


# =========================================================
# GITHUB
# =========================================================

def github_enabled():

    return bool(
        GITHUB_TOKEN
        and GITHUB_REPO
        and GITHUB_VOICE_DATA_PATH
    )


def github_api_url():

    encoded_path = urllib.parse.quote(
        GITHUB_VOICE_DATA_PATH,
        safe="/"
    )

    return (
        "https://api.github.com/repos/"
        f"{GITHUB_REPO}/contents/{encoded_path}"
    )


def github_request(
    method: str,
    url: str,
    body=None
):

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TempVoice-Bot"
    }

    data = None

    if body is not None:

        data = json.dumps(
            body
        ).encode("utf-8")

        headers["Content-Type"] = (
            "application/json"
        )

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read()

            if not raw:
                return {}

            return json.loads(
                raw.decode("utf-8")
            )

    except urllib.error.HTTPError as e:

        error_body = ""

        try:

            error_body = e.read().decode(
                "utf-8",
                errors="replace"
            )

        except Exception:
            pass

        raise RuntimeError(
            f"GitHub API HTTP {e.code}: {error_body}"
        )

    except urllib.error.URLError as e:

        raise RuntimeError(
            f"GitHub API connection failed: {e}"
        )


def github_load_voice_data():

    global github_file_sha

    if not github_enabled():

        print(
            "⚠️ GitHub persistence is not configured."
        )

        return None

    try:

        response = github_request(
            "GET",
            github_api_url()
        )

        content = response.get(
            "content"
        )

        if not content:

            print(
                "⚠️ GitHub voice_data.json has no content."
            )

            return None

        github_file_sha = response.get(
            "sha"
        )

        decoded = base64.b64decode(
            content.replace("\n", "")
        ).decode("utf-8")

        data = json.loads(
            decoded
        )

        print(
            "☁️ voice_data.json loaded from GitHub."
        )

        return data

    except Exception as e:

        print(
            f"❌ Failed to load voice_data.json "
            f"from GitHub: {e}"
        )

        return None


def github_save_voice_data_sync(
    data: dict
):

    global github_file_sha

    if not github_enabled():
        return False

    try:

        # Always refresh SHA before writing.

        try:

            current = github_request(
                "GET",
                github_api_url()
            )

            if current.get("sha"):

                github_file_sha = current["sha"]

        except Exception as e:

            print(
                f"⚠️ Could not refresh GitHub SHA: {e}"
            )

        json_text = json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        )

        encoded = base64.b64encode(
            json_text.encode("utf-8")
        ).decode("ascii")

        body = {
            "message":
                "Update TempVoice voice_data.json",

            "content":
                encoded,

            "branch":
                GITHUB_BRANCH
        }

        if github_file_sha:

            body["sha"] = github_file_sha

        response = github_request(
            "PUT",
            github_api_url(),
            body
        )

        new_sha = response.get(
            "content",
            {}
        ).get(
            "sha"
        )

        if new_sha:

            github_file_sha = new_sha

        print(
            "☁️ voice_data.json saved to GitHub."
        )

        return True

    except Exception as e:

        print(
            f"❌ Failed to save voice_data.json: {e}"
        )

        return False


# =========================================================
# LOCAL STORAGE
# =========================================================

def ensure_data_storage():

    try:

        os.makedirs(
            DATA_DIR,
            exist_ok=True
        )

        if not os.path.exists(
            DATA_FILE
        ):

            with open(
                DATA_FILE,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    {
                        "servers": {},
                        "temporary_channels": {}
                    },
                    f,
                    indent=2
                )

    except Exception as e:

        print(
            f"❌ Failed to initialize local storage: {e}"
        )


def save_local_cache(
    data: dict
):

    try:

        ensure_data_storage()

        with open(
            DATA_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                indent=2,
                ensure_ascii=False
            )

    except Exception as e:

        print(
            f"⚠️ Failed to save local cache: {e}"
        )


def build_voice_data():

    return {
        "servers": server_configs,

        "temporary_channels": {
            str(channel_id): data
            for channel_id, data
            in temporary_channels.items()
        }
    }


# =========================================================
# DATA NORMALIZATION
# =========================================================

def normalize_channel_data(data):

    data.setdefault(
        "panel_message_id",
        None
    )

    data.setdefault(
        "locked",
        False
    )

    data.setdefault(
        "hidden",
        False
    )

    data.setdefault(
        "allowed_users",
        []
    )

    data.setdefault(
        "denied_users",
        []
    )

    data.setdefault(
        "moderators",
        []
    )

    # -----------------------------------------------------
    # Chat
    # -----------------------------------------------------

    data.setdefault(
        "chat",
        {}
    )

    chat = data["chat"]

    chat.setdefault(
        "mode",
        "everyone"
    )

    chat.setdefault(
        "outside_read",
        True
    )

    chat.setdefault(
        "outside_write",
        True
    )

    chat.setdefault(
        "locked",
        False
    )

    # -----------------------------------------------------
    # Waiting room
    # -----------------------------------------------------

    data.setdefault(
        "waiting_room",
        {}
    )

    waiting = data["waiting_room"]

    waiting.setdefault(
        "enabled",
        False
    )

    waiting.setdefault(
        "channel_id",
        None
    )

    waiting.setdefault(
        "pending_users",
        []
    )

    # -----------------------------------------------------
    # Region
    # -----------------------------------------------------

    data.setdefault(
        "region",
        None
    )

    # -----------------------------------------------------
    # Per-member voice controls
    # -----------------------------------------------------

    data.setdefault(
        "member_controls",
        {}
    )

    return data


def normalize_server_config(config):

    config.setdefault(
        "default_name",
        "{user}'s Room"
    )

    config.setdefault(
        "default_limit",
        0
    )

    return config


# =========================================================
# LOAD
# =========================================================

def load_data():

    global server_configs
    global temporary_channels

    data = None

    if github_enabled():

        data = github_load_voice_data()

    if data is None:

        ensure_data_storage()

        try:

            with open(
                DATA_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            print(
                "💾 voice_data.json loaded "
                "from local cache."
            )

        except Exception:

            data = {
                "servers": {},
                "temporary_channels": {}
            }

    server_configs = data.get(
        "servers",
        {}
    )

    for guild_id, config in server_configs.items():

        normalize_server_config(
            config
        )

    saved_channels = data.get(
        "temporary_channels",
        {}
    )

    temporary_channels = {}

    for channel_id, channel_data in saved_channels.items():

        try:

            channel_id = int(
                channel_id
            )

        except ValueError:

            continue

        normalize_channel_data(
            channel_data
        )

        temporary_channels[
            channel_id
        ] = channel_data

    save_local_cache(
        build_voice_data()
    )

    print(
        "✅ Voice configuration loaded."
    )

    print(
        f"🏠 Configured servers: "
        f"{len(server_configs)}"
    )

    print(
        f"🎙️ Saved temporary rooms: "
        f"{len(temporary_channels)}"
    )


# =========================================================
# SAVE
# =========================================================

def save_data():

    data = build_voice_data()

    save_local_cache(
        data
    )

    if not github_enabled():
        return

    async def github_save():

        async with github_save_lock:

            try:

                await asyncio.to_thread(
                    github_save_voice_data_sync,
                    data
                )

            except Exception as e:

                print(
                    f"❌ GitHub save failed: {e}"
                )

    try:

        asyncio.get_running_loop().create_task(
            github_save()
        )

    except RuntimeError:

        pass


# =========================================================
# HELPERS
# =========================================================

def get_config(
    guild_id: int
):

    config = server_configs.get(
        str(guild_id)
    )

    if config:

        normalize_server_config(
            config
        )

    return config


def get_temporary_channel(
    channel_id: int
):

    return temporary_channels.get(
        channel_id
    )


def get_member_channel(
    interaction: discord.Interaction
) -> Optional[discord.VoiceChannel]:

    if not interaction.user.voice:
        return None

    channel = interaction.user.voice.channel

    if isinstance(
        channel,
        discord.VoiceChannel
    ):

        return channel

    return None


def is_channel_owner(
    interaction,
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    return bool(
        data
        and data.get("owner_id")
        == interaction.user.id
    )


def is_channel_manager(
    interaction,
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return False

    if data.get("owner_id") == interaction.user.id:
        return True

    return (
        interaction.user.id
        in data.get(
            "moderators",
            []
        )
    )


def format_voice_name(
    template,
    member
):

    name = template

    name = name.replace(
        "{user}",
        member.display_name
    )

    name = name.replace(
        "{username}",
        member.name
    )

    name = name.replace(
        "{server}",
        member.guild.name
    )

    return name[
        :MAX_CHANNEL_NAME_LENGTH
    ]


def cancel_claim_task(
    channel_id
):

    task = claim_tasks.pop(
        channel_id,
        None
    )

    if task and not task.done():

        task.cancel()


def get_voice_control(
    data,
    member_id
):

    controls = data.setdefault(
        "member_controls",
        {}
    )

    controls.setdefault(
        str(member_id),
        {}
    )

    control = controls[
        str(member_id)
    ]

    control.setdefault(
        "speak",
        True
    )

    control.setdefault(
        "stream",
        True
    )

    control.setdefault(
        "soundboard",
        True
    )

    return control


# =========================================================
# CHAT PERMISSIONS
# =========================================================

async def apply_chat_permissions(
    channel: discord.VoiceChannel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    chat = data["chat"]

    mode = chat.get(
        "mode",
        "everyone"
    )

    locked = chat.get(
        "locked",
        False
    )

    outside_read = chat.get(
        "outside_read",
        True
    )

    outside_write = chat.get(
        "outside_write",
        True
    )

    guild = channel.guild

    # -----------------------------------------------------
    # Base permissions
    # -----------------------------------------------------

    if not outside_read:

        await channel.set_permissions(
            guild.default_role,
            view_channel=False,
            reason="TempVoice chat visibility"
        )

    else:

        await channel.set_permissions(
            guild.default_role,
            view_channel=True,
            reason="TempVoice chat visibility"
        )

    # -----------------------------------------------------
    # Determine default writing permission
    # -----------------------------------------------------

    if locked:

        default_send = False

    elif mode == "everyone":

        default_send = outside_write

    else:

        default_send = False

    await channel.set_permissions(
        guild.default_role,
        send_messages=default_send,
        read_message_history=True,
        reason="TempVoice chat mode"
    )

    owner = guild.get_member(
        data["owner_id"]
    )

    if owner:

        await channel.set_permissions(
            owner,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            reason="TempVoice chat owner"
        )

    # -----------------------------------------------------
    # Moderators
    # -----------------------------------------------------

    for user_id in data.get(
        "moderators",
        []
    ):

        member = guild.get_member(
            user_id
        )

        if member:

            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=(
                    not locked
                    and mode
                    in {
                        "everyone",
                        "voice_members",
                        "owner_mods"
                    }
                ),
                read_message_history=True,
                reason="TempVoice chat moderator"
            )

    # -----------------------------------------------------
    # Voice-members-only mode
    # -----------------------------------------------------

    if (
        mode == "voice_members"
        and not locked
    ):

        for member in channel.members:

            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                reason="TempVoice voice-member chat"
            )

    # -----------------------------------------------------
    # Owner-only mode
    # -----------------------------------------------------

    if mode == "owner_only":

        if owner:

            await channel.set_permissions(
                owner,
                view_channel=True,
                send_messages=not locked,
                read_message_history=True,
                reason="TempVoice owner-only chat"
            )

    # -----------------------------------------------------
    # Outside write restriction
    # -----------------------------------------------------

    if not outside_write:

        # Current voice members can still write
        # in voice_members mode.

        if mode == "voice_members":

            for member in channel.members:

                await channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=not locked,
                    read_message_history=True,
                    reason="TempVoice outside write restriction"
                )


# =========================================================
# VOICE PERMISSIONS
# =========================================================

async def apply_channel_permissions(
    channel: discord.VoiceChannel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    guild = channel.guild

    locked = data.get(
        "locked",
        False
    )

    hidden = data.get(
        "hidden",
        False
    )

    await channel.set_permissions(
        guild.default_role,
        connect=not locked,
        view_channel=not hidden,
        reason="TempVoice default permissions"
    )

    owner = guild.get_member(
        data["owner_id"]
    )

    if owner:

        control = get_voice_control(
            data,
            owner.id
        )

        await channel.set_permissions(
            owner,
            connect=True,
            view_channel=True,
            speak=control["speak"],
            stream=control["stream"],
            use_soundboard=control["soundboard"],
            reason="TempVoice owner permissions"
        )

    for user_id in data.get(
        "moderators",
        []
    ):

        member = guild.get_member(
            user_id
        )

        if not member:
            continue

        control = get_voice_control(
            data,
            member.id
        )

        await channel.set_permissions(
            member,
            connect=True,
            view_channel=True,
            speak=control["speak"],
            stream=control["stream"],
            use_soundboard=control["soundboard"],
            reason="TempVoice moderator permissions"
        )

    for user_id in data.get(
        "allowed_users",
        []
    ):

        member = guild.get_member(
            user_id
        )

        if member:

            await channel.set_permissions(
                member,
                connect=True,
                view_channel=True,
                reason="TempVoice allowed user"
            )

    for user_id in data.get(
        "denied_users",
        []
    ):

        member = guild.get_member(
            user_id
        )

        if member:

            await channel.set_permissions(
                member,
                connect=False,
                view_channel=False,
                reason="TempVoice denied user"
            )

    # -----------------------------------------------------
    # Apply individual voice controls
    # -----------------------------------------------------

    for user_id, controls in data.get(
        "member_controls",
        {}
    ).items():

        member = guild.get_member(
            int(user_id)
        )

        if not member:
            continue

        await channel.set_permissions(
            member,
            speak=controls.get(
                "speak",
                True
            ),
            stream=controls.get(
                "stream",
                True
            ),
            use_soundboard=controls.get(
                "soundboard",
                True
            ),
            reason="TempVoice member voice controls"
        )

    await apply_chat_permissions(
        channel
    )


# =========================================================
# PANEL EMBED
# =========================================================

def build_panel_embed(
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:

        return discord.Embed(
            title="🎙️ Temporary Voice Controls"
        )

    owner = channel.guild.get_member(
        data["owner_id"]
    )

    owner_text = (
        owner.mention
        if owner
        else "Unknown"
    )

    locked = data.get(
        "locked",
        False
    )

    hidden = data.get(
        "hidden",
        False
    )

    chat = data.get(
        "chat",
        {}
    )

    waiting = data.get(
        "waiting_room",
        {}
    )

    mode_names = {
        "everyone":
            "Everyone",

        "voice_members":
            "Voice members",

        "owner_mods":
            "Owner + moderators",

        "owner_only":
            "Owner only"
    }

    chat_mode = mode_names.get(
        chat.get("mode"),
        "Unknown"
    )

    chat_status = (
        "🔒 Locked"
        if chat.get("locked")
        else f"🔓 {chat_mode}"
    )

    region = data.get(
        "region"
    ) or "Automatic"

    waiting_status = (
        "🟢 Enabled"
        if waiting.get("enabled")
        else "🔴 Disabled"
    )

    waiting_count = len(
        waiting.get(
            "pending_users",
            []
        )
    )

    embed = discord.Embed(
        title="🎙️ Temporary Voice Controls",
        description=(
            f"Manage **{channel.name}**.\n\n"
            f"👑 **Owner:** {owner_text}\n"
            f"👥 **Members:** "
            f"{len(channel.members)} / "
            f"{channel.user_limit or '∞'}\n\n"
            f"🔊 **Voice:** "
            f"{'🔒 Locked' if locked else '🔓 Unlocked'}\n"
            f"👁️ **Visibility:** "
            f"{'🙈 Hidden' if hidden else '👀 Visible'}\n"
            f"💬 **Chat:** {chat_status}\n"
            f"🚪 **Waiting Room:** "
            f"{waiting_status}\n"
            f"🌍 **Region:** {region}"
        ),
        color=(
            discord.Color.red()
            if locked or hidden or chat.get("locked")
            else discord.Color.blurple()
        )
    )

    embed.add_field(
        name="🏠 Room",
        value=(
            "✏️ Rename\n"
            "👥 User limit\n"
            f"{'🔓 Unlock' if locked else '🔒 Lock'}\n"
            f"{'👀 Show' if hidden else '👁️ Hide'}\n"
            "🌍 Region\n"
            "🔗 Invite"
        ),
        inline=True
    )

    embed.add_field(
        name="💬 Chat",
        value=(
            f"Mode: **{chat_mode}**\n"
            f"Status: **"
            f"{'Locked' if chat.get('locked') else 'Unlocked'}**\n"
            f"Outside read: "
            f"{'ON' if chat.get('outside_read') else 'OFF'}\n"
            f"Outside write: "
            f"{'ON' if chat.get('outside_write') else 'OFF'}"
        ),
        inline=True
    )

    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "➕ Allow\n"
            "➖ Deny\n"
            "👢 Kick\n"
            "🛡️ Moderator\n"
            "🎙️ Voice controls"
        ),
        inline=True
    )

    embed.add_field(
        name="🚪 Waiting Room",
        value=(
            f"Status: {waiting_status}\n"
            f"Waiting: **{waiting_count}**\n"
            "Manage pending users"
        ),
        inline=True
    )

    embed.add_field(
        name="👑 Ownership",
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
# CHAT SETTINGS VIEW
# =========================================================

class ChatSettingsView(
    discord.ui.View
):

    def __init__(
        self,
        channel
    ):

        super().__init__(
            timeout=120
        )

        self.channel = channel

        data = get_temporary_channel(
            channel.id
        )

        chat = data["chat"]

        self.mode_select = discord.ui.Select(
            placeholder="Choose chat mode...",
            options=[
                discord.SelectOption(
                    label="Everyone",
                    value="everyone",
                    description="Everyone who can see the chat can type."
                ),

                discord.SelectOption(
                    label="Voice members only",
                    value="voice_members",
                    description="Only people currently in voice can type."
                ),

                discord.SelectOption(
                    label="Owner + moderators",
                    value="owner_mods",
                    description="Only the owner and VC moderators can type."
                ),

                discord.SelectOption(
                    label="Owner only",
                    value="owner_only",
                    description="Only the room owner can type."
                )
            ]
        )

        self.mode_select.default_values = []

        self.mode_select.callback = (
            self.mode_callback
        )

        self.add_item(
            self.mode_select
        )

        self.lock_button = discord.ui.Button(
            label=(
                "Unlock Chat"
                if chat.get("locked")
                else "Lock Chat"
            ),
            emoji=(
                "🔓"
                if chat.get("locked")
                else "🔒"
            ),
            style=(
                discord.ButtonStyle.success
                if chat.get("locked")
                else discord.ButtonStyle.danger
            )
        )

        self.lock_button.callback = (
            self.lock_callback
        )

        self.add_item(
            self.lock_button
        )

        self.read_button = discord.ui.Button(
            label=(
                "Disable Outside Read"
                if chat.get("outside_read")
                else "Enable Outside Read"
            ),
            emoji="👁️",
            style=discord.ButtonStyle.secondary
        )

        self.read_button.callback = (
            self.read_callback
        )

        self.add_item(
            self.read_button
        )

        self.write_button = discord.ui.Button(
            label=(
                "Disable Outside Write"
                if chat.get("outside_write")
                else "Enable Outside Write"
            ),
            emoji="✏️",
            style=discord.ButtonStyle.secondary
        )

        self.write_button.callback = (
            self.write_callback
        )

        self.add_item(
            self.write_button
        )

    async def mode_callback(
        self,
        interaction
    ):

        data = get_temporary_channel(
            self.channel.id
        )

        data["chat"]["mode"] = (
            self.mode_select.values[0]
        )

        await apply_chat_permissions(
            self.channel
        )

        save_data()

        await update_control_panel(
            self.channel
        )

        await interaction.response.edit_message(
            content="💬 Chat mode updated.",
            view=ChatSettingsView(
                self.channel
            )
        )

    async def lock_callback(
        self,
        interaction
    ):

        data = get_temporary_channel(
            self.channel.id
        )

        data["chat"]["locked"] = not data[
            "chat"
        ].get(
            "locked",
            False
        )

        await apply_chat_permissions(
            self.channel
        )

        save_data()

        await update_control_panel(
            self.channel
        )

        await interaction.response.edit_message(
            content="💬 Chat lock updated.",
            view=ChatSettingsView(
                self.channel
            )
        )

    async def read_callback(
        self,
        interaction
    ):

        data = get_temporary_channel(
            self.channel.id
        )

        data["chat"]["outside_read"] = not data[
            "chat"
        ].get(
            "outside_read",
            True
        )

        await apply_chat_permissions(
            self.channel
        )

        save_data()

        await update_control_panel(
            self.channel
        )

        await interaction.response.edit_message(
            content="👁️ Outside chat visibility updated.",
            view=ChatSettingsView(
                self.channel
            )
        )

    async def write_callback(
        self,
        interaction
    ):

        data = get_temporary_channel(
            self.channel.id
        )

        data["chat"]["outside_write"] = not data[
            "chat"
        ].get(
            "outside_write",
            True
        )

        await apply_chat_permissions(
            self.channel
        )

        save_data()

        await update_control_panel(
            self.channel
        )

        await interaction.response.edit_message(
            content="✏️ Outside chat writing updated.",
            view=ChatSettingsView(
                self.channel
            )
        )


# =========================================================
# VOICE MODERATION VIEW
# =========================================================

class VoiceModerationView(
    discord.ui.View
):

    def __init__(
        self,
        channel
    ):

        super().__init__(
            timeout=120
        )

        self.channel = channel

        self.member_select = discord.ui.UserSelect(
            placeholder="Select a member...",
            min_values=1,
            max_values=1
        )

        self.member_select.callback = (
            self.member_callback
        )

        self.add_item(
            self.member_select
        )

        self.action_select = discord.ui.Select(
            placeholder="Choose a voice action...",
            options=[
                discord.SelectOption(
                    label="Server mute",
                    value="mute",
                    emoji="🔇"
                ),

                discord.SelectOption(
                    label="Server unmute",
                    value="unmute",
                    emoji="🔊"
                ),

                discord.SelectOption(
                    label="Server deafen",
                    value="deafen",
                    emoji="🙉"
                ),

                discord.SelectOption(
                    label="Server undeafen",
                    value="undeafen",
                    emoji="👂"
                ),

                discord.SelectOption(
                    label="Allow speaking",
                    value="speak_on",
                    emoji="🗣️"
                ),

                discord.SelectOption(
                    label="Disable speaking",
                    value="speak_off",
                    emoji="🤐"
                ),

                discord.SelectOption(
                    label="Allow video / screen share",
                    value="stream_on",
                    emoji="📹"
                ),

                discord.SelectOption(
                    label="Block video / screen share",
                    value="stream_off",
                    emoji="🚫"
                ),

                discord.SelectOption(
                    label="Allow soundboard",
                    value="soundboard_on",
                    emoji="🔊"
                ),

                discord.SelectOption(
                    label="Block soundboard",
                    value="soundboard_off",
                    emoji="🔇"
                ),

                discord.SelectOption(
                    label="Disconnect",
                    value="disconnect",
                    emoji="👢"
                )
            ]
        )

        self.action_select.callback = (
            self.action_callback
        )

        self.add_item(
            self.action_select
        )

        self.selected_member = None

    async def member_callback(
        self,
        interaction
    ):

        self.selected_member = (
            self.member_select.values[0]
        )

        await interaction.response.send_message(
            f"👤 Selected **{self.selected_member.display_name}**.",
            ephemeral=True
        )

    async def action_callback(
        self,
        interaction
    ):

        if self.selected_member is None:

            await interaction.response.send_message(
                "❌ Select a member first.",
                ephemeral=True
            )

            return

        member = self.selected_member

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:

            await interaction.response.send_message(
                "❌ This room no longer exists.",
                ephemeral=True
            )

            return

        if member.id == data["owner_id"]:

            await interaction.response.send_message(
                "❌ You cannot moderate the room owner.",
                ephemeral=True
            )

            return

        action = self.action_select.values[0]

        try:

            if action == "mute":

                await member.edit(
                    mute=True,
                    reason="TempVoice moderator mute"
                )

                message = (
                    f"🔇 Muted {member.mention}."
                )

            elif action == "unmute":

                await member.edit(
                    mute=False,
                    reason="TempVoice moderator unmute"
                )

                message = (
                    f"🔊 Unmuted {member.mention}."
                )

            elif action == "deafen":

                await member.edit(
                    deaf=True,
                    reason="TempVoice moderator deafen"
                )

                message = (
                    f"🙉 Deafened {member.mention}."
                )

            elif action == "undeafen":

                await member.edit(
                    deaf=False,
                    reason="TempVoice moderator undeafen"
                )

                message = (
                    f"👂 Undeafened {member.mention}."
                )

            else:

                controls = get_voice_control(
                    data,
                    member.id
                )

                if action == "speak_on":

                    controls["speak"] = True
                    message = (
                        f"🗣️ {member.mention} can speak again."
                    )

                elif action == "speak_off":

                    controls["speak"] = False
                    message = (
                        f"🤐 {member.mention} can no longer speak."
                    )

                elif action == "stream_on":

                    controls["stream"] = True
                    message = (
                        f"📹 Video/screen sharing enabled for "
                        f"{member.mention}."
                    )

                elif action == "stream_off":

                    controls["stream"] = False
                    message = (
                        f"🚫 Video/screen sharing disabled for "
                        f"{member.mention}."
                    )

                elif action == "soundboard_on":

                    controls["soundboard"] = True
                    message = (
                        f"🔊 Soundboard enabled for "
                        f"{member.mention}."
                    )

                elif action == "soundboard_off":

                    controls["soundboard"] = False
                    message = (
                        f"🔇 Soundboard disabled for "
                        f"{member.mention}."
                    )

                elif action == "disconnect":

                    if (
                        not member.voice
                        or member.voice.channel
                        != self.channel
                    ):

                        await interaction.response.send_message(
                            "❌ That member isn't in this room.",
                            ephemeral=True
                        )

                        return

                    await member.move_to(
                        None,
                        reason="TempVoice moderator disconnect"
                    )

                    message = (
                        f"👢 Disconnected {member.mention}."
                    )

                else:

                    message = "❌ Unknown action."

                await apply_channel_permissions(
                    self.channel
                )

            save_data()

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ Discord denied that action. "
                "Check the bot's server permissions and role position.",
                ephemeral=True
            )

        except discord.HTTPException as e:

            await interaction.response.send_message(
                f"❌ Discord rejected the action: {e}",
                ephemeral=True
            )


# =========================================================
# WAITING ROOM VIEW
# =========================================================

class WaitingRoomView(
    discord.ui.View
):

    def __init__(
        self,
        channel
    ):

        super().__init__(
            timeout=120
        )

        self.channel = channel
        self.selected_member = None

        self.select = discord.ui.UserSelect(
            placeholder="Select someone waiting...",
            min_values=1,
            max_values=1
        )

        self.select.callback = (
            self.select_callback
        )

        self.add_item(
            self.select
        )

        approve = discord.ui.Button(
            label="Approve",
            emoji="✅",
            style=discord.ButtonStyle.success
        )

        approve.callback = (
            self.approve_callback
        )

        self.add_item(
            approve
        )

        reject = discord.ui.Button(
            label="Reject",
            emoji="❌",
            style=discord.ButtonStyle.danger
        )

        reject.callback = (
            self.reject_callback
        )

        self.add_item(
            reject
        )

    async def select_callback(
        self,
        interaction
    ):

        self.selected_member = (
            self.select.values[0]
        )

        waiting_id = (
            get_temporary_channel(
                self.channel.id
            )["waiting_room"]["channel_id"]
        )

        waiting_channel = (
            interaction.guild.get_channel(
                waiting_id
            )
        )

        if (
            not waiting_channel
            or self.selected_member.voice is None
            or self.selected_member.voice.channel
            != waiting_channel
        ):

            await interaction.response.send_message(
                "❌ That member isn't currently waiting.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            f"👤 Selected **{self.selected_member.display_name}**.",
            ephemeral=True
        )

    async def approve_callback(
        self,
        interaction
    ):

        if not self.selected_member:

            await interaction.response.send_message(
                "❌ Select someone first.",
                ephemeral=True
            )

            return

        member = self.selected_member

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:

            return

        waiting_id = (
            data["waiting_room"]["channel_id"]
        )

        waiting_channel = (
            interaction.guild.get_channel(
                waiting_id
            )
        )

        if (
            not waiting_channel
            or not member.voice
            or member.voice.channel
            != waiting_channel
        ):

            await interaction.response.send_message(
                "❌ That member is no longer waiting.",
                ephemeral=True
            )

            return

        try:

            await member.move_to(
                self.channel,
                reason="TempVoice waiting room approval"
            )

            if member.id in data[
                "waiting_room"
            ]["pending_users"]:

                data[
                    "waiting_room"
                ]["pending_users"].remove(
                    member.id
                )

            save_data()

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                f"✅ {member.mention} was approved.",
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I cannot move that member.",
                ephemeral=True
            )

    async def reject_callback(
        self,
        interaction
    ):

        if not self.selected_member:

            await interaction.response.send_message(
                "❌ Select someone first.",
                ephemeral=True
            )

            return

        member = self.selected_member

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:
            return

        waiting_id = (
            data["waiting_room"]["channel_id"]
        )

        waiting_channel = (
            interaction.guild.get_channel(
                waiting_id
            )
        )

        if (
            not waiting_channel
            or not member.voice
            or member.voice.channel
            != waiting_channel
        ):

            await interaction.response.send_message(
                "❌ That member is no longer waiting.",
                ephemeral=True
            )

            return

        try:

            await member.move_to(
                None,
                reason="TempVoice waiting room rejection"
            )

            if member.id in data[
                "waiting_room"
            ]["pending_users"]:

                data[
                    "waiting_room"
                ]["pending_users"].remove(
                    member.id
                )

            save_data()

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                f"❌ {member.mention} was rejected.",
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I cannot disconnect that member.",
                ephemeral=True
            )


# =========================================================
# CREATE / DELETE WAITING ROOM
# =========================================================

async def create_waiting_room(
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return None

    existing_id = (
        data["waiting_room"]
        .get("channel_id")
    )

    if existing_id:

        existing = channel.guild.get_channel(
            existing_id
        )

        if existing:

            return existing

    guild = channel.guild

    overwrites = {
        guild.default_role:
            discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=False
            ),

        guild.me:
            discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                move_members=True,
                manage_channels=True
            )
    }

    waiting = await guild.create_voice_channel(
        name=f"⏳ {channel.name} Waiting Room"[
            :100
        ],
        category=channel.category,
        overwrites=overwrites,
        reason="TempVoice waiting room"
    )

    data[
        "waiting_room"
    ]["channel_id"] = waiting.id

    return waiting


async def delete_waiting_room(
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    waiting_id = (
        data["waiting_room"]
        .get("channel_id")
    )

    if waiting_id:

        waiting = channel.guild.get_channel(
            waiting_id
        )

        if waiting:

            try:

                await waiting.delete(
                    reason="TempVoice waiting room disabled"
                )

            except discord.HTTPException:
                pass

    data[
        "waiting_room"
    ]["channel_id"] = None

    data[
        "waiting_room"
    ]["pending_users"] = []


# =========================================================
# REGION VIEW
# =========================================================

class RegionView(
    discord.ui.View
):

    def __init__(
        self,
        channel
    ):

        super().__init__(
            timeout=120
        )

        self.channel = channel

        options = [
            ("Automatic", "auto"),
            ("Europe", "europe"),
            ("US East", "us-east"),
            ("US Central", "us-central"),
            ("US South", "us-south"),
            ("US West", "us-west"),
            ("Brazil", "brazil"),
            ("Singapore", "singapore"),
            ("Japan", "japan"),
            ("India", "india"),
            ("South Africa", "southafrica"),
            ("Dubai", "dubai")
        ]

        select = discord.ui.Select(
            placeholder="Choose a voice region...",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value
                )
                for label, value in options
            ]
        )

        select.callback = (
            self.region_callback
        )

        self.add_item(
            select
        )

    async def region_callback(
        self,
        interaction
    ):

        value = interaction.data[
            "values"
        ][0]

        try:

            await self.channel.edit(
                rtc_region=(
                    None
                    if value == "auto"
                    else value
                ),
                reason="TempVoice region change"
            )

            data = get_temporary_channel(
                self.channel.id
            )

            data["region"] = (
                None
                if value == "auto"
                else value
            )

            save_data()

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                f"🌍 Region set to **{value}**.",
                ephemeral=True
            )

        except discord.HTTPException as e:

            await interaction.response.send_message(
                f"❌ Failed to change region: {e}",
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
        channel,
        action
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
        interaction
    ):

        member = self.member_select.values[0]

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

        if self.action == "allow":

            if member.id not in data[
                "allowed_users"
            ]:

                data[
                    "allowed_users"
                ].append(
                    member.id
                )

            if member.id in data[
                "denied_users"
            ]:

                data[
                    "denied_users"
                ].remove(
                    member.id
                )

            message = (
                f"➕ Allowed {member.mention}."
            )

        elif self.action == "deny":

            if member.id not in data[
                "denied_users"
            ]:

                data[
                    "denied_users"
                ].append(
                    member.id
                )

            if member.id in data[
                "allowed_users"
            ]:

                data[
                    "allowed_users"
                ].remove(
                    member.id
                )

            if (
                member.voice
                and member.voice.channel
                == self.channel
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

        elif self.action == "kick":

            if (
                member.voice
                and member.voice.channel
                == self.channel
            ):

                try:

                    await member.move_to(
                        None,
                        reason="TempVoice owner kick"
                    )

                    message = (
                        f"👢 Kicked {member.mention}."
                    )

                except discord.Forbidden:

                    message = (
                        "❌ I cannot move that member."
                    )

            else:

                message = (
                    "❌ That member isn't in this room."
                )

        elif self.action == "moderator":

            moderators = data[
                "moderators"
            ]

            if member.id in moderators:

                moderators.remove(
                    member.id
                )

                message = (
                    f"🛡️ Removed {member.mention} "
                    "from moderators."
                )

            else:

                moderators.append(
                    member.id
                )

                message = (
                    f"🛡️ Added {member.mention} "
                    "as a moderator."
                )

        else:

            message = "❌ Unknown action."

        await apply_channel_permissions(
            self.channel
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
# TRANSFER
# =========================================================

class TransferView(
    discord.ui.View
):

    def __init__(
        self,
        channel
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
        interaction
    ):

        member = self.select.values[0]

        data = get_temporary_channel(
            self.channel.id
        )

        if not data:
            return

        if member.id == interaction.user.id:

            await interaction.response.send_message(
                "❌ You already own this room.",
                ephemeral=True
            )

            return

        if (
            not member.voice
            or member.voice.channel
            != self.channel
        ):

            await interaction.response.send_message(
                "❌ The new owner must be inside this room.",
                ephemeral=True
            )

            return

        cancel_claim_task(
            self.channel.id
        )

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
# TEMPVOICE PANEL
# =========================================================

class TempVoiceView(
    discord.ui.View
):

    def __init__(
        self,
        channel=None
    ):

        super().__init__(
            timeout=None
        )

        # -------------------------------------------------
        # All buttons are added to persistent fallback.
        # Dynamic panels only show appropriate buttons.
        # -------------------------------------------------

        self.add_item(
            self.button(
                "Rename",
                "✏️",
                discord.ButtonStyle.primary,
                "tempvoice:rename",
                self.rename_button
            )
        )

        self.add_item(
            self.button(
                "Limit",
                "👥",
                discord.ButtonStyle.secondary,
                "tempvoice:limit",
                self.limit_button
            )
        )

        if channel:

            data = get_temporary_channel(
                channel.id
            )

            locked = data.get(
                "locked",
                False
            )

            hidden = data.get(
                "hidden",
                False
            )

            chat_locked = data[
                "chat"
            ].get(
                "locked",
                False
            )

            waiting_enabled = data[
                "waiting_room"
            ].get(
                "enabled",
                False
            )

            region_button = self.button(
                "Region",
                "🌍",
                discord.ButtonStyle.secondary,
                "tempvoice:region",
                self.region_button
            )

            self.add_item(
                region_button
            )

            self.add_item(
                self.button(
                    "Invite",
                    "🔗",
                    discord.ButtonStyle.success,
                    "tempvoice:invite",
                    self.invite_button
                )
            )

            self.add_item(
                self.button(
                    "Unlock"
                    if locked
                    else "Lock",
                    "🔓"
                    if locked
                    else "🔒",
                    discord.ButtonStyle.success
                    if locked
                    else discord.ButtonStyle.danger,
                    "tempvoice:unlock"
                    if locked
                    else "tempvoice:lock",
                    self.unlock_button
                    if locked
                    else self.lock_button
                )
            )

            self.add_item(
                self.button(
                    "Show"
                    if hidden
                    else "Hide",
                    "👀"
                    if hidden
                    else "👁️",
                    discord.ButtonStyle.success
                    if hidden
                    else discord.ButtonStyle.danger,
                    "tempvoice:show"
                    if hidden
                    else "tempvoice:hide",
                    self.show_button
                    if hidden
                    else self.hide_button
                )
            )

            self.add_item(
                self.button(
                    "Chat",
                    "💬",
                    discord.ButtonStyle.secondary,
                    "tempvoice:chat",
                    self.chat_button
                )
            )

            self.add_item(
                self.button(
                    "Waiting OFF"
                    if waiting_enabled
                    else "Waiting ON",
                    "🚪",
                    discord.ButtonStyle.danger
                    if waiting_enabled
                    else discord.ButtonStyle.success,
                    "tempvoice:waiting",
                    self.waiting_button
                )
            )

            self.add_item(
                self.button(
                    "Allow",
                    "➕",
                    discord.ButtonStyle.success,
                    "tempvoice:allow",
                    self.allow_button
                )
            )

            self.add_item(
                self.button(
                    "Deny",
                    "➖",
                    discord.ButtonStyle.danger,
                    "tempvoice:deny",
                    self.deny_button
                )
            )

            self.add_item(
                self.button(
                    "Kick",
                    "👢",
                    discord.ButtonStyle.danger,
                    "tempvoice:kick",
                    self.kick_button
                )
            )

            self.add_item(
                self.button(
                    "Voice Mod",
                    "🎙️",
                    discord.ButtonStyle.secondary,
                    "tempvoice:voice_mod",
                    self.voice_mod_button
                )
            )

            self.add_item(
                self.button(
                    "Moderator",
                    "🛡️",
                    discord.ButtonStyle.secondary,
                    "tempvoice:moderator",
                    self.moderator_button
                )
            )

            self.add_item(
                self.button(
                    "Transfer",
                    "👑",
                    discord.ButtonStyle.primary,
                    "tempvoice:transfer",
                    self.transfer_button
                )
            )

            if data:

                owner = channel.guild.get_member(
                    data["owner_id"]
                )

                if not (
                    owner
                    and owner.voice
                    and owner.voice.channel
                    == channel
                ):

                    self.add_item(
                        self.button(
                            "Claim",
                            "🙋",
                            discord.ButtonStyle.secondary,
                            "tempvoice:claim",
                            self.claim_button
                        )
                    )

            self.add_item(
                self.button(
                    "Delete",
                    "🗑️",
                    discord.ButtonStyle.danger,
                    "tempvoice:delete",
                    self.delete_button
                )

            )

        else:

            # Persistent fallback view.
            self.add_item(
                self.button(
                    "Lock",
                    "🔒",
                    discord.ButtonStyle.danger,
                    "tempvoice:lock",
                    self.lock_button
                )
            )

            self.add_item(
                self.button(
                    "Unlock",
                    "🔓",
                    discord.ButtonStyle.success,
                    "tempvoice:unlock",
                    self.unlock_button
                )
            )

            self.add_item(
                self.button(
                    "Chat",
                    "💬",
                    discord.ButtonStyle.secondary,
                    "tempvoice:chat",
                    self.chat_button
                )
            )

            self.add_item(
                self.button(
                    "Region",
                    "🌍",
                    discord.ButtonStyle.secondary,
                    "tempvoice:region",
                    self.region_button
                )
            )

            self.add_item(
                self.button(
                    "Invite",
                    "🔗",
                    discord.ButtonStyle.success,
                    "tempvoice:invite",
                    self.invite_button
                )
            )

            self.add_item(
                self.button(
                    "Waiting",
                    "🚪",
                    discord.ButtonStyle.secondary,
                    "tempvoice:waiting",
                    self.waiting_button
                )
            )

            self.add_item(
                self.button(
                    "Voice Mod",
                    "🎙️",
                    discord.ButtonStyle.secondary,
                    "tempvoice:voice_mod",
                    self.voice_mod_button
                )
            )

            self.add_item(
                self.button(
                    "Delete",
                    "🗑️",
                    discord.ButtonStyle.danger,
                    "tempvoice:delete",
                    self.delete_button
                )
            )

    def button(
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

    async def manager(
        self,
        interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if not channel:
            return None

        if not is_channel_manager(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ You aren't the owner or a VC moderator.",
                ephemeral=True
            )

            return None

        return channel

    async def owner(
        self,
        interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if not channel:
            return None

        if not is_channel_owner(
            interaction,
            channel
        ):

            await interaction.response.send_message(
                "❌ Only the room owner can do that.",
                ephemeral=True
            )

            return None

        return channel

    async def rename_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if channel:

            await interaction.response.send_modal(
                RenameVoiceModal(
                    channel
                )
            )

    async def limit_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if channel:

            await interaction.response.send_modal(
                LimitVoiceModal(
                    channel
                )
            )

    async def lock_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        data["locked"] = True

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "🔒 Room locked.",
            ephemeral=True
        )

    async def unlock_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        data["locked"] = False

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "🔓 Room unlocked.",
            ephemeral=True
        )

    async def hide_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        data["hidden"] = True

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "🙈 Room hidden.",
            ephemeral=True
        )

    async def show_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        data["hidden"] = False

        await apply_channel_permissions(
            channel
        )

        save_data()

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "👀 Room visible.",
            ephemeral=True
        )

    async def chat_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "💬 Configure the room's text chat:",
                view=ChatSettingsView(
                    channel
                ),
                ephemeral=True
            )

    async def region_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "🌍 Choose a voice region:",
                view=RegionView(
                    channel
                ),
                ephemeral=True
            )

    async def invite_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        try:

            invite = await channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=True,
                reason="TempVoice room invite"
            )

            await interaction.response.send_message(
                f"🔗 **Room invite:**\n{invite.url}",
                ephemeral=True
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I cannot create an invite for this room.",
                ephemeral=True
            )

    async def waiting_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if not channel:
            return

        data = get_temporary_channel(
            channel.id
        )

        waiting = data[
            "waiting_room"
        ]

        if waiting["enabled"]:

            await delete_waiting_room(
                channel
            )

            waiting["enabled"] = False

            save_data()

            await update_control_panel(
                channel
            )

            await interaction.response.send_message(
                "🚪 Waiting room disabled.",
                ephemeral=True
            )

            return

        try:

            waiting_channel = await create_waiting_room(
                channel
            )

            waiting["enabled"] = True

            save_data()

            await interaction.response.send_message(
                f"🚪 **Waiting room enabled!**\n"
                f"Users can wait in {waiting_channel.mention}.\n"
                "Use the panel to approve or reject them.",
                ephemeral=True
            )

            await update_control_panel(
                channel
            )

        except discord.HTTPException as e:

            await interaction.response.send_message(
                f"❌ Failed to create waiting room: {e}",
                ephemeral=True
            )

    async def allow_button(
        self,
        interaction
    ):

        channel = await self.manager(
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

    async def deny_button(
        self,
        interaction
    ):

        channel = await self.manager(
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

    async def kick_button(
        self,
        interaction
    ):

        channel = await self.manager(
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

    async def voice_mod_button(
        self,
        interaction
    ):

        channel = await self.manager(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "🎙️ Select a member and then choose a voice action:",
                view=VoiceModerationView(
                    channel
                ),
                ephemeral=True
            )

    async def moderator_button(
        self,
        interaction
    ):

        channel = await self.owner(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "Select a member to add/remove as a VC moderator:",
                view=MemberActionView(
                    channel,
                    "moderator"
                ),
                ephemeral=True
            )

    async def transfer_button(
        self,
        interaction
    ):

        channel = await self.owner(
            interaction
        )

        if channel:

            await interaction.response.send_message(
                "Select the new owner:",
                view=TransferView(
                    channel
                ),
                ephemeral=True
            )

    async def claim_button(
        self,
        interaction
    ):

        channel = await require_managed_channel(
            interaction
        )

        if channel:

            await claim_channel(
                interaction,
                channel
            )

    async def delete_button(
        self,
        interaction
    ):

        channel = await self.owner(
            interaction
        )

        if not channel:
            return

        await interaction.response.send_message(
            "🗑️ Deleting your temporary room...",
            ephemeral=True
        )

        await delete_temporary_channel(
            channel,
            "Deleted by room owner."
        )


# =========================================================
# PANEL
# =========================================================

async def update_control_panel(
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    message_id = data.get(
        "panel_message_id"
    )

    if message_id:

        try:

            message = await channel.fetch_message(
                message_id
            )

            await message.edit(
                embed=build_panel_embed(
                    channel
                ),
                view=TempVoiceView(
                    channel
                )
            )

            return

        except discord.NotFound:

            data[
                "panel_message_id"
            ] = None

        except discord.HTTPException:
            pass

    await send_control_panel(
        channel
    )


async def send_control_panel(
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    try:

        message = await channel.send(
            embed=build_panel_embed(
                channel
            ),
            view=TempVoiceView(
                channel
            )
        )

        data[
            "panel_message_id"
        ] = message.id

        save_data()

        print(
            f"✅ TempVoice panel created in "
            f"{channel.name}"
        )

    except discord.Forbidden:

        print(
            f"❌ Cannot send panel in "
            f"{channel.name}"
        )

    except discord.HTTPException as e:

        print(
            f"❌ Failed to send panel: {e}"
        )


# =========================================================
# DELETE ROOM
# =========================================================

async def delete_temporary_channel(
    channel,
    reason="TempVoice cleanup."
):

    channel_id = channel.id

    cancel_claim_task(
        channel_id
    )

    data = get_temporary_channel(
        channel_id
    )

    if data:

        await delete_waiting_room(
            channel
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
            f"❌ Cannot delete {channel.name}"
        )

    except discord.HTTPException as e:

        print(
            f"❌ Failed deleting {channel.name}: {e}"
        )


# =========================================================
# CREATE ROOM
# =========================================================

async def create_temporary_channel(
    member,
    config
):

    guild = member.guild

    category = guild.get_channel(
        config.get(
            "category_id"
        )
    )

    if not isinstance(
        category,
        discord.CategoryChannel
    ):

        raise RuntimeError(
            "Configured TempVoice category no longer exists."
        )

    template = config.get(
        "default_name",
        "{user}'s Room"
    )

    name = format_voice_name(
        template,
        member
    )

    limit = int(
        config.get(
            "default_limit",
            0
        )
    )

    overwrites = {

        guild.default_role:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                send_messages=True,
                read_message_history=True
            ),

        member:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                speak=True,
                stream=True,
                use_soundboard=True,
                send_messages=True,
                read_message_history=True,
                use_voice_activation=True
            ),

        guild.me:
            discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                send_messages=True,
                read_message_history=True
            )
    }

    channel = await guild.create_voice_channel(
        name=name,
        category=category,
        user_limit=limit,
        overwrites=overwrites,
        reason="TempVoice room creation"
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
            None,

        "chat": {

            "mode":
                "everyone",

            "outside_read":
                True,

            "outside_write":
                True,

            "locked":
                False
        },

        "waiting_room": {

            "enabled":
                False,

            "channel_id":
                None,

            "pending_users":
                []
        },

        "region":
            None,

        "member_controls":
            {}
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
# ACCESS
# =========================================================

async def require_managed_channel(
    interaction
):

    channel = get_member_channel(
        interaction
    )

    if not channel:

        await interaction.response.send_message(
            "❌ You aren't currently in a voice channel.",
            ephemeral=True
        )

        return None

    if not get_temporary_channel(
        channel.id
    ):

        await interaction.response.send_message(
            "❌ This isn't a TempVoice room.",
            ephemeral=True
        )

        return None

    return channel


# =========================================================
# CLAIM
# =========================================================

async def claim_channel(
    interaction,
    channel
):

    data = get_temporary_channel(
        channel.id
    )

    if not data:
        return

    owner = channel.guild.get_member(
        data["owner_id"]
    )

    if (
        owner
        and owner.voice
        and owner.voice.channel
        == channel
    ):

        await interaction.response.send_message(
            "❌ The current owner is still here.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        f"🙋 Claiming **{channel.name}** in "
        f"{CLAIM_DELAY} seconds...",
        ephemeral=True
    )

    async def finish():

        try:

            await asyncio.sleep(
                CLAIM_DELAY
            )

            if (
                not interaction.user.voice
                or interaction.user.voice.channel
                != channel
            ):

                return

            current = get_temporary_channel(
                channel.id
            )

            if not current:
                return

            owner = channel.guild.get_member(
                current["owner_id"]
            )

            if (
                owner
                and owner.voice
                and owner.voice.channel
                == channel
            ):

                return

            current[
                "owner_id"
            ] = interaction.user.id

            save_data()

            await apply_channel_permissions(
                channel
            )

            await update_control_panel(
                channel
            )

            await interaction.followup.send(
                "👑 You now own this room.",
                ephemeral=True
            )

        except asyncio.CancelledError:
            pass

        finally:

            if (
                claim_tasks.get(
                    channel.id
                )
                is asyncio.current_task()
            ):

                claim_tasks.pop(
                    channel.id,
                    None
                )

    cancel_claim_task(
        channel.id
    )

    task = asyncio.create_task(
        finish()
    )

    claim_tasks[
        channel.id
    ] = task


# =========================================================
# VOICE STATE
# =========================================================

@bot.event
async def on_voice_state_update(
    member,
    before,
    after
):

    # -----------------------------------------------------
    # CREATE ROOM
    # -----------------------------------------------------

    if after.channel:

        config = get_config(
            member.guild.id
        )

        if config:

            create_id = config.get(
                "create_channel_id"
            )

            if after.channel.id == create_id:

                try:

                    channel = await create_temporary_channel(
                        member,
                        config
                    )

                    await asyncio.sleep(
                        0.5
                    )

                    await send_control_panel(
                        channel
                    )

                except Exception as e:

                    print(
                        f"❌ Failed creating room: {e}"
                    )

    # -----------------------------------------------------
    # WAITING ROOM
    # -----------------------------------------------------

    if after.channel:

        for channel_id, data in list(
            temporary_channels.items()
        ):

            waiting_id = (
                data.get(
                    "waiting_room",
                    {}
                ).get(
                    "channel_id"
                )
            )

            if (
                waiting_id
                and after.channel.id
                == waiting_id
            ):

                pending = data[
                    "waiting_room"
                ][
                    "pending_users"
                ]

                if member.id not in pending:

                    pending.append(
                        member.id
                    )

                save_data()

                main = member.guild.get_channel(
                    channel_id
                )

                if isinstance(
                    main,
                    discord.VoiceChannel
                ):

                    await update_control_panel(
                        main
                    )

    # -----------------------------------------------------
    # Refresh affected rooms
    # -----------------------------------------------------

    affected = set()

    if before.channel:
        affected.add(
            before.channel.id
        )

    if after.channel:
        affected.add(
            after.channel.id
        )

    for channel_id in affected:

        if channel_id not in temporary_channels:
            continue

        channel = member.guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):

            continue

        # Reapply chat permissions because
        # voice-members-only chat depends on membership.

        await apply_chat_permissions(
            channel
        )

        if len(channel.members) == 0:

            await delete_temporary_channel(
                channel,
                "Temporary room became empty."
            )

            continue

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
        channel
    ):

        super().__init__(
            title="Rename Voice Channel"
        )

        self.channel = channel

        self.name_input = discord.ui.TextInput(
            label="Channel name",
            placeholder="New channel name...",
            max_length=100
        )

        self.add_item(
            self.name_input
        )

    async def on_submit(
        self,
        interaction
    ):

        try:

            await self.channel.edit(
                name=self.name_input.value,
                reason="TempVoice rename"
            )

            data = get_temporary_channel(
                self.channel.id
            )

            waiting_id = (
                data.get(
                    "waiting_room",
                    {}
                ).get(
                    "channel_id"
                )
            )

            if waiting_id:

                waiting = interaction.guild.get_channel(
                    waiting_id
                )

                if waiting:

                    await waiting.edit(
                        name=f"⏳ {self.channel.name} Waiting Room"[
                            :100
                        ]
                    )

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                "✏️ Room renamed.",
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "❌ Failed to rename the room.",
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
        channel
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
        interaction
    ):

        try:

            limit = int(
                self.limit_input.value
            )

            if not 0 <= limit <= 99:
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
                reason="TempVoice user limit"
            )

            await update_control_panel(
                self.channel
            )

            await interaction.response.send_message(
                (
                    "👥 Unlimited users."
                    if limit == 0
                    else
                    f"👥 Limit set to **{limit}**."
                ),
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "❌ Failed to change the limit.",
                ephemeral=True
            )


# =========================================================
# /TEMPVOICE SETUP
# =========================================================

@bot.tree.command(
    name="tempvoice_setup",
    description="Set up TempVoice for this server."
)
@app_commands.describe(
    create_channel="Channel users join to create their room.",
    category="Category for temporary rooms.",
    default_name="Default room name. Use {user}, {username}, or {server}.",
    default_limit="Default room limit. 0 = unlimited."
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def tempvoice_setup(
    interaction,
    create_channel: discord.VoiceChannel,
    category: discord.CategoryChannel,
    default_name: str = "{user}'s Room",
    default_limit: app_commands.Range[int, 0, 99] = 0
):

    if not interaction.guild:
        return

    if len(default_name) > 100:

        await interaction.response.send_message(
            "❌ Default name cannot exceed 100 characters.",
            ephemeral=True
        )

        return

    server_configs[
        str(interaction.guild.id)
    ] = {

        "create_channel_id":
            create_channel.id,

        "category_id":
            category.id,

        "default_name":
            default_name,

        "default_limit":
            default_limit
    }

    save_data()

    await interaction.response.send_message(
        "✅ **TempVoice configured!**\n\n"
        f"🎙️ Create channel: {create_channel.mention}\n"
        f"📁 Category: **{category.name}**\n"
        f"🏷️ Default name: `{default_name}`\n"
        f"👥 Default limit: **"
        f"{default_limit or 'Unlimited'}**",
        ephemeral=True
    )


# =========================================================
# /TEMPVOICE NAME
# =========================================================

@bot.tree.command(
    name="tempvoice_name",
    description="Change the default name of new TempVoice rooms."
)
@app_commands.describe(
    name="Use {user}, {username}, or {server}."
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def tempvoice_name(
    interaction,
    name: str
):

    if not interaction.guild:
        return

    config = get_config(
        interaction.guild.id
    )

    if not config:

        await interaction.response.send_message(
            "❌ Run `/tempvoice_setup` first.",
            ephemeral=True
        )

        return

    name = name.strip()

    if not name or len(name) > 100:

        await interaction.response.send_message(
            "❌ Name must be between 1 and 100 characters.",
            ephemeral=True
        )

        return

    config[
        "default_name"
    ] = name

    save_data()

    preview = format_voice_name(
        name,
        interaction.user
    )

    await interaction.response.send_message(
        f"✅ Default room name changed.\n\n"
        f"Template: `{name}`\n"
        f"Preview: **{preview}**",
        ephemeral=True
    )


# =========================================================
# /TEMPVOICE PANEL
# =========================================================

@bot.tree.command(
    name="tempvoice_panel",
    description="Refresh the TempVoice panel in your room."
)
async def tempvoice_panel(
    interaction
):

    channel = get_member_channel(
        interaction
    )

    if (
        channel
        and get_temporary_channel(
            channel.id
        )
    ):

        await update_control_panel(
            channel
        )

        await interaction.response.send_message(
            "✅ Panel refreshed.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "❌ You aren't currently in a TempVoice room.",
        ephemeral=True
    )


# =========================================================
# /TEMPVOICE CONFIG
# =========================================================

@bot.tree.command(
    name="tempvoice_config",
    description="View this server's TempVoice configuration."
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def tempvoice_config(
    interaction
):

    if not interaction.guild:
        return

    config = get_config(
        interaction.guild.id
    )

    if not config:

        await interaction.response.send_message(
            "❌ TempVoice isn't configured.",
            ephemeral=True
        )

        return

    create = interaction.guild.get_channel(
        config.get(
            "create_channel_id"
        )
    )

    category = interaction.guild.get_channel(
        config.get(
            "category_id"
        )
    )

    embed = discord.Embed(
        title="⚙️ TempVoice Configuration",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Create Channel",
        value=(
            create.mention
            if create
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
            "default_name"
        ),
        inline=False
    )

    embed.add_field(
        name="Default Limit",
        value=str(
            config.get(
                "default_limit",
                0
            ) or "Unlimited"
        ),
        inline=False
    )

    embed.add_field(
        name="Storage",
        value=(
            "☁️ GitHub / voice_data.json"
            if github_enabled()
            else
            "💾 Local fallback"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# CLEANUP
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
    interaction,
    error
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

    global data_loaded

    if not data_loaded:

        load_data()

        data_loaded = True

    if not hasattr(
        bot,
        "tempvoice_view_added"
    ):

        bot.add_view(
            TempVoiceView()
        )

        bot.tempvoice_view_added = True

    if not cleanup_channels.is_running():

        cleanup_channels.start()

    try:

        synced = await bot.tree.sync()

        print(
            f"✅ TempVoice online as {bot.user}"
        )

        print(
            f"🔧 Synced {len(synced)} commands"
        )

        print(
            f"🎙️ Tracking "
            f"{len(temporary_channels)} rooms"
        )

        if github_enabled():

            print(
                f"☁️ GitHub persistence: "
                f"{GITHUB_REPO}/"
                f"{GITHUB_VOICE_DATA_PATH}"
            )

        else:

            print(
                "💾 GitHub persistence disabled."
            )

        for channel_id in list(
            temporary_channels.keys()
        ):

            channel = bot.get_channel(
                channel_id
            )

            if isinstance(
                channel,
                discord.VoiceChannel
            ):

                try:

                    await apply_channel_permissions(
                        channel
                    )

                    await update_control_panel(
                        channel
                    )

                except Exception as e:

                    print(
                        f"⚠️ Failed restoring "
                        f"{channel.name}: {e}"
                    )

    except Exception as e:

        print(
            f"❌ Failed to sync commands: {e}"
        )


# =========================================================
# START
# =========================================================

if not BOT_TOKEN:

    raise RuntimeError(
        "BOT_TOKEN environment variable is missing!"
    )

bot.run(BOT_TOKEN)
