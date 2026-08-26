# ProductionRun v1 架构规格

状态：已确认，作为 v1 实现基线

日期：2026-08-09

适用范围：`manju-tool` 的跨阶段制作编排

## 1. 目标

ProductionRun v1 将现有分镜 Agent、图像 Agent、配音和视频流水线组织成一个可恢复、可审计的制作控制面。用户不再负责判断下一条内部命令、寻找子 run 或解释状态文件。

核心用户入口是幂等命令：

```powershell
manju run project.json
```

该命令持续推进所有当前可执行的节点，直到项目完成、等待审批、需要人工判断、被外部条件阻塞或发生技术失败。

v1 的完成产物是通过门禁的制作素材包，不包括最终时间线剪辑、字幕设计、口型同步、混音、转场、调色或成片质检。

## 2. 强制设计原则

文档中的 MUST、MUST NOT、SHOULD 和 MAY 具有规范含义。

1. 顶层 ProductionRun MUST 是确定性控制器，不得由 LLM 选择下一制作阶段。
2. 脚本和策略代码 MUST 拥有流程、预算、权限、恢复和完成条件。
3. Agent SHOULD 只处理需要语义理解或创作判断的问题，并返回结构化、有证据的决策。
4. 人 MUST 保留付费授权、审美取舍和高风险歧义的最终决定权。
5. 顶层编排账本与各子 Agent 的自治账本 MUST 分离。
6. 已发生的外部副作用 MUST 被保留和对账，不得通过删除本地文件伪装成回滚。
7. 所有影响费用、来源或状态的操作 MUST 可审计、可恢复且可测试。
8. CLI MUST 是应用服务的薄适配层，领域层不得依赖终端交互。

## 3. 系统边界

### 3.1 顶层控制器负责

- 冻结和验证运行合同；
- 维护阶段 DAG；
- 判断当前可执行节点；
- 调用 StageAdapter；
- 映射统一状态、原因码和下一步动作；
- 维护顶层事件链及其投影；
- 管理跨阶段预算、审批请求和付费 operation 摘要；
- 处理 pause、cancel、revision 和后继 run；
- 根据依赖图传播产物失效。

### 3.2 顶层控制器不负责

- 理解分镜或图像 Agent 的内部检查点格式；
- 绕过子 Agent 的 `needs_review` 或质量修复流程；
- 自行重新执行失败的创作决策；
- 替代供应商对账；
- 删除或改写已完成 run 的历史事实；
- 承担最终成片后期制作。

### 3.3 子阶段负责

- 分镜 Agent：故事理解、工具选择、证据审计和定向修订；
- 图像 Agent：视觉规划、候选审批、质量门、repair plan 和图像账本；
- 配音阶段：配音脚本、TTS 生成和素材状态；
- 视频阶段：视频提示词、视频任务提交、轮询和素材状态。

## 4. 两级状态架构

ProductionRun 使用两级权威状态：

```text
production/events.jsonl
    跨阶段事实和子 run 引用
              |
              +-- storyboard 子 run 的 SQLite/trace
              +-- visual 子 run 的哈希事件链
              +-- voice 子 run 的阶段状态
              +-- video 子 run 的任务与恢复记录
```

顶层 MUST NOT 复制子系统的完整内部事件。顶层事件只引用：

- `stage`；
- 子系统 `run_id`；
- 权威账本位置；
- 权威账本摘要或最终事件哈希；
- 输入合同哈希；
- 产物 ID 和版本；
- 归一化状态、原因码和 operation 摘要。

恢复顺序 MUST 是：验证顶层事件链，验证被引用的子账本，再由 reducer 重建投影。任何一步失败都不得回退到读取投影并猜测状态。

## 5. 项目、revision 和 run

### 5.1 Project

一个项目是长期存在、可持续修订的制作容器。`project.json` 描述当前期望配置，而不是某次运行已经发生的历史。

v1 支持三种持久化输入：

- `novel`：改编后进入分镜；
- `script`：直接进入分镜；
- `storyboard`：从后续素材阶段开始。

交互式故事创作 MUST 先保存为版本化 source artifact，才能进入正式 ProductionRun。

### 5.2 Run contract

每次 run 创建后合同不可变。合同至少冻结：

