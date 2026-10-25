"""图库与 QQ 空间读写测试（离线，不连 NapCat、不调模型）。

    PYTHONIOENCODING=utf-8 py -m unittest tools.test_album tools.test_qzone -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from pawbot.album import Album
from pawbot.qzone import Qzone

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


class 假配置(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class 假事件:
    def __init__(self, uid="1000000001", group_id=None):
        self.type = "message"
        self.message_type = "group" if group_id else "private"
        self.user_id = uid
        self.group_id = group_id


class 假Ctx:
    def __init__(self, uid="1000000001", group_id=None):
        self.event = 假事件(uid, group_id)
        self.is_group = bool(group_id)


class 假Conn:
    """`支持` 里的动作正常回包，其余一律抛错（模拟「后端没有这个接口」）。"""

    def __init__(self, 支持=None, 回包=None):
        self.支持 = set(支持 or [])
        self.回包 = 回包 or {}
        self.调用记录 = []

    async def call(self, action, **kwargs):
        self.调用记录.append((action, kwargs))
        if action not in self.支持:
            raise RuntimeError(f"unknown action {action}")
        return self.回包.get(action, {"status": "ok", "retcode": 0, "data": {}})


class 假Bot:
    def __init__(self, base_dir, conn=None, **cfg):
        self.base_dir = base_dir
        self.config = 假配置({
            "qzone.enabled": True,
            "qzone.enforce_limits": False,
            "qzone.owner_ids": ["1000000001"],
            "qzone.daily_limit": 3,
            "qzone.ugc_right": 1,
            "album.dir": os.path.join(base_dir, "album"),
            **cfg,
        })
        self._conn = conn

    def conn_for(self, self_id=None):
        return self._conn


# ------------------------------------------------------------------ 图库
class 图库测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.img = os.path.join(self.dir, "a.png")
        with open(self.img, "wb") as f:
            f.write(PNG)
        self.库 = Album(self.dir, 假配置({"album.dir": os.path.join(self.dir, "album")}))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_必须有描述(self):
        with self.assertRaises(ValueError) as 上:
            self.库.add(self.img, "   ")
        self.assertIn("描述", str(上.exception))

    def test_文件不存在要拒绝(self):
        with self.assertRaises(ValueError):
            self.库.add(os.path.join(self.dir, "没有这个.png"), "一张图")

    def test_收图后能按描述找到(self):
        图id = self.库.add(self.img, "熊猫头举牌写「在吗」，用来催人回话", ["熊猫头", "催人"])
        self.assertTrue(图id)
        命中 = self.库.search("熊猫头 催人", 3)
        self.assertTrue(命中)
        self.assertEqual(命中[0]["id"], 图id)
        self.assertIn("在吗", 命中[0]["desc"])

    def test_取描述不碰视觉模型(self):
        # desc() 是纯本地读：这里没有任何视觉依赖，能读到就说明没走模型
        图id = self.库.add(self.img, "蓝色鲸鱼贴纸", [])
        self.assertEqual(self.库.desc(图id), "蓝色鲸鱼贴纸")
        self.assertEqual(self.库.desc("不存在的id"), "")

    def test_搜索不到就空(self):
        self.库.add(self.img, "熊猫头", [])
        self.assertEqual(self.库.search("完全无关的词组", 3), [])

    def test_删除同时清文件(self):
        图id = self.库.add(self.img, "要删的图", [])
        路径 = self.库.get(图id)["path"]
        self.assertTrue(os.path.isfile(路径))
        self.assertTrue(self.库.remove(图id))
        self.assertFalse(os.path.isfile(路径))
        self.assertEqual(self.库.search("要删的图", 3), [])

    def test_索引可重载(self):
        图id = self.库.add(self.img, "重启后还在", [])
        新库 = Album(self.dir, 假配置({"album.dir": os.path.join(self.dir, "album")}))
        self.assertEqual(新库.desc(图id), "重启后还在")


# ------------------------------------------------------------------ 空间
class 空间测试(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_接口不支持时降级不抛异常(self):
        bot = 假Bot(self.dir, 假Conn(支持=set()))     # 一个都不支持
        qz = Qzone(bot)
        读 = await qz.list_own(ctx=假Ctx())
        self.assertFalse(读["ok"])
        self.assertIn("不支持", 读["reason"])
        留言 = await qz.comments("t1", ctx=假Ctx())
        self.assertFalse(留言["ok"])
        回 = await qz.reply("t1", "c1", "在", ctx=假Ctx())
        self.assertFalse(回["ok"])
        self.assertEqual(读.get("items"), [])

    async def test_支持时能读说说(self):
        回包 = {"get_qzone_msg_list": {"status": "ok", "data": {"list": [
            {"tid": "t1", "content": "今天吃了火锅", "created_time": 1792730602,
             "images": ["a.jpg"]},
            {"tid": "t2", "content": "晚安", "created_time": 1792730000},
        ]}}}
        bot = 假Bot(self.dir, 假Conn(支持={"get_qzone_msg_list"}, 回包=回包))
        读 = await Qzone(bot).list_own(10, ctx=假Ctx())
        self.assertTrue(读["ok"])
        self.assertEqual(读["count"], 2)
        self.assertEqual(读["items"][0]["tid"], "t1")
        self.assertEqual(读["items"][0]["content"], "今天吃了火锅")

    async def test_读留言同时给正文和全部留言(self):
        回包 = {
            "get_qzone_msg": {"status": "ok"},
            "get_qzone_comments": {"status": "ok", "data": {
                "content": "今天去海边了",
                "comments": [
                    {"commentid": "c1", "nickname": "阿明", "content": "好看"},
                    {"commentid": "c2", "nickname": "小鱼", "content": "下次带我"},
                ],
            }},
        }
        bot = 假Bot(self.dir, 假Conn(支持={"get_qzone_comments"}, 回包=回包))
        果 = await Qzone(bot).comments("t1", ctx=假Ctx())
        self.assertTrue(果["ok"])
        self.assertEqual(果["content"], "今天去海边了")
        self.assertEqual(果["count"], 2)
        self.assertEqual([c["id"] for c in 果["comments"]], ["c1", "c2"])

    async def test_发说说会把图传下去(self):
        conn = 假Conn(支持={"send_qzone_msg"}, 回包={"send_qzone_msg": {"status": "ok", "data": {"tid": "t9"}}})
        bot = 假Bot(self.dir, conn, **{"qzone.owner_ids": ["1000000001"]})
        果 = await Qzone(bot).post("配图说说", ["/tmp/a.jpg", "/tmp/b.jpg"], ctx=假Ctx())
        self.assertEqual(果["data"]["tid"], "t9")
        动作, 参数 = conn.调用记录[-1]
        self.assertEqual(动作, "send_qzone_msg")
        self.assertEqual(参数["images"], ["/tmp/a.jpg", "/tmp/b.jpg"])

    async def test_动作名可以配置(self):
        回包 = {"我的自定义动作": {"status": "ok", "data": {"list": [{"tid": "t1", "content": "x"}]}}}
        conn = 假Conn(支持={"我的自定义动作"}, 回包=回包)
        bot = 假Bot(self.dir, conn, **{"qzone.actions.list": "我的自定义动作"})
        读 = await Qzone(bot).list_own(5, ctx=假Ctx())
        self.assertTrue(读["ok"])
        self.assertEqual(conn.调用记录[-1][0], "我的自定义动作")

    async def test_主人名单对写操作始终生效(self):
        bot = 假Bot(self.dir, 假Conn(支持=set()),
                    **{"qzone.owner_ids": ["999"], "qzone.daily_limit": 1})
        qz = Qzone(bot)
        outsider, _ = qz.check_permission(假Ctx(uid="123"), mutation=True)
        owner, _ = qz.check_permission(假Ctx(uid="999"), mutation=True)
        self.assertFalse(outsider)
        self.assertTrue(owner)

    async def test_被动私聊读取不受每日写额度影响(self):
        bot = 假Bot(self.dir, 假Conn(支持=set()),
                    **{"qzone.owner_ids": ["1000000001"], "qzone.daily_limit": 1})
        qz = Qzone(bot)
        qz.records.append({"day": __import__("time").strftime("%Y-%m-%d"), "ts": 1})
        self.assertTrue(qz.check_permission(假Ctx(), mutation=False)[0])
        self.assertFalse(qz.check_permission(假Ctx(), mutation=True)[0])


if __name__ == "__main__":
    unittest.main()
