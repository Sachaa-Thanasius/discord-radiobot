"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""

from __future__ import annotations

import asyncio
import logging
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Self

import apsw
import discord
import wavelink
from discord import app_commands
from wavelink.ext import spotify


log = logging.getLogger(__name__)

with Path("config.toml").open("rb") as f:
    config = tomllib.load(f)

INITIALIZATION_STATEMENTS = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = wal;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = memory;
CREATE TABLE IF NOT EXISTS tracks (
    track_id            INTEGER     PRIMARY KEY,
    track_link          TEXT        NOT NULL        UNIQUE
) STRICT;
CREATE TABLE IF NOT EXISTS playlist (
    playlist_id         INTEGER     PRIMARY KEY,
    playlist_name       TEXT        NOT NULL        UNIQUE,
    track_id            INTEGER     NOT NULL        REFERENCES tracks(track_id),
    position            INTEGER     NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS radio_stations (
    station_id          INTEGER     PRIMARY KEY,
    station_name        TEXT        NOT NULL,
    playlist_id         INTEGER     NOT NULL        REFERENCES playlist(playlist_id)
) STRICT;
CREATE TABLE IF NOT EXISTS guild_radios (
    guild_id            INTEGER     PRIMARY KEY     NOT NULL,
    channel_id          INTEGER     NOT NULL,
    tracks_list         TEXT        NOT NULL,
    always_shuffle      INTEGER     NOT NULL        DEFAULT 1,
    default_station     INTEGER     NOT NULL        REFERENCES radio_stations(station_id)
) STRICT, WITHOUT ROWID;
"""

SELECT_BY_GUILD_STATEMENT = """
SELECT * FROM guild_radios WHERE guild_id = ?;
"""


def _setup_db(conn: apsw.Connection) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENTS)


def _query(conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]) -> list[tuple[apsw.SQLiteValue, ...]]:
    cursor = conn.cursor()
    return list(cursor.execute(query_str, params))


async def format_track_embed(embed: discord.Embed, track: wavelink.Playable | spotify.SpotifyTrack) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    end_time = str(timedelta(seconds=track.duration // 1000))

    if isinstance(track, wavelink.Playable):
        embed.description = (
            f"[{discord.utils.escape_markdown(track.title, as_needed=True)}]({track.uri})\n"
            f"{discord.utils.escape_markdown(track.author or '', as_needed=True)}\n"
        )
    else:
        embed.description = (
            f"[{discord.utils.escape_markdown(track.title, as_needed=True)}]"
            f"(https://open.spotify.com/track/{track.uri.rpartition(':')[2]})\n"
            f"{discord.utils.escape_markdown(', '.join(track.artists), as_needed=True)}\n"
        )

    embed.description = embed.description + f"`[0:00-{end_time}]`"

    if isinstance(track, wavelink.YouTubeTrack):
        thumbnail = await track.fetch_thumbnail()
        embed.set_thumbnail(url=thumbnail)

    return embed


class RadioBot(discord.AutoShardedClient):
    def __init__(self: Self) -> None:
        super().__init__(
            intents=discord.Intents.default(),  # Can be reduced later.
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-radiobot"),
        )
        self.tree = app_commands.CommandTree(self)

        # Connect to the database that will store the radio information.
        db_path = Path(config["DATABASE"]["path"])
        resolved_path_as_str = str(db_path.resolve(strict=True))
        self.connection = apsw.Connection(resolved_path_as_str)

    async def on_connect(self: Self) -> None:
        # Create an invite link.
        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self: Self) -> None:
        # Sync commands. Maybe remove later.
        await self.tree.sync()

        # Initialize the database.
        await asyncio.to_thread(_setup_db, self.connection)

        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(**config["LAVALINK"])
        sc = spotify.SpotifyClient(**config["SPOTIFY"]) if ("SPOTIFY" in config) else None
        await wavelink.NodePool.connect(client=self, nodes=[node], spotify=sc)


bot = RadioBot()


@bot.event
async def on_wavelink_node_ready(node: wavelink.Node) -> None:
    """Called when the Node you are connecting to has initialised and successfully connected to Lavalink."""

    log.info("Wavelink node %s is ready!", node.id)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEventPayload) -> None:
    """Called when the current track has finished playing."""

    player = payload.player

    if player.is_connected():
        next_track = await player.queue.get_wait()
        await player.play(next_track)
    else:
        await player.stop()


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackEventPayload) -> None:
    """Called when a new track has just started playing."""

    # Send a notification of the song now playing.
    if original := payload.original:
        current_embed = await format_track_embed(discord.Embed(color=0x149CDF, title="Now Playing"), original)
        if channel := payload.player.channel:
            await channel.send(embed=current_embed)


@bot.tree.command()
@app_commands.guild_only()
async def invite(interaction: discord.Interaction[RadioBot]) -> None:
    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=interaction.client.invite_link))
    await interaction.response.send_message(embed=embed, view=view)


radio_group = app_commands.Group(
    name="radio",
    description="The group of commands responsible for setting up, modifying, and using the radio.",
    guild_only=True,
    default_permissions=discord.Permissions(32),
)


@radio_group.command(name="get")
async def radio_get(interaction: discord.Interaction[RadioBot]) -> None:
    """Get the information about your server's current radio setup."""

    assert interaction.guild_id  # Known quantity since this is a guild-only command.

    records = await asyncio.to_thread(
        _query,
        interaction.client.connection,
        SELECT_BY_GUILD_STATEMENT,
        (interaction.guild_id,),
    )
    record = records[0]
    if record:
        embed = (
            discord.Embed(title="Current Guild's Radio")
            .add_field(name="Channel", value=f"<#{record['channel_id']}>")
            .add_field(name="Tracks Source", value=record["tracks_list"])
            .add_field(name="Always Shuffle", value=("Yes" if record["always_shuffle"] else "No"))
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("No radio found for this guild.")


bot.tree.add_command(radio_group)


@bot.tree.command(description="See what's currently playing on the radio.")
@app_commands.guild_only()
async def current(interaction: discord.Interaction[RadioBot]) -> None:
    assert interaction.guild  # Known quantity since this is a guild-only command.
    vc: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if vc and vc.current:
        embed = await format_track_embed(discord.Embed(title="Currently Playing"), vc.current)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("No radio currently active in this server.")


@bot.tree.command(description="See or change the volume of the radio.")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction[RadioBot], volume: int | None = None) -> None:
    assert interaction.guild  # Known quantity since this is a guild-only command.
    vc: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if vc:
        if volume is None:
            await interaction.response.send_message(f"Volume is currently set to {vc.volume}.")


async def main() -> None:
    token: str = config["DISCORD"]["token"]
    bot.run(token)


if __name__ == "__main__":
    asyncio.run(main())
