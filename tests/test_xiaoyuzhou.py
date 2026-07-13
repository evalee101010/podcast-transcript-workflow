import json
import unittest

from podcast_tracker import xiaoyuzhou


def _episode_html() -> str:
    data = {
        "props": {
            "pageProps": {
                "episode": {
                    "eid": "6a275ed57444b5722235a897",
                    "title": "测试单集标题",
                    "pubDate": "2026-06-10T16:00:00.000Z",
                    "enclosure": {"url": "https://media.xyzcdn.net/abc/test.m4a"},
                    "podcast": {"pid": "pid123", "title": "测试节目", "type": "PODCAST"},
                }
            }
        }
    }
    blob = json.dumps(data, ensure_ascii=False)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


def _podcast_html() -> str:
    data = {
        "props": {
            "pageProps": {
                "podcast": {
                    "pid": "pid123",
                    "title": "测试节目",
                    "type": "PODCAST",
                    "image": {"picUrl": "https://image.xyzcdn.net/test.png"},
                },
                "episodes": [
                    {
                        "eid": "ep1",
                        "title": "第一期",
                        "pubDate": "2026-06-01T00:00:00.000Z",
                        "enclosure": {"url": "https://media.xyzcdn.net/1.m4a"},
                        "podcast": {"pid": "pid123", "title": "测试节目"},
                    },
                    {
                        "eid": "ep2",
                        "title": "第二期",
                        "pubDate": "2026-06-08T00:00:00.000Z",
                        "enclosure": {"url": "https://media.xyzcdn.net/2.m4a"},
                        "podcast": {"pid": "pid123", "title": "测试节目"},
                    },
                ],
            }
        }
    }
    blob = json.dumps(data, ensure_ascii=False)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


class XiaoyuzhouTests(unittest.TestCase):
    def test_url_helpers(self) -> None:
        self.assertTrue(xiaoyuzhou.is_xiaoyuzhou_url("https://www.xiaoyuzhoufm.com/episode/x"))
        self.assertTrue(xiaoyuzhou.is_episode_url("https://www.xiaoyuzhoufm.com/episode/x"))
        self.assertFalse(xiaoyuzhou.is_episode_url("https://www.xiaoyuzhoufm.com/podcast/x"))

    def test_parse_episode_record(self) -> None:
        url = "https://www.xiaoyuzhoufm.com/episode/6a275ed57444b5722235a897"
        xiaoyuzhou.fetch_html = lambda _u, timeout=25: _episode_html()  # type: ignore
        try:
            episode = xiaoyuzhou.parse_episode_record(url)
        finally:
            pass
        self.assertEqual(episode.title, "测试单集标题")
        self.assertEqual(episode.audio_url, "https://media.xyzcdn.net/abc/test.m4a")
        self.assertEqual(episode.program_title, "测试节目")
        self.assertEqual(episode.source_url, url)
        self.assertEqual(episode.published_at, "2026-06-10T16:00:00.000Z")

    def test_parse_podcast_as_feed_orders_newest_first(self) -> None:
        url = "https://www.xiaoyuzhoufm.com/podcast/pid123"
        xiaoyuzhou.fetch_html = lambda _u, timeout=25: _podcast_html()  # type: ignore
        feed = xiaoyuzhou.parse_podcast_as_feed(url)
        self.assertEqual(feed.title, "测试节目")
        self.assertEqual(feed.avatar_url, "https://image.xyzcdn.net/test.png")
        self.assertEqual(len(feed.episodes), 2)
        self.assertEqual(feed.episodes[0].title, "第二期")  # newest first
        self.assertTrue(feed.episodes[0].audio_url.endswith("2.m4a"))

    def test_podcast_url_from_episode_url(self) -> None:
        url = "https://www.xiaoyuzhoufm.com/episode/6a275ed57444b5722235a897"
        xiaoyuzhou.fetch_html = lambda _u, timeout=25: _episode_html()  # type: ignore
        podcast_url = xiaoyuzhou.podcast_url_from_episode_url(url)
        self.assertEqual(podcast_url, "https://www.xiaoyuzhoufm.com/podcast/pid123")

    def test_og_meta_fallback(self) -> None:
        html = (
            '<html><head>'
            '<meta property="og:audio" content="https://media.xyzcdn.net/fallback.m4a">'
            '<meta property="og:title" content="备用标题">'
            "</head></html>"
        )
        result = xiaoyuzhou._episode_from_meta(html)
        self.assertIsNotNone(result)
        self.assertEqual(result["audio_url"], "https://media.xyzcdn.net/fallback.m4a")
        self.assertEqual(result["title"], "备用标题")


if __name__ == "__main__":
    unittest.main()
