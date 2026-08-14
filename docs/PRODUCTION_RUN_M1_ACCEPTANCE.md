# ProductionRun M1 实施与验收清单

状态：已实施并完成离线验收

日期：2026-08-10

依赖规格：`docs/PRODUCTION_RUN_V1_ARCHITECTURE.md`

## 0. 实施结果

- ProductionRun M1 已实现持久化项目合同、确定性调度、顶层哈希事件链、分镜 StageAdapter、可恢复运行、暂停、状态查询、doctor 和稳定 JSON CLI。
- 分镜子运行 authority 会只读校验 SQLite `quick_check`、checkpoint schema、manifest `run_id` 对应 thread、LangGraph 最新 checkpoint 反序列化及 source/model/status/budget 绑定。
- `agent_trace.jsonl` 必须非空、序号连续并绑定同一子 `run_id`；任意字节 checkpoint 或 `{}` trace 均按 `STAGE_INTEGRITY_FAILED` 拒绝。
- execution lease 的 acquire/release/recover 已写入顶层哈希事件链，reducer 会校验租约生命周期。
- 2026-08-10 M1.1 本地验收：ProductionRun 专项 `40 passed`；完整离线套件 `385 passed, 2 skipped`；`compileall`、5 个 ProductionRun CLI help 和 `git diff --check` 均通过。
- 两个 skip 是当前 Windows 环境没有 symlink 创建权限；同范围的 Windows junction 测试已执行。未读取真实 provider 凭据，未调用真实或付费 API。
- 独立 `gpt-5.6-sol` medium 验收已按约定执行 3 轮。第 3 轮剩余的 child authority 深度校验和 execution lease 审计问题，已在本版本修复并由新增离线测试覆盖；因 3 轮额度已用满，没有启动第 4 轮独立复验。
- M1.1 增加全部 7 个定义的崩溃恢复窗口测试；`run_created` 后恢复会显式补记 `run_started`，避免 reducer 拒绝后续阶段事件。`doctor` 额外返回 `integrity_status` 和 `run_status`。失败 manifest 缺少 storyboard 仅在 manifest、checkpoint 和 trace 均通过验证时归为分镜执行失败；任何篡改仍为完整性失败。
- M1.1 独立 `gpt-5.6-sol` medium 复验第 1 轮发现 failed manifest 旁路校验和 SHA256 路径分隔符问题；修复后第 2 轮通过。WSL 复验确认清单无 BOM、使用正斜杠，GNU `sha256sum -c` 返回 0。

## 1. M1 目标

M1 交付第一个完整纵向切片：从持久化项目合同，通过顶层确定性控制器驱动现有分镜 Agent，并支持状态查看、幂等运行、中断恢复、暂停和完整性验证。

M1 不调用生图、视觉、TTS 或视频 API，不实施任何付费媒体路径。

## 2. 用户场景

M1 必须支持：

```powershell
manju project init --source sample_story.txt --source-type script -o demo-project
manju run demo-project/project.json
manju status demo-project/project.json
manju pause demo-project/project.json
manju run demo-project/project.json
manju doctor demo-project/project.json
```

所有查询和命令必须提供 `--json`，并返回稳定 DTO。

`manju run` 的语义是：不存在活动 run 时创建 run；存在兼容活动 run 时恢复；等待人工时只报告；已完成时不重复执行；合同不兼容或账本损坏时拒绝继续。

## 3. M1 范围

### 3.1 必须实现

- `project.json` v1 schema 和校验；
- source intake：路径规范化、只读读取、SHA-256 和格式记录；
- 不可变 `contract.json`；
- 项目 ID、run ID 和 storyboard stage run ID；
- 顶层 JSONL 哈希事件链；
- `state.json` 投影及完整重建；
- 单项目互斥锁；
- 最小 DAG：`source -> storyboard`；
- 确定性 scheduler；
- `StoryboardStageAdapter`；
- `ProductionService.advance()`；
- `ProductionService.run_until_blocked()`；
- `init/run/status/pause/doctor` CLI；
- CLI 文本输出与 JSON 输出；
- 统一状态、原因码、next actions 和退出码；
- mock LLM 的离线集成测试；
- Windows 和 Linux 锁/路径测试。

### 3.2 明确不实现