- source 类型、内容哈希和格式版本；
- 启用的阶段和引擎；
- 模型名、提示词版本、工具协议版本和 schema 版本；
- 影响结果的模型参数；
- 项目预算上限、调用上限和审批策略；
- provider profile 名称；
- 代码或包版本；
- HMAC `key_id`；
- DAG 定义版本。

密钥轮换、模型替换、源内容变化、预算变化或制作范围变化不得静默修改活动 run。

### 5.3 Revision

合法修改通过 revision 完成：

1. 记录操作者、理由和变更；
2. 计算受影响的逻辑产物；
3. 当前 run 在安全边界停止并标为 `superseded`；
4. 创建引用前一 run 和 revision 的后继 run；
5. 复用未失效产物；
6. 为新增付费工作创建新的 grant。

## 6. 项目目录

一个 ProductionRun 项目 MUST 收敛到单个自包含目录：

```text
my-project/
  project.json
  production/
    events.jsonl
    state.json
    artifacts.json
    approvals/
    revisions/
    runs/
      <run_id>/contract.json
      <run_id>/stages/storyboard/<stage_run_id>/
      <run_id>/stages/visual/<stage_run_id>/
      <run_id>/stages/voice/<stage_run_id>/
      <run_id>/stages/video/<stage_run_id>/
  outputs/
    storyboard/
    images/
    audio/
    videos/
    reports/
```

内部引用 SHOULD 使用相对路径。外部 source 必须只读访问并记录规范路径与 SHA-256。外部参考素材进入付费派生流程前必须经过受控 intake。

旧项目通过显式导入进入新目录：

```powershell
manju project import old-output -o my-project
```

导入不得原地改写旧目录。无法验证来源或授权的旧产物必须标记为 `unverified`。

## 7. 状态合同

顶层状态枚举固定为：

```text
pending
ready
running
awaiting_approval
needs_review
blocked
failed
completed
superseded
cancelled
```

每个非完成状态必须附带机器可读信息：

```json
{
  "status": "awaiting_approval",
  "reason_code": "PAID_VISUAL_BATCH_APPROVAL_REQUIRED",
  "stage": "visual",
  "stage_run_id": "...",
  "blocking_artifact_ids": ["character.main"],
  "next_actions": [
    {
      "action": "approve",
      "request_id": "req_123"
    }
  ]
}
```

子阶段状态由 adapter 映射，顶层不得解析自然语言错误消息。

建议 CLI 退出码：

- `0`：本次推进成功或项目已完成；
- `1`：技术失败或合同/账本损坏；
- `2`：需要人工语义判断；
- `3`：等待审批；
- `4`：预算、供应商或其他外部条件阻塞。

## 8. DAG 与调度

内部使用依赖图，不强制把所有阶段串成一条链：

```text
source
  -> adaptation? -> storyboard
                      |-> voice_script -> tts_audio?
                      |-> video_prompts
                      `-> visual_plan -> shot_images -> video_clips?
```

v1 顶层一次只推进一个节点。子阶段内部 MAY 使用受限并发。

固定调度优先级：

1. 账本校验、恢复和未知结果对账；
2. 本地校验、缓存命中和投影重建；
3. 已有产物质量检查；
4. 免费计划和导出；
5. 审批请求创建；
6. 已授权 LLM/TTS；
7. 已授权生图；
8. 已授权视频。

同优先级按项目阶段顺序和稳定节点 ID 排序。调度选择必须可重放和可测试。

## 9. StageAdapter

各阶段保留现有内部实现，通过稳定边界接入：

```python
class StageAdapter(Protocol):
    def inspect(self, context: StageContext) -> StageSnapshot: ...
    def plan(self, context: StageContext) -> StagePlan: ...
    def execute(self, context: StageContext, grant: Grant | None) -> StageResult: ...
    def resume(self, context: StageContext) -> StageResult: ...
    def invalidate(self, changes: ChangeSet) -> InvalidationResult: ...
