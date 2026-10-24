"""Pixiv 免登录检索：人气门槛标签、画幅/收藏过滤、大图地址。全部离线。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import imagesearch as im


class Cfg:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)


def row(iid, *, w=1600, h=2000, xr=0, ai=0, tags=("初音ミク",)):
    return {"id": str(iid), "width": w, "height": h, "xRestrict": xr,
            "aiType": ai, "tags": list(tags), "userName": "作者", "title": "t"}


def detail(**over):
    d = {"url": "https://i.pximg.net/img-master/img/x_p0_master1200.jpg",
         "thumb": "https://i.pximg.net/c/540x540_70/x.jpg",
         "title": "标题", "author": "作者", "tags": ["初音ミク"],
         "xRestrict": 0, "bookmarks": 1000, "width": 1600, "height": 2000}
    d.update(over)
    return d


CFG = {"tools.pixiv_min_side": 1000, "tools.pixiv_exclude_ai": True,
       "tools.pixiv_candidate_limit": 30, "tools.image_timeout": 5}


class DetailTests(unittest.TestCase):
    def test_prefers_regular_over_large(self):
        payload = {"body": {"title": "t", "userName": "u", "xRestrict": 0,
                            "bookmarkCount": 5000, "width": 2000, "height": 3000,
                            "urls": {"large": "https://i.pximg.net/c/540x540_70/a.jpg",
                                     "regular": "https://i.pximg.net/img-master/a_p0_master1200.jpg",
                                     "small": "https://i.pximg.net/c/540/b.jpg"},
                            "tags": {"tags": [{"tag": "初音ミク"}]}}}

        class Resp(str):
            def decode(self, *a):
                return str(self)

        import json as _json
        with patch.object(im.websearch, "_open", return_value=_json.dumps(payload)):
            d = im.pixiv_detail("1")
        self.assertIn("master1200", d["url"])
        self.assertIn("540x540", d["thumb"])
        self.assertEqual(d["bookmarks"], 5000)
        self.assertEqual(d["width"], 2000)


class SearchTests(unittest.TestCase):
    def test_first_tier_fills_pool_and_other_tiers_skipped(self):
        calls = []

        def page(word, p, timeout=20):
            calls.append((word, p))
            return [row(1000 + p * 10 + i) for i in range(40)]

        with patch.object(im, "_pixiv_search_page", side_effect=page), \
                patch.object(im, "pixiv_detail", side_effect=lambda iid, *a: detail()):
            out = im.search_pixiv_public("初音ミク", Cfg(CFG), 5)
        self.assertEqual(len(out), 5)
        self.assertTrue(all(w.endswith("10000users入り") for w, _ in calls))

    def test_tiers_widen_when_high_tier_is_thin(self):
        words = []

        def page(word, p, timeout=20):
            words.append(word)
            return [row(2000 + int(p))] if "10000" not in word and "5000" not in word else []

        with patch.object(im, "_pixiv_search_page", side_effect=page), \
                patch.object(im, "pixiv_detail", side_effect=lambda iid, *a: detail()):
            out = im.search_pixiv_public("初音ミク", Cfg(CFG), 1)
        self.assertEqual(len(out), 1)
        self.assertIn("初音ミク 1000users入り", words)

    def test_filters_r18_small_and_ai(self):
        rows = [row(1, xr=1), row(2, w=500, h=600), row(3, ai=2), row(4)]

        def page(word, p, timeout=20):
            return rows if p == 1 else []

        with patch.object(im, "_pixiv_search_page", side_effect=page), \
                patch.object(im, "pixiv_detail", side_effect=lambda iid, *a: detail()):
            out = im.search_pixiv_public("x", Cfg(CFG), 5)
        self.assertEqual([x["page"].rsplit("/", 1)[-1] for x in out], ["4"])

    def test_ai_tag_in_detail_is_dropped(self):
        with patch.object(im, "_pixiv_search_page", return_value=[row(1), row(2)]), \
                patch.object(im, "pixiv_detail",
                             side_effect=lambda iid, *a: detail(tags=["AI生成", "初音ミク"])
                             if iid == "1" else detail()):
            out = im.search_pixiv_public("x", Cfg(CFG), 5)
        self.assertEqual(len(out), 1)

    def test_ranking_used_for_home(self):
        with patch.object(im, "_pixiv_ranking",
                          return_value=[{"illust_id": 7}, {"illust_id": 8}]) as rank, \
                patch.object(im, "_pixiv_search_page") as page, \
                patch.object(im, "pixiv_detail", side_effect=lambda iid, *a: detail()):
            out = im.search_pixiv_public("", Cfg(CFG), 2, home=True)
        rank.assert_called()
        page.assert_not_called()
        self.assertEqual(len(out), 2)

    def test_rank_prefers_popular_tier(self):
        items = [{"bookmarks": b, "page": str(b)} for b in (10, 6000, 1200, 20000)]
        out = im._rank_by_popularity(list(items), 2)
        self.assertEqual([x["bookmarks"] for x in out], [20000, 6000])

    def test_rank_relaxes_when_not_enough_popular(self):
        items = [{"bookmarks": b, "page": str(b)} for b in (10, 40, 600)]
        out = im._rank_by_popularity(list(items), 3)
        self.assertEqual([x["bookmarks"] for x in out], [600, 40, 10])

    def test_empty_query_without_home_returns_empty(self):
        self.assertEqual(im.search_pixiv_public("  ", Cfg(CFG), 3), [])

    def test_items_carry_referer_and_page(self):
        with patch.object(im, "_pixiv_search_page", return_value=[row(42)]), \
                patch.object(im, "pixiv_detail", side_effect=lambda iid, *a: detail()):
            out = im.search_pixiv_public("x", Cfg(CFG), 1)
        self.assertTrue(out[0]["need_referer"])
        self.assertEqual(out[0]["page"], "https://www.pixiv.net/artworks/42")
        self.assertEqual(out[0]["rating"], "sfw")


if __name__ == "__main__":
    unittest.main()