- VisualStageAdapter；
- 生图、视觉复核、TTS 或视频调用；
- 付费 grant 和三阶段 operation 的真实执行；
- artifact DAG 的完整选择性失效；
- revision 命令和后继 run；
- approve/reject；
- 旧项目导入；
- Web、桌面前端、REST 或 WebSocket；
- 顶层并行节点执行；
- 顶层 SQLite 权威存储；
- 有执行权的顶层 LLM。

M1 必须冻结以上后续能力所需的接口，但不得用空实现伪装功能已经可用。

## 4. 建议代码结构

```text
manju/production/
  __init__.py
  models.py
  events.py
  store.py
  reducer.py
  graph.py
  scheduler.py
  service.py
  locking.py
  paths.py
  adapters/
    __init__.py
    base.py
    storyboard.py
tests/
  test_production_models.py
  test_production_store.py
  test_production_reducer.py
  test_production_scheduler.py
  test_production_storyboard_adapter.py
  test_production_service.py
  test_production_cli.py
```

CLI 只能调用 ProductionService。现有 `run_storyboard()` 的终端输出和返回约定由 adapter 隔离，不得传播到 ProductionService 的公共 DTO。

## 5. 最小数据合同

### 5.1 project.json

```json
{
  "schema_version": "1",
  "project_id": "prj_...",
  "source": {
    "type": "script",
    "path": "sources/sample_story.txt",
    "sha256": "...",
    "format": "text/plain"
  },
  "profile": "audited-agent",
  "production": {
    "storyboard": {
      "enabled": true,
      "engine": "agent",
      "max_scenes": 6,
      "max_steps": 40,
      "max_calls": "auto",
      "max_revisions": 2
    },
    "visual": {"enabled": false},
    "voice": {"enabled": false},
    "video": {"enabled": false}
  },
  "provider_profiles": {
    "llm": "default"
  },
  "integrity": {
    "hmac_key_id": "manju-local-default"
  }
}
```

初始化时 source 可以复制进项目 `sources/`，或登记为外部只读 source。无论哪种方式，contract 都必须冻结规范路径和内容哈希。

### 5.2 contract.json

除规范化 ProjectSpec 外，至少包含：

- `run_id`；
- `created_at`；
- `project_spec_fingerprint`；
- `source_fingerprint`；
- `dag_version`；
- `adapter_contract_versions`；
- `model` 和 provider profile 引用；
- prompt/tool/schema/code 版本；
- 归一化预算字段；
- 整体 `contract_fingerprint`。

### 5.3 ProductionSnapshot DTO

```json
{
  "schema_version": "1",
  "project_id": "prj_...",
  "run_id": "run_...",
  "status": "needs_review",
  "current_stage": "storyboard",
  "reason": {
    "code": "STORYBOARD_REVIEW_REQUIRED",
    "message": "当前分镜需要人工判断"
  },
  "progress": {"completed": 1, "total": 2},
  "next_actions": [],
  "updated_at": "..."
}
```

DTO 字段名和枚举值使用稳定英文；CLI 可以提供中文展示文本。

## 6. M1 顶层事件

M1 至少定义以下事件类型：

```text
project_initialized
run_created
run_started
stage_scheduled
stage_run_attached
stage_completed
stage_needs_review
stage_failed
pause_requested
run_paused
run_resumed
run_completed
integrity_check_failed
```

事件必须：

- 采用规范 JSON 序列化；
- 使用单调递增序号；
- 包含 `previous_hash` 和 `event_hash`；
- 绑定 `project_id` 和 `run_id`；
- 明确 `event_version`；
- 在追加后刷新落盘；
- 不得包含 API Key 或完整授权头。

对 reducer 来说，未知 event version 必须失败关闭；已知但与当前状态不兼容的事件必须被拒绝。

## 7. M1 状态转换

```text
pending -> ready -> running -> completed
                     |  |  |
                     |  |  `-> failed
                     |  `----> needs_review
                     `-------> paused