```

顶层只能调用 adapter，不得直接读取子系统私有 `state.json` 字段。adapter 负责验证子账本并产生统一 StageSnapshot。

首批 adapter：

- `StoryboardStageAdapter`；
- `VisualStageAdapter`；
- `VoiceStageAdapter`；
- `VideoStageAdapter`。

## 10. 顶层事件账本

`production/events.jsonl` 是追加式权威账本：

- 每条事件包含序号、时间、事件类型、run ID、payload、前序哈希和事件哈希；
- 事件内容采用规范序列化；
- 写入必须刷新落盘；
- `state.json` 和 `artifacts.json` 是可重建投影；
- 付费授权、人工审批和外部素材 intake 事件额外使用 HMAC；
- 项目目录只记录 `key_id`，不得保存 HMAC 明文密钥；
- 缺少密钥时允许只读验证普通哈希链，不允许创建敏感事件。

顶层可在未来增加 SQLite 查询索引，但该索引不得成为恢复权威。

## 11. 产物与选择性失效

产物身份由稳定逻辑 ID 和内容版本组成：

```json
{
  "logical_id": "shot.scene-02.shot-03.image",
  "version_id": "sha256:abc123",
  "path": "outputs/images/scene-02/shot-03/abc123.png",
  "depends_on": [
    {"logical_id": "storyboard.scene-02.shot-03", "version_id": "sha256:def456"},
    {"logical_id": "character.main", "version_id": "sha256:789abc"}
  ]
}
```

路径和数组位置不得作为产物身份。内容变化生成新版本，旧版本保留并标记 `superseded`。审批、人工覆盖和 grant 只绑定具体版本，不得自动继承。

镜头拆分、合并或重排必须记录显式 ID 映射。失效传播只影响依赖发生变化的下游节点。

## 12. Agent 决策合同

阶段内部可以保留专用 schema，但任何影响跨阶段状态的 Agent 决策必须映射成统一 DecisionEnvelope：

```json
{
  "schema_version": "1",
  "decision_id": "dec_123",
  "decision_type": "request_revision",
  "scope": {
    "stage": "visual",
    "artifact_ids": ["shot.scene-02.shot-03.image"]
  },
  "evidence": [
    {"logical_id": "storyboard.scene-02.shot-03", "version_id": "sha256:def456"}
  ],
  "issues": [
    {"code": "CHARACTER_IDENTITY_DRIFT", "severity": "blocking", "message": "..."}
  ],
  "proposal": {"action": "targeted_regeneration", "parameters": {}},
  "confidence": 0.86
}
```

代码必须验证 evidence、作用域、工具白名单、预算、审批和当前版本。Agent 不得自行批准费用或越过人工门。

v1 不建设有执行权的顶层 LLM。未来只读顾问可以解释状态、预测成本或提出优先级建议，但不能改变 ProductionRun 状态。

## 13. 审批与人工覆盖

人工审批通过命令或应用服务完成，手工编辑 JSON 不再是主要交互：

```powershell
manju approve project.json --request req_123 --reviewer "张三"
manju reject project.json --request req_123 --reviewer "张三" --reason "主角服装不一致"
```

审批记录必须绑定 request、reviewer、理由、完整审核项、产物版本和当前指纹。JSON 文件仍作为审计投影和高级接口存在。

人工可以覆盖审美偏好和已经由导演裁决的语义歧义。人工不得覆盖：

- 事件链、签名或内容哈希错误；
- grant 缺失或预算不足；
- 外部调用结果未知；
- 素材来源无法验证；
- 必需检查未实际执行；
- 路径越界或链接替换；
- 合同与 schema 不兼容。

本地 reviewer 记录只代表可审计确认，不宣称法律级不可抵赖身份认证。

## 14. 预算与付费 operation

授权模型是“项目硬上限 + 阶段批次 grant”：

- 项目总预算由代码强制执行；
- 免费、离线和缓存步骤自动运行；
- 常规 LLM 可在小额预授权范围运行；
- 生图和视频按明确资产集合授权；
- repair 不继承旧 grant；
- 价格不确定时同时限制金额估算和调用次数。

所有有费用或不可逆副作用的工具使用三阶段协议：

```text
call_reserved -> call_submitted -> call_settled
```

每个调用必须有稳定 `operation_id`、输入指纹、预占额度和供应商任务 ID。恢复时先对账，不得直接重交。`outcome_unknown` 阻塞相关分支，直到供应商或人工确认结果。

## 15. 失败、暂停与取消

- 临时网络错误由子阶段按固定上限重试；
- 顶层恢复同一个子 run，不重新做创作决策；
- `needs_review` 只能进入人工或子阶段 repair 流程；
- `failed` 不得被顶层静默转换为新 run；
- `pause` 停止调度新节点，保留活动 run；
- `cancel` 将当前 run 置为终态，保留已发生事实；
- `superseded` 表示 run 被 revision 的后继 run 替代；
- `purge` 是单独的高风险维护操作，不属于正常运行。

已完成和结果未知的外部调用不得回滚。能安全取消的供应商任务可以执行补偿取消，并记录真实结果。

## 16. 应用服务与未来前端

领域实现位于独立 `manju/production/` 包。建议结构：

```text
manju/production/
  models.py
  events.py
  store.py
  reducer.py
  graph.py
  scheduler.py
  artifacts.py
  approvals.py
  revisions.py
  operations.py
  service.py
  adapters/
