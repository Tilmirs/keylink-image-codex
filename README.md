# Keylink Image Codex Skill

通过 Keylink 的 OpenAI 兼容接口，在 Codex 中生成和编辑位图。默认直接请求 `https://keylinkclub.com`，支持 `gpt-image-2`、Gemini 图片模型以及服务端后续公布的模型 ID。

## 功能

- 每次生成或编辑前调用 `GET /v1/models`，展示模型后等待用户选择。
- 自动路由统一优先 Images：文生图先 `/v1/images/generations`，图生图先 `/v1/images/edits`，失败后再尝试 `/v1/chat/completions`。
- 支持用户上传参考图，也支持按 Codex 任务复用最近一次成功图片继续修改。
- 支持 URL、Base64、data URL 和 Chat 消息图片等常见返回格式。
- 默认使用保守尺寸，并对 2K、4K 等实验尺寸执行确认和结果尺寸检查。
- 将结果保存为本地图片，并记录实际使用的模型、端点和像素尺寸。

## 安装

在 Codex 中使用 `skill-installer` 安装以下仓库路径：

```text
https://github.com/Tilmirs/keylink-image-codex/tree/main/skills/keylink-image
```

也可以把 `skills/keylink-image` 目录放到 `$CODEX_HOME/skills/keylink-image`。

## 凭据

Skill 按以下顺序读取凭据，并且不会输出密钥：

1. `KEYLINK_API_KEY`
2. `OPENAI_API_KEY`
3. 当前 CCSwitch Provider 中与 Keylink 主机匹配的凭据

CCSwitch 仅作为凭据来源。图片 API 请求不会经过 CCSwitch 的本地监听地址。

## 使用流程

用户提出生成或编辑请求后，Codex 会先查询可用模型并展示给用户。用户选择模型后才会发送图片请求。每次新请求都必须重新查询和选择；模型选择凭证只能使用一次。

4K 请求可能耗时数分钟。发送 `3840x2160` 前，Codex 会提示用户耐心等待，并持续等待同一个请求完成。

## 测试

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```
