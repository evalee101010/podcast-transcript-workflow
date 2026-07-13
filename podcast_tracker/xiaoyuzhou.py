"""小宇宙 (xiaoyuzhoufm.com) page parsing.

Two entry points the rest of the app uses:

  * ``parse_episode_record(url, subscription_id=None)`` — resolve a single
    episode page (https://www.xiaoyuzhoufm.com/episode/<id>) to an Episode with
    its real .m4a audio URL. Used for "发单集链接即可转写" (历史节目).

  * ``parse_podcast_as_feed(url)`` — resolve a podcast/show page
    (https://www.xiaoyuzhoufm.com/podcast/<id>) into a ParsedFeed of episodes,
    so it plugs straight into the existing subscribe/check flow.

小宇宙 is a Next.js app: every page embeds a ``<script id="__NEXT_DATA__">`` JSON
blob with the fully hydrated data. We parse that first and fall back to OpenGraph
meta tags (og:audio / og:title) for single episodes if the blob shape changes.
"""

from __future__ import annotations

import json
import re
import urllib.request
from html.parser import HTMLParser

from .feed import FeedEpisode, ParsedFeed, stable_id
from .models import Episode, utc_now_iso

XYZ_HOST = "xiaoyuzhoufm.com"
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_xiaoyuzhou_url(url: str) -> bool:
    return XYZ_HOST in (url or "").lower()


def is_episode_url(url: str) -> bool:
    return "/episode/" in (url or "")


def is_podcast_url(url: str) -> bool:
    return "/podcast/" in (url or "")


