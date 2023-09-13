"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""

from __future__ import annotations

import argparse
import asyncio
import functools
import getpass
import json
import logging
import os
from collections.abc import AsyncIterator, Iterable
from datetime import timedelta
from itertools import chain
from pathlib import Path
from typing import Any, Literal, Self, TypeAlias, cast

import apsw
import apsw.bestpractice
import attrs
import base2048
import discord
import platformdirs
import wavelink
import xxhash
import yarl
from discord.ext import tasks
from wavelink.ext import spotify


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None

RadioInfoTuple: TypeAlias = tuple[int, int, str, int]
AnyTrack: TypeAlias = wavelink.Playable | spotify.SpotifyTrack
AnyTrackIterable: TypeAlias = list[wavelink.Playable] | list[spotify.SpotifyTrack] | spotify.SpotifyAsyncIterator

# Set up logging.
apsw.bestpractice.apply(apsw.bestpractice.recommended)  # type: ignore # SQLite WAL mode, logging, and other things.
discord.utils.setup_logging()
log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-radiobot", "Sachaa-Thanasius", roaming=False)
escape_markdown = functools.partial(discord.utils.escape_markdown, as_needed=True)

MUSIC_EMOJIS: dict[type[AnyTrack], str] = {
    wavelink.YouTubeTrack: "<:youtube:1108460195270631537>",
    wavelink.YouTubeMusicTrack: "<:youtubemusic:954046930713985074>",
    wavelink.SoundCloudTrack: "<:soundcloud:1147265178505846804>",
    spotify.SpotifyTrack: "<:spotify:1108458132826501140>",
}

SPOTIFY_URL = "https://open.spotify.com/track/{}"

INITIALIZATION_STATEMENTS = """
CREATE TABLE IF NOT EXISTS guild_radios (
    guild_id        INTEGER         NOT NULL        PRIMARY KEY,
    channel_id      INTEGER         NOT NULL,
    station_link    TEXT            NOT NULL,
    always_shuffle  INTEGER         NOT NULL        DEFAULT TRUE
) STRICT, WITHOUT ROWID;
"""

SELECT_ALL_BY_GUILD_STATEMENT = """
SELECT guild_id, channel_id, station_link, always_shuffle FROM guild_radios WHERE guild_id = ?;
"""

SELECT_ENABLED_GUILDS_STATEMENT = """
SELECT guild_id FROM guild_radios;
"""

UPSERT_GUILD_RADIO_STATEMENT = """
INSERT INTO guild_radios(guild_id, channel_id, station_link, always_shuffle)
VALUES (?, ?, ?, ?)
ON CONFLICT (guild_id)
DO UPDATE
    SET channel_id = EXCLUDED.channel_id,
        station_link = EXCLUDED.station_link,
        always_shuffle = EXCLUDED.always_shuffle
RETURNING *;
"""

DELETE_RADIO_BY_GUILD_STATEMENT = """
DELETE FROM guild_radios WHERE guild_id = ?;
"""


@attrs.frozen
class GuildRadioInfo:
    guild_id: int
    channel_id: int
    station_link: str
    always_shuffle: bool

    @classmethod
    def from_row(cls: type[Self], row: RadioInfoTuple) -> Self:
        guild_id, channel_id, station_link, always_shuffle = row
        return cls(guild_id, channel_id, station_link, bool(always_shuffle))

    def display_embed(self: Self) -> discord.Embed:
        """Format the radio's information into a Discord embed."""

        return (
            discord.Embed(title="Current Guild's Radio")
            .add_field(name="Channel", value=f"<#{self.channel_id}>")
            .add_field(name="Station", value=f"[Source]({self.station_link})")
            .add_field(name="Always Shuffle", value=("Yes" if self.always_shuffle else "No"))
        )


def _setup_db(conn: apsw.Connection) -> set[int]:
    with conn:
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENTS)
        cursor.execute(SELECT_ENABLED_GUILDS_STATEMENT)
        return set(chain.from_iterable(cursor))


