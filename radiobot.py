"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""
# TODO: Have a better way to check for DJ roles.
# TODO: Create a radio_delete command.
# TODO: Create a station_delete command.
# TODO: Debug radio_restart command.
# TODO: Check asyncio.TaskGroup provides any benefit over a for loop + loop.create_task.
# TODO: Update existing players immediately with new info if possible whenever their radio or current station changes.

from __future__ import annotations

import argparse
import asyncio
import datetime
import getpass
import logging
import os
import re
from collections.abc import AsyncIterator, Iterable
from itertools import chain
from typing import Any, Literal, Self, TypeAlias, cast

import apsw
import attrs
import base2048
import discord
import platformdirs
import wavelink
import yarl
from discord import app_commands
from discord.ext import tasks
from wavelink.ext import spotify


GuildRadioInfoTuple: TypeAlias = tuple[int, int, bool, int, str, str, int]
RadioStationTuple: TypeAlias = tuple[int, str, str, int]
AnyTrack: TypeAlias = wavelink.Playable | spotify.SpotifyTrack
AnyTrackIterable: TypeAlias = list[wavelink.Playable] | list[spotify.SpotifyTrack] | spotify.SpotifyAsyncIterator
VocalGuildChannel: TypeAlias = discord.VoiceChannel | discord.StageChannel

