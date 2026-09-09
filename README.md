# Keylink Image Codex Skill

通过 Keylink 的 OpenAI 兼容接口，在 Codex 中生成和编辑位图。这是独立 Skill，适用于 Windows 和 macOS；默认直接请求 `https://keylinkclub.com`。

## 支持的模型

| 模型 ID | 能力 | 自动调用顺序 |
| --- | --- | --- |
| `gpt-image-2` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |
| `gpt-image-2.5` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |
| `gpt-image-2.5-sunburst` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |
| `gpt-image-2.5-flare` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |
| `gemini-3-pro-image` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |
| `gemini-3.1-flash-image` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |

具体可用性、尺寸和编辑能力取决于当前凭据及 Keylink 渠道，以 `GET /v1/models` 返回和实际请求结果为准。模型 ID 原样透传，服务端新增模型通常无需修改 Skill；自动识别支持图片名称或图片能力元数据的条目。新增请求字段、响应格式或识别规则时才需要适配代码。

`gpt-image-2.5-sunburst` 和 `gpt-image-2.5-flare` 是独立可选的变体 ID。选择后，文生图、参考图编辑、连续修改和端点重试都保留完整 ID，不替换为 `gpt-image-2.5` 或 `gpt-image-2`。各变体的尺寸信息分别读取；未公布时仍使用保守尺寸，高分辨率请求继续先确认。

## 功能

- 每次生成或编辑前调用 `GET /v1/models`，展示模型后等待用户选择。
- 自动路由统一优先 Images：文生图先 `/v1/images/generations`，图生图先 `/v1/images/edits`，失败后再尝试 `/v1/chat/completions`。
- 支持用户上传参考图，也支持按 Codex 任务复用最近一次成功图片继续修改。
- 支持 URL、Base64、data URL 和 Chat 消息图片等常见返回格式。
- 默认使用保守尺寸，并对 2K、4K 等实验尺寸执行确认和结果尺寸检查。
- 将结果保存为本地图片，并记录实际使用的模型、端点和像素尺寸。
- 大图单独生成显示预览，原图保留用于下载和后续编辑；显示中断时可恢复最后结果，无需重新生图。

## 安装

在 Codex 中使用 `skill-installer` 安装以下仓库路径：

```text
https://github.com/Tilmirs/keylink-image-codex/tree/main/skills/keylink-image
```

也可以把 `skills/keylink-image` 目录放到 `$CODEX_HOME/skills/keylink-image`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/keylink-image`。安装后开启一个新的 Codex 任务加载 Skill。

### macOS

需要 Python 3.11 或更新版本，支持 Apple Silicon 和 Intel 的 Python 环境。先检查 `python3 --version`；如版本过旧，可通过 Python 官网或 Homebrew 安装。

下载本仓库后，在仓库根目录执行：

```sh
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/keylink-image"
mkdir -p "$skill_dir"
cp -R skills/keylink-image/. "$skill_dir/"
sh "$skill_dir/scripts/keylink-image.sh" --help
```

建议安装 Pillow，便于显示 4K 大图的独立预览。虚拟环境避免修改 macOS 系统 Python：

```sh
python3 -m venv "$skill_dir/.venv"
"$skill_dir/.venv/bin/python3" -m pip install Pillow
```

启动脚本优先使用该虚拟环境，然后检查 PATH 和 Apple Silicon / Intel 的 Homebrew 常见路径。需要指定其他 Python 时，设置 `KEYLINK_PYTHON` 为解释器路径。路径包含空格时保留双引号。脚本使用 `sh` 启动，无需额外安装 PowerShell，也不依赖可执行文件权限。

### Windows

安装相同的 Skill 目录后，通过 `scripts/keylink-image.ps1` 启动。它会优先寻找 Codex 内置 Python，再检查 PATH 中的 Python。核心程序需要 Python 3.11 或更新版本，Pillow 用于可选的大图预览。

## 凭据

Skill 按以下顺序读取凭据，并且不会输出密钥：

1. `KEYLINK_API_KEY`
2. `OPENAI_API_KEY`
3. 当前 CCSwitch Provider 中与 Keylink 主机匹配的凭据

CCSwitch 仅作为凭据来源。图片 API 请求不会经过 CCSwitch 的本地监听地址。

Windows 和 macOS 均读取用户主目录下的 `~/.cc-switch/cc-switch.db`。没有 CCSwitch 时，可给运行 Codex 的进程配置 `KEYLINK_API_KEY`。macOS 从 Finder / Dock 打开的应用不一定继承终端中设置的环境变量；可使用匹配 Keylink 的 CCSwitch 当前 Provider，或从已配置变量的终端启动 Codex。不要把密钥写进聊天、命令参数或提交到仓库。

### VPN 和地址

API 地址固定为 `https://keylinkclub.com`，只有 `KEYLINK_BASE_URL` 或 `--base-url` 会覆盖它。忽略通用的 `OPENAI_BASE_URL`、`OPENAI_API_BASE` 和 CCSwitch 本地 API 地址。