def _delete(conn: apsw.Connection, query_str: str, params: apsw.Bindings | None = None) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(query_str, params)


def _query(conn: apsw.Connection, guild_ids: list[tuple[int]]) -> list[GuildRadioInfo]:
    cursor = conn.cursor()
    return [GuildRadioInfo.from_row(row) for row in cursor.executemany(SELECT_ALL_BY_GUILD_STATEMENT, guild_ids)]


def _add_radio(
    conn: apsw.Connection,
    *,
    guild_id: int,
    channel_id: int,
    station_link: str,
    always_shuffle: bool,
) -> GuildRadioInfo | None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(UPSERT_GUILD_RADIO_STATEMENT, (guild_id, channel_id, station_link, always_shuffle))
        # Throws an BusyError if not done like this.
        rows = list(cursor)
        return GuildRadioInfo.from_row(rows[0]) if rows[0] else None


def resolve_path_with_links(path: Path, folder: bool = False) -> Path:
    """Resolve a path strictly with more secure default permissions, creating the path if necessary.

    Python only resolves with strict=True if the path exists.

    Source: https://github.com/mikeshardmind/discord-rolebot/blob/4374149bc75d5a0768d219101b4dc7bff3b9e38e/rolebot.py#L350
    """

    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(mode=0o700)  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


