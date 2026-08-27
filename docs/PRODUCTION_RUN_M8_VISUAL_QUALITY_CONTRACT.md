# ProductionRun M8 图像 Agent 质量评测合同

状态：合同和固定评测范围已冻结；真实图片、人工盲评和发布门仍未完成。
冻结基线：`feat/m3.4.1-audit-baseline` / `d7191cd`。
权威离线产物：`m8_evaluation/contract.json`、`m8_evaluation/samples.json`。

## 1. 审计结论与边界

现有图像 Agent v4 负责确定性阶段路由、视觉规划、修订范围和人工门；
`VisualStageAdapter` 负责 Provider 边界、回执、发布和结算。M2 的审批、授权、预算、
`outcome_unknown` 和恢复合同仍然是高风险操作的权威，M8 不改变这些权责。

M7 已证明 ProductionRun 的离线控制面和匿名材料流程可以审计，但 M7 的 19 组盲评、
历史候选结果和当前 dirty 工作区都不是 M8 的 20 故事/60 场景组证据。M8 必须重新绑定
本合同、同一 storyboard、同一参考素材和两侧可比较的图片；旧结果不能自动继承。

本次工具只做三件事：冻结源文件哈希绑定的合同与样本范围、打包已经存在的本地 A/B 图片、
校验并汇总三份人工评分。它不导入 Provider，不读环境凭据，不调用网络，也不会把规则检查
写成视觉模型通过。

## 2. 固定评测合同

一个评测单元是“一个故事、一个预注册场景组和一对相同输入下的 A/B 图片”。最低范围为
20 个故事、60 个场景组；当前 `m7_samples/manifest.json` 恰好提供 20 个源样本，冻结器为
每个样本生成 3 个待绑定 storyboard 的场景组槽位。样本清单是预注册范围，不是已经生成图片
或已经通过视觉审核的证明。

覆盖标签必须至少出现以下七类：

- 多人关系；
- 重名角色身份；
- 跨镜头服装连续性；
- 关键道具身份、状态、位置和尺度；
- 昼夜/时间变化；
- 动作顺序和前后状态；
- 多人、遮挡、前后景或多关键物体的复杂构图。

每个场景组在真实运行时还要绑定来源或 storyboard、角色身份卡、场景母版、镜头输入、
Agent 候选、legacy 候选、修订轨迹、视觉审核证据和费用/零成本 fixture 记录。

每位独立评委对两侧分别按 1–5 评分，维度固定为：

`source_fidelity`、`character_consistency`、`wardrobe_continuity`、
`prop_continuity`、`composition_readability`、`action_continuity`、
`production_readiness`。

三位评委视角固定为 `content`、`visual_production`、`target_user`。每组总体偏好只能是
`A`、`B` 或 `tie`；三票严格多数决定组胜负，其他情况为 tie。严重错误定义为改变来源
事实、人物身份/数量、服装状态、关键道具、空间/动作时序，或令声明镜头不可用的可见错误。
固定代码为：

`source_fact_invented_or_omitted`、`character_identity_or_count_wrong`、
`wardrobe_state_breaks_continuity`、`key_prop_identity_state_or_scale_wrong`、
`spatial_relation_or_action_time_wrong`、`unusable_artifact_or_multi_panel_output`。

严重错误必须有至少一个代码和具体说明；单纯风格偏好、轻微渲染瑕疵或来源兼容的不同构图
不自动算严重错误。某侧在一组得到至少 2/3 评委标记，才计为该侧的严重错误组。

质量门必须同时满足：按故事聚类后的 Agent 胜率单侧 95% 下界不低于 60%；Agent 相对
legacy 的来源忠实性、角色一致性配对差值单侧 95% 下界均不低于 0；Agent 的这两个维度
绝对均分均不低于 3.0；Agent 严重错误组数不多于 legacy，且绝对比例不高于 10%；三份
评分覆盖全部可比较组。tie 进入分母，不计为 Agent 胜。汇总同时报告评委偏好完全一致率。

质量门通过仍不等于发布通过。发布还必须有结构/审批/grant/预算/恢复/防重复测试、批准的
低额度真实 Provider 运行和结算证据、Windows/Linux clean-commit 全量回归，以及三轮独立
红队关闭全部高优先级问题。默认 legacy 路由在所有门完成前保持不变。

## 3. 源绑定样本清单

运行 `freeze` 会逐项核对源 manifest 中的样本 ID、相对文件名、字节数和 SHA-256，生成：

- `m8_evaluation/contract.json`：版本化合同、评分表、严重错误、预算和发布门；
- `m8_evaluation/samples.json`：20 个故事、60 个稳定场景组 ID、覆盖计数和源 manifest 哈希。