```

ProductionService 必须：

- 接收结构化 command/query；
- 返回稳定、可序列化 DTO；
- 不依赖 Click；
- 不直接 `print`、读取终端或调用 `sys.exit()`；
- 支持进度事件或回调；
- 支持 JSON 输出契约。

核心执行提供原子小步推进：

```python
service.advance(project_id)
service.run_until_blocked(project_id)
service.request_pause(project_id)
service.subscribe(project_id, listener)
```

`advance()` 最多推进一个可提交节点，只在一次事件提交期间持有项目锁。CLI、未来 REST/WebSocket、桌面前端和远程 Worker 都通过同一应用服务边界接入。

## 17. 凭据与隐私

- `project.json` 只引用 provider profile；
- API Key、HMAC 密钥和 Authorization header 不得写入项目；
- 顶层事件只保存模型、端点类别、输入哈希、operation ID、用量和脱敏状态；
- 带签名 URL、data URL 和完整供应商响应默认不得进入事件；
- 提示词可以存入受控阶段产物，但不得复制到每条顶层事件；
- 错误响应必须字段级脱敏并限制长度；
- 缺少 HMAC 密钥时只能只读，不得继续敏感操作。

## 18. 默认引擎与发布策略

新 `manju project init` 使用 `audited-agent` 配置。分镜 Agent 已完成 M7 固定评测和人工盲评，现为 `storyboard` 与 `pipeline` 的默认分镜引擎；`legacy` 和冻结的 `workflow` 继续作为显式兼容与对照路径。

图像阶段仍需显式启用，旧 CLI 的默认图片引擎仍为 `legacy`。图像 Agent 完成独立视觉质量评测并达到后续发布门槛后，才能升级为新项目的推荐图片引擎。

顶层使用普通 Python reducer、DAG 和调度器。LangGraph 只保留在确实需要 Agent 推理的子阶段。

## 19. v1 发布门槛

- 20 个固定故事样本；
- 100% 无未授权付费调用；
- 100% 中断恢复不重复已提交付费调用；
- 100% 检测合同、顶层事件链和子账本篡改；
- 100% 停止状态提供原因码和 next actions；
- 至少 95% 无故障样本到达正确终点或正确人工门；
- 选择性失效不重做无关资产；
- 5 个低额度真实 API 项目完成端到端冒烟；
- 分镜 Agent 相对冻结 workflow 的来源忠实性不得退步，盲评综合偏好胜率至少 60%；
- 所有样本不突破金额和调用次数上限；
- Windows 与 Linux 均通过恢复、锁和路径安全测试。

## 20. 实施顺序

1. M1：分镜纵向切片；
2. 图像审批与 grant 闭环；
3. artifact graph、revision 和选择性失效；
4. 配音与 TTS；
5. 视频提示词和视频三阶段调用；
6. 旧项目显式导入；
7. 固定评测集、真实 API 冒烟和跨平台发布验收。

M1 的具体边界和测试清单见 `docs/PRODUCTION_RUN_M1_ACCEPTANCE.md`。

M7 之后的创作层 Agent 化顺序、质量门槛和红队要求见 `docs/AGENTIZATION_NEXT_PHASE_PLAN.md`。