paused -> ready
needs_review -> needs_review  # M1 只报告，不实现审批恢复
```

M1 的 storyboard 映射：

- 子 Agent `completed` -> 顶层 `completed`；
- 子 Agent `needs_review` -> 顶层 `needs_review`；
- 子 Agent 明确失败或不可读 -> 顶层 `failed`；
- 子 run 可恢复 -> 顶层保持 `running` 或在请求后 `paused`；
- 子 Agent 输出存在但其权威检查点不一致 -> `failed`，原因 `STAGE_INTEGRITY_FAILED`。

## 8. M1 原因码

至少支持：

```text
PROJECT_READY
PROJECT_ALREADY_COMPLETED
PROJECT_PAUSED
PROJECT_LOCKED
PROJECT_CONTRACT_CHANGED
PROJECT_EVENT_CHAIN_INVALID
SOURCE_MISSING
SOURCE_HASH_MISMATCH
STORYBOARD_RUNNING
STORYBOARD_REVIEW_REQUIRED
STORYBOARD_FAILED
STAGE_INTEGRITY_FAILED
DEPENDENCY_UNSATISFIED
UNSUPPORTED_SCHEMA_VERSION
INTERNAL_ERROR
```

每个原因码必须具有：

- 默认用户消息；
- 对应顶层状态；
- 建议退出码；
- 可选 next action 生成器；
- 单元测试。

## 9. 幂等与恢复规则

### 9.1 重复运行

- 连续执行两次 `manju run` 不得创建两个活动 run；
- 已完成项目不得再次调用 StoryboardStageAdapter；
- `needs_review` 状态不得自动继续子 Agent；
- `paused` 状态执行 `run` 时必须记录显式 resume 事件后继续；
- adapter 返回后、顶层事件写入前发生中断时，恢复必须先 inspect 子 run，再决定补记事件，不得重新调用模型。

### 9.2 合同变化

M1 尚未实现 revision，因此检测到影响合同的 `project.json` 变化时必须停止，返回 `PROJECT_CONTRACT_CHANGED`。不得自动创建新 run。

### 9.3 完整性变化

- source 内容变化 -> `SOURCE_HASH_MISMATCH`；
- 顶层事件链损坏 -> `PROJECT_EVENT_CHAIN_INVALID`；
- contract 指纹变化 -> `PROJECT_CONTRACT_CHANGED`；
- 子账本无法验证 -> `STAGE_INTEGRITY_FAILED`。

## 10. 锁与进程模型

M1 使用本地单控制器：

- 对同一项目的 mutating command 必须获得排他锁；
- `status` 和只读 `doctor` SHOULD 可并发读取一致快照；
- 锁不得只依赖永久存在的时间戳文件；
- 进程崩溃后锁必须可安全释放或判定失效；
- 锁冲突返回 `PROJECT_LOCKED`，不得等待无限时间；
- 不得在整个模型调用期间持有仅用于事件提交的细粒度写锁；
- 若需要运行租约，租约状态必须可审计并有明确恢复规则。

## 11. ProductionService 合同

M1 至少提供：

```python
class ProductionService:
    def initialize(self, command: InitializeProject) -> ProductionSnapshot: ...
    def advance(self, command: AdvanceProject) -> ProductionSnapshot: ...
    def run_until_blocked(self, command: RunProject) -> ProductionSnapshot: ...
    def get_status(self, query: GetProjectStatus) -> ProductionSnapshot: ...
    def request_pause(self, command: PauseProject) -> ProductionSnapshot: ...
    def doctor(self, query: InspectProject) -> DiagnosticReport: ...
