from __future__ import annotations

import hashlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from .models import Episode, Subscription, utc_now_iso


@dataclass(frozen=True)
class FeedEpisode:
    id: str
    title: str
    source_url: str
    audio_url: str | None
    published_at: str | None


@dataclass(frozen=True)
class ParsedFeed:
    id: str
    title: str
    feed_url: str
    source_url: str
    episodes: list[FeedEpisode]
    avatar_url: str | None = None


class FeedDiscoveryParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        attr = {key.lower(): value or "" for key, value in attrs}
        rel = attr.get("rel", "").lower()
        link_type = attr.get("type", "").lower()
        href = attr.get("href", "")
        if "alternate" in rel and ("rss" in link_type or "atom" in link_type) and href:
            self.links.append(urljoin(self.base_url, href))


def stable_id(*parts: str | None) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fetch_text(url: str, timeout: int = 20) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "podcast-tracker-mvp/0.1",
            "Accept": "application/rss+xml, application/atom+xml, text/xml, text/html, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        body = response.read()
    charset_match = re.search(r"charset=([^;]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip() if charset_match else "utf-8"
    return body.decode(charset, errors="replace"), content_type


def discover_feed(url: str) -> tuple[str, str]:
    text, content_type = fetch_text(url)
    if _looks_like_xml_feed(text, content_type):
        return url, text

    parser = FeedDiscoveryParser(url)
    parser.feed(text)
    if parser.links:
        feed_url = parser.links[0]
        feed_text, _ = fetch_text(feed_url)
        return feed_url, feed_text

    match = re.search(r"https?://[^\"'<>\s]+(?:rss|feed|xml)[^\"'<>\s]*", text, re.IGNORECASE)
    if match:
        feed_url = match.group(0)
        feed_text, _ = fetch_text(feed_url)
        return feed_url, feed_text

    raise ValueError(f"No RSS/Atom feed found for URL: {url}")


def parse_feed(feed_url: str, source_url: str, xml_text: str) -> ParsedFeed:
    root = ET.fromstring(xml_text)
    if _local_name(root.tag) == "rss":
        return _parse_rss(root, feed_url, source_url)
    if _local_name(root.tag) == "feed":
        return _parse_atom(root, feed_url, source_url)
    raise ValueError("Unsupported feed format")


def to_subscription(feed: ParsedFeed) -> Subscription:
    return Subscription(
        id=feed.id,
        title=feed.title,
        feed_url=feed.feed_url,
        source_url=feed.source_url,
        created_at=utc_now_iso(),
        latest_episode_id=feed.episodes[0].id if feed.episodes else None,
        avatar_url=feed.avatar_url,
    )


def to_episode(feed: ParsedFeed, item: FeedEpisode) -> Episode:
    return Episode(
        id=item.id,
        subscription_id=feed.id,
        program_title=feed.title,
        title=item.title,
        source_url=item.source_url,
        audio_url=item.audio_url,
        published_at=item.published_at,
        created_at=utc_now_iso(),
    )


def _parse_rss(root: ET.Element, feed_url: str, source_url: str) -> ParsedFeed:
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed missing channel")
    title = _text(channel, "title") or feed_url
    episodes: list[FeedEpisode] = []
    for item in channel.findall("item"):
        item_title = _text(item, "title") or "Untitled episode"
        link = _text(item, "link") or source_url
        guid = _text(item, "guid") or link or item_title
        audio_url = _rss_enclosure(item)
        published_at = _parse_date(_text(item, "pubDate"))
        episodes.append(
            FeedEpisode(
                id=stable_id(feed_url, guid, item_title),
                title=item_title,
                source_url=link,
                audio_url=audio_url,
                published_at=published_at,
            )
        )
    return ParsedFeed(
        id=stable_id(feed_url),
        title=title,
        feed_url=feed_url,
        source_url=source_url,
        episodes=episodes,
        avatar_url=_rss_image(channel),
    )


def _parse_atom(root: ET.Element, feed_url: str, source_url: str) -> ParsedFeed:
    title = _namespaced_text(root, "title") or feed_url
    episodes: list[FeedEpisode] = []
    for entry in root.findall("{*}entry"):
        item_title = _namespaced_text(entry, "title") or "Untitled episode"
        link = _atom_link(entry) or source_url
        entry_id = _namespaced_text(entry, "id") or link or item_title
        published_at = _parse_date(
            _namespaced_text(entry, "published") or _namespaced_text(entry, "updated")
        )
        episodes.append(
            FeedEpisode(
                id=stable_id(feed_url, entry_id, item_title),
                title=item_title,
                source_url=link,
                audio_url=_atom_enclosure(entry),
                published_at=published_at,
            )
        )
    return ParsedFeed(
        id=stable_id(feed_url),
        title=title,
        feed_url=feed_url,
        source_url=source_url,
        episodes=episodes,
        avatar_url=_atom_image(root),
    )


def _looks_like_xml_feed(text: str, content_type: str) -> bool:
    lowered_type = content_type.lower()
    stripped = text.lstrip()
    return (
        "xml" in lowered_type
        or "rss" in lowered_type
        or stripped.startswith("<?xml")
        or stripped.startswith("<rss")
        or stripped.startswith("<feed")
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(parent: ET.Element, tag: str) -> str | None:
    child = parent.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _namespaced_text(parent: ET.Element, name: str) -> str | None:
    child = parent.find(f"{{*}}{name}")
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _rss_enclosure(item: ET.Element) -> str | None:
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url")
        if url:
            return url
    for child in item:
        if _local_name(child.tag) == "content":
            url = child.attrib.get("url")
            if url:
                return url
    return None


def _rss_image(channel: ET.Element) -> str | None:
    image = channel.find("image")
    if image is not None:
        url = _text(image, "url")
        if url:
            return url
    for child in channel:
        if _local_name(child.tag) == "image":
            href = child.attrib.get("href")
            if href:
                return href
    return None


def _atom_link(entry: ET.Element) -> str | None:
    for link in entry.findall("{*}link"):
        rel = link.attrib.get("rel", "alternate")
        href = link.attrib.get("href")
        if href and rel == "alternate":
            return href
    return None


def _atom_enclosure(entry: ET.Element) -> str | None:
    for link in entry.findall("{*}link"):
        rel = link.attrib.get("rel", "")
        href = link.attrib.get("href")
        if href and rel == "enclosure":
            return href
    return None


def _atom_image(root: ET.Element) -> str | None:
    return _namespaced_text(root, "logo") or _namespaced_text(root, "icon")


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return value
