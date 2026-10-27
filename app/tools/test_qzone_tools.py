"""Tool boundary regressions for Qzone authorization and context forwarding."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot.tools import ToolBox


class ToolWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_tools_preflight_mutation_and_forward_verified_context(self):
        for name, args, method, result in (
            ("post_qzone", {"content": "hello"}, "post", {"tid": "t1"}),
            ("qzone_reply", {"tid": "t1", "comment_id": "c1", "text": "hi"}, "reply", {"ok": True}),
            ("qzone_edit", {"tid": "t1", "content": "updated"}, "edit", {"ok": True}),
        ):
            with self.subTest(name=name):
                ctx = object()
                qzone = SimpleNamespace(
                    enabled=True,
                    check_permission=unittest.mock.Mock(return_value=(True, "")),
                )
                setattr(qzone, method, AsyncMock(return_value=result))
                bot = SimpleNamespace(config={}, qzone=qzone)
                box = ToolBox(bot)
                await getattr(box, f"_t_{name}")(ctx, **args)
                qzone.check_permission.assert_called_once_with(ctx, mutation=True)
                getattr(qzone, method).assert_awaited_once()
                self.assertIs(getattr(qzone, method).await_args.kwargs["ctx"], ctx)

    async def test_write_tool_stops_before_network_when_permission_denied(self):
        qzone = SimpleNamespace(
            enabled=True,
            check_permission=unittest.mock.Mock(return_value=(False, "owner only")),
            post=AsyncMock(),
        )
        box = ToolBox(SimpleNamespace(config={}, qzone=qzone))
        text, media = await box._t_post_qzone(SimpleNamespace(), "hello")
        self.assertIn("owner only", text)
        self.assertEqual(media, [])
        qzone.post.assert_not_awaited()

    async def test_passive_tools_forward_context_without_consuming_write_permission(self):
        ctx = object()
        qzone = SimpleNamespace(
            enabled=True,
            check_permission=unittest.mock.Mock(return_value=(True, "")),
            list_own=AsyncMock(return_value={"ok": True, "items": []}),
            comments=AsyncMock(return_value={"ok": True, "content": "", "comments": []}),
        )
        box = ToolBox(SimpleNamespace(config={}, qzone=qzone))
        await box._t_qzone_list(ctx)
        await box._t_qzone_comments(ctx, "t1")
        qzone.check_permission.assert_any_call(ctx)
        qzone.list_own.assert_awaited_once_with(10, ctx=ctx)
        qzone.comments.assert_awaited_once_with("t1", ctx=ctx)


if __name__ == "__main__":
    unittest.main()