log = logging.getLogger(__name__)
platformdir_info = platformdirs.PlatformDirs("discord-radiobot", "Sachaa-Thanasius", roaming=False)

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
    guild_id        INTEGER         NOT NULL        PRIMARY KEY,
    station_id      INTEGER         NOT NULL,
    channel_id      INTEGER         NOT NULL,
    always_shuffle  INTEGER         NOT NULL        DEFAULT TRUE,
    FOREIGN KEY     (station_id)    REFERENCES radio_stations(station_id) ON UPDATE CASCADE ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS guild_managing_roles (
    guild_id        INTEGER     NOT NULL,
    role_id         INTEGER     NOT NULL,
    FOREIGN KEY     (guild_id)  REFERENCES guild_radios(guild_id) ON UPDATE CASCADE ON DELETE CASCADE,
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
INSERT INTO radio_stations(station_name, playlist_link, owner_id) VALUES (?, ?, ?)
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

    def display_embed(self: Self) -> discord.Embed:
        return (
            discord.Embed(title=f"Station {self.station_id}: {self.station_name}")
            .add_field(name="Source", value=f"[Here]({self.playlist_link})")
            .add_field(name="Owner", value=f"<@{self.owner_id}>")
        )


@attrs.define
class GuildRadioInfo:
    guild_id: int
    channel_id: int
    always_shuffle: bool
    station: StationInfo
    dj_roles: list[int] = attrs.Factory(list)

    @classmethod
    def from_row(cls: type[Self], row: GuildRadioInfoTuple) -> Self:
        guild_id, channel_id, always_shuffle, station_id, station_name, playlist_link, owner_id = row
        station_info = StationInfo(station_id, station_name, playlist_link, owner_id)
        return cls(guild_id, channel_id, bool(always_shuffle), station_info)

    def display_embed(self: Self) -> discord.Embed:
        return (
            discord.Embed(title="Current Guild's Radio")
            .add_field(name="Channel", value=f"<#{self.channel_id}>")
            .add_field(name=f"Station: {self.station.station_name}", value=f"[Source]({self.station.playlist_link})")
            .add_field(name="Always Shuffle", value=("Yes" if self.always_shuffle else "No"))
        )


def _setup_db(conn: apsw.Connection) -> set[int]:
    # with conn:
    cursor = conn.cursor()
    cursor.execute(INITIALIZATION_STATEMENTS)
    cursor.fetchall()  # To get rid of the ("wal",) that's returned for some reason.
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
    2. Lookup by first valid Wavelink track class if the argument matches the search/url format.
    3. Lookup by assuming argument to be a direct url or local file address.
    """

    @staticmethod
    def _get_search_type(argument: str) -> type[AnyTrack]:
        """Get the searchable wavelink class that matches the argument string closest."""

        check = yarl.URL(argument)

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
        elif check.host in ("soundcloud.com", "www.soundcloud.com") and "sets" in check.path.split("/"):
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
                tracks = search_type.iterator(query=argument)
            except TypeError:
                tracks = await search_type.search(argument)
        else:
            tracks = await search_type.search(argument)

        if not tracks:
            msg = f"Your search query `{argument}` returned no tracks."
            raise wavelink.NoTracksError(msg)

        # Still technically possible for tracks to be a Playlist subclass now.
        if issubclass(search_type, wavelink.Playable) and isinstance(tracks, list):
            tracks = tracks[0]

        return tracks


async def format_track_embed(embed: discord.Embed, track: AnyTrack) -> discord.Embed:
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


def convert_list_to_roles(roles_input_str: str, guild: discord.Guild) -> list[discord.Role]:
    split_role_ids_pattern = re.compile(r"(?:<@&|.*?)([0-9]{15,20})(?:>|.*?)")
    matches = split_role_ids_pattern.findall(roles_input_str)
    return [role for match in matches if (role := guild.get_role(int(match)))] if matches else []


async def station_autocomplete(itx: discord.Interaction[RadioBot], current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for all existing stations."""

    stations = await itx.client.fetch_all_stations()
    return [
        app_commands.Choice(name=stn.station_name, value=stn.station_name)
        for stn in stations
        if current.casefold() in stn.station_name.casefold()
    ]


async def station_set_autocomplete(itx: discord.Interaction[RadioBot], current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for all stations created by the user."""

    stations = await itx.client.fetch_owner_stations(itx.user.id)
    return [
        app_commands.Choice(name=stn.station_name, value=stn.station_name)
        for stn in stations
        if current.casefold() in stn.station_name.casefold()
    ]


class RadioGroup(app_commands.Group):
    """The group of commands responsible for setting up, modifying, and using the radio."""

    def __init__(self: Self) -> None:
        super().__init__(name="radio", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @app_commands.command(name="set")
    @app_commands.autocomplete(station=station_autocomplete)
    async def radio_set(
        self: Self,
        itx: discord.Interaction[RadioBot],
        channel: VocalGuildChannel,
        station: str,
        always_shuffle: bool = True,
        managing_roles: str | None = None,
    ) -> None:
        """Create or update your server's radio player, specifically its location and what it will play.

        Parameters
        ----------
        itx : discord.Interaction[RadioBot]
            The interaction that triggered this command.
        channel : VocalGuildChannel
            The channel the radio should automatically play in and, if necessary, reconnect to.
        station : str
            The 'radio station' with the music you want playing. Create your own with /station set.
        always_shuffle : bool, optional
            Whether the station should shuffle its internal playlist whenever it loops. By default True.
        managing_roles : str | None, optional
            The roles with enhanced server radio permissions. Comma-separated list if more than one. By default, None.
        """

        assert itx.guild  # Known quantity in guild-only command.

        station_record = await itx.client.fetch_named_station(station)
        if not station_record:
            await itx.response.send_message(
                "That station doesn't exist. Did you mean to select a different one or make your own?",
            )
            return
        stn_id = station_record.station_id
        roles = convert_list_to_roles(managing_roles, itx.guild) if managing_roles else None

        record = await itx.client.save_radio(
            guild_id=itx.guild.id,
            channel_id=channel.id,
            station_id=stn_id,
            always_shuffle=always_shuffle,
            managing_roles=roles,
        )

        if record:
            content = f"Radio with station {record.station.station_name} set in <#{record.channel_id}>."
        else:
            content = f"Unable to set radio in {channel.mention} with station {station} at this time."
        await itx.response.send_message(content)

    @app_commands.command(name="get")
    async def radio_get(self: Self, itx: discord.Interaction[RadioBot]) -> None:
        """Get information about your server's current radio setup. May need /restart to be up to date."""

        assert itx.guild_id  # Known quantity in guild-only command.

        local_radio_results = await asyncio.to_thread(
            _get_all_guilds_radio_info,
            itx.client.db_connection,
            [(itx.guild_id,)],
        )

        if local_radio_results and (local_radio := local_radio_results[0]):
            await itx.response.send_message(embed=local_radio.display_embed())
        else:
            await itx.response.send_message("No radio found for this guild.")

    @app_commands.command(name="restart")
    async def radio_restart(self: Self, itx: discord.Interaction[RadioBot]) -> None:
        """Restart your server's radio. Acts as a reset in case you change something."""

        assert itx.guild  # Known quantity in guild-only command.

        if vc := itx.guild.voice_client:
            await vc.disconnect(force=True)

        guild_radio_records = await asyncio.to_thread(
            _get_all_guilds_radio_info,
            itx.client.db_connection,
            [(itx.guild.id,)],
        )

        if guild_radio_records and (record := guild_radio_records[0]):
            await itx.client.start_guild_radio(record)
            await itx.response.send_message("Restarting radio now...")
        else:
            await itx.response.send_message("No radio found for this guild.")

    @app_commands.command(name="next")
    async def radio_next(self: Self, itx: discord.Interaction[RadioBot]) -> None:
        """Skip to the next track. If managing roles are set, only members with those can use this command."""

        assert itx.guild  # Known quantity in guild-only command.

        vc = itx.guild.voice_client
        assert isinstance(vc, RadioPlayer | None)  # Known at runtime.

        if vc:
            await vc.stop()
            await itx.response.send_message("Skipping current track...")
        else:
            await itx.response.send_message("No radio currently active in this server.")


class StationGroup(app_commands.Group):
    """The group of commands responsible for setting up, modifying, and using 'radio stations'."""

    def __init__(self: Self) -> None:
        super().__init__(name="station", guild_only=True)

    @app_commands.command(name="set")
    @app_commands.autocomplete(station_name=station_set_autocomplete)
    async def station_set(
        self: Self,
        itx: discord.Interaction[RadioBot],
        station_name: str,
        playlist_link: str,
    ) -> None:
        """Create or edit a 'radio station' that can be used in any server with this bot.

        Parameters
        ----------
        itx : discord.Interaction[RadioBot]
            The interaction that triggered this command.
        station_name : str
            The name you want the station to have.
        playlist_link : str
            The link you want the station to play a track or tracks from.
        """

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

    @app_commands.command(name="info")
    @app_commands.autocomplete(station_name=station_autocomplete)
    async def station_info(self: Self, itx: discord.Interaction[RadioBot], station_name: str) -> None:
        """Get information about an available 'radio station'.

        Parameters
        ----------
        itx : discord.Interaction[RadioBot]
            The interaction that triggered this command.
        station_name : str
            The name of the station you're looking for.
        """

        station_record = await itx.client.fetch_named_station(station_name)
        if station_record:
            await itx.response.send_message(embed=station_record.display_embed(), ephemeral=True)
        else:
            await itx.response.send_message("No such station found.")


@app_commands.command()
@app_commands.guild_only()
async def current(itx: discord.Interaction[RadioBot], level: Literal["track", "station", "radio"] = "track") -> None:
    """See what's currently playing on the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    level : Literal["track", "station", "radio"], optional
        What to get information about: the currently playing track, station, or radio. By default, "track".
    """

    assert itx.guild  # Known quantity in guild-only command.

    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)  # Known at runtime.

    if vc:
        if level == "track":
            if vc.current:
                embed = await format_track_embed(discord.Embed(color=0x0389DA, title="Currently Playing"), vc.current)
            else:
                embed = discord.Embed(description="Nothing is currently playing.")
        elif level == "station":
            embed = vc.station_info.display_embed()
        else:
            embed = vc.radio_info.display_embed()
        await itx.response.send_message(embed=embed, ephemeral=True)
    else:
        await itx.response.send_message("No radio currently active in this server.")


@app_commands.command()
@app_commands.guild_only()
async def volume(itx: discord.Interaction[RadioBot], volume: int | None = None) -> None:
    """See or change the volume of the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    volume : int | None, optional
        What to change the volume to, between 1 and 1000. Locked to managing roles if those are set. By default, None.
    """

    # Known quantities in guild-only command.
    assert itx.guild
    assert isinstance(itx.user, discord.Member)

    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)  # Known at runtime.

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


