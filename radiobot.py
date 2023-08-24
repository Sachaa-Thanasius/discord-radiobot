"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import tomllib
from collections.abc import AsyncIterable, Iterable
from itertools import chain
from pathlib import Path
from typing import Literal, Self, TypeAlias
from urllib.parse import urlparse

import apsw
import attrs
import discord
import wavelink
import yarl
from apsw.ext import log_sqlite
from discord import app_commands
from discord.ext import tasks
from wavelink.ext import spotify


GuildRadioInfoTuple: TypeAlias = tuple[int, int, bool, int, str, str, int]
RadioStationTuple: TypeAlias = tuple[int, str, str, int]
AnyTrack: TypeAlias = wavelink.Playable | spotify.SpotifyTrack
AnyTrackIterable: TypeAlias = list[wavelink.Playable] | list[spotify.SpotifyTrack] | AsyncIterable[spotify.SpotifyTrack]

log = logging.getLogger(__name__)

with Path("config.toml").open("rb") as file_:
    config = tomllib.load(file_)

INITIALIZATION_STATEMENTS = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = normal;
PRAGMA temp_store = memory;
CREATE TABLE IF NOT EXISTS radio_stations (
    station_id      INTEGER     NOT NULL        PRIMARY KEY,
    station_name    TEXT        NOT NULL        UNIQUE,
    playlist_link   TEXT        NOT NULL,
    owner_id        INTEGER     NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS guild_radios (
    guild_id        INTEGER     NOT NULL        PRIMARY KEY,
    station_id      INTEGER     NOT NULL,
    channel_id      INTEGER     NOT NULL,
    always_shuffle  INTEGER     NOT NULL        DEFAULT TRUE,
    FOREIGN KEY     (station_id)  REFERENCES radio_stations(station_id) ON UPDATE CASCADE ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS guild_managing_roles (
    guild_id        INTEGER     NOT NULL,
    role_id         INTEGER     NOT NULL,
    FOREIGN KEY     (guild_id)    REFERENCES guild_radios(guild_id) ON UPDATE CASCADE ON DELETE CASCADE,
    PRIMARY KEY     (guild_id, role_id)
) STRICT, WITHOUT ROWID;
"""

SELECT_ALL_INFO_BY_GUILD_STATEMENT = """
SELECT guild_id, channel_id, always_shuffle, station_id, station_name, playlist_link, owner_id
FROM guild_radios INNER JOIN radio_stations USING (station_id)
WHERE guild_id = ?;
"""

SELECT_ENABLED_GUILDS_STATEMENT = """
SELECT guild_id FROM guild_radios;
"""

SELECT_STATIONS_STATEMENTS = """
SELECT * FROM radio_stations;
"""

SELECT_STATIONS_BY_NAME_STATEMENT = """
SELECT * FROM radio_stations WHERE station_name = ?;
"""

SELECT_STATIONS_BY_OWNER_STATEMENT = """
SELECT * FROM radio_stations WHERE owner_id = ?;
"""

SELECT_ROLES_BY_GUILD_STATEMENT = """
SELECT role_id FROM guild_managing_roles WHERE guild_id = ?;
"""

UPSERT_STATION_STATEMENT = """
INSERT INTO radio_stations(station_name, playlist_link, owner_id) VALUES (:station_name, :playlist_link, :owner_id)
ON CONFLICT (station_name)
DO UPDATE
    SET playlist_link = excluded.playlist_link
    WHERE owner_id = excluded.owner_id
RETURNING *;
"""

UPSERT_GUILD_RADIO_STATEMENT = """
INSERT INTO guild_radios(guild_id, channel_id, station_id, always_shuffle)
VALUES (?, ?, ?, ?)
ON CONFLICT (guild_id)
DO UPDATE
    SET channel_id = EXCLUDED.channel_id,
        station_id = EXCLUDED.station_id,
        always_shuffle = EXCLUDED.always_shuffle;
"""

INSERT_MANAGING_ROLE_STATEMENT = """
INSERT INTO guild_managing_roles (guild_id, role_id) VALUES (?, ?) ON CONFLICT DO NOTHING;
"""


@attrs.define
class StationInfo:
    station_id: int
    station_name: str
    playlist_link: str
    owner_id: int

    @classmethod
    def from_row(cls: type[Self], row: RadioStationTuple) -> Self:
        station_id, station_name, playlist_link, owner_id = row
        return cls(station_id, station_name, playlist_link, owner_id)


@attrs.define
class GuildRadioInfo:
    guild_id: int
    channel_id: int
    always_shuffle: bool
    station: StationInfo
    dj_roles: list[int] = attrs.Factory(list)

    @classmethod
    def from_row(cls: type[Self], row: GuildRadioInfoTuple) -> Self:
        print(row)
        guild_id, channel_id, always_shuffle, station_id, station_name, playlist_link, owner_id = row
        return cls(
            guild_id,
            channel_id,
            bool(always_shuffle),
            StationInfo(station_id, station_name, playlist_link, owner_id),
        )


def _setup_db(conn: apsw.Connection) -> set[int]:
    # with conn:
    cursor = conn.cursor()
    cursor.execute(INITIALIZATION_STATEMENTS)
    print("thing", list(cursor))  # This shouldn't have anything.
    cursor.execute(SELECT_ENABLED_GUILDS_STATEMENT)
    return set(chain.from_iterable(cursor))


def _query(conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]) -> list[tuple[apsw.SQLiteValue, ...]]:
    cursor = conn.cursor()
    return list(cursor.execute(query_str, params))


def _query_stations(
    conn: apsw.Connection,
    query_str: str,
    params: tuple[int | str, ...] | None = None,
) -> list[StationInfo]:
    cursor = conn.cursor()
    return [StationInfo.from_row(row) for row in cursor.execute(query_str, params)]


def _get_all_guilds_radio_info(conn: apsw.Connection, guild_ids: list[tuple[int]]) -> list[GuildRadioInfo]:
    cursor = conn.cursor()
    return [GuildRadioInfo.from_row(row) for row in cursor.executemany(SELECT_ALL_INFO_BY_GUILD_STATEMENT, guild_ids)]


def _add_radio(
    conn: apsw.Connection,
    *,
    guild_id: int,
    channel_id: int,
    station_id: int,
    always_shuffle: bool,
    managing_roles: list[discord.Role] | None,
) -> GuildRadioInfo | None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(UPSERT_GUILD_RADIO_STATEMENT, (guild_id, channel_id, station_id, always_shuffle))
        if managing_roles:
            cursor.executemany(INSERT_MANAGING_ROLE_STATEMENT, [(guild_id, role.id) for role in managing_roles])
        record = cursor.execute(SELECT_ALL_INFO_BY_GUILD_STATEMENT, (guild_id,))
        return GuildRadioInfo.from_row(rec) if (rec := record.fetchone()) else None


class WavelinkTrackConverter:
    """Converts to what Wavelink considers a playable track (:class:`AnyPlayable` or :class:`AnyTrackIterable`).

    The lookup strategy is as follows (in order):

    1. Lookup by :class:`wavelink.YouTubeTrack` if the argument has no url "scheme".
    2. Lookup by first valid wavelink track class if the argument matches the search/url format.
    3. Lookup by assuming argument to be a direct url or local file address.
    """

    @staticmethod
    def _get_search_type(argument: str) -> type[AnyTrack]:
        """Get the searchable wavelink class that matches the argument string closest."""

        check = yarl.URL(argument)
        print(check.parts, "\n", urlparse(argument))

        if (
            (not check.host and not check.scheme)
            or (check.host in ("youtube.com", "www.youtube.com", "m.youtube.com") and "v" in check.query)
            or check.scheme == "ytsearch"
        ):
            search_type = wavelink.YouTubeTrack
        elif (
            check.host in ("youtube.com", "www.youtube.com", "m.youtube.com") and "list" in check.query
        ) or check.scheme == "ytpl":
            search_type = wavelink.YouTubePlaylist
        elif check.host == "music.youtube.com" or check.scheme == "ytmsearch":
            search_type = wavelink.YouTubeMusicTrack
        elif check.host in ("soundcloud.com", "www.soundcloud.com") and "sets" in check.parts:
            search_type = wavelink.SoundCloudPlaylist
        elif check.host in ("soundcloud.com", "www.soundcloud.com") or check.scheme == "scsearch":
            search_type = wavelink.SoundCloudTrack
        elif check.host in ("spotify.com", "open.spotify.com"):
            search_type = spotify.SpotifyTrack
        else:
            search_type = wavelink.GenericTrack

        return search_type

    @classmethod
    async def convert(cls: type[Self], argument: str) -> AnyTrack | AnyTrackIterable:
        """Attempt to convert a string into a Wavelink track or list of tracks."""

        search_type = cls._get_search_type(argument)
        if issubclass(search_type, spotify.SpotifyTrack):
            try:
                tracks = (track async for track in search_type.iterator(query=argument))  # type: ignore # wl typing
            except TypeError:
                tracks = await search_type.search(argument)
        else:
            tracks = await search_type.search(argument)

        if not tracks:
            msg = f"Your search query `{argument}` returned no tracks."
            raise wavelink.NoTracksError(msg)

        # Still possible for tracks to be a Playlist subclass at this point.
        if issubclass(search_type, wavelink.Playable) and isinstance(tracks, list):  # type: ignore # wl typing
            tracks = tracks[0]

        return tracks  # type: ignore # wl spotify iterator typing


async def format_track_embed(embed: discord.Embed, track: wavelink.Playable | spotify.SpotifyTrack) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    end_time = str(datetime.timedelta(seconds=track.duration // 1000))

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


def convert_list_role(roles_input_str: str, guild: discord.Guild) -> list[discord.Role]:
    split_role_ids_pattern = re.compile(r"(?:<@&|.*?)([0-9]{15,20})(?:>|.*?)")
    matches = split_role_ids_pattern.findall(roles_input_str)
    return [role for match in matches if (role := guild.get_role(int(match)))] if matches else []


class RadioBot(discord.AutoShardedClient):
    def __init__(self: Self) -> None:
        super().__init__(
            intents=discord.Intents.default(),  # Can be reduced later.
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-radiobot"),
        )
        self.tree = app_commands.CommandTree(self)

        # Connect to the database that will store the radio information.
        db_path = Path(config["DATABASE"]["path"])
        resolved_path_as_str = str(db_path.resolve())
        self.db_connection = apsw.Connection(resolved_path_as_str)

    async def on_connect(self: Self) -> None:
        # Create an invite link.
        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self: Self) -> None:
        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(**config["LAVALINK"])
        sc = spotify.SpotifyClient(**config["SPOTIFY"]) if ("SPOTIFY" in config) else None
        await wavelink.NodePool.connect(client=self, nodes=[node], spotify=sc)

        # Initialize the database and start the loop.
        self.radio_enabled_guilds: set[int] = await asyncio.to_thread(_setup_db, self.db_connection)
        self.radio_loop.start()

    async def close(self: Self) -> None:
        self.radio_loop.cancel()
        return await super().close()

    async def start_guild_radio(self: Self, radio_info: GuildRadioInfo) -> None:
        # Initialize the guild's specific radio voice client.
        guild: discord.Guild = self.get_guild(radio_info.guild_id)  # type: ignore # Known during runtime.
        voice_channel: discord.abc.Connectable = guild.get_channel(radio_info.channel_id)  # type: ignore # Known
        vc = await voice_channel.connect(cls=wavelink.Player)  # type: ignore # Valid class.

        # Get the playlist of the guild's registered radio station and play it on loop.
        converted = await WavelinkTrackConverter.convert(radio_info.station.playlist_link)
        if isinstance(converted, Iterable):
            for sub_item in converted:
                await vc.queue.put_wait(sub_item)
        elif isinstance(converted, AsyncIterable):
            async for sub_item in converted:
                await vc.queue.put_wait(sub_item)
        else:
            await vc.queue.put_wait(converted)

        vc.queue.loop_all = True
        if radio_info.always_shuffle:
            vc.queue.shuffle()

        await vc.play(vc.queue.get())

    @tasks.loop(seconds=10.0)
    async def radio_loop(self: Self) -> None:
        """The main loop for the radios.

        It (re)connects voice clients to voice channels and plays preset stations.
        """

        inactive_radio_guilds = [
            guild
            for guild_id in self.radio_enabled_guilds
            if (guild := self.get_guild(guild_id)) and not guild.voice_client
        ]
        radio_results = await asyncio.to_thread(
            _get_all_guilds_radio_info,
            self.db_connection,
            [(guild.id,) for guild in inactive_radio_guilds],
        )
        for radio in radio_results:
            await self.start_guild_radio(radio)

    @radio_loop.before_loop
    async def radio_loop_before(self: Self) -> None:
        await self.wait_until_ready()


bot = RadioBot()


########################
### Wavelink listeners
########################
@bot.event
async def on_wavelink_node_ready(node: wavelink.Node) -> None:
    """Called when the Node you are connecting to has initialised and successfully connected to Lavalink."""

    log.info("Wavelink node %s is ready!", node.id)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEventPayload) -> None:
    """Called when the current track has finished playing.

    Plays the next track in the queue so long as the player hasn't disconnected.
    """

    player = payload.player

    if player.is_connected():
        next_track = await player.queue.get_wait()
        await player.play(next_track)
    else:
        await player.stop()


########################
### Application commands
########################
radio_group = app_commands.Group(
    name="radio",
    description="The group of commands responsible for setting up, modifying, and using the radio.",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@radio_group.command(
    name="set",
    description="Create or update your server's radio player, specifically its location and what it will play.",
)
@app_commands.describe(
    channel="The channel the radio should automatically play in and, if necessary, reconnect to.",
    station="The 'radio station' with the music you want playing. Create your own with /station set.",
    always_shuffle="Whether the station should shuffle its internal playlist whenever it loops.",
    managing_roles="The roles that have permission to edit the server radio. Comma-separated list if more than one.",
)
async def radio_set(
    itx: discord.Interaction[RadioBot],
    channel: discord.VoiceChannel | discord.StageChannel,
    station: str,
    always_shuffle: bool = True,
    managing_roles: str | None = None,
) -> None:
    assert itx.guild  # Known quantity since this is a guild-only command.

    station_records = await asyncio.to_thread(
        _query_stations,
        itx.client.db_connection,
        SELECT_STATIONS_BY_NAME_STATEMENT,
        (station,),
    )
    if not station_records:
        await itx.response.send_message(
            "That station doesn't exist. Did you mean to select a different one or make your own?",
        )
        return
    stn_id = station_records[0].station_id
    roles = convert_list_role(managing_roles, itx.guild) if managing_roles else None

    record = await asyncio.to_thread(
        _add_radio,
        itx.client.db_connection,
        guild_id=itx.guild.id,
        channel_id=channel.id,
        station_id=stn_id,
        always_shuffle=always_shuffle,
        managing_roles=roles,
    )
    itx.client.radio_enabled_guilds.add(itx.guild.id)

    if record:
        content = f"Radio with station {record.station.station_name} set in <#{record.channel_id}>."
    else:
        content = f"Unable to set radio in {channel.mention} with station {station} at this time."
    await itx.response.send_message(content)


@radio_group.command(name="get", description="Get information about your server's current radio setup.")
async def radio_get(itx: discord.Interaction[RadioBot]) -> None:
    assert itx.guild_id  # Known quantity since this is a guild-only command.

    local_radio_results = await asyncio.to_thread(
        _get_all_guilds_radio_info,
        itx.client.db_connection,
        [(itx.guild_id,)],
    )

    if local_radio_results and (local_radio := local_radio_results[0]):
        station = local_radio.station
        embed = (
            discord.Embed(title="Current Guild's Radio")
            .add_field(name="Channel", value=f"<#{local_radio.channel_id}>")
            .add_field(name=f"Station: {station.station_name}", value=f"[Source]({station.playlist_link})")
            .add_field(name="Always Shuffle", value=("Yes" if local_radio.always_shuffle else "No"))
        )
        await itx.response.send_message(embed=embed)
    else:
        await itx.response.send_message("No radio found for this guild.")


bot.tree.add_command(radio_group)

station_group = app_commands.Group(
    name="station",
    description="The group of commands responsible for setting up, modifying, and using 'radio stations'.",
    guild_only=True,
)


@station_group.command(
    name="set",
    description="Create or edit a 'radio station' that can be used in any server with this bot.",
)
async def station_set(itx: discord.Interaction[RadioBot], station_name: str, playlist_link: str) -> None:
    records = await asyncio.to_thread(
        _query_stations,
        itx.client.db_connection,
        UPSERT_STATION_STATEMENT,
        (station_name, playlist_link, itx.user.id),
    )
    if records and (upd_stn := records[0]):
        content = f"Station {upd_stn.station_name} set to use `<{upd_stn.playlist_link}>`."
    else:
        content = f"Could not set station {station_name} at this time."
    await itx.response.send_message(content)


@station_group.command(name="info", description="Get information about an available 'radio station'.")
async def station_info(itx: discord.Interaction[RadioBot], station_name: str) -> None:
    records = await asyncio.to_thread(
        _query_stations,
        itx.client.db_connection,
        SELECT_STATIONS_BY_NAME_STATEMENT,
        (station_name,),
    )
    if records and (stn := records[0]):
        embed = (
            discord.Embed(title=f"Station {stn.station_id}: {stn.station_name}")
            .add_field(name="Source", value=f"[Here]({stn.playlist_link})")
            .add_field(name="Owner", value=f"<@{stn.owner_id}>")
        )
        await itx.response.send_message(embed=embed, ephemeral=True)
    else:
        await itx.response.send_message("No such station found.")


@radio_set.autocomplete("station")
@station_info.autocomplete("station_name")
async def station_autocomplete(itx: discord.Interaction[RadioBot], current: str) -> list[app_commands.Choice[str]]:
    stations = await asyncio.to_thread(_query_stations, itx.client.db_connection, SELECT_STATIONS_STATEMENTS)
    return [
        app_commands.Choice(name=stn.station_name, value=stn.station_name)
        for stn in stations
        if current.casefold() in stn.station_name.casefold()
    ]


@station_set.autocomplete("station_name")
async def station_set_autocomplete(itx: discord.Interaction[RadioBot], current: str) -> list[app_commands.Choice[str]]:
    stations = await asyncio.to_thread(
        _query_stations,
        itx.client.db_connection,
        SELECT_STATIONS_BY_OWNER_STATEMENT,
        (itx.user.id,),
    )
    return [
        app_commands.Choice(name=stn.station_name, value=stn.station_name)
        for stn in stations
        if current.casefold() in stn.station_name.casefold()
    ]


bot.tree.add_command(station_group)


@bot.tree.command(description="See what's currently playing on the radio.")
@app_commands.guild_only()
async def current(itx: discord.Interaction[RadioBot], level: Literal["track", "station", "radio"] = "track") -> None:
    assert itx.guild  # Known quantity in guild-only command.

    vc: wavelink.Player | None = itx.guild.voice_client  # type: ignore
    if vc:
        if level == "track" and vc.current:
            embed = await format_track_embed(discord.Embed(color=0x0389DA, title="Currently Playing"), vc.current)
            await itx.response.send_message(embed=embed, ephemeral=True)
        elif level == "station":
            pass
    else:
        await itx.response.send_message("No radio currently active in this server.")


@bot.tree.command(description="See or change the volume of the radio.")
@app_commands.guild_only()
async def volume(itx: discord.Interaction[RadioBot], volume: int | None = None) -> None:
    # Known quantities in guild-only command.
    assert itx.guild
    assert isinstance(itx.user, discord.Member)

    vc: wavelink.Player | None = itx.guild.voice_client  # type: ignore

    if vc:
        if volume is None:
            await itx.response.send_message(f"Volume is currently set to {vc.volume}.", ephemeral=True)
        else:
            raw_results = await asyncio.to_thread(
                _query,
                itx.client.db_connection,
                SELECT_ROLES_BY_GUILD_STATEMENT,
                (itx.guild.id,),
            )
            dj_role_ids = [result[1] for result in raw_results] if raw_results else None

            if (not dj_role_ids) or any((role.id in dj_role_ids) for role in itx.user.roles):
                await vc.set_volume(volume)
                await itx.response.send_message(f"Volume now changed to {vc.volume}.")
            else:
                await itx.response.send_message("You don't have permission to do this.", ephemeral=True)
    else:
        await itx.response.send_message("No radio currently active in this server.")


@bot.tree.command(description="Get a link to invite this bot to a server.")
async def invite(itx: discord.Interaction[RadioBot]) -> None:
    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


def main() -> None:
    log_sqlite()
    token: str = config["DISCORD"]["token"]
    bot.run(token)


if __name__ == "__main__":
    main()
