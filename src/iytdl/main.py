__all__ = ["iYTDL"]

import asyncio
import hashlib
import re

from pathlib import Path, WindowsPath
from typing import Optional, Tuple, Union

from aiohttp import ClientSession
from html_telegraph_poster import TelegraphPoster
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from iytdl import types
from iytdl.constants import YT_VID_URL
from iytdl.downloader import Downloader
from iytdl.exceptions import *  # noqa ignore=F405
from iytdl.extractors import Extractor
from iytdl.formatter import ResultFormatter, gen_search_markup
from iytdl.sql_cache import AioSQLiteDB
from iytdl.types import Buttons
from iytdl.upload_lib.uploader import Uploader
from iytdl.utils import run_command, run_sync


class iYTDL(Extractor, Downloader, Uploader):
    def __init__(
        self,
        log_group_id: Union[int, str],
        session: Optional[ClientSession] = None,
        silent: bool = False,
        download_path: str = "downloads",
        loop: Optional[asyncio.AbstractEventLoop] = None,
        default_thumb: str = "https://i.imgur.com/4LwPLai.png",
        cache_path: str = "",
        delete_media: bool = False,
        external_downloader: Optional[types.ExternalDownloader] = None,
        ffmpeg_location: str = "ffmpeg",
    ) -> None:
        """Main class

        Parameters:
        ----------
            - log_group_id (`Union[int, str]`):  Log channel / group id to upload downloaded media.
            - session (`Optional[ClientSession]`, optional): Aiohttp ClientSession. (Defaults to `None`)
            - silent (`bool`, optional): Disable youtube_dl stdout. (Defaults to `False`)
            - download_path (`str`, optional): Custom download location. (Defaults to `"downloads"`)
            - loop (`Optional[asyncio.AbstractEventLoop]`, optional): Event loop. (Defaults to `None`)
            - default_thumb (`str`, optional): Fallback thumbnail. (Defaults to `"https://i.imgur.com/4LwPLai.png"`)
            - cache_path (`str`, optional): Path to store cache. (Defaults to `""`)
            - delete_media: (`bool`, optional): Delete media from local storage after uploading on Telegram. (Defaults to `False`)
            - external_downloader: (`Optional[types.ExternalDownloader]`, optional): External Downloader e.g `types.external_downloader.Aria2c`. (Defaults to `None`)
            - ffmpeg_location (`str`, optional): Custom location for FFMPEG. (Defaults to `"ffmpeg"`)
        """
        self.yt_link_regex = re.compile(
            r"(?:youtube(?:-nocookie)?\.com|youtu\.be)/(?:[\w-]+\?v=|embed/|v/|shorts/)?([\w-]{11})"
        )
        self.generic_url_regex = re.compile(r"^https?://\S+")
        self.default_thumb = default_thumb
        self.http = session or ClientSession()
        _cache_path = Path(cache_path)
        _cache_path.mkdir(exist_ok=True, parents=True)
        if _cache_path.is_file():
            raise TypeError(f"'{cache_path}' expected a Directory got a File instead")
        self.cache = AioSQLiteDB(
            _cache_path.joinpath("yt_search_cache.db"), clean=False
        )
        self.loop = loop or asyncio.get_event_loop()
        self.download_path = Path(download_path)
        self.log_group_id = log_group_id

        self.download_path.mkdir(exist_ok=True, parents=True)
        self.external_downloader = external_downloader
        self.delete_file_after_upload = delete_media
        if ffmpeg_location != "ffmpeg":
            ffmpeg_location = Path(ffmpeg_location)
            if not ffmpeg_location.is_file():
                raise FileNotFoundError(ffmpeg_location)
        self._ffmpeg = ffmpeg_location
        super().__init__(silent=silent)

    @classmethod
    async def init(cls, *args, **kwargs) -> "iYTDL":
        """Utility method to create new class instance, Note: no need to start()

        Parameters:
        ----------
            - log_group_id (`Union[int, str]`):  Log channel / group id to upload downloaded media.
            - session (`Optional[ClientSession]`, optional): Aiohttp ClientSession. (Defaults to `None`)
            - silent (`bool`, optional): Disable youtube_dl stdout. (Defaults to `False`)
            - download_path (`str`, optional): Custom download location. (Defaults to `"downloads"`)
            - loop (`Optional[asyncio.AbstractEventLoop]`, optional): Event loop. (Defaults to `None`)
            - default_thumb (`str`, optional): Fallback thumbnail. (Defaults to `"https://i.imgur.com/4LwPLai.png"`)
            - cache_path (`str`, optional): Path to store cache. (Defaults to `""`)
            - delete_media: (`bool`, optional): Delete media from local storage after uploading on Telegram. (Defaults to `False`)
            - external_downloader: (`Optional[types.ExternalDownloader]`, optional): External Downloader e.g `types.external_downloader.Aria2c`. (Defaults to `None`)
            - ffmpeg_location (`str`, optional): Custom location for FFMPEG. (Defaults to `"ffmpeg"`)

        Returns:
        -------
            `~iytdl.iYTDL`
        """

        yt = cls(*args, **kwargs)
        await yt.start()
        return yt

    async def extract_info_from_key(self, key: str) -> Optional[types.SearhResult]:
        """
        Parameters:
        ----------
            - key (`str`): Unique Key.

        Returns:
        -------
            `Optional[types.SearhResult]`: If key exist in cache.
        """
        if len(key) == 11:
            # yt_id
            return await self.get_download_button(key)
        if url := await self.cache.get_url(key):
            return await self.generic_extractor(key, url)

    async def get_ytthumb(self, yt_id: str) -> str:
        """Get YouTube video thumbnail from video ID

        Parameters:
        ----------
            - yt_id (`str`): YouTube video ID.

        Returns:
        -------
            `str`: Thumbnail URL
        """
        for quality in (
            "maxresdefault",
            "hqdefault",
            "sddefault",
            "mqdefault",
            "default",
        ):
            link = f"https://i.ytimg.com/vi/{yt_id}/{quality}.jpg"
            async with self.http.get(link) as resp:
                if resp.status == 200:
                    break
        else:
            link = self.default_thumb
        return link

    async def _check_ffmpeg(self) -> None:
        if isinstance(self._ffmpeg, Path):
            ffmpeg = self._ffmpeg
            _ffprobe = self._ffmpeg.parent.joinpath(
                f"ffprobe{'.exe' if isinstance(self._ffmpeg, WindowsPath) else ''}"
            )
            ffprobe = _ffprobe if _ffprobe.is_file() else None
        else:
            ffmpeg = "ffmpeg"
            ffprobe = "ffprobe"
        out_1 = await run_command(f"{ffmpeg} -version", shell=True)
        if out_1[1] != 0:
            raise ValueError(f"'{ffmpeg}' was not Found !")
        if ffprobe is not None:
            out_2 = await run_command(f"{ffprobe} -version", shell=True, silent=True)
            if out_2[1] == 0:
                setattr(self, "_ffprobe", ffprobe)

    async def stop(self) -> None:
        """Stop iYTDL instance manually or Use Context Manager"""
        if self.http and not self.http.closed:
            await self.http.close()
        await self.cache.close()

    async def start(self) -> None:
        """Start iYTDL instance manually or Use Context Manager"""
        await self._check_ffmpeg()
        await self.cache._init()

    async def __aenter__(self) -> "iYTDL":
        await self.start()
        return self

    async def __aexit__(self, *_, **__) -> None:
        await self.stop()