@app_commands.command()
async def invite(itx: discord.Interaction[RadioBot]) -> None:
    """Get a link to invite this bot to a server."""

    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


@app_commands.command()
async def setup_help(itx: discord.Interaction[RadioBot]) -> None:
    """Basic instructions for setting up your radio."""

    description = (
        "1. If you want a custom radio station, create one with a specific song/playlist link via `/station set`.\n"
        "2. Create the radio for your server with /radio set, using the name of a preexisting station or one you must "
        "made.\n"
        "3. The bot should join the channel specified in Step 2 and begin playing shortly!"
    )
    embed = discord.Embed(description=description)
    await itx.response.send_message(embed=embed)


APP_COMMANDS = [RadioGroup(), StationGroup(), current, volume, invite, setup_help]


class RadioPlayer(wavelink.Player):
    """A wavelink player with data about the radio it represents.

    Parameters
    ----------
    args
        The positional arguments for the parent `wavelink.Player`. See that class for more information.
    radio_info : GuildRadioInfo
        A dataclass instance with information about the radio that this player is representing.
    kwargs
        The keyword arguments for the parent `wavelink.Player`. See that class for more information.

    Attributes
    ----------
    radio_info : GuildRadioInfo
        A dataclass instance with information about the radio that this player is representing.
    station_info
    """

    def __init__(self: Self, *args: Any, radio_info: GuildRadioInfo, **kwargs: Any) -> None:
        self.radio_info = radio_info
        super().__init__(*args, *kwargs)

    @property
    def station_info(self: Self) -> StationInfo:
        """`StationInfo`: A shortcut to access information about the station this player is streaming with."""

        return self.radio_info.station


