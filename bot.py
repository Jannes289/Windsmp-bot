import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
import os
import asyncio
import io
from datetime import datetime, timedelta
import re
from keep_alive import keep_alive
import json
import aiohttp

TOKEN = os.environ["DISCORD_TOKEN"]
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")

CONFIG_FILE = "artifacts/discord-bot/config.json"
GIVEAWAY_FILE = "artifacts/discord-bot/giveaways.json"

def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

STORAGE_CHANNEL_ID = int(os.environ["GIVEAWAY_STORAGE_CHANNEL_ID"]) if os.environ.get("GIVEAWAY_STORAGE_CHANNEL_ID") else None
STORAGE_MARKER = "GIVEAWAY_DATA_V1"

def _giveaways_to_dict() -> dict:
    data = {}
    for mid, gw in giveaways.items():
        data[str(mid)] = {
            "channel_id": gw["channel_id"],
            "end_time": gw["end_time"].isoformat(),
            "prize": gw["prize"],
            "description": gw["description"],
            "winners": gw["winners"],
            "participants": {str(k): v for k, v in gw["participants"].items()},
            "ended": gw["ended"],
        }
    return data

def _dict_to_giveaways(data: dict) -> dict:
    result = {}
    for mid_str, gw in data.items():
        result[int(mid_str)] = {
            "channel_id": gw["channel_id"],
            "end_time": datetime.fromisoformat(gw["end_time"]),
            "prize": gw["prize"],
            "description": gw["description"],
            "winners": gw["winners"],
            "participants": {int(k): v for k, v in gw["participants"].items()},
            "ended": gw["ended"],
        }
    return result

def save_giveaways_file():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, "giveaways.json")
        with open(path, "w") as f:
            json.dump(_giveaways_to_dict(), f, indent=2)
    except Exception:
        pass

def load_giveaways_file() -> dict:
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, "giveaways.json")
        with open(path, "r") as f:
            return _dict_to_giveaways(json.load(f))
    except Exception:
        return {}

def _get_storage_channel_id() -> int | None:
    if STORAGE_CHANNEL_ID:
        return STORAGE_CHANNEL_ID
    cfg = load_config()
    cid = cfg.get("storage_channel_id")
    return int(cid) if cid else None

async def save_giveaways():
    save_giveaways_file()
    cid = _get_storage_channel_id()
    if not cid:
        return
    channel = bot.get_channel(cid)
    if not channel:
        return
    json_bytes = json.dumps(_giveaways_to_dict(), indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(json_bytes), filename="giveaways.json")
    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.content.startswith(STORAGE_MARKER):
            try:
                await msg.delete()
            except Exception:
                pass
            break
    await channel.send(STORAGE_MARKER, file=file)

async def load_giveaways_discord() -> dict:
    cid = _get_storage_channel_id()
    if not cid:
        return {}
    channel = bot.get_channel(cid)
    if not channel:
        return {}
    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.content.startswith(STORAGE_MARKER) and msg.attachments:
            try:
                data_bytes = await msg.attachments[0].read()
                return _dict_to_giveaways(json.loads(data_bytes))
            except Exception:
                return {}
    return {}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Laufende Giveaways: {message_id: {channel_id, end_time, prize, description, winners, participants, ended}}
# participants: {user_id (int): ign (str)}
giveaways = load_giveaways_file()

GIVEAWAY_EMOJI = "🎉"


# ─── Hilfsfunktionen ────────────────────────────────────────────────────────

def parse_duration(duration_str: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", duration_str.lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def giveaway_embed(prize: str, description: str, end_time: datetime, winner_count: int = 1,
                   participant_count: int = 0, ended: bool = False, winners=None):
    if ended and winners:
        color = discord.Color.gold()
        body = (
            f"{description}\n\n"
            f"🏆 **Gewinner:** {', '.join(w.mention for w in winners)}"
        )
        title = f"🎉 Giveaway beendet — {prize}"
    elif ended:
        color = discord.Color.greyple()
        body = f"{description}\n\n❌ Keine Teilnehmer — kein Gewinner."
        title = f"🎉 Giveaway beendet — {prize}"
    else:
        color = discord.Color.blurple()
        timestamp_unix = int(end_time.timestamp())
        body = (
            f"{description}\n\n"
            f"⏰ Endet: <t:{timestamp_unix}:R>\n"
            f"👥 Teilnehmer: **{participant_count}**\n"
            f"🏆 Gewinner: **{winner_count}**"
        )
        title = f"🎉 {prize}"

    embed = discord.Embed(title=title, description=body, color=color)
    if not ended:
        embed.set_footer(text="Klicke auf den Button um teilzunehmen!")
    return embed


async def end_giveaway(message_id: int):
    if message_id not in giveaways:
        return

    data = giveaways[message_id]
    channel = bot.get_channel(data["channel_id"])
    if channel is None:
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        del giveaways[message_id]
        return

    participant_ids = list(data["participants"].keys())
    winner_count = data["winners"]
    winner_ids = random.sample(participant_ids, min(winner_count, len(participant_ids))) if participant_ids else []
    winners = [channel.guild.get_member(uid) for uid in winner_ids]
    winners = [w for w in winners if w]

    embed = giveaway_embed(
        data["prize"], data["description"], data["end_time"],
        winner_count, len(participant_ids), ended=True, winners=winners
    )

    # Panel ohne Button nach Ende
    await message.edit(embed=embed, view=None)

    if winners:
        winner_lines = ", ".join(
            f"{w.mention} (**{data['participants'][w.id]}**)" for w in winners
        )
        await channel.send(
            f"🎉 Glückwunsch {winner_lines}! Du hast **{data['prize']}** gewonnen!",
            reference=message
        )
    else:
        await channel.send("❌ Niemand hat am Giveaway teilgenommen. Kein Gewinner.", reference=message)

    giveaways[message_id]["ended"] = True
    await save_giveaways()


