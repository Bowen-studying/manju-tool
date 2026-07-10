# API 配置与兼容契约

## LLM

```env
LLM_API_KEY=...
LLM_API_BASE=https://provider.example/v1
LLM_MODEL=model-name
```

根地址会补全为 `/chat/completions`；如果已经填写完整端点，不会重复追加。
支持标准 `choices[0].message.content`，也支持字符串 `output_text`。429、5xx 和网络错误会退避重试。

## 图片

```env
MANJU_IMAGE_API_KEY=...
MANJU_IMAGE_API_BASE=https://provider.example/v1
MANJU_IMAGE_MODEL=model-name
```

- 文生图：`POST /images/generations`。
- 本地参考图：multipart `POST /images/edits`。
- URL/data URL 参考图：兼容服务的 JSON `/images/generations` + `image`。
- 响应：支持 `data[0].url` 与 `data[0].b64_json`。

分镜批量生图按场景建立参考图，避免第一场背景污染所有场景。服务不支持 `/images/edits` 时，会回退到 JSON 参考图协议。

## 语音

```env
MANJU_VOICE_API_KEY=...
MANJU_VOICE_API_BASE=https://provider.example/v1
MANJU_VOICE_MODEL=tts-1
```

根地址补全为 `/audio/speech`，不会生成重复 `/v1/v1/`。未配置 API 时使用随项目安装的 `edge-tts`。
每个角色会获得稳定的 Edge/API 音色；文本、音色或参数变化时旧缓存自动失效。

## 视频

```env
MANJU_VIDEO_API_KEY=...
MANJU_VIDEO_API_BASE=https://provider.example/v1
MANJU_VIDEO_MODEL=model-name
MANJU_VIDEO_POLL_BASE=https://provider.example/tasks/{video_id}
MANJU_VIDEO_MAX_WAIT=600
```

支持：

- 直接返回顶层或嵌套视频 URL；
- 返回 `video_id`、`task_id` 或 `id` 后轮询；
- `completed/succeeded/success/done/finished` 成功状态；
- `failed/error/cancelled/canceled/expired` 失败状态；
- poll URL `{video_id}` 占位符或 `?video_id=` 查询模式。

本地参考图编码为 data URL 发送。具体视频供应商若不接受 data URL，需要使用公网 URL。

## 隐私

工具会把剧本、小说分块、提示词和参考图发送给配置的服务。工具不会主动上传 GitHub，
但第三方 API 的日志、保留周期和训练政策由服务商决定。敏感项目应使用受控服务。
