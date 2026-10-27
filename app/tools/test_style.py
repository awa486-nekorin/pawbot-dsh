"""Offline tests for the final-reply style cleanup."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.style import polish as _polish


def polish(text, *args, **kwargs):
    """老测试默认关掉「语气词削减」，只测其它清理规则。

    语气词削减有自己的测试（ToneTrimTests），别让 0.7 的哈希概率把
    这些与语气词无关的断言弄成随机失败。
    """
    with patch("pawbot.style._configured_tone_trim", return_value=0.0):
        return _polish(text, *args, **kwargs)


class PolishTests(unittest.TestCase):
    def test_strips_emoji_but_keeps_symbols(self):
        self.assertEqual(polish("哈哈别夸了 😎"), "哈哈别夸了")
        self.assertEqual(polish("好耶✔️"), "好耶")
        self.assertEqual(polish("今天✨真好"), "今天真好")
        # 2026-10-25：清理完是空串时退回原始输入（被 @ 时不能一声不吭），
        # 所以纯 emoji 的原样回来，不再变成空消息。
        self.assertEqual(polish("🍚🍚🍚"), "🍚🍚🍚")
        # 語C 里的 ♡ 是语气符号，保留
        self.assertIn("♡", polish("嗯…啊……♡ 不、不行了……"))

    def test_removes_mechanical_openers(self):
        for source, expected in [
            ("行行行，你说是我那就是我", "你说是我那就是我"),
            ("好好好，听你的", "听你的"),
            ("成，好呀", "好呀"),
            ("哦，懂了，你先说", "你先说"),
            ("行", "行"),
            # 单个「行」「好」是中性口语，保留
            ("行，那就这样。", "行，那就这样。"),
            ("好，今天我不去了", "好，今天我不去了"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(polish(source), expected)

    def test_unwraps_whole_part_quotes_but_keeps_inline_quotes(self):
        source = "“哼、哼……才不是因为被你说不可爱才改口的呢。”‖ 这样够轻小说了吧"
        self.assertEqual(
            polish(source),
            "哼、哼……才不是因为被你说不可爱才改口的呢。‖这样够轻小说了吧",
        )
        self.assertEqual(polish('她说“才不去呢”，我信了。'), '她说“才不去呢”，我信了。')
        self.assertEqual(polish('「哼」'), '哼')
        self.assertEqual(polish('"好"'), '好')

    def test_removes_dash_slash_and_standalone_meta_clauses(self):
        self.assertEqual(polish("怎么能这样——就这样了"), "怎么能这样，就这样了")
        self.assertEqual(polish("想你啦 / 我给你留了白饭"), "想你啦，我给你留了白饭")
        self.assertEqual(
            polish("好，「我认」和那串横线当场退役，往后不带了。"),
            "好，「我认」和那串横线当场退役，往后不带了。",
        )
        self.assertEqual(polish("好，我认。"), "好，我认。")
        self.assertEqual(polish("我认这个颜色很漂亮。"), "我认这个颜色很漂亮。")
        # 整句都是要被删的模板句时，删空就退回原句：宁可原样发一句，也别静默不发。
        self.assertEqual(polish("横线当场退役。"), "横线当场退役。")
        self.assertEqual(polish("当场退役"), "当场退役")

    def test_regional_dialect_is_neutralized(self):
        for source, expected in [
            ("得嘞，您猜怎么着，我贼拉想你", "好，你猜怎么着，我超想你"),
            ("老铁们咋整啊", "大家怎么办啊"),
            ("哟，又来唠嗑了", "又来聊天了"),
            ("别忽悠我，看你那熊样，还嘚瑟", "别骗我，看你那样子，还得意"),
            ("整一个呗，嘎嘎好吃", "来一个呗，超好吃"),
            ("你这儿玩儿得挺开心，等一会儿再说", "你这里玩得挺开心，等一会再说"),
            ("我女儿在这儿", "我女儿在这儿"),        # 单个儿化不动
        ]:
            with self.subTest(source=source):
                self.assertEqual(polish(source), expected)

    def test_neutral_colloquial_words_survive(self):
        for source in ("嗯，可以", "好家伙，这波可以", "别急，我想想", "来了来了", "有点东西"):
            with self.subTest(source=source):
                self.assertEqual(polish(source), source)

    def test_removes_duplicate_blank_lines(self):
        # 新规则：闲聊里段落之间的换行 = 另起一条，不再用多余空行塞回同一条
        self.assertEqual(polish("第一句。\n\n\n第二句。"), "第一句。‖第二句。")

    def test_collapses_excess_parts_without_losing_content(self):
        source = "一。‖二。‖‖三。‖四。"
        result = polish(source)
        # 上限 3 条：第四条并到最后一条，用句末标点相接，不用 \n
        self.assertEqual(result, "一。‖二。‖三。四。")
        self.assertEqual(result.count("‖"), 2)
        self.assertNotIn("\n", result)
        self.assertEqual(result.replace("‖", "").replace("\n", ""), "一。二。三。四。")

    def test_strips_empty_parts(self):
        self.assertEqual(polish(" ‖第一句。‖ ‖第二句。‖ "), "第一句。‖第二句。")
        self.assertEqual(polish("‖‖"), "")

    def test_pass_protocol_is_untouched(self):
        for source in ("<pass>", " <pass> ", "开头。‖<pass>‖结尾。"):
            with self.subTest(source=source):
                self.assertEqual(polish(source), source)

    def test_urls_and_markup_are_untouched(self):
        url = "https://example.com/a-b?x=1&y=2"
        self.assertEqual(polish(f"行行行，看看 {url} 吧"), f"看看 {url} 吧")
        self.assertEqual(polish(f"访问：{url} —记得哦"), f"访问：{url}，记得哦")
        self.assertEqual(polish("[CQ:image,file=a-b.png] —还在呢"),
                         "[CQ:image,file=a-b.png]，还在呢")
        self.assertEqual(polish("`行，x — y`"), "`行，x — y`")
        self.assertEqual(polish("<at id=1/> —过来啦"), "<at id=1/>，过来啦")
        self.assertEqual(polish("[图片](https://example.com/a-b.png)"),
                         "[图片](https://example.com/a-b.png)")

    def test_idempotency(self):
        cases = (
            "行行行，你说是我那就是我——反正这样啦",
            "“哼、哼……才不是因为被你说不可爱才改口的呢。”‖这样够轻小说了吧",
            "好，「我认」和那串横线当场退役，往后不带了。",
            "一。‖二。‖‖三。‖四。",
            "https://example.com/a-b —[CQ:image,file=a-b.png]",
            "<pass>",
        )
        for source in cases:
            with self.subTest(source=source):
                result = polish(source)
                self.assertEqual(polish(result), result)


class EmptyResultFallbackTests(unittest.TestCase):
    """P0-1：清理完不能是空串；被 @ 时 bot 会先 polish 再决定要不要保底，空串就静默不发。"""

    def test_never_returns_empty_for_non_empty_input(self):
        for src in ("（脸红）", "（别过脸去）", "😀😀", "🍚🍚🍚",
                    "我在这里。我会一直陪着你。", "横线当场退役。", "当场退役"):
            with self.subTest(source=src):
                self.assertTrue(polish(src), f"{src!r} 被清理成空串了")
                self.assertEqual(polish(src), src)

    def test_separator_only_input_still_empty(self):
        # 原文只有分隔符/空白等于没内容，别把「‖‖」当一句话发出去
        self.assertEqual(polish("‖‖"), "")
        self.assertEqual(polish("   "), "")


class AiPhraseStripTests(unittest.TestCase):
    def test_ack_decl_dropped_inside_reply(self):
        self.assertEqual(polish("我认了。其实我很早就想告诉你了。"), "其实我很早就想告诉你了。")
        self.assertEqual(polish("我认错。这事是我没做好。"), "这事是我没做好。")

    def test_ack_decl_alone_kept_for_empty_protection(self):
        # 整条回复只剩这句时退回原句，别变成空消息
        self.assertEqual(polish("我认了。"), "我认了。")

    def test_ack_opener_stripped(self):
        self.assertEqual(polish("我认，这次是我没做好。"), "这次是我没做好。")

    def test_normal_ren_kept(self):
        self.assertEqual(polish("我认这个颜色很漂亮。"), "我认这个颜色很漂亮。")
        self.assertEqual(polish("我认不出这个角色。"), "我认不出这个角色。")

    def test_self_narration_dropped(self):
        self.assertEqual(polish("我在这里。今天想跟你说件事。"), "今天想跟你说件事。")
        self.assertEqual(polish("我稳稳地接住你。别怕。"), "别怕。")
        self.assertEqual(polish("我会一直陪着你。真的。"), "真的。")

    def test_self_narration_embedded_kept(self):
        self.assertEqual(polish("我在这里等你。"), "我在这里等你。")

    def test_not_but_rewritten(self):
        # 2026-10-24：衔接词改成「只是」——「其实是累」不是自然中文，「只是累」才是。
        self.assertEqual(polish("不是懒，而是累。"), "只是累。")
        self.assertEqual(polish("并不是讨厌你，而是太忙了。"), "并不是讨厌你，而是太忙了。")
        # 2026-10-25：A 必须是状态/感受词才敢丢（见 _NOT_BUT_STATES 注释）。
        # 「不想去」「我的错」「不行」「要你道歉」丢掉都是改意思，整句保留。
        self.assertEqual(polish("不是不想去，是今天真的有急事。"),
                         "不是不想去，是今天真的有急事。")
        self.assertEqual(polish("不是我的错，而是你的错"), "不是我的错，而是你的错")
        self.assertEqual(polish("不是不行，是需要时间"), "不是不行，是需要时间")
        self.assertEqual(polish("不是要你道歉，而是要你改"), "不是要你道歉，而是要你改")
        # 状态词前挂程度副词仍然算状态词
        self.assertEqual(polish("不是太累，而是真困了。"), "只是真困了。")
        # B 自己已经带转折词就不再加前缀，免得「只是其实…」
        self.assertEqual(polish("不是懒，而是其实累。"), "其实累。")

    def test_not_but_uncertain_kept(self):
        # 逗号里还套逗号，A/B 抽不干净 → 整句不动
        self.assertEqual(polish("不是这个问题，而是，你根本没上心。"),
                         "不是这个问题，而是，你根本没上心。")
        # 没有 A 的部分 → 不动
        self.assertEqual(polish("不是，而是你说得对。"), "不是，而是你说得对。")

    def test_ai_phrases_idempotent(self):
        for src in ("我认了。其实我早就知道了。",
                    "不是懒，而是累。",
                    "我在这里。别怕。"):
            with self.subTest(source=src):
                first = polish(src)
                self.assertEqual(polish(first), first)

    def test_strip_ai_phrases_config_off(self):
        with patch("pawbot.style._configured_strip_ai", return_value=False):
            self.assertEqual(polish("我认了。其实我早就知道了。"),
                             "我认了。其实我早就知道了。")


class BodyReactionCapTests(unittest.TestCase):
    def test_three_reactions_keep_one(self):
        src = "我脸红了。心跳也好快。尾巴还不自觉地摇起来。"
        self.assertEqual(polish(src), "我脸红了。")

    def test_reaction_quota_is_shared_across_parts(self):
        # 2026-10-25：额度按整条回覆算——拆成两条、三条的结果必须一致。
        self.assertEqual(polish("我脸红了。‖心跳好快。尾巴在摇。"), "我脸红了。")
        self.assertEqual(polish("我脸红了。‖心跳好快。‖尾巴在摇。"), "我脸红了。")
        # 额度用完时，还有正文的那条只削反应句，正文留着
        self.assertEqual(polish("我脸红了。‖心跳好快。这题选 B。"), "我脸红了。‖这题选 B。")

    def test_reaction_only_content_kept(self):
        self.assertEqual(polish("我脸红了。"), "我脸红了。")

    def test_sticker_description_not_treated_as_reaction(self):
        self.assertEqual(polish("这张脸红红的表情包好可爱。"), "这张脸红红的表情包好可爱。")

    def test_other_person_reaction_not_capped(self):
        self.assertEqual(polish("他说你脸红了。"), "他说你脸红了。")

    def test_roleplay_not_capped(self):
        src = "呜……不、不要……♡ 脸、脸都红了……尾巴也不听使唤了……哈……"
        self.assertEqual(polish(src), src)

    def test_body_reaction_max_zero(self):
        with patch("pawbot.style._configured_body_reaction_max", return_value=0):
            # 有其它内容时全删反应句
            self.assertEqual(polish("我脸红了。这题选 B。"), "这题选 B。")
            # 全是反应句时保留一句，别空
            self.assertEqual(polish("我脸红了。尾巴在摇。"), "我脸红了。")


class ToneTrimTests(unittest.TestCase):
    """语气词收敛：啊/嘛/呢/啦/呀/哦/哟 按比例削；吧/吗 不动。"""

    def _trim(self, text, rate):
        with patch("pawbot.style._configured_tone_trim", return_value=rate):
            return _polish(text)

    def test_rate_one_strips_trailing_particles(self):
        self.assertEqual(self._trim("在呀 怎么啦", 1.0), "在 怎么")
        self.assertEqual(self._trim("好呀 那我等你嘛", 1.0), "好 那我等你")
        self.assertEqual(self._trim("这题确实没转过弯来呢", 1.0), "这题确实没转过弯来呢")  # 呢 当疑问语气保留
        self.assertEqual(self._trim("好吧 那就这样啦~", 1.0), "好吧 那就这样~")

    def test_keeps_ba_and_ma(self):
        # 吧/吗 是疑问/商量语气，删了会改意思；ten 句里 ten 个都保留
        self.assertEqual(self._trim("你连我都不认识了吗", 1.0), "你连我都不认识了吗")
        self.assertEqual(self._trim("快去躺着吧", 1.0), "快去躺着吧")
        self.assertEqual(self._trim("那你要不要来嘛", 1.0), "那你要不要来")

    def test_protected_words_survive(self):
        self.assertEqual(self._trim("干嘛突然摸头啊", 1.0), "干嘛突然摸头")
        self.assertEqual(self._trim("哎呀 别闹呀", 1.0), "哎呀 别闹")

    def test_rate_zero_is_noop(self):
        for text in ("在呀", "好嘛", "这题呢", "好吧那就这样啦~"):
            self.assertEqual(self._trim(text, 0.0), text)

    def test_fractional_rate_keeps_some(self):
        # 0.7 是哈希削，不保证哪句留；只断言「削了但没全削」「吧/吗 原样」
        source = "在呀 好嘛 快去吧 这题呢"
        out = self._trim(source, 0.7)
        self.assertNotEqual(out, source)
        self.assertIn("快去吧", out)
        self.assertIn("这题", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
