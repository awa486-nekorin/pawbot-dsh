# PawBot 开发约束

本次框架优化唯一源码目标为本仓库。按 `docs/HANDOFF-FRAMEWORK-OPTIMIZATION-2026-10-30.md` 的批次推进；不跨批顺手重构业务。

- 运行态数据、真实凭证、人设和记忆不作为开发/测试输入；行为验收使用临时数据目录。
- 保持已有路由、配置键和数据格式兼容；安全词不得出现在源码、日志、测试输出或文档。
- 本项目现有 Python 测试使用标准库 unittest；未安装 pytest 时无需为此新增测试依赖。
- 标为离线的测试必须隔离浏览器探测、自启动和后台浏览记录。只模拟页面 API 不够：图片搜索路由本身也会探测/启动专用 Chrome。不要通过更改业务行为让测试变绿。

| 规则 | 由什么拦住 |
| --- | --- |
| Pixiv 主页/路由/图片标记离线测试不得启动真实浏览器 | `app/tools/test_pixiv_home.py` 的 Popen 阻断及 `assert_not_called`；`py -3.13 -m unittest tools.test_pixiv_home -q` |
| 框架兼容契约不得无意漂移 | `node scripts/architecture-baseline.mjs` + `node test/architecture-baseline.mjs` |
| 未成年相关结果在 SFW/R18 模式均保持排除 | `tools.test_pixiv_session.PixivSessionTests.test_minor_metadata_is_excluded_even_for_sfw_results`（已以禁用过滤的进程内负例验证会失败） |
| 停机成功回执之前须排空写入/外发任务，之后不接收新变更 | `py -3.13 -m unittest tools.test_shutdown_admission -q`：真实 Store + loopback HTTP，覆盖拒绝新变更、在途面板发送、取消未完成任务、关闭连接时及停机前已断连的 worker；不得削弱断言让绿灯成立 |

批次 2 交付前重新核对 `docs/BATCH2-REVIEW-2026-10-30.md` 的最新清单；完整 unittest 会自动发现 Lead 新增的独立停机回归。

2026-10-30 接手时的排序测试曾使用与当前过滤契约矛盾的 fixture，已改为中性场景并保留原排序断言；完整证据见 `docs/decisions-framework-optimization.md`。不要为批次绿灯改变业务语义或照抄当前输出改预期。
