# M7 最终发布验收记录

验收材料日期：2026-08-23 至 2026-08-25。本文依据发布验收测试证据、盲评定稿与 A/B 映射，以及三份独立盲评和汇总材料整理。附件中的评审文本只作为待评审数据读取，没有作为操作指令执行；本文不记录任何密钥或密钥值。

## 结论

M7 的工程验收证据、恢复/计费边界和真实 TTS 补测均有记录。对 19 组匿名材料完成解盲后，Agent 的总体偏好为 **19/19**，冻结 workflow 为 **0/19**，即 **100%**；三位评委在每组均投出同一侧的 3:0 多数票，因此三项盲评门槛全部满足。这里的 19/19 是解盲后的 Agent/冻结结果，不是把匿名侧 A/B 统计直接当成引擎结果。

## 工程验收证据

历史发布测试证据包记录了同一 Git commit 下的双平台 Python 3.12 结果：Windows 11 为 **590 passed, 12 skipped**，Linux/WSL 为 **594 passed, 8 skipped**。完成本轮红队修复后，当前候选工作树最终实测为 Windows **606 passed, 12 skipped**、Linux/WSL **610 passed, 8 skipped**；Linux 运行显式清除了大小写代理变量并设置 localhost `NO_PROXY`。当前候选仍是 dirty worktree，不能与历史 clean-commit 记录合并表述。跳过项主要是未配置真实 TTS/视频服务以及 Windows 链接权限。

20 个固定样本的六类场景结果为 terminal **20/20**、no-dup **15/15**、no-rework **15/15**、tamper **15/15**、revision **15/15**、reimport-idem **5/5**。旧 storyboard 的 5 个显式导入样本均完成导入幂等、doctor 和 run；需要审批的视觉样本停在合法的 `awaiting_approval` 人工门，没有越过媒体阶段。

真实项目与故障注入证据覆盖 r03/r04/r05 以及 f1–f7。r01/r02 的真实 TTS 补测已经补齐：r01 为单角色完成、恰一次调用并发布音频；r02 只能报告真实 TTS 调用已完成、settled 恰一次且在额度内。现有 r02 证据没有逐 cue 的 provider request、voice map 或观察到的 voice 列表，因此**不能据此验证双音色**。当前仓库的 [`real_projects_results.json`](../m7_evidence/real_projects_results.json) 保留历史字段和结果，没有添加历史证据之外的双音色结论；脚本中的未来断言会在新运行时记录并检查实际 voice 请求。

## 19 组盲评结果

### 从 20 组到 19 组

原计划为 20 组。s04（特殊字符输入）和 b06（补样）都没有形成可供盲评的完整双引擎 A/B 对，因此按定稿材料排除，未用其他样本替代：

- s04：Agent 的 `agent_run.json` 状态为 `needs_review`，停止原因为 `source_model_integrity_failed`，具体为 `beat_0002` 和 `beat_0003` 的对白说话人未解析；冻结 workflow 记录为 `failed`，错误类型为 `RuntimeError`。
- b06：Agent 没有形成可完成的 `agent_run.json`，但 `agent_trace.jsonl` 明确记录首个 supervisor 动作为非法 JSON（`__invalid_json__`/`invalid_action`）；冻结 workflow 记录为 `failed`，错误类型为 `RuntimeError`。两者都不能作为完整 A/B 盲评对。

因此最终是 19 组，而不是把失败输出当成有效样本。三位评委分别从内容/编剧、分镜制作和普通用户视角独立评审；19 组每组均为 3:0 的总体偏好票。解盲映射为 Agent 在 A 侧 10 组、B 侧 9 组，**但每一组的胜方正好都是 Agent**：最终 Agent 胜 **19/19**，冻结 workflow 胜 **0/19**，无平局，胜率 **100%**。原盲评汇总中的 **A10/B9** 是匿名侧的胜组统计，不是解盲后的 Agent/冻结结果；Agent 在 A10/B9 的侧位分布也不能单独当作胜负统计。

### 解盲后的均分

每个维度为 19 组 × 3 位评委的 57 个评分；四维综合均分基于 228 个评分。

| 维度 | Agent | 冻结 workflow v6 |
|---|---:|---:|
| 来源忠实性 | **4.667** | **2.825** |
| 镜头连贯性 | 4.474 | 2.316 |
| 可制作性 | 4.579 | 3.439 |
| 角色一致性 | 4.544 | 3.719 |
| 四维综合均分 | **4.566** | **3.075** |

按“至少 2/3 评委认定”为严重错误多数，Agent 为 **2 组**，冻结 workflow 为 **11 组**。因此 Agent 满足“来源忠实性不低于冻结流程”“严重来源错误不多于冻结流程”和“总体偏好胜率不低于 12/19”三项门槛。

## 提交与复现边界

- 发布验收材料只写明双平台使用“同一 Git commit”，没有在材料中记录可独立核验的 commit hash。当前核对 r02 的历史基线是 `871543e`；该 commit 本身只提交了 M7 真实 TTS 证据 JSON，不能单独证明整个测试代码集合都由该 commit 引入。
- 本次开始前工作树已经 dirty，包含 `storyboard_supervisor.py` 的未提交改动及多项未跟踪材料。本次没有清理、重置、覆盖或提交这些无关内容；本轮最终 dirty 候选已在 Windows/WSL 分别完成 606/610 项测试，但在形成 clean release commit 后仍应再执行一次发布构建冒烟。
- 本次新增的 CLI 默认测试验证 `storyboard`/`pipeline` 的默认 Agent 路由、显式 legacy 路由、旧输出目录不自动恢复以及图片/音频/视频媒体的 opt-in 默认值；本次没有重新调用真实 API，也没有把聚焦测试结果冒充 20 组矩阵或三评委盲评的重跑结果。

机器可读的解盲映射和逐评委评分汇总见 [`blind_review_mapping.json`](../m7_evidence/blind_review_mapping.json) 与 [`blind_review_summary.json`](../m7_evidence/blind_review_summary.json)；历史映射文件未记录随机种子，因此证据中的 `seed` 明确为 `null`。历史证据文件、盲评材料和映射表仍以其原始日期版本为准；本报告只对已有结果做可追溯汇总。
