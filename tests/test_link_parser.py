# -*- coding: utf-8 -*-
"""链接解析单元测试：python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import link_parser as lp  # noqa: E402


class TestPublicLink(unittest.TestCase):
    def test_simple_public(self):
        link = lp.parse_one_link("https://t.me/telegram/123")
        self.assertEqual(link.kind, lp.PUBLIC)
        self.assertEqual(link.chat, "telegram")
        self.assertEqual((link.from_id, link.to_id), (123, 123))
        self.assertIsNone(link.topic_id)
        self.assertFalse(link.single)

    def test_public_range(self):
        link = lp.parse_one_link("https://t.me/my_channel/1001-1010")
        self.assertEqual((link.from_id, link.to_id), (1001, 1010))
        self.assertEqual(link.count, 10)

    def test_public_range_with_spaces(self):
        link = lp.parse_one_link("https://t.me/my_channel/101 - 120")
        self.assertEqual((link.from_id, link.to_id), (101, 120))

    def test_public_single_flag(self):
        link = lp.parse_one_link("https://t.me/my_channel/123?single")
        self.assertTrue(link.single)
        self.assertEqual(link.from_id, 123)

    def test_public_topic(self):
        link = lp.parse_one_link("https://t.me/my_group/2/45")
        self.assertEqual(link.kind, lp.PUBLIC)
        self.assertEqual(link.topic_id, 2)
        self.assertEqual(link.from_id, 45)

    def test_web_preview(self):
        link = lp.parse_one_link("https://t.me/s/my_channel/10")
        self.assertEqual(link.kind, lp.PUBLIC)
        self.assertEqual(link.chat, "my_channel")
        self.assertEqual(link.from_id, 10)

    def test_no_scheme(self):
        link = lp.parse_one_link("t.me/my_channel/7")
        self.assertEqual(link.chat, "my_channel")
        self.assertEqual(link.from_id, 7)


class TestPrivateLink(unittest.TestCase):
    def test_private_simple(self):
        link = lp.parse_one_link("https://t.me/c/3568041317/1331")
        self.assertEqual(link.kind, lp.PRIVATE)
        self.assertEqual(link.chat, -1003568041317)
        self.assertEqual((link.from_id, link.to_id), (1331, 1331))
        self.assertIsNone(link.topic_id)

    def test_private_forum_topic(self):
        """新增支持：https://t.me/c/3568041317/1/1331?single"""
        link = lp.parse_one_link("https://t.me/c/3568041317/1/1331?single")
        self.assertEqual(link.kind, lp.PRIVATE)
        self.assertEqual(link.chat, -1003568041317)
        self.assertEqual(link.topic_id, 1)
        self.assertEqual(link.from_id, 1331)
        self.assertEqual(link.to_id, 1331)
        self.assertTrue(link.single)

    def test_private_topic_range(self):
        link = lp.parse_one_link("https://t.me/c/3568041317/5/1001-1010")
        self.assertEqual(link.topic_id, 5)
        self.assertEqual((link.from_id, link.to_id), (1001, 1010))

    def test_private_full_chat_id(self):
        link = lp.parse_one_link("https://t.me/c/-1003568041317/1331")
        self.assertEqual(link.chat, -1003568041317)

    def test_private_without_message_id(self):
        self.assertIsNone(lp.parse_one_link("https://t.me/c/3568041317"))


class TestOtherKinds(unittest.TestCase):
    def test_bot_link(self):
        link = lp.parse_one_link("https://t.me/b/my_bot/4321")
        self.assertEqual(link.kind, lp.BOT)
        self.assertEqual(link.chat, "my_bot")
        self.assertEqual(link.from_id, 4321)

    def test_invite_plus(self):
        link = lp.parse_one_link("https://t.me/+AbCdEfGhIjK")
        self.assertEqual(link.kind, lp.INVITE)
        self.assertEqual(link.chat, "+AbCdEfGhIjK")

    def test_invite_joinchat(self):
        link = lp.parse_one_link("https://t.me/joinchat/AbCdEfGhIjK")
        self.assertEqual(link.kind, lp.INVITE)
        self.assertEqual(link.chat, "AbCdEfGhIjK")


class TestTextParsing(unittest.TestCase):
    def test_multiple_links(self):
        text = ("第一个 https://t.me/a/1 第二个 "
                "https://t.me/c/3568041317/1/1331?single 结束")
        links = lp.parse_links(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].chat, "a")
        self.assertEqual(links[1].topic_id, 1)

    def test_trailing_chinese_text(self):
        links = lp.parse_links("帮我保存 https://t.me/mychannel/123可以吗")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].from_id, 123)

    def test_trailing_punctuation(self):
        for text in ("https://t.me/x/1，", "https://t.me/x/1。", "(https://t.me/x/1)"):
            links = lp.parse_links(text)
            self.assertEqual(len(links), 1, text)
            self.assertEqual(links[0].from_id, 1, text)

    def test_no_link(self):
        self.assertEqual(lp.parse_links("你好，世界"), [])

    def test_plain_username_without_message(self):
        self.assertEqual(lp.parse_links("https://t.me/somechannel"), [])


class TestCommentLink(unittest.TestCase):
    """频道评论区链接：?comment=<评论ID> / ?thread=<评论ID>"""

    def test_public_comment(self):
        link = lp.parse_one_link("https://t.me/chan/123?comment=456")
        self.assertEqual(link.kind, lp.PUBLIC)
        self.assertEqual(link.chat, "chan")
        self.assertEqual(link.from_id, 123)
        self.assertEqual(link.comment_id, 456)

    def test_private_comment(self):
        link = lp.parse_one_link("https://t.me/c/3568041317/123?comment=789")
        self.assertEqual(link.kind, lp.PRIVATE)
        self.assertEqual(link.chat, -1003568041317)
        self.assertEqual(link.from_id, 123)
        self.assertEqual(link.comment_id, 789)

    def test_thread_param(self):
        link = lp.parse_one_link("https://t.me/chan/123?thread=99")
        self.assertEqual(link.comment_id, 99)

    def test_comment_with_single(self):
        link = lp.parse_one_link("https://t.me/chan/123?single&comment=5")
        self.assertTrue(link.single)
        self.assertEqual(link.comment_id, 5)

    def test_topic_without_comment(self):
        link = lp.parse_one_link("https://t.me/c/3568041317/1/1331?single")
        self.assertIsNone(link.comment_id)
        self.assertEqual(link.topic_id, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
