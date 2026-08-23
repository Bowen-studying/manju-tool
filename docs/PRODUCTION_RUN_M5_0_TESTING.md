# ProductionRun M5.0 离线 video_prompt 测试说明

M5.0 只实现 storyboard 下游的确定性、离线 `video_prompt` 阶段。它读取经过 SHA-256 快照校验的 `storyboard.output`，为每个 storyboard shot 生成一个 `video-prompt-v1` 记录，并写入 `video_prompt.main`。阶段不调用 LLM、视频 Provider、网络、审批、grant 或任何 `call_*` 事件。

M5.0 的线性调度顺序是 `storyboard → 已启用的 voice 阶段 → video_prompt → visual`；产物图中 `video_prompt.main` 只依赖 `storyboard.output`。完整产物可复用，残缺、篡改、输入快照或 authority/manifest 绑定不一致时 fail-closed。项目状态 DTO 不暴露路径。

专项测试：

```text
pytest -q tests/test_production_m5_0_video_prompt.py
```

相关回归：

```text
pytest -q tests/test_production_m4_3_paid_voice_tts.py tests/test_production_m4_2_voice_tts.py tests/test_production_m4_1_voice_director.py tests/test_production_m4_0_voice_script.py
```

真实视频 Provider、视频三阶段调用链以及费用/审批/授权集成明确 deferred 到 M5.1；`production.video` 仍保持既有兼容配置，M5.0 使用独立的 `production.video_prompt` 配置。