清单中的 `execution.status` 为 `not_started`，Provider 调用、付费金额和人工评分行数均为
0。这些 0 是“尚未执行”的事实，不是图像质量为零或自动通过的结论。冻结器和所有输出
写入均为 new-only，发现已有文件会停止，避免覆盖旧证据。

## 4. A/B 输入和匿名材料

图片生成必须由受控的 ProductionRun/Provider 流程在用户批准后另行完成。完成后，操作者
提供一个不含评分结果的 pair input，例如：

```json
{
  "schema_version": "m8-visual-pair-input-v1",
  "pairs": [
    {
      "group_id": "story-01-group-01",
      "story_id": "story-01",
      "source_file": "local/exact-frozen-source.txt",
      "agent_image": "local/agent-image.png",
      "legacy_image": "local/legacy-image.png",
      "evidence_file": "local/private-evidence/story-01-group-01/evidence.json"
    }
  ]
}
```

`source_file` 也可以替换为 `source_text`。`evidence_file` 使用
`m8-visual-pair-evidence-v1`，必须绑定 `group_id`、`story_id`、共享输入 SHA-256、冻结来源
SHA-256，以及合同要求的十二类逐组产物（包括 `visual_agent_run`、内部哈希链
`visual_event_log` 和 ProductionRun 顶层 `production_event_log`）。每个产物都用相对路径、字节数和 SHA-256 固定；正式评审的
`source_file` 必须与冻结源文件字节完全一致，storyboard/镜头绑定另存于私有证据，不能用任意
摘要替换评委看到的来源。
Agent 执行必须是 `completed`、自动视觉审核完成、`passed_without_override=true`、
`manual_quality_override=false`、阻塞状态 clear。费用记录必须与同一 run ID 绑定、已结算或为
零成本 fixture、无未知付费任务且实际调用不超过批准调用。候选图哈希必须与证据中的候选图
完全一致。打包器会解析 `visual_agent_run`、`visual_review`、`cost_plan` 和 `visual_event_log`，
交叉核对 run ID、最终状态、事件序号/校验和、自动审核、人工覆盖及费用；附件与顶层摘要矛盾
时直接拒绝。正式打包还必须从受控环境读取 `MANJU_PRODUCTION_HMAC_KEY`，验证顶层 ProductionRun
审批、Grant、调用生命周期及签名，并要求已签名的成功结算结果 SHA-256 精确绑定 Agent 候选图；
普通可自建哈希链只能用于协议演练，不能进入正式盲评。HMAC key 不能写入任何证据或评审包。
所有私有逐组产物定稿后，操作者先运行 `attest-pair`；该命令验证已声明的产物哈希，并在对应
ProductionRun 事件链末尾追加 `m8_visual_evidence_attested` 敏感事件。见证事件整体绑定组、故事、
冻结来源、Agent run、候选图、审核与费用产物，以及先前 `stage_completed` 的事件哈希。
正式打包还要求 `MANJU_M8_PRODUCTION_HEADS_JSON`：它由操作者在打包前从当前权威项目逐 run
导出，键为 `project_id/run_id`，值为该 run 当前最后事件哈希。证据中的 attestation 必须与该
外部 current-head 锚一致，撤销前的旧签名前缀不能充当当前状态。
两侧必须来自相同来源绑定、画幅要求和参考素材；当使用完整冻结清单时，pair 的
60 个 `group_id`、故事归属和冻结来源哈希必须精确匹配 `samples.json`。

```powershell
python tools/m8_visual_evaluation.py generate-ab `
  --pairs-file C:\path\to\m8-pairs.json `
  --sample-manifest m8_evaluation/samples.json `
  --public-dir C:\path\to\m8-reviewer-materials `
  --mapping-output C:\path\to\m8-private-mapping.json
```

评委只接收 `public-dir`：每组有 `input.txt`、`A/image.<ext>`、`B/image.<ext>` 和中性
`manifest.json`。随机种子和解盲表只写入 public 目录之外的 private mapping；公共 JSON 只
保存解盲表的 SHA-256 承诺，不泄露分配内容。汇总必须同时匹配该公共承诺和公共 manifest
哈希；正式 seed 由冻结样本 fingerprint 唯一派生，并按故事及覆盖类型约束平衡 A/B，不能
反复挑 seed。评分
文件还必须记录它实际看到的 manifest 哈希和映射承诺，防止评分后同时改写 manifest 与 A/B
映射。公共 JSON 不包含 Agent/legacy、Provider/model、run ID、
绝对源路径、凭据或评委结果。PNG 打包时只保留
图像结构和必要透明度块，去掉文本/EXIF/ICC 等附加块；JPEG 去掉 APP1–APP15 和评论块；
含 EXIF/XMP/ICC 的 WebP 直接拒绝，要求先在受控环境清理。任一文件哈希或字节数改变都会
使汇总停止。