# ─── Giveaway Timer ─────────────────────────────────────────────────────────

@tasks.loop(seconds=10)
async def giveaway_check():
    now = datetime.utcnow()
    to_end = [
        mid for mid, data in list(giveaways.items())
        if not data.get("ended") and data["end_time"] <= now
    ]
    for mid in to_end:
        await end_giveaway(mid)


# ─── Giveaway Panel View ─────────────────────────────────────────────────────

class IGNModal(discord.ui.Modal, title="🎮 Minecraft IGN eintragen"):
    ign = discord.ui.TextInput(
        label="Dein Minecraft In-Game Name",
        placeholder="z.B. Notch",
        min_length=3,
        max_length=16
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        data = giveaways.get(self.message_id)
        if data is None or data.get("ended"):
            await interaction.response.send_message(
                "❌ Dieses Giveaway ist nicht mehr aktiv.", ephemeral=True
            )
            return

        user = interaction.user
        ign_value = self.ign.value.strip()
        data["participants"][user.id] = ign_value
        await save_giveaways()

        embed = giveaway_embed(
            data["prize"], data["description"], data["end_time"],
            data["winners"], len(data["participants"])
        )
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message(
            f"✅ Du nimmst jetzt am Giveaway um **{data['prize']}** teil!\n"
            f"Eingetragener IGN: **{ign_value}** 🍀",
            ephemeral=True
        )


class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(
        label="🎉 Giveaway Teilnehmen",
        style=discord.ButtonStyle.success,
        custom_id="giveaway:join"
    )
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = interaction.message.id
        data = giveaways.get(message_id)

        if data is None or data.get("ended"):
            await interaction.response.send_message(
                "❌ Dieses Giveaway ist nicht mehr aktiv.", ephemeral=True
            )
            return

        user = interaction.user
        if user.id in data["participants"]:
            # Bereits drin → austreten
            del data["participants"][user.id]
            embed = giveaway_embed(
                data["prize"], data["description"], data["end_time"],
                data["winners"], len(data["participants"])
            )
            await interaction.message.edit(embed=embed)
            await save_giveaways()
            await interaction.response.send_message(
                "↩️ Du hast das Giveaway **verlassen**.", ephemeral=True
            )
        else:
            # IGN Modal öffnen
            await interaction.response.send_modal(IGNModal(message_id=message_id))


# ─── Giveaway Panel Modal ─────────────────────────────────────────────────────

class GiveawayModal(discord.ui.Modal, title="🎉 Giveaway erstellen"):
    preis = discord.ui.TextInput(
        label="Preis",
        placeholder="z.B. Nitro, 10€ Steam Guthaben, ...",
        max_length=100
    )
    beschreibung = discord.ui.TextInput(
        label="Beschreibung",
        placeholder="Was gibt es zu gewinnen? Bedingungen?",
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    dauer = discord.ui.TextInput(
        label="Dauer",
        placeholder="z.B. 10m, 2h, 1d (m=Minuten, h=Stunden, d=Tage)",
        max_length=10
    )
    gewinner = discord.ui.TextInput(
        label="Anzahl Gewinner",
        placeholder="z.B. 1",
        default="1",
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.dauer.value.strip())
        if seconds is None:
            await interaction.response.send_message(
                "❌ Ungültige Dauer. Beispiele: `10m`, `2h`, `1d`", ephemeral=True
            )
            return

        try:
            winner_count = int(self.gewinner.value.strip())
            if winner_count < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Ungültige Gewinner-Anzahl.", ephemeral=True)
            return

        end_time = datetime.utcnow() + timedelta(seconds=seconds)
        prize = self.preis.value.strip()
        description = self.beschreibung.value.strip()

        embed = giveaway_embed(prize, description, end_time, winner_count, 0)
        view = GiveawayView()

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        giveaways[message.id] = {
            "channel_id": interaction.channel.id,
            "end_time": end_time,
            "prize": prize,
            "description": description,
            "winners": winner_count,
            "participants": {},
            "ended": False,
        }
        await save_giveaways()

        await asyncio.sleep(seconds)
        await end_giveaway(message.id)


# ─── Giveaway Slash Commands ─────────────────────────────────────────────────

giveaway_group = app_commands.Group(name="giveaway", description="Giveaway-Befehle")


@giveaway_group.command(name="create", description="Öffnet ein Formular zum Erstellen eines Giveaway-Panels")
@app_commands.default_permissions(manage_guild=True)
async def giveaway_create(interaction: discord.Interaction):
    await interaction.response.send_modal(GiveawayModal())


@giveaway_group.command(name="end", description="Beendet ein laufendes Giveaway sofort")
@app_commands.describe(nachricht_id="Die Nachrichten-ID des Giveaways")
@app_commands.default_permissions(manage_guild=True)
async def giveaway_end(interaction: discord.Interaction, nachricht_id: str):
    try:
        mid = int(nachricht_id)
    except ValueError:
        await interaction.response.send_message("❌ Ungültige Nachrichten-ID.", ephemeral=True)
        return

    if mid not in giveaways or giveaways[mid].get("ended"):
        await interaction.response.send_message("❌ Kein aktives Giveaway mit dieser ID gefunden.", ephemeral=True)
        return

    await interaction.response.send_message("✅ Giveaway wird beendet…", ephemeral=True)
    await end_giveaway(mid)


@giveaway_group.command(name="reroll", description="Zieht einen neuen Gewinner")
@app_commands.describe(nachricht_id="Die Nachrichten-ID des Giveaways")
@app_commands.default_permissions(manage_guild=True)
async def giveaway_reroll(interaction: discord.Interaction, nachricht_id: str):
    try:
        mid = int(nachricht_id)
    except ValueError:
        await interaction.response.send_message("❌ Ungültige Nachrichten-ID.", ephemeral=True)
        return

    data = giveaways.get(mid)
    if data is None:
        await interaction.response.send_message("❌ Giveaway nicht gefunden.", ephemeral=True)
        return

    participants = data["participants"]
    if not participants:
        await interaction.response.send_message("❌ Keine Teilnehmer für einen Reroll.", ephemeral=True)
        return

    winner_id = random.choice(list(participants.keys()))
    winner_ign = participants[winner_id]
    channel = bot.get_channel(data["channel_id"])
    winner = channel.guild.get_member(winner_id) if channel else None
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    msg_text = f"🎉 Neuer Gewinner: {winner_mention} (**{winner_ign}**) — Glückwunsch zum **{data['prize']}**!"
    try:
        message = await channel.fetch_message(mid)
        await interaction.response.send_message(msg_text, reference=message)
    except discord.NotFound:
        await interaction.response.send_message(msg_text)


@giveaway_group.command(name="list", description="Zeigt alle aktiven Giveaways")
async def giveaway_list(interaction: discord.Interaction):
    active = {mid: data for mid, data in giveaways.items() if not data.get("ended")}

    if not active:
        await interaction.response.send_message("📭 Aktuell laufen keine Giveaways.", ephemeral=True)
        return

    embed = discord.Embed(title="🎉 Aktive Giveaways", color=discord.Color.blurple())
    for mid, data in active.items():
        timestamp_unix = int(data["end_time"].timestamp())
        channel = bot.get_channel(data["channel_id"])
        channel_mention = channel.mention if channel else "Unbekannt"
        embed.add_field(
            name=f"🎁 {data['prize']}",
            value=(
                f"Kanal: {channel_mention}\n"
                f"Endet: <t:{timestamp_unix}:R>\n"
                f"Teilnehmer: **{len(data['participants'])}**\n"
                f"Gewinner: **{data['winners']}**\n"
                f"ID: `{mid}`"
            ),
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@giveaway_group.command(name="participants", description="Zeigt alle Teilnehmer eines Giveaways")
@app_commands.describe(nachricht_id="Die Nachrichten-ID des Giveaways")
@app_commands.default_permissions(administrator=True)
async def giveaway_participants(interaction: discord.Interaction, nachricht_id: str):
    try:
        mid = int(nachricht_id)
    except ValueError:
        await interaction.response.send_message("❌ Ungültige Nachrichten-ID.", ephemeral=True)
        return

    data = giveaways.get(mid)
    if data is None:
        await interaction.response.send_message("❌ Giveaway nicht gefunden.", ephemeral=True)
        return

    participants = list(data["participants"].items())
    if not participants:
        await interaction.response.send_message("📭 Noch keine Teilnehmer.", ephemeral=True)
        return

    # Aufteilen falls > 20 Teilnehmer (Embed-Limit)
    lines = [f"`{i+1}.` <@{uid}> — IGN: **{ign}**" for i, (uid, ign) in enumerate(participants)]
    chunks = [lines[i:i+20] for i in range(0, len(lines), 20)]

    embed = discord.Embed(
        title=f"👥 Teilnehmer — {data['prize']}",
        description="\n".join(chunks[0]),
        color=discord.Color.blurple()
    )
    embed.set_footer(text=f"Gesamt: {len(participants)} Teilnehmer • ID: {mid}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Falls mehr als 20, weitere Seiten schicken
    for chunk in chunks[1:]:
        embed2 = discord.Embed(description="\n".join(chunk), color=discord.Color.blurple())
        await interaction.followup.send(embed=embed2, ephemeral=True)


@giveaway_group.command(name="remove", description="Entfernt einen Teilnehmer aus dem Giveaway")
@app_commands.describe(
    nachricht_id="Die Nachrichten-ID des Giveaways",
    user="Der User der entfernt werden soll"
)
@app_commands.default_permissions(administrator=True)
async def giveaway_remove(interaction: discord.Interaction, nachricht_id: str, user: discord.Member):
    try:
        mid = int(nachricht_id)
    except ValueError:
        await interaction.response.send_message("❌ Ungültige Nachrichten-ID.", ephemeral=True)
        return

    data = giveaways.get(mid)
    if data is None:
        await interaction.response.send_message("❌ Giveaway nicht gefunden.", ephemeral=True)
        return

    if data.get("ended"):
        await interaction.response.send_message("❌ Das Giveaway ist bereits beendet.", ephemeral=True)
        return

    if user.id not in data["participants"]:
        await interaction.response.send_message(
            f"❌ {user.mention} nimmt gar nicht am Giveaway teil.", ephemeral=True
        )
        return

    del data["participants"][user.id]
    await save_giveaways()

    # Embed im Panel aktualisieren
    channel = bot.get_channel(data["channel_id"])
    try:
        message = await channel.fetch_message(mid)
        updated_embed = giveaway_embed(
            data["prize"], data["description"], data["end_time"],
            data["winners"], len(data["participants"])
        )
        await message.edit(embed=updated_embed)
    except (discord.NotFound, discord.Forbidden):
        pass

    await interaction.response.send_message(
        f"✅ {user.mention} wurde aus dem Giveaway **{data['prize']}** entfernt.\n"
        f"Verbleibende Teilnehmer: **{len(data['participants'])}**",
        ephemeral=True
    )


# ─── Ticket System ───────────────────────────────────────────────────────────

# Offene Tickets: {channel_id: {"user_id": ..., "kategorie": ...}}
open_tickets = {}

TICKET_KATEGORIEN = {
    "allgemein": {"label": "💬 Allgemein", "emoji": "💬", "farbe": discord.Color.blue(),
                  "beschreibung": "Allgemeine Fragen und Anliegen.",
                  "hinweis": "Beschreibe dein Anliegen so genau wie möglich."},
    "discord_mod": {"label": "🛡️ Discord Mod", "emoji": "🛡️", "farbe": discord.Color.blurple(),
                  "beschreibung": "Bewirb dich als Discord Moderator!",
                  "hinweis": "Bitte beantworte folgendes:\n• Wie alt bist du?\n• Wie aktiv bist du auf dem Discord Server?\n• Warum möchtest du Discord Moderator werden?\n• Hast du Erfahrung als Moderator?\n• Wie viele Stunden pro Woche kannst du aktiv sein?"},
    "twitch_mod":  {"label": "🎮 Twitch Mod", "emoji": "🎮", "farbe": discord.Color.purple(),
                  "beschreibung": "Bewirb dich als Twitch Moderator!",
                  "hinweis": "Bitte beantworte folgendes:\n• Wie alt bist du?\n• Wie oft schaust du den Stream?\n• Warum möchtest du Twitch Moderator werden?\n• Hast du Erfahrung als Twitch Mod?\n• Wie viele Stunden pro Woche kannst du aktiv sein?"},
    "bug":       {"label": "🐛 Bug melden", "emoji": "🐛", "farbe": discord.Color.red(),
                  "beschreibung": "Du hast einen Bug oder Fehler gefunden?",
                  "hinweis": "Beschreibe den Bug genau: Was ist passiert? Wie kann man ihn reproduzieren?"},
}


async def erstelle_ticket_kanal(interaction: discord.Interaction, kategorie_key: str):
    guild = interaction.guild
    user = interaction.user
    kat = TICKET_KATEGORIEN[kategorie_key]

    # Prüfen ob User bereits ein offenes Ticket hat
    for ch_id, data in open_tickets.items():
        if data["user_id"] == user.id:
            existing = guild.get_channel(ch_id)
            if existing:
                await interaction.followup.send(
                    f"❌ Du hast bereits ein offenes Ticket: {existing.mention}",
                    ephemeral=True
                )
                return

    # Kategorie-Ordner suchen oder erstellen
    try:
        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets")
    except discord.Forbidden:
        category = None

    # Berechtigungen
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
    }
    for role in guild.roles:
        if role.permissions.manage_messages and not role.is_default():
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    try:
        ticket_channel = await guild.create_text_channel(
            name=f"{kat['emoji']}-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"{kat['label']} von {user} (ID: {user.id})"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Der Bot hat keine Berechtigung, Kanäle zu erstellen.\n"
            "Bitte gib dem Bot die Berechtigung **'Kanäle verwalten'**.",
            ephemeral=True
        )
        return

    open_tickets[ticket_channel.id] = {"user_id": user.id, "kategorie": kategorie_key}

    embed = discord.Embed(
        title=f"{kat['emoji']} {kat['label']}",
        description=(
            f"Willkommen {user.mention}!\n\n"
            f"**{kat['beschreibung']}**\n\n"
            f"{kat['hinweis']}\n\n"
            "Ein Mod wird sich so bald wie möglich bei dir melden.\n"
            "Ticket schließen wenn das Anliegen erledigt ist."
        ),
        color=kat["farbe"],
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Ticket von {user} • {kat['label']}")

    await ticket_channel.send(content=f"{user.mention}", embed=embed, view=TicketCloseView())
    await interaction.followup.send(f"✅ Dein Ticket wurde erstellt: {ticket_channel.mention}", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Ticket schließen", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if channel.id not in open_tickets:
            await interaction.response.send_message("❌ Das ist kein aktives Ticket.", ephemeral=True)
            return

        data = open_tickets[channel.id]
        is_staff = interaction.user.guild_permissions.manage_messages
        is_owner = interaction.user.id == data["user_id"]

        if not (is_staff or is_owner):
            await interaction.response.send_message(
                "❌ Nur Mods oder der Ticket-Ersteller können das Ticket schließen.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔒 Ticket wird in **5 Sekunden** geschlossen von {interaction.user.mention}…"
        )
        del open_tickets[channel.id]
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket geschlossen von {interaction.user}")


class TicketKategorieSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="💬 Allgemein", value="allgemein", description="Allgemeine Fragen und Anliegen", emoji="💬"),
            discord.SelectOption(label="🛡️ Discord Mod", value="discord_mod", description="Bewirb dich als Discord Moderator", emoji="🛡️"),
            discord.SelectOption(label="🎮 Twitch Mod", value="twitch_mod", description="Bewirb dich als Twitch Moderator", emoji="🎮"),
            discord.SelectOption(label="🐛 Bug melden", value="bug", description="Einen Fehler oder Bug melden", emoji="🐛"),
        ]
        super().__init__(
            placeholder="🎫 Wähle eine Kategorie…",
            options=options,
            custom_id="ticket:kategorie"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await erstelle_ticket_kanal(interaction, self.values[0])


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketKategorieSelect())


ticket_panel_message_id: int | None = None

@bot.command()
@commands.has_permissions(administrator=True)
async def ticketpanel(ctx):
    global ticket_panel_message_id
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    # Altes Panel löschen falls vorhanden
    if ticket_panel_message_id:
        try:
            old_msg = await ctx.channel.fetch_message(ticket_panel_message_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    embed = discord.Embed(
        title="🎫 Support-Tickets",
        description=(
            "Brauchst du Hilfe oder möchtest dich bewerben?\n\n"
            "💬 **Allgemein** — Allgemeine Fragen & Anliegen\n"
            "🛡️ **Discord Mod** — Bewirb dich als Discord Moderator\n"
            "🎮 **Twitch Mod** — Bewirb dich als Twitch Moderator\n"
            "🐛 **Bug melden** — Fehler & Bugs melden\n\n"
            "Wähle eine Kategorie aus dem Menü unten!"
        ),
        color=discord.Color.blue()
    )
    msg = await ctx.send(embed=embed, view=TicketPanelView())
    ticket_panel_message_id = msg.id


# ─── Twitch Live Benachrichtigung ────────────────────────────────────────────

TWITCH_USERNAME = "jannes1128"
twitch_access_token = None
last_twitch_stream_id = None


async def get_twitch_token() -> str | None:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return None
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials"
            }
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("access_token")
    return None


@tasks.loop(minutes=2)
async def twitch_check():
    global twitch_access_token, last_twitch_stream_id

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return

    if twitch_access_token is None:
        twitch_access_token = await get_twitch_token()
        if twitch_access_token is None:
            return

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {twitch_access_token}"
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": TWITCH_USERNAME},
            headers=headers
        ) as resp:
            if resp.status == 401:
                twitch_access_token = await get_twitch_token()
                return
            if resp.status != 200:
                return
            data = await resp.json()

    streams = data.get("data", [])
    if not streams:
        last_twitch_stream_id = None
        return

    stream = streams[0]
    stream_id = stream["id"]

    if stream_id == last_twitch_stream_id:
        return

    last_twitch_stream_id = stream_id

    config = load_config()
    for guild in bot.guilds:
        guild_config = config.get(str(guild.id), {})
        channel_id = guild_config.get("twitch_channel")
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue

        title = stream.get("title", "Kein Titel")
        game = stream.get("game_name", "Unbekannt")
        viewer_count = stream.get("viewer_count", 0)
        thumbnail = stream.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")
        stream_url = f"https://twitch.tv/{TWITCH_USERNAME}"

        embed = discord.Embed(
            title=f"🔴 {TWITCH_USERNAME} ist jetzt LIVE!",
            description=f"**{title}**",
            url=stream_url,
            color=discord.Color.purple()
        )
        embed.add_field(name="🎮 Spiel", value=game, inline=True)
        embed.add_field(name="👁️ Zuschauer", value=str(viewer_count), inline=True)
        embed.set_image(url=thumbnail)
        embed.set_footer(text="Klicke auf den Titel um zuzuschauen!")
        embed.timestamp = datetime.utcnow()

        await channel.send(
            content=f"@everyone 🔴 **{TWITCH_USERNAME}** ist live auf Twitch!",
            embed=embed
        )


@bot.command()
@commands.has_permissions(administrator=True)
async def twitchsetup(ctx, kanal: discord.TextChannel = None):
    if kanal is None:
        kanal = ctx.channel

    config = load_config()
    guild_key = str(ctx.guild.id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key]["twitch_channel"] = kanal.id
    save_config(config)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    await ctx.send(
        f"✅ Twitch-Benachrichtigungen werden in {kanal.mention} gesendet!\n"
        f"Twitch-Kanal: **twitch.tv/{TWITCH_USERNAME}**",
        delete_after=15
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def twitchtest(ctx):
    config = load_config()
    guild_config = config.get(str(ctx.guild.id), {})
    channel_id = guild_config.get("twitch_channel")

    if not channel_id:
        await ctx.send("❌ Kein Twitch-Kanal gesetzt. Nutze `!twitchsetup #kanal`.", delete_after=10)
        return

    channel = ctx.guild.get_channel(channel_id)
    if not channel:
        await ctx.send("❌ Kanal nicht gefunden. Bitte erneut mit `!twitchsetup #kanal` einrichten.", delete_after=10)
        return

    embed = discord.Embed(
        title=f"🔴 {TWITCH_USERNAME} ist jetzt LIVE!",
        description="**Das ist eine Testnachricht!**",
        url=f"https://twitch.tv/{TWITCH_USERNAME}",
        color=discord.Color.purple()
    )
    embed.add_field(name="🎮 Spiel", value="Minecraft", inline=True)
    embed.add_field(name="👁️ Zuschauer", value="999", inline=True)
    embed.set_footer(text="Das ist nur ein Test!")
    embed.timestamp = datetime.utcnow()

    await channel.send(
        content=f"@everyone 🔴 **{TWITCH_USERNAME}** ist live auf Twitch!",
        embed=embed
    )
    await ctx.send(f"✅ Testnachricht in {channel.mention} gesendet.", delete_after=5)


# ─── Willkommen System ───────────────────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def welcomesetup(ctx, kanal: discord.TextChannel = None):
    if kanal is None:
        kanal = ctx.channel

    config = load_config()
    config[str(ctx.guild.id)] = {"welcome_channel": kanal.id}
    save_config(config)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    await ctx.send(
        f"✅ Willkommens-Kanal wurde auf {kanal.mention} gesetzt!",
        delete_after=10
    )

    # Vorschau
    embed = _welcome_embed(ctx.author, ctx.guild)
    await kanal.send(content=f"📋 **Vorschau** — so sehen Willkommensnachrichten aus:", embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def welcometest(ctx):
    config = load_config()
    guild_config = config.get(str(ctx.guild.id), {})
    channel_id = guild_config.get("welcome_channel")

    if not channel_id:
        await ctx.send("❌ Kein Willkommens-Kanal gesetzt. Nutze `!welcomesetup #kanal`.", delete_after=10)
        return

    channel = ctx.guild.get_channel(channel_id)
    if not channel:
        await ctx.send("❌ Kanal nicht gefunden. Bitte erneut mit `!welcomesetup #kanal` einrichten.", delete_after=10)
        return

    embed = _welcome_embed(ctx.author, ctx.guild)
    await channel.send(embed=embed)
    await ctx.send(f"✅ Testnachricht wurde in {channel.mention} gesendet.", delete_after=5)


def _welcome_embed(member: discord.Member, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"👋 Willkommen auf {guild.name}!",
        description=(
            f"Hey {member.mention}, schön dass du dabei bist!\n\n"
            f"Du bist unser **{guild.member_count}. Mitglied**. 🎉\n\n"
            f"Schau dir die Regeln an und viel Spaß auf dem Server!"
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{guild.name}", icon_url=guild.icon.url if guild.icon else None)
    return embed


@bot.event
async def on_member_join(member: discord.Member):
    config = load_config()
    guild_config = config.get(str(member.guild.id), {})
    channel_id = guild_config.get("welcome_channel")

    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if not channel:
        return

    embed = _welcome_embed(member, member.guild)
    await channel.send(embed=embed)


# ─── Umfrage System ──────────────────────────────────────────────────────────

# Aktive Umfragen: {message_id: {question, options, votes: {user_id: option_index}, ended}}
polls = {}

POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
POLL_COLORS = [
    discord.Color.blue(), discord.Color.green(), discord.Color.orange(),
    discord.Color.purple(), discord.Color.red()
]


def poll_embed(question: str, options: list, votes: dict, ended: bool = False):
    total = len(votes)
    color = discord.Color.greyple() if ended else discord.Color.blurple()

    description_lines = []
    for i, option in enumerate(options):
        count = sum(1 for v in votes.values() if v == i)
        percent = (count / total * 100) if total > 0 else 0
        bar_filled = int(percent / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        description_lines.append(
            f"{POLL_EMOJIS[i]} **{option}**\n`{bar}` {count} Stimme(n) ({percent:.0f}%)"
        )

    embed = discord.Embed(
        title=f"{'📊' if not ended else '🔒'} {'Umfrage' if not ended else 'Umfrage beendet'}: {question}",
        description="\n\n".join(description_lines),
        color=color
    )
    embed.set_footer(text=f"{'Abstimmen mit den Buttons unten!' if not ended else 'Diese Umfrage ist beendet.'} • {total} Stimme(n)")
    return embed


class PollView(discord.ui.View):
    def __init__(self, message_id: int, options: list):
        super().__init__(timeout=None)
        for i, option in enumerate(options[:5]):
            self.add_item(PollButton(message_id=message_id, index=i, label=option))


class PollButton(discord.ui.Button):
    def __init__(self, message_id: int, index: int, label: str):
        super().__init__(
            label=f"{POLL_EMOJIS[index]} {label[:50]}",
            style=discord.ButtonStyle.primary,
            custom_id=f"poll:{message_id}:{index}"
        )
        self.message_id = message_id
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        data = polls.get(self.message_id)
        if data is None or data.get("ended"):
            await interaction.response.send_message("❌ Diese Umfrage ist bereits beendet.", ephemeral=True)
            return

        user_id = interaction.user.id
        previous = data["votes"].get(user_id)

        if previous == self.index:
            del data["votes"][user_id]
            msg = f"↩️ Deine Stimme für **{data['options'][self.index]}** wurde entfernt."
        else:
            data["votes"][user_id] = self.index
            if previous is not None:
                msg = f"🔄 Deine Stimme wurde auf **{data['options'][self.index]}** geändert."
            else:
                msg = f"✅ Du hast für **{data['options'][self.index]}** abgestimmt!"

        embed = poll_embed(data["question"], data["options"], data["votes"])
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message(msg, ephemeral=True)


@bot.command()
async def poll(ctx, *, eingabe: str = None):
    """Erstellt eine Umfrage: !poll Frage | Option1 | Option2 | ..."""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    if eingabe is None:
        await ctx.send(
            "❌ Nutzung: `!poll Frage | Option1 | Option2 | Option3`\n"
            "Beispiel: `!poll Was spielen wir heute? | Minecraft | Fortnite | Valorant`",
            delete_after=20
        )
        return

    teile = [t.strip() for t in eingabe.split("|")]
    if len(teile) < 3:
        await ctx.send(
            "❌ Mindestens **2 Antwortmöglichkeiten** angeben!\n"
            "Beispiel: `!poll Was spielen wir? | Minecraft | Fortnite`",
            delete_after=15
        )
        return
    if len(teile) > 6:
        await ctx.send("❌ Maximal **5 Antwortmöglichkeiten** erlaubt.", delete_after=10)
        return

    question = teile[0]
    options = teile[1:]

    embed = poll_embed(question, options, {})
    view = PollView(message_id=0, options=options)

    msg = await ctx.send(embed=embed, view=view)

    polls[msg.id] = {
        "question": question,
        "options": options,
        "votes": {},
        "ended": False,
        "channel_id": ctx.channel.id
    }

    # View mit echter message_id neu erstellen
    view2 = PollView(message_id=msg.id, options=options)
    await msg.edit(view=view2)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def pollend(ctx, nachricht_id: str = None):
    """Beendet eine Umfrage: !pollend [Nachrichten-ID]"""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    if nachricht_id is None:
        await ctx.send("❌ Bitte gib die Nachrichten-ID an: `!pollend ID`", delete_after=10)
        return

    try:
        mid = int(nachricht_id)
    except ValueError:
        await ctx.send("❌ Ungültige ID.", delete_after=10)
        return

    data = polls.get(mid)
    if data is None:
        await ctx.send("❌ Keine aktive Umfrage mit dieser ID gefunden.", delete_after=10)
        return
    if data.get("ended"):
        await ctx.send("❌ Diese Umfrage ist bereits beendet.", delete_after=10)
        return

    data["ended"] = True
    channel = bot.get_channel(data["channel_id"])
    try:
        message = await channel.fetch_message(mid)
        embed = poll_embed(data["question"], data["options"], data["votes"], ended=True)
        await message.edit(embed=embed, view=None)
    except (discord.NotFound, discord.Forbidden):
        pass

    total = len(data["votes"])
    if total > 0:
        winner_index = max(range(len(data["options"])), key=lambda i: sum(1 for v in data["votes"].values() if v == i))
        winner = data["options"][winner_index]
        winner_votes = sum(1 for v in data["votes"].values() if v == winner_index)
        await ctx.send(
            f"🔒 Umfrage beendet! **{total}** Stimme(n) abgegeben.\n"
            f"🏆 Gewinner: **{winner}** mit **{winner_votes}** Stimme(n)!",
            delete_after=30
        )
    else:
        await ctx.send("🔒 Umfrage beendet — keine Stimmen abgegeben.", delete_after=15)


# ─── TempVoice System ────────────────────────────────────────────────────────

# temp_voices = {voice_channel_id: {"owner_id": int, "guild_id": int, "locked": bool}}
temp_voices = {}


class UmbenennenModal(discord.ui.Modal, title="✏️ Kanal umbenennen"):
    name = discord.ui.TextInput(label="Neuer Kanalname", placeholder="z.B. WindSMP Runde", min_length=1, max_length=100)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.edit(name=self.name.value.strip())
            await interaction.response.send_message(f"✅ Kanal umbenannt zu **{self.name.value.strip()}**", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)


class LimitModal(discord.ui.Modal, title="👥 Nutzer-Limit setzen"):
    limit = discord.ui.TextInput(label="Maximale Nutzer (0 = kein Limit)", placeholder="z.B. 5", min_length=1, max_length=2)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.limit.value.strip())
            if val < 0 or val > 99:
                raise ValueError
            await self.channel.edit(user_limit=val)
            msg = f"✅ Limit gesetzt auf **{val}**" if val > 0 else "✅ Limit **entfernt**"
            await interaction.response.send_message(msg, ephemeral=True)
        except (ValueError, discord.Forbidden):
            await interaction.response.send_message("❌ Ungültiger Wert (0–99).", ephemeral=True)


class TempVoicePanel(discord.ui.View):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(timeout=None)
        self.channel = channel

    def _is_owner(self, user_id: int) -> bool:
        data = temp_voices.get(self.channel.id)
        return data is not None and data["owner_id"] == user_id

    @discord.ui.button(label="✏️ Umbenennen", style=discord.ButtonStyle.secondary, row=0)
    async def umbenennen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Nur der Besitzer kann den Kanal bearbeiten.", ephemeral=True)
            return
        await interaction.response.send_modal(UmbenennenModal(self.channel))

    @discord.ui.button(label="👥 Limit", style=discord.ButtonStyle.secondary, row=0)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Nur der Besitzer kann das Limit ändern.", ephemeral=True)
            return
        await interaction.response.send_modal(LimitModal(self.channel))

    @discord.ui.button(label="🔒 Sperren", style=discord.ButtonStyle.danger, row=0)
    async def sperren(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Nur der Besitzer kann den Kanal sperren.", ephemeral=True)
            return
        data = temp_voices.get(self.channel.id)
        if data is None:
            return
        locked = data.get("locked", False)
        guild = interaction.guild
        overwrite = self.channel.overwrites_for(guild.default_role)
        if locked:
            overwrite.connect = None
            data["locked"] = False
            button.label = "🔒 Sperren"
            await interaction.response.send_message("🔓 Kanal wurde **entsperrt**.", ephemeral=True)
        else:
            overwrite.connect = False
            data["locked"] = True
            button.label = "🔓 Entsperren"
            await interaction.response.send_message("🔒 Kanal wurde **gesperrt**.", ephemeral=True)
        await self.channel.set_permissions(guild.default_role, overwrite=overwrite)
        await interaction.message.edit(view=self)

    @discord.ui.button(label="👑 Besitzer übertragen", style=discord.ButtonStyle.primary, row=1)
    async def besitzer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Nur der aktuelle Besitzer kann übertragen.", ephemeral=True)
            return
        members = [m for m in self.channel.members if m.id != interaction.user.id]
        if not members:
            await interaction.response.send_message("❌ Niemand anderes ist im Kanal.", ephemeral=True)
            return

        options = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members[:25]]

        class OwnerSelect(discord.ui.Select):
            def __init__(self_inner):
                super().__init__(placeholder="Wähle den neuen Besitzer…", options=options)

            async def callback(self_inner, sel_interaction: discord.Interaction):
                new_owner_id = int(self_inner.values[0])
                temp_voices[self.channel.id]["owner_id"] = new_owner_id
                new_owner = interaction.guild.get_member(new_owner_id)
                await sel_interaction.response.send_message(
                    f"👑 **{new_owner.display_name}** ist jetzt der neue Besitzer des Kanals!", ephemeral=False
                )

        view = discord.ui.View(timeout=30)
        view.add_item(OwnerSelect())
        await interaction.response.send_message("Wähle den neuen Besitzer:", view=view, ephemeral=True)

    @discord.ui.button(label="🗑️ Kanal löschen", style=discord.ButtonStyle.danger, row=1)
    async def loeschen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Nur der Besitzer kann den Kanal löschen.", ephemeral=True)
            return
        if self.channel.id in temp_voices:
            del temp_voices[self.channel.id]
        await interaction.response.send_message("🗑️ Kanal wird gelöscht…", ephemeral=True)
        await self.channel.delete(reason="Vom Besitzer manuell gelöscht")


@bot.command()
@commands.has_permissions(administrator=True)
async def tempvoicesetup(ctx):
    """Erstellt den 'Erstelle Channel' Voice-Kanal für TempVoice"""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    guild = ctx.guild
    category = discord.utils.get(guild.categories, name="Voice")
    if category is None:
        try:
            category = await guild.create_category("Voice")
        except discord.Forbidden:
            category = None

    try:
        join_channel = await guild.create_voice_channel("➕ Erstelle Channel", category=category)
    except discord.Forbidden:
        await ctx.send("❌ Keine Berechtigung, Sprachkanäle zu erstellen.", delete_after=10)
        return

    config = load_config()
    guild_key = str(guild.id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key]["tempvoice_channel"] = join_channel.id
    save_config(config)

    await ctx.send(
        f"✅ TempVoice eingerichtet!\n"
        f"Kanal: {join_channel.mention}\n"
        f"Wenn jemand diesen Kanal betritt, wird automatisch ein privater Kanal erstellt.",
        delete_after=20
    )


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    config = load_config()
    guild_key = str(member.guild.id)
    join_channel_id = config.get(guild_key, {}).get("tempvoice_channel")

    # Jemand betritt den "Erstelle Channel"
    if after.channel and join_channel_id and after.channel.id == join_channel_id:
        guild = member.guild
        category = after.channel.category
        try:
            new_channel = await guild.create_voice_channel(
                name=f"🎮 {member.display_name}s Channel",
                category=category,
                user_limit=0
            )
            await member.move_to(new_channel)
            temp_voices[new_channel.id] = {"owner_id": member.id, "guild_id": guild.id, "locked": False}

            # Kontrollpanel als DM oder im ersten verfügbaren Textkanal senden
            embed = discord.Embed(
                title="🎙️ Dein TempVoice-Kanal",
                description=(
                    f"**{new_channel.name}** wurde erstellt!\n\n"
                    "Nutze die Buttons unten um deinen Kanal zu konfigurieren.\n"
                    "Der Kanal wird automatisch gelöscht wenn du ihn verlässt."
                ),
                color=discord.Color.green()
            )
            embed.add_field(name="✏️ Umbenennen", value="Namen ändern", inline=True)
            embed.add_field(name="👥 Limit", value="Max. Nutzer setzen", inline=True)
            embed.add_field(name="🔒 Sperren", value="Kanal sperren/entsperren", inline=True)
            embed.add_field(name="👑 Übertragen", value="Besitz übertragen", inline=True)

            try:
                await member.send(embed=embed, view=TempVoicePanel(new_channel))
            except discord.Forbidden:
                # DM nicht möglich → Nachricht im Textkanal
                text_channel = discord.utils.get(guild.text_channels)
                if text_channel:
                    msg = await text_channel.send(
                        content=f"{member.mention} — dein Kanal-Panel:",
                        embed=embed,
                        view=TempVoicePanel(new_channel),
                        delete_after=300
                    )
        except discord.Forbidden:
            pass

    # Jemand verlässt einen TempVoice-Kanal
    if before.channel and before.channel.id in temp_voices:
        channel = before.channel
        data = temp_voices[channel.id]

        # Kanal leer → löschen
        if len(channel.members) == 0:
            del temp_voices[channel.id]
            try:
                await channel.delete(reason="TempVoice: Kanal leer")
            except discord.NotFound:
                pass
            return

        # Besitzer hat den Kanal verlassen → Kanal löschen
        if member.id == data["owner_id"]:
            del temp_voices[channel.id]
            try:
                await channel.delete(reason="TempVoice: Besitzer hat verlassen")
            except discord.NotFound:
                pass


# ─── Events ─────────────────────────────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def setupstorage(ctx):
    """Richtet diesen Kanal als Giveaway-Speicher ein (für Railway-Persistenz)."""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    cfg = load_config()
    cfg["storage_channel_id"] = ctx.channel.id
    save_config(cfg)
    # Speichere aktuelle Giveaways sofort
    await save_giveaways()
    await ctx.send(
        f"✅ Dieser Kanal wird jetzt als **Giveaway-Speicher** verwendet.\n"
        f"Giveaways überleben jetzt jeden Bot-Neustart! 🎉\n"
        f"*(Diesen Kanal bitte nicht löschen oder bereinigen)*",
        delete_after=30
    )


@bot.event
async def on_ready():
    global giveaways
    bot.add_view(GiveawayView())
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())
    bot.tree.add_command(giveaway_group)
    try:
        synced = await bot.tree.sync()
        print(f"Slash Commands synchronisiert: {len(synced)} Commands")
    except Exception as e:
        print(f"Fehler beim Synchronisieren: {e}")

    # Giveaways aus Discord laden (überschreibt lokale Datei falls vorhanden)
    discord_data = await load_giveaways_discord()
    if discord_data:
        giveaways.update(discord_data)
        print(f"Giveaways aus Discord-Storage geladen: {len(discord_data)} Einträge")

    # Abgelaufene Giveaways sofort beenden, aktive zählen
    now = datetime.utcnow()
    restored = 0
    for mid, data in list(giveaways.items()):
        if not data.get("ended"):
            if data["end_time"] <= now:
                await end_giveaway(mid)
            else:
                restored += 1
    if restored:
        print(f"{restored} aktive Giveaway(s) wiederhergestellt.")

    giveaway_check.start()
    twitch_check.start()
    print(f"{bot.user} ist online!")


keep_alive()
bot.run(TOKEN)