class RadioBot(discord.AutoShardedClient):
    """The Discord client subclass that provides radio-related functionality.

    Parameters
    ----------
    config : dict[str, Any]
        The configuration data for the radios, including the client's token, credentials for a Lavalink node, a database
        storage path, and potentially Spotify application credentials to allow Spotify links to work for stations.

    Attributes
    ----------
    config : dict[str, Any]
        The configuration data for the radios, including credentials for a Lavalink node and potentially Spotify
        application credentials to allow Spotify links to work for stations.
    """

    def __init__(self: Self, config: dict[str, Any]) -> None:
        self.config = config
        super().__init__(
            intents=discord.Intents.default(),  # Can be reduced later.
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-radiobot"),
        )

        self.tree = app_commands.CommandTree(self)

        # Connect to the database that will store the radio information.
        # -- Need to account for the directories and/or file not existing.
        data_path = platformdir_info.user_data_path
        data_path.mkdir(parents=True, exist_ok=True)
        db_path = data_path / "radiobot_data.db"
        resolved_path_as_str = str(db_path.resolve())
        self.db_connection = apsw.Connection(resolved_path_as_str)

    async def on_connect(self: Self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self: Self) -> None:
        """Perform a few operations before the bot connects to the Discord Gateway."""

        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(**self.config["LAVALINK"])
        sc = spotify.SpotifyClient(**self.config["SPOTIFY"]) if ("SPOTIFY" in self.config) else None
        await wavelink.NodePool.connect(client=self, nodes=[node], spotify=sc)

        # Initialize the database and start the loop.
        self._radio_enabled_guilds: set[int] = await asyncio.to_thread(_setup_db, self.db_connection)
        self.radio_loop.start()

        # Add the app commands to the tree and sync it.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

        # In production, this should rarely run, so it's probably fine to automate it.
        await self.tree.sync()

    async def close(self: Self) -> None:
        self.radio_loop.cancel()
        return await super().close()

    async def on_wavelink_track_end(self: Self, payload: wavelink.TrackEventPayload) -> None:
        """Called when the current track has finished playing.

        Plays the next track in the queue so long as the player hasn't disconnected.
        """

        player = payload.player
        assert isinstance(player, RadioPlayer)

        if player.is_connected():
            queue_length_before = len(player.queue)
            try:
                next_track = player.queue.get()
            except wavelink.QueueEmpty:
                assert player.channel  # Known at runtime.
                await player.channel.send("Something went wrong with the current station. Stopping now.")
                await player.stop()
            else:
                await player.play(next_track)
                if queue_length_before == 1 and player.radio_info.always_shuffle:
                    player.queue.shuffle()
        else:
            await player.stop()

    async def start_guild_radio(self: Self, radio_info: GuildRadioInfo) -> None:
        """Create a radio voice client for a guild and start its preset station playlist.

        Parameters
        ----------
        radio_info : GuildRadioInfo
            A dataclass instance with the guild radio's settings.
        """

        # Initialize a guild's radio voice client.
        guild = self.get_guild(radio_info.guild_id)
        if not guild:
            return

        voice_channel = guild.get_channel(radio_info.channel_id)
        assert isinstance(voice_channel, VocalGuildChannel)

        # This player should be compatible with discord.py's connect.
        player = RadioPlayer(radio_info=radio_info)
        vc = await voice_channel.connect(cls=player)  # pyright: ignore [reportGeneralTypeIssues] # Wavelink typing

        # Get the playlist of the guild's registered radio station and play it on loop.
        converted = await WavelinkTrackConverter.convert(radio_info.station.playlist_link)
        if isinstance(converted, Iterable):
            for sub_item in converted:
                await vc.queue.put_wait(sub_item)
        elif isinstance(converted, spotify.SpotifyAsyncIterator):
            # Awkward casting to satisfy pyright since wavelink isn't fully typed.
            async for sub_item in cast(AsyncIterator[spotify.SpotifyTrack], converted):
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
            for guild_id in self._radio_enabled_guilds
            if (guild := self.get_guild(guild_id)) and not guild.voice_client
        ]

        radio_results = await asyncio.to_thread(
            _get_all_guilds_radio_info,
            self.db_connection,
            [(guild.id,) for guild in inactive_radio_guilds],
        )

        async with asyncio.TaskGroup() as tg:
            for radio in radio_results:
                tg.create_task(self.start_guild_radio(radio))

    @radio_loop.before_loop
    async def radio_loop_before(self: Self) -> None:
        await self.wait_until_ready()

    async def fetch_all_stations(self: Self) -> list[StationInfo]:
        """Fetch all existing radio stations.

        Returns
        -------
        list[StationInfo]
            A list of dataclasses instances with information about each station.
        """

        return await asyncio.to_thread(_query_stations, self.db_connection, SELECT_STATIONS_STATEMENTS)

    async def fetch_owner_stations(self: Self, owner_id: int) -> list[StationInfo]:
        """Fetch all existing radio stations created by a given Discord User.

        Parameters
        ----------
        owner_id : int
            The Discord ID of the person that created the stations.

        Returns
        -------
        list[StationInfo]
            A list of dataclasses instances with information about each station.
        """

        return await asyncio.to_thread(
            _query_stations,
            self.db_connection,
            SELECT_STATIONS_BY_OWNER_STATEMENT,
            (owner_id,),
        )

    async def fetch_named_station(self: Self, station_name: str) -> StationInfo | None:
        """Fetch the radio station with a specific name.

        Parameters
        ----------
        station_name : str
            The name of the station.

        Returns
        -------
        StationInfo | None
            A dataclass instance with information about the station, or None if not found.
        """

        records = await asyncio.to_thread(
            _query_stations,
            self.db_connection,
            SELECT_STATIONS_BY_NAME_STATEMENT,
            (station_name,),
        )
        return records[0] if records else None

    async def save_radio(
        self: Self,
        guild_id: int,
        channel_id: int,
        station_id: int,
        always_shuffle: bool,
        managing_roles: list[discord.Role] | None,
    ) -> GuildRadioInfo | None:
        """Create or update a radio.

        Parameters
        ----------
        guild_id : int
            The Discord ID for the guild this radio will be active in.
        channel_id : int
            The Discord ID for the channel this radio will be active in.
        station_id : int
            The ID of the radio's new station.
        always_shuffle : bool
            Whether to always shuffle the station's playlist when the radio starts and as it cycles.
        managing_roles : list[discord.Role] | None
            The Discord roles whose members are allowed to change radio and station settings within this guild.

        Returns
        -------
        GuildRadioInfo | None
            A dataclass instance with information about the newly created or updated radio, or None if the operation
            failed.
        """

        self._radio_enabled_guilds.add(guild_id)
        return await asyncio.to_thread(
            _add_radio,
            self.db_connection,
            guild_id=guild_id,
            channel_id=channel_id,
            station_id=station_id,
            always_shuffle=always_shuffle,
            managing_roles=managing_roles,
        )