## 5. 评分文件和汇总

每位评委提交一份 `m8-visual-review-v2` JSON，必须覆盖所有组，并对 A、B 两侧分别包含完整七维评分、
偏好、A/B 严重错误布尔值、错误代码列表和 A/B 说明。评委结果不应写入引擎名、Provider、
模型、run ID、绝对路径或凭据。`aggregate` 必须收到且仅收到三个不同视角和不同 reviewer ID：

```json
{
  "schema_version": "m8-visual-review-v2",
  "reviewer_id": "reviewer-01",
  "perspective": "content",
  "materials_manifest_sha256": "64位十六进制哈希",
  "mapping_commitment_sha256": "64位十六进制哈希",
  "rows": [
    {
      "group_id": "g001",
      "scores": {
        "A": {
          "source_fidelity": 1,
          "character_consistency": 1,
          "wardrobe_continuity": 1,
          "prop_continuity": 1,
          "composition_readability": 1,
          "action_continuity": 1,
          "production_readiness": 1
        },
        "B": {
          "source_fidelity": 1,
          "character_consistency": 1,
          "wardrobe_continuity": 1,
          "prop_continuity": 1,
          "composition_readability": 1,
          "action_continuity": 1,
          "production_readiness": 1
        }
      },
      "preference": "tie",
      "serious_error": {"A": false, "B": false},
      "serious_error_codes": {"A": [], "B": []},
      "notes": {"A": "", "B": ""}
    }
  ]
}
```

示例中的分数和 `g001` 只是字段形状，不是 M8 结果；实际文件必须为 60 个冻结组逐组填写。

```powershell
python tools/m8_visual_evaluation.py aggregate `
  --materials-manifest C:\path\to\m8-reviewer-materials\manifest.json `
  --mapping-file C:\path\to\m8-private-mapping.json `
  --review-file C:\path\to\review-content.json `
  --review-file C:\path\to\review-production.json `
  --review-file C:\path\to\review-user.json `
  --contract m8_evaluation/contract.json `
  --sample-manifest m8_evaluation/samples.json `
  --output C:\path\to\m8-visual-summary.json
```

只有同时提供并匹配冻结合同与冻结样本、且范围达到 20 故事/60 组时，汇总结果才可能是
`visual_gate_passed` 或 `visual_gate_failed`；API 层缺少任一正式范围输入时只能得到
`visual_gate_incomplete`。所有状态都固定写出
`release_eligible=false` 及仍需补齐的工程、平台、真实执行和红队阻塞项。没有图片或真人
评分时，不得调用汇总器伪造结果；不完整评分不会插值。

## 6. 可复现的本地步骤

在干净输出目录中冻结范围（默认输出已存在时会安全拒绝覆盖）：

```powershell
python tools/m8_visual_evaluation.py freeze `
  --source-manifest m7_samples/manifest.json `
  --contract-output m8_evaluation/contract.json `
  --sample-output m8_evaluation/samples.json `
  --baseline-commit d7191cd `
  --baseline-branch feat/m3.4.1-audit-baseline
python tools/m8_visual_evaluation.py validate-sample `
  --sample-manifest m8_evaluation/samples.json
```

本地单元测试不生成图片、不调用 Provider：

```powershell
python -m pytest -q tests/test_m8_visual_evaluation.py
```

## 7. 当前停止点与人工输入

仓库已经完成合同、样本槽位、逐组证据绑定、匿名格式、严重错误定义、评分校验、哈希完整性和离线汇总
代码。当前没有声称 M8 视觉质量通过，也没有调用真实付费 Provider。

继续推进需要用户/操作员明确提供：

1. 20 个故事的已选 storyboard、角色身份卡、场景母版、镜头输入和相同参考素材；
2. Agent v4 与当前 legacy 路径的至少 60 组图片、修订轨迹、视觉审核记录、批准/Grant、
   成本和结算证据；
3. 三位互不讨论的人工评委及其三份完整 JSON 评分；
4. 低额度真实 Provider 的账号授权、隔离 profile、预算和人工确认；
5. clean commit 上的 Windows/Linux 全回归和三轮独立红队复验。

这些输入分别进入 pair input、private evidence 和 reviewer JSON，不应把 M7 的历史盲评或
规则检查当成替代品。完成人工门前，图像生成保持显式启用、付费保持审批/grant 控制，
默认推荐引擎不变。