```

公共方法不得：

- 调用 `sys.exit()`；
- 依赖 Click context；
- 请求终端输入；
- 返回不可序列化异常对象；
- 通过读取 CLI 输出判断子阶段状态。

领域错误应转换成稳定 error DTO 或具有稳定 code 的应用异常，再由 CLI 映射退出码。

## 12. StoryboardStageAdapter 合同

adapter 必须：

- 根据冻结合同调用现有 `run_storyboard()`；
- 强制 `image_api=False` 和 `image_engine="legacy"` 或完全禁用图片路径；
- 将现有 Agent manifest 和 storyboard metadata 映射成 StageSnapshot；
- 记录并复用 storyboard 子 run 身份；
- 在恢复前 inspect 现有检查点；
- 不将终端输出作为状态来源；
- 返回 storyboard 逻辑产物 ID、内容哈希和受控路径；
- 验证输出属于当前 source 和 contract；
- 对 `needs_review` 保留当前 storyboard，不伪装成完成。

M1 的 adapter 不应重构分镜 Agent 的内部工作流。

## 13. 测试矩阵

### 13.1 Models 和 schema

- 最小合法 ProjectSpec；
- 未知 schema version；
- 非法 source type；
- 非法或越界路径；
- 预算和 Agent 限制字段规范化；
- 稳定 contract fingerprint；
- 字段顺序不影响 fingerprint；
- 实质字段变化改变 fingerprint。

### 13.2 Event store 和 reducer

- 空账本初始化；
- 多事件顺序回放；
- 事件篡改；
- 删除中间事件；
- 复制或乱序事件；
- 截断最后一行；
- 未知 event version；
- 投影删除后重建；
- 投影损坏后重建；
- 不兼容状态转换失败关闭。

### 13.3 Scheduler

- 依赖未满足时不调度；
- ready 节点只选择一次；
- 完成后项目终止；
- `needs_review` 时不继续；
- paused 时不调度；
- 相同状态产生相同选择；
- 无 ready 节点时返回正确原因码。

### 13.4 Adapter

- Agent 完成；
- Agent `needs_review`；
- Agent 技术失败；
- manifest 丢失或损坏；
- storyboard 输出哈希不匹配；
- 已有可恢复子 run；
- adapter 返回后顶层提交前中断；
- 断言没有图片、视觉、TTS 或视频调用。

### 13.5 Service 和 CLI

- init -> run -> completed；
- init -> run -> needs_review；
- 重复 run 幂等；
- pause -> run 恢复；
- 合同变化拒绝；
- source 变化拒绝；
- 锁冲突；
- `status --json` schema；
- CLI 退出码映射；
- 用户文本与 DTO 状态一致；
- ProductionService 不依赖 Click 或终端输入。

### 13.6 跨平台

- Windows 路径、保留名和大小写行为；
- Linux 路径与权限；
- 项目目录复制后的相对路径；
- 外部 source 丢失；
- symlink/junction 越界；
- 崩溃后的锁恢复。

## 14. 故障注入点

测试必须能够在以下位置模拟中断：

1. contract 写入前；
2. `run_created` 事件后；
3. `stage_scheduled` 后、adapter 调用前；
4. 子 Agent 完成后、`stage_completed` 前；
5. `stage_completed` 后、投影更新前；
6. pause 请求写入后；
7. 最终 `run_completed` 后、CLI 返回前。

每个注入点恢复后都不得产生重复完成事件、第二个活动 run 或额外模型调用。

## 15. M1 验收命令

实现完成后至少运行：

```powershell
python -m compileall -q manju tests
python -m pytest tests/test_production_models.py tests/test_production_store.py tests/test_production_reducer.py
python -m pytest tests/test_production_scheduler.py tests/test_production_storyboard_adapter.py
python -m pytest tests/test_production_service.py tests/test_production_cli.py
python -m pytest
python -m manju.cli project --help
python -m manju.cli run --help
python -m manju.cli status --help
git diff --check
```

不得读取真实 `.manju.env`，不得调用 Provider 或产生费用。真实 LLM 冒烟不属于 M1 自动验收。

## 16. M1 完成定义

只有同时满足以下条件才能标记 M1 完成：

- 所有“必须实现”项有代码和测试；
- 所有“明确不实现”项没有伪实现或误导性 CLI；
- 完整离线测试通过；
- 所有故障注入恢复测试通过；
- Windows 和 Linux 路径/锁测试通过；
- `run`、`status` 和 `doctor` 的 JSON 契约有快照测试；
- 重复运行不增加模型调用计数；
- 现有 storyboard、visual 和 pipeline 回归测试通过；
- README 提供新命令最小示例，并明确 M1 只接入分镜；
- 代码评审确认 CLI 没有新增领域编排分支；
- 发布记录列出未实现的 M2 能力。

## 17. M2 入口条件

只有 M1 完成后才能开始图像审批闭环。M2 接入前必须先冻结：

- ApprovalRequest DTO；
- Grant DTO；
- VisualStageAdapter 映射；
- 顶层敏感事件 HMAC 实现；
- `awaiting_approval` 的命令和退出码；
- `call_reserved/submitted/settled` operation schema。