async def format_track_embed(title: str, track: AnyTrack) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    icon = MUSIC_EMOJIS.get(type(track), "\N{MUSICAL NOTE}")
    title = f"{icon} {title}"
    description_template = "[{0}]({1})\n{2}\n`[0:00-{3}]`"

    try:
        end_time = timedelta(seconds=track.duration // 1000)
    except OverflowError:
        end_time = "\N{INFINITY}"

    if isinstance(track, wavelink.Playable):
        uri = track.uri or ""
        author = escape_markdown(track.author) if track.author else ""
    else:
        uri = SPOTIFY_URL.format(track.uri.rpartition(":")[2])
        author = escape_markdown(", ".join(track.artists))

    track_title = escape_markdown(track.title)
    description = description_template.format(track_title, uri, author, end_time)

    embed = discord.Embed(color=0x0389DA, title=title, description=description)

    if isinstance(track, wavelink.YouTubeTrack):
        thumbnail = await track.fetch_thumbnail()
        embed.set_thumbnail(url=thumbnail)

    return embed


@discord.app_commands.command()
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def radio_set(
    itx: discord.Interaction[RadioBot],
    channel: discord.VoiceChannel | discord.StageChannel,
    station_link: str,
    always_shuffle: bool = True,
) -> None:
    """Create or update your server's radio player, specifically its location and what it will play.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    channel : discord.VoiceChannel | discord.StageChannel
        The channel the radio should automatically play in and, if necessary, reconnect to.
    station_link : str
        The 'radio station' you want to play in your server, e.g. a link to a playlist/audio stream.
    always_shuffle : bool, optional
        Whether the station should shuffle its internal playlist whenever it loops. By default True.
    """

    assert itx.guild  # Known at runtime.

    record = await itx.client.save_radio(
        guild_id=itx.guild.id,
        channel_id=channel.id,
        station_link=station_link,
        always_shuffle=always_shuffle,
    )

    if record:
        content = f"Radio with station {record.station_link} set in <#{record.channel_id}>."
    else:
        content = f"Unable to set radio in {channel.mention} with [this station]({station_link}) at this time."
    await itx.response.send_message(content)


@discord.app_commands.command()
@discord.app_commands.guild_only()
async def radio_get(itx: discord.Interaction[RadioBot]) -> None:
    """Get information about your server's current radio setup. May need /restart to be up to date."""

    assert itx.guild_id  # Known at runtime.

    local_radio_results = await asyncio.to_thread(_query, itx.client.db_connection, [(itx.guild_id,)])

    if local_radio_results and (local_radio := local_radio_results[0]):
        await itx.response.send_message(embed=local_radio.display_embed())
    else:
        await itx.response.send_message("No radio found for this guild.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def radio_delete(itx: discord.Interaction[RadioBot]) -> None:
    """Delete the radio for the current guild. May need /restart to be up to date."""

    assert itx.guild_id  # Known at runtime.

    await itx.client.delete_radio(itx.guild_id)
    await itx.response.send_message("If this guild had a radio, it has now been deleted.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def radio_restart(itx: discord.Interaction[RadioBot]) -> None:
    """Restart your server's radio. Acts as a reset in case you change something."""

    assert itx.guild  # Known at runtime.

    if vc := itx.guild.voice_client:
        await vc.disconnect(force=True)

    guild_radio_records = await asyncio.to_thread(_query, itx.client.db_connection, [(itx.guild.id,)])

    if guild_radio_records:
        await itx.response.send_message("Restarting radio now. Give it a few seconds to rejoin.")
    else:
        await itx.response.send_message("This server's radio does not exist. Not restarting.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
async def radio_next(itx: discord.Interaction[RadioBot]) -> None:
    """Skip to the next track. If managing roles are set, only members with those can use this command."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        await vc.stop()
        await itx.response.send_message("Skipping to next track.")
    else:
        await itx.response.send_message("No radio currently active in this server.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
async def current(itx: discord.Interaction[RadioBot], level: Literal["track", "radio"] = "track") -> None:
    """See what's currently playing on the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    level : Literal["track", "station", "radio"], optional
        What to get information about: the currently playing track, station, or radio. By default, "track".
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        if level == "track":
            if vc.current:
                embed = await format_track_embed("Currently Playing", vc.current)
                if original := vc.current_original:
                    spotify_url = SPOTIFY_URL.format(original.uri.rpartition(":")[2])
                    spotify_emoji = MUSIC_EMOJIS[spotify.SpotifyTrack]
                    embed.add_field(name=f"{spotify_emoji} Spotify Source", value=f"[Link]({spotify_url})")
            else:
                embed = discord.Embed(description="Nothing is currently playing.")
        else:
            embed = vc.radio_info.display_embed()
        await itx.response.send_message(embed=embed, ephemeral=True)
    else:
        await itx.response.send_message("No radio currently active in this server.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
async def volume(itx: discord.Interaction[RadioBot], volume: int | None = None) -> None:
    """See or change the volume of the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    volume : int | None, optional
        What to change the volume to, between 1 and 1000. Locked to managing roles if those are set. By default, None.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        if volume is None:
            await itx.response.send_message(f"Volume is currently set to {vc.volume}.", ephemeral=True)
        else:
            await vc.set_volume(volume)
            await itx.response.send_message(f"Volume now changed to {vc.volume}.")
    else:
        await itx.response.send_message("No radio currently active in this server.")


@discord.app_commands.command()
@discord.app_commands.guild_only()
async def invite(itx: discord.Interaction[RadioBot]) -> None:
    """Get a link to invite this bot to a server."""

    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


@discord.app_commands.command(name="help")
@discord.app_commands.guild_only()
async def help_(itx: discord.Interaction[RadioBot]) -> None:
    """Basic instructions for setting up your radio."""

    description = (
        "1. Create the radio for your server with `/radio_set`, using an audio streaming–capable URL "  # noqa: RUF001
        "for the 'station'.\n"
        " - If you want to edit the radio, use the same command. It will require reentering the channel, though.\n"
        "2. The bot should join the channel specified and begin playing shortly!\n\n"
        "`/radio_delete`, `/radio_restart`, and `/radio_next` are restricted by default. To change those usage "
        "permissions, use your server's Integration settings."
    )
    embed = discord.Embed(description=description)
    await itx.response.send_message(embed=embed)


APP_COMMANDS = [radio_set, radio_get, radio_delete, radio_restart, radio_next, current, volume, invite, help_]


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


class RadioQueue(wavelink.Queue):
    async def put_iterable_wait(self: Self, tracks: AnyTrack | AnyTrackIterable) -> None:
        """Put items from an iterable into the queue asynchronously using await."""

        if isinstance(tracks, Iterable):
            for sub_item in tracks:
                await self.put_wait(sub_item)
        elif isinstance(tracks, spotify.SpotifyAsyncIterator):
            # Awkward casting to satisfy pyright since wavelink isn't fully typed.
            async for sub_item in cast(AsyncIterator[spotify.SpotifyTrack], tracks):
                await self.put_wait(sub_item)
        else:
            await self.put_wait(tracks)


class RadioPlayer(wavelink.Player):
    """A wavelink player with data about the radio it represents.

    Attributes
    ----------
    radio_info
    """

    def __init__(
        self: Self,
        client: discord.Client = discord.utils.MISSING,
        channel: discord.VoiceChannel | discord.StageChannel = discord.utils.MISSING,
        *,
        nodes: list[wavelink.Node] | None = None,
        swap_node_on_disconnect: bool = True,
    ) -> None:
        super().__init__(client, channel, nodes=nodes, swap_node_on_disconnect=swap_node_on_disconnect)
        self.queue: RadioQueue = RadioQueue()
        self._current_original: spotify.SpotifyTrack | None = None

    @property
    def radio_info(self: Self) -> GuildRadioInfo:
        """`GuildRadioInfo`: A dataclass instance with information about the radio that this player is representing."""

        return self._radio_info

    @radio_info.setter
    def radio_info(self: Self, value: GuildRadioInfo) -> None:
        self._radio_info = value

    @property
    def current_original(self: Self) -> spotify.SpotifyTrack | None:
        """`spotify.SpotifyTrack | None`: If the current track is from Spotify, this will have the original metadata."""

        return self._current_original

    async def play(
        self: Self,
        track: AnyTrack,
        replace: bool = True,
        start: int | None = None,
        end: int | None = None,
        volume: int | None = None,
        *,
        populate: bool = False,
    ) -> wavelink.Playable:
        # Only for capturing the original Spotify track.
        self._current_original = track if isinstance(track, spotify.SpotifyTrack) else None
        return await super().play(track, replace, start, end, volume, populate=populate)

    async def regenerate_radio_queue(self: Self) -> None:
        """Recreate the queue based on the track link in the player's radio info."""

        self.queue.clear()
        converted = await WavelinkTrackConverter.convert(self.radio_info.station_link)
        await self.queue.put_iterable_wait(converted)
        self.queue.loop_all = True
        if self.radio_info.always_shuffle:
            self.queue.shuffle()


class VersionableTree(discord.app_commands.CommandTree):
    """A command tree with a two new methods:

    1. Generate a unique hash to represent all commands currently in the tree.
    2. Compare hash of the current tree against that of a previous version using the above method.

    Credit to @mikeshardmind: Everything in this class is his.

    Notes
    -----
    The main use case is autosyncing using the hash comparison as a condition.
    """

    async def get_hash(self: Self) -> bytes:
        commands = sorted(self._get_all_commands(guild=None), key=lambda c: c.qualified_name)

        translator = self.translator
        if translator:
            payload = [await command.get_translated_payload(translator) for command in commands]
        else:
            payload = [command.to_dict() for command in commands]

        return xxhash.xxh3_64_digest(json.dumps(payload).encode("utf-8"), seed=1)

    async def sync_if_commands_updated(self: Self) -> None:
        """Sync the tree globally if its commands are different from the tree's most recent previous version.

        Comparison is done with hashes, with the hash being stored in a specific file if unique for later comparison.

        Notes
        -----
        This uses blocking file IO, so don't run this in situations where that matters. `setup_hook()` should be fine
        a fine place though.
        """

        tree_hash = await self.get_hash()
        tree_hash_path = platformdir_info.user_cache_path / "radiobot_tree.hash"
        tree_hash_path = resolve_path_with_links(tree_hash_path)
        with tree_hash_path.open("r+b") as fp:
            data = fp.read()
            if data != tree_hash:
                log.info("New version of the command tree. Syncing now.")
                await self.sync()
                fp.seek(0)
                fp.write(tree_hash)


class RadioBot(discord.AutoShardedClient):
    """The Discord client subclass that provides radio-related functionality.

    Parameters
    ----------
    config : dict[str, Any]
        The configuration data for the radios, including Lavalink node credentials and potentially Spotify
        application credentials to allow Spotify links to work for stations.

    Attributes
    ----------
    config : dict[str, Any]
        The configuration data for the radios, including Lavalink node credentials and potentially Spotify
        application credentials to allow Spotify links to work for stations.
    """

    def __init__(self: Self, config: dict[str, Any]) -> None:
        self.config = config
        super().__init__(
            intents=discord.Intents.default(),  # Can be reduced later.
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-radiobot"),
        )

        self.tree = VersionableTree(self)

        # Connect to the database that will store the radio information.
        # -- Need to account for the directories and/or file not existing.
        db_path = platformdir_info.user_data_path / "radiobot_data.db"
        resolved_path_as_str = str(resolve_path_with_links(db_path))
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

        # Add the app commands to the tree.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

        # Sync the tree if it's different from the previous version, using hashing for comparison.
        await self.tree.sync_if_commands_updated()

    async def close(self: Self) -> None:
        self.radio_loop.cancel()
        await super().close()

    async def on_wavelink_track_end(self: Self, payload: wavelink.TrackEventPayload) -> None:
        """Called when the current track has finished playing.

        Plays the next track in the queue so long as the player hasn't disconnected.
        """

        player = payload.player
        assert isinstance(player, RadioPlayer)  # Known at runtime.

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
        assert isinstance(voice_channel, discord.VoiceChannel | discord.StageChannel)

        # This player should be compatible with discord.py's connect.
        vc = await voice_channel.connect(cls=RadioPlayer)  # type: ignore
        vc.radio_info = radio_info

        # Get the playlist of the guild's registered radio station and play it on loop.
        await vc.regenerate_radio_queue()
        await vc.play(vc.queue.get())

    @tasks.loop(seconds=10.0)
    async def radio_loop(self: Self) -> None:
        """The main loop for the radios.

        It (re)connects voice clients to voice channels and plays preset stations.
        """

        inactive_radio_guild_ids = [
            guild_id
            for guild_id in self._radio_enabled_guilds
            if (guild := self.get_guild(guild_id)) and not guild.voice_client
        ]

        radio_results = await asyncio.to_thread(
            _query,
            self.db_connection,
            [(guild_id,) for guild_id in inactive_radio_guild_ids],
        )

        for radio in radio_results:
            self.loop.create_task(self.start_guild_radio(radio))

    @radio_loop.before_loop
    async def radio_loop_before(self: Self) -> None:
        await self.wait_until_ready()

    async def save_radio(
        self: Self,
        guild_id: int,
        channel_id: int,
        station_link: str,
        always_shuffle: bool,
    ) -> GuildRadioInfo | None:
        """Create or update a radio.

        Parameters
        ----------
        guild_id : int
            The Discord ID for the guild this radio will be active in.
        channel_id : int
            The Discord ID for the channel this radio will be active in.
        station_link : int
            The URL for a playlist, track, or some other audio stream that will act as the "station".
        always_shuffle : bool
            Whether to always shuffle the station's playlist when the radio starts and as it cycles.

        Returns
        -------
        GuildRadioInfo | None
            A dataclass instance with information about the newly created or updated radio, or None if the operation
            failed.
        """

        record = await asyncio.to_thread(
            _add_radio,
            self.db_connection,
            guild_id=guild_id,
            channel_id=channel_id,
            station_link=station_link,
            always_shuffle=always_shuffle,
        )
        self._radio_enabled_guilds.add(guild_id)

        if (guild := self.get_guild(guild_id)) and isinstance((vc := guild.voice_client), RadioPlayer) and record:
            old_record = vc.radio_info
            vc.radio_info = record
            if record.station_link != old_record.station_link:
                await vc.regenerate_radio_queue()

        return record

    async def delete_radio(self: Self, guild_id: int) -> None:
        """Delete a guild's radio.

        Parameters
        ----------
        guild_id : int
            The Discord ID of the guild.
        """

        record = await asyncio.to_thread(_delete, self.db_connection, DELETE_RADIO_BY_GUILD_STATEMENT, (guild_id,))
        self._radio_enabled_guilds.discard(guild_id)

        if (guild := self.get_guild(guild_id)) and (vc := guild.voice_client):
            await vc.disconnect(force=True)

        return record


def _get_stored_credentials(filename: str) -> tuple[str, ...] | None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("r", encoding="utf-8") as fp:
        return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())


def _store_credentials(filename: str, *credentials: str) -> None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("w", encoding="utf-8") as fp:
        for cred in credentials:
            fp.write(base2048.encode(cred.encode()))
            fp.write("\n")


def _input_token() -> None:
    prompt = "Paste your discord token (won't be visible), then press enter. It will be stored for later use."
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token."
        raise RuntimeError(msg)
    _store_credentials("radiobot.token", token)


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
    _store_credentials("radiobot_lavalink.secrets", *creds)


def _input_spotify_creds() -> None:
    prompts = (
        "If you want the radio to process Spotify links, paste your Spotify app client id (won't be visible), then "
        "press enter. It will be stored for later use. Otherwise, just press enter to continue.",
        "If you previously entered a Spotify app client id, paste your corresponding app client secret, then press "
        "enter. It will be stored for later use. Otherwise, just press enter to continue.",
    )
    creds: list[str] = [secret for prompt in prompts if (secret := getpass.getpass(prompt))]
    if not creds:
        log.info("No Spotify credentials passed in. Continuing...")
        return
    if len(creds) == 1:
        msg = "If you add Spotify credentials, you must add the client ID AND the client secret, not just one."
        raise RuntimeError(msg)
    _store_credentials("radiobot_spotify.secrets", *creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_credentials("radiobot.token")
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token[0] if isinstance(token, tuple) else token


def _get_lavalink_creds() -> dict[str, str]:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = {"uri": ll_uri, "password": ll_pwd}
    elif ll_creds := _get_stored_credentials("radiobot_lavalink.secrets"):
        lavalink_creds = {"uri": ll_creds[0], "password": ll_creds[1]}
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def _get_spotify_creds() -> dict[str, str] | None:
    if (sp_client_id := os.getenv("SPOTIFY_CLIENT_ID")) and (sp_client_secret := os.getenv("SPOTIFY_CLIENT_SECRET")):
        spotify_creds = {"client_id": sp_client_id, "client_secret": sp_client_secret}
    elif sp_creds := _get_stored_credentials("radiobot_spotify.secrets"):
        spotify_creds = {"client_id": sp_creds[0], "client_secret": sp_creds[1]}
    else:
        log.warning(
            "(Optional) You're missing Spotify node credentials. Use '--spotify' in the CLI to trigger setup for it, "
            "or provide environmental variables labelled 'SPOTIFY_CLIENT_ID' and 'SPOTIFY_CLIENT_SECRET'.",
        )
        spotify_creds = None
    return spotify_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    async def bot_runner(client: RadioBot) -> None:
        async with client:
            await client.start(token, reconnect=True)

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()
    spotify_creds = _get_spotify_creds()

    config: dict[str, Any] = {"LAVALINK": lavalink_creds}
    if spotify_creds:
        config["SPOTIFY"] = spotify_creds

    client = RadioBot(config)

    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner(client))


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )
    setup_group.add_argument(
        "--token",
        action="store_true",
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
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
    os.umask(0o077)
    raise SystemExit(main())