def _get_stored_token() -> str | None:
    secret_file_path = platformdir_info.user_config_path / "radiobot.token"
    if secret_file_path.exists():
        with secret_file_path.open("r", encoding="utf-8") as fp:
            return base2048.decode(fp.read()).decode("utf-8")
    return None


def _get_stored_lavalink_creds() -> tuple[str, str] | None:
    secret_file_path = platformdir_info.user_config_path / "lavalink.secrets"
    if secret_file_path.exists():
        with secret_file_path.open("r", encoding="utf-8") as fp:
            return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())
    return None


def _get_stored_spotify_creds() -> tuple[str, str] | None:
    secret_file_path = platformdir_info.user_config_path / "spotify.secrets"
    if secret_file_path.exists():
        with secret_file_path.open("r", encoding="utf-8") as fp:
            return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())
    return None


def _store_token(token: str, /) -> None:
    platformdir_info.user_config_path.mkdir(parents=True, exist_ok=True)
    token_file_path = platformdir_info.user_config_path / "radiobot.token"
    with token_file_path.open("w", encoding="utf-8") as fp:
        fp.write(base2048.encode(token.encode()))


def _store_lavalink_creds(uri: str, password: str, /) -> None:
    platformdir_info.user_config_path.mkdir(parents=True, exist_ok=True)
    secrets_file_path = platformdir_info.user_config_path / "lavalink.secrets"
    with secrets_file_path.open("w", encoding="utf-8") as fp:
        fp.write(base2048.encode(uri.encode()))
        fp.write("\n")
        fp.write(base2048.encode(password.encode()))