网络连接沿用 Python 支持的系统 HTTP 代理或 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`。VPN 使用系统隧道或 HTTP / mixed 代理端口即可；本客户端不直接支持 SOCKS-only URL。代理端口应填入代理设置，不能作为 Keylink API 地址。macOS 终端需要手动指定代理时，可使用下列形式，将端口替换为 VPN 的实际 HTTP 端口：

```sh
export HTTPS_PROXY="http://127.0.0.1:7890"
export HTTP_PROXY="$HTTPS_PROXY"
```

## 使用流程

1. 调用 `GET /v1/models` 查询当前凭据可用的模型和服务端公布尺寸，展示图片模型列表。
2. 等待用户选择模型；每次新生成或编辑都重新查询、选择，选择凭证只能使用一次。
3. 确定文生图或图生图。用户新上传的参考图优先；“不满意”“把刚才的改成”等纠正意图复用当前任务最近成功的原图，仅发送本次修改要求。意图不明确时先询问。
4. 确认尺寸。优先 `1024x1024`、`1536x1024`、`1024x1536`；高分辨率先展示服务端尺寸和可尝试候选，用户确认后才请求。
5. `2560x1440` 和 `3840x2160` 使用后台作业：启动后返回 job ID，轮询状态直到保存完成。后台作业继续接收响应，即使 Codex 当前任务中断，也不能重复提交。若任务中途被中断，恢复后先执行 `status`，不要重新执行 `run`。
6. 自动模式下按下表调用。首个端点出现 HTTP 错误、200 但没有图片、图片下载或解码失败时，使用同一模型、提示词、尺寸和参考图尝试 Chat。
7. 成功后保存本地原图，展示图片并返回原图链接、实际模型、端点和检测到的像素尺寸。两个端点都失败才汇总错误并询问是否换模型。

| 操作 | 第一次请求 | 失败后请求 |
| --- | --- | --- |
| 文生图 | `POST /v1/images/generations`，JSON `prompt` | `POST /v1/chat/completions` |
| 上传参考图 / 继续修改 | `POST /v1/images/edits`，multipart `image` 文件字段和 `prompt` | `POST /v1/chat/completions`，同时发送 `messages[].content[].image_url` 和 `images[].image_url` |

以上顺序对表中全部模型、变体及其他透传模型统一适用。用户明确指定 `--endpoint images`、`--endpoint chat` 或自定义端点时只调用指定端点，不自动切换。

### 高分辨率与等待

“高清”“超高清”“更高分辨率”“2K”“4K”“UHD”以及更大的像素尺寸都会进入高分辨率确认流程。16:9 的 2K 级候选为 `2560x1440`，4K 为 `3840x2160`；服务端未公布的候选会注明渠道可能不支持，不假定支持 `2048x2048`。

4K 请求可能耗时数分钟。发送 `3840x2160` 前，Codex 会提示用户耐心等待，并通过后台 job 持续等待同一个请求完成，避免重复提交。端点重试不降低尺寸、不更换用户选择的模型，也不本地放大。Chat 不保证遵循像素尺寸；结果会报告实际检测值，不将低分辨率图片称为 4K。

### macOS 调用示例

```sh
sh "$skill_dir/scripts/keylink-image.sh" models
# 用户从本次返回的模型列表选择后，再使用返回的 selection_token：
sh "$skill_dir/scripts/keylink-image.sh" run \
  --prompt "海边的灯塔" --model "gpt-image-2" \
  --selection-token "<本次查询返回的凭证>" --aspect landscape

# 2K/4K 使用后台作业；start 返回 job_id，随后查询 status
sh "$skill_dir/scripts/keylink-image.sh" start \
  --prompt "黑洞" --model "gpt-image-2.5" \
  --selection-token "<本次查询返回的凭证>" --size "3840x2160" --confirm-high-res
sh "$skill_dir/scripts/keylink-image.sh" status --job-id "<job_id>" --thread-id "<thread_id>"
```

编辑时添加 `--image "/Users/name/Pictures/reference image.png"`，或用 `--use-last` 继续修改当前任务最后一张图。显式固定 Chat 时添加 `--endpoint chat`。在 Codex 外手动调用时，给相关命令使用同一个 `--thread-id`，并保持工作目录一致。

### 显示成功但没有图片

如果是后台作业，先在原任务的工作目录查询保存状态：

```sh
sh "$skill_dir/scripts/keylink-image.sh" status --job-id "<job_id>" --thread-id "<原任务 ID>"
```

如果是旧版前台请求，再恢复保存结果：

```sh
sh "$skill_dir/scripts/keylink-image.sh" last --thread-id "<原任务 ID>"
```

Windows 使用 `keylink-image.ps1` 并传入相同参数。恢复不查询模型、不需要凭据、不重新调用生图 API。大图可能触发 Codex 预览的 `Invalid padding` 错误；独立 JPEG 预览用于显示，完整 PNG 原图保持不变。没有 Pillow 时仍返回原图链接。

## 测试

```sh
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

macOS 也可使用 `python3` 或虚拟环境内的 Python。GitHub Actions 在 Windows 和 macOS 上运行测试；测试使用本地模拟服务，不消耗 Keylink 生图额度。