def fetch_html(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Single episode
# --------------------------------------------------------------------------- #
def parse_episode_record(url: str, subscription_id: str | None = None) -> Episode:
    html = fetch_html(url)
    raw = _episode_from_next_data(html) or _episode_from_meta(html)
    if not raw or not raw.get("audio_url"):
        raise ValueError(f"Could not resolve audio URL from 小宇宙 page: {url}")

    program = raw.get("program") or "小宇宙播客"
    sub_id = subscription_id or stable_id("xyz", raw.get("pid") or program)
    eid = raw.get("eid") or url
    return Episode(
        id=stable_id("xyz", eid),
        subscription_id=sub_id,
        program_title=program,
        title=raw.get("title") or "未命名单集",
        source_url=url,
        audio_url=raw["audio_url"],
        published_at=raw.get("published_at"),
        created_at=utc_now_iso(),
    )


def podcast_url_from_episode_url(url: str) -> str:
    html = fetch_html(url)
    raw = _episode_from_next_data(html)
    pid = raw.get("pid") if raw else None
    if not pid:
        raise ValueError(f"Could not resolve podcast ID from 小宇宙 episode: {url}")
    return f"https://www.{XYZ_HOST}/podcast/{pid}"


# --------------------------------------------------------------------------- #
# Podcast / show -> ParsedFeed
# --------------------------------------------------------------------------- #
def parse_podcast_as_feed(url: str) -> ParsedFeed:
    html = fetch_html(url)
    data = _load_next_data(html)
    podcast = _podcast_dict(data)
    podcast_title = _podcast_title_from_dict(podcast) or "小宇宙播客"
    pid = _podcast_pid_from_dict(podcast) or url
    feed_id = stable_id("xyz", pid)

    episodes: list[FeedEpisode] = []
    for raw in _iter_episode_dicts(data):
        normalised = _normalise_episode_dict(raw)
        if not normalised or not normalised.get("audio_url"):
            continue
        eid = normalised.get("eid") or normalised["audio_url"]
        episode_url = (
            f"https://www.{XYZ_HOST}/episode/{normalised['eid']}"
            if normalised.get("eid")
            else url
        )
        episodes.append(
            FeedEpisode(
                id=stable_id("xyz", eid),
                title=normalised.get("title") or "未命名单集",
                source_url=episode_url,
                audio_url=normalised["audio_url"],
                published_at=normalised.get("published_at"),
            )
        )

    # Newest first, matching the RSS path's ordering expectations.
    episodes.sort(key=lambda item: item.published_at or "", reverse=True)
    return ParsedFeed(
        id=feed_id,
        title=podcast_title,
        feed_url=url,
        source_url=url,
        episodes=episodes,
        avatar_url=_podcast_image_url(podcast),
    )


# --------------------------------------------------------------------------- #
# __NEXT_DATA__ helpers
# --------------------------------------------------------------------------- #
def _load_next_data(html: str) -> object:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _episode_from_next_data(html: str) -> dict | None:
    data = _load_next_data(html)
    if data is None:
        return None
    raw = _find_first(
        data,
        lambda d: isinstance(d, dict)
        and isinstance(d.get("enclosure"), dict)
        and bool(d["enclosure"].get("url")),
    )
    return _normalise_episode_dict(raw) if raw else None


def _normalise_episode_dict(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    enclosure = raw.get("enclosure")
    audio_url = enclosure.get("url") if isinstance(enclosure, dict) else None
    if not audio_url:
        return None
    podcast = raw.get("podcast") if isinstance(raw.get("podcast"), dict) else {}
    return {
        "eid": raw.get("eid") or raw.get("id"),
        "title": raw.get("title"),
        "audio_url": audio_url,
        "published_at": raw.get("pubDate") or raw.get("publishDate"),
        "program": podcast.get("title"),
        "pid": podcast.get("pid") or podcast.get("id"),
    }


def _iter_episode_dicts(data: object) -> list[dict]:
    return _find_all(
        data,
        lambda d: isinstance(d, dict)
        and isinstance(d.get("enclosure"), dict)
        and bool(d["enclosure"].get("url")),
    )


def _podcast_dict(data: object) -> dict | None:
    podcast = _find_first(
        data,
        lambda d: isinstance(d, dict)
        and d.get("type") == "PODCAST"
        and bool(d.get("title")),
    )
    if isinstance(podcast, dict):
        return podcast
    any_podcast = _find_first(
        data,
        lambda d: isinstance(d, dict) and bool(d.get("title")) and "pid" in d,
    )
    return any_podcast if isinstance(any_podcast, dict) else None


def _podcast_title_from_dict(podcast: dict | None) -> str | None:
    return podcast.get("title") if isinstance(podcast, dict) else None


def _podcast_pid_from_dict(podcast: dict | None) -> str | None:
    return podcast.get("pid") if isinstance(podcast, dict) else None


def _podcast_image_url(podcast: dict | None) -> str | None:
    if not isinstance(podcast, dict):
        return None
    image = podcast.get("image")
    if not isinstance(image, dict):
        return None
    for key in ("picUrl", "largePicUrl", "middlePicUrl", "smallPicUrl", "thumbnailUrl"):
        value = image.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# --------------------------------------------------------------------------- #
# OpenGraph fallback (single episode only)
# --------------------------------------------------------------------------- #
class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        key = attr.get("property") or attr.get("name")
        content = attr.get("content")
        if key and content:
            self.meta[key.lower()] = content


def _episode_from_meta(html: str) -> dict | None:
    parser = _MetaParser()
    parser.feed(html)
    audio_url = parser.meta.get("og:audio") or parser.meta.get("twitter:player:stream")
    if not audio_url:
        return None
    return {
        "eid": None,
        "title": parser.meta.get("og:title"),
        "audio_url": audio_url,
        "published_at": None,
        "program": parser.meta.get("og:site_name"),
        "pid": None,
    }


# --------------------------------------------------------------------------- #
# Generic recursive search
# --------------------------------------------------------------------------- #
def _find_first(obj: object, predicate) -> object | None:
    stack = [obj]
    while stack:
        current = stack.pop()
        if predicate(current):
            return current
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


def _find_all(obj: object, predicate) -> list[dict]:
    found: list[dict] = []
    seen: set[int] = set()
    stack = [obj]
    while stack:
        current = stack.pop()
        if predicate(current):
            marker = id(current)
            if marker not in seen:
                seen.add(marker)
                found.append(current)  # type: ignore[arg-type]
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found