def _store_spotify_creds(client_id: str, client_secret: str, /) -> None:
    platformdir_info.user_config_path.mkdir(parents=True, exist_ok=True)
    secrets_file_path = platformdir_info.user_config_path / "spotify.secrets"
    with secrets_file_path.open("w", encoding="utf-8") as fp:
        fp.write(base2048.encode(client_id.encode()))
        fp.write("\n")
        fp.write(base2048.encode(client_secret.encode()))


def _input_token() -> None:
    prompt = "Paste your discord token (won't be visible), then press enter. It will be stored for later use."
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token."
        raise RuntimeError(msg)
    _store_token(token)


def _input_lavalink_creds() -> None:
    prompts = (
        "Paste your Lavalink node URI (won't be visible), then press enter. It will be stored for later use.",
        "Paste your Lavalink node password (won't be visible), then press enter. It will be stored for later use.",
    )
    creds: list[str] = []
    for prompt in prompts:
        secret = getpass.getpass(prompt)
        if not secret:
            msg = "Not storing empty lavalink cred."
            raise RuntimeError(msg)
        creds.append(secret)
    _store_lavalink_creds(*creds)


def _input_spotify_creds() -> None:
    prompts = (
        "If you want the radio to process Spotify links, paste your Spotify app client id (won't be visible), then "
        "press enter. It will be stored for later use. Otherwise, just press enter to continue.",
        "If you previously entered a Spotify app client id, paste your corresponding app client secret, then press "
        "enter. It will be stored for later use. Otherwise, just press enter to continue.",
    )
    creds: list[str] = [secret for prompt in prompts if (secret := getpass.getpass(prompt))]
    if not creds:
        print("No Spotify credentials passed in. Continuing...")  # noqa: T201
        return
    if len(creds) == 1:
        msg = "If you add Spotify credentials, you must add the client ID AND the client secret, not just one."
        raise RuntimeError(msg)
    _store_spotify_creds(*creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_token()
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token


def _get_lavalink_creds() -> tuple[str, str]:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = (ll_uri, ll_pwd)
    elif ll_creds := _get_stored_lavalink_creds():
        lavalink_creds = ll_creds
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def _get_spotify_creds() -> tuple[str, str] | None:
    if (sp_client_id := os.getenv("SPOTIFY_CLIENT_ID")) and (sp_client_secret := os.getenv("SPOTIFY_CLIENT_SECRET")):
        spotify_creds = (sp_client_id, sp_client_secret)
    elif sp_creds := _get_stored_spotify_creds():
        spotify_creds = sp_creds
    else:
        log.warning(
            "(Optional) You're missing Spotify node credentials. Use '--spotify' in the CLI to trigger setup for it, "
            "or provide environmental variables labelled 'SPOTIFY_CLIENT_ID' and 'SPOTIFY_CLIENT_SECRET'.",
        )
        spotify_creds = None
    return spotify_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()
    spotify_creds = _get_spotify_creds()

    config: dict[str, Any] = {"LAVALINK": {"uri": lavalink_creds[0], "password": lavalink_creds[1]}}
    if spotify_creds:
        config["SPOTIFY"] = {"client_id": spotify_creds[0], "client_secret": spotify_creds[1]}

    client = RadioBot(config)
    client.run(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )

    token_req = bool(_get_stored_token())
    setup_group.add_argument(
        "--token",
        action="store_true",
        default=not token_req,
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )

    lavalink_req = bool(_get_stored_lavalink_creds())
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
        default=not lavalink_req,
        help="Whether you want to specify the Lavalink node URI.",
        dest="specify_lavalink",
    )

    spotify_help = (
        "Whether to specify your Spotify app's credentials (required to use Spotify links in stations). "
        "Initiates interactive setup."
    )
    setup_group.add_argument("--spotify", action="store_true", help=spotify_help, dest="specify_spotify")

    args = parser.parse_args()

    if args.specify_token:
        _input_token()
    if args.specify_lavalink:
        _input_lavalink_creds()
    if args.specify_spotify:
        _input_spotify_creds()

    run_client()


if __name__ == "__main__":
    raise SystemExit(main())
