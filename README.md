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
| `gemini-nano-banana-2.1` | 文生图、参考图编辑、上一张图继续修改 | Images → Chat |

具体可用性、尺寸和编辑能力取决于当前凭据及 Keylink 渠道，以 `GET /v1/models` 返回和实际请求结果为准。模型 ID 原样透传，服务端新增模型通常无需修改 Skill；自动识别支持图片名称或图片能力元数据的条目。新增请求字段、响应格式或识别规则时才需要适配代码。

`gemini-nano-banana-2.1` 已加入图片模型识别：目录仅返回该 ID、没有图片能力元数据时也会列入图片模型供用户选择。调用前仍必须在当前凭据的模型目录中查到它；沿用其他 Gemini 图片模型的尺寸规则和 Images → Chat 顺序，也允许显式固定 Chat。

`gpt-image-2.5-sunburst` 和 `gpt-image-2.5-flare` 是独立可选的变体 ID。选择后，文生图、参考图编辑、连续修改和端点重试都保留完整 ID，不替换为 `gpt-image-2.5` 或 `gpt-image-2`。各变体的尺寸信息分别读取；未公布时仍使用保守尺寸，高分辨率请求继续先确认。

## 功能

- 每次生成或编辑前调用 `GET /v1/models`，展示模型后等待用户选择。
- 自动路由统一优先 Images：文生图先 `/v1/images/generations`，图生图先 `/v1/images/edits`，失败后再尝试 `/v1/chat/completions`。
- 支持用户上传参考图，也支持按 Codex 任务复用最近一次成功图片继续修改。
- 编辑时清理明确的对话包装，仅发送本次修改要求；检查参考图尺寸和体积，必要时生成上传副本，保留原图与用户确认的输出分辨率。
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

建议安装 Pillow，用于参考图检查、压缩、生成结果的完整解码验证及 4K 大图的独立预览。需要规范化的大参考图在缺少 Pillow 时会停止发送。虚拟环境避免修改 macOS 系统 Python：

```sh
python3 -m venv "$skill_dir/.venv"
"$skill_dir/.venv/bin/python3" -m pip install Pillow
```

启动脚本优先使用该虚拟环境，然后检查 PATH 和 Apple Silicon / Intel 的 Homebrew 常见路径。需要指定其他 Python 时，设置 `KEYLINK_PYTHON` 为解释器路径。路径包含空格时保留双引号。脚本使用 `sh` 启动，无需额外安装 PowerShell，也不依赖可执行文件权限。

### Windows

安装相同的 Skill 目录后，通过 `scripts/keylink-image.ps1` 启动。它会优先寻找 Codex 内置 Python，再检查 PATH 中的 Python。核心程序需要 Python 3.11 或更新版本；参考图规范化需要在该启动器实际使用的 Python 环境安装 Pillow，Pillow 也用于大图预览。

## 凭据

Skill 不会输出密钥，按以下规则选择：

1. 明确配置的 `KEYLINK_API_KEY` 直接作为 Keylink 密钥使用。
2. 检测 CCSwitch 是否真正运行。兼容旧版当前 Provider、新版路由模式和聚合模式；聚合模式从当前成员中查找 API 地址匹配的 Keylink Provider，不把默认的 OpenAI 官方账号当作 Keylink。如果有多个不同的 Keylink 密钥且默认项无法确定选择，会要求用户选择 Keylink 默认 Provider 或手动设置 `KEYLINK_API_KEY`。
3. CCSwitch 没有可用密钥时，检查 Codex/Codex++ 是否保存了账号登录状态。**已登录账号：不从 `auth.json` 或 `OPENAI_API_KEY` 兜底**；提示用户手动配置 Keylink 密钥，启动 CCSwitch 并选择 Keylink（聚合模式需加入成员），或设置 `KEYLINK_API_KEY`。`--confirm-codex-auth` 不能绕过账号登录保护。
4. **未检测到账号登录：** 才检查 Codex/Codex++ 的 API Key 配置。无论 CCSwitch 未运行还是运行中但未找到密钥，都可以进入这个兜底。发现候选后先询问“这个凭据是否是 Keylink 的 Key”，用户确认后使用 `--confirm-codex-auth` 继续；否认或不确定则要求手动配置。

CCSwitch 只提供真实供应商密钥，图片请求仍直连 `https://keylinkclub.com`。新版写入客户端的 `PROXY_MANAGED` 是本地路由占位值，不能用于直连；OAuth 登录令牌也不会作为 API Key 使用。Skill 只读取 CCSwitch 和 Codex 配置，不覆盖账号登录文件。

**CCSwitch 已打开却提示未运行：** Codex 沙箱可能禁止查询宿主进程或连接本地监听端口。客户端现在将这种情况报告为 `ccswitch_detection_required` / `requires_host_execution`，并把运行状态标为 `null`（无法确认），不会当作未运行。Skill 会通过客户端的批准流程在宿主环境重试同一条命令；若执行权限仍不可用，会提示在自己的终端执行。无需因此重新配置密钥；也不会仅凭数据库里存在密钥就认定 CCSwitch 正在运行。

| 登录状态 | CCSwitch 状态 | 处理方式（未显式设置 `KEYLINK_API_KEY` 时） |
| --- | --- | --- |
| 已登录 Codex 账号 | 运行且找到 Keylink 密钥 | 使用 CCSwitch 密钥 |
| 已登录 Codex 账号 | 未运行或找不到 Keylink 密钥 | 报错并提示手动配置，不使用 `auth.json` 兜底 |
| 未登录 Codex 账号 | 运行且找到 Keylink 密钥 | 优先使用 CCSwitch 密钥 |
| 未登录 Codex 账号 | 未运行或找不到 Keylink 密钥 | 检查 API-key-only 配置，用户确认属于 Keylink 后使用 |

Codex++ 可通过 `CODEX_PLUS_HOME`、`CODEXPP_HOME` 或 `KEYLINK_CODEX_CONFIG_DIR` 指定配置目录；也支持 `CODEX_PLUS_CONFIG`、`CODEXPP_CONFIG` 和 `KEYLINK_CODEX_AUTH_FILE` 指定文件。不要把密钥写进聊天、命令参数或提交到仓库。

### VPN 和地址

API 地址固定为 `https://keylinkclub.com`，只有 `KEYLINK_BASE_URL` 或 `--base-url` 会覆盖它。忽略通用的 `OPENAI_BASE_URL`、`OPENAI_API_BASE` 和 CCSwitch 本地 API 地址。

网络连接沿用 Python 支持的系统 HTTP 代理或 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`。VPN 使用系统隧道或 HTTP / mixed 代理端口即可；本客户端不直接支持 SOCKS-only URL。代理端口应填入代理设置，不能作为 Keylink API 地址。macOS 终端需要手动指定代理时，可使用下列形式，将端口替换为 VPN 的实际 HTTP 端口：

```sh
export HTTPS_PROXY="http://127.0.0.1:<your-http-proxy-port>"
export HTTP_PROXY="$HTTPS_PROXY"
```

## 使用流程

1. 调用 `GET /v1/models` 查询当前凭据可用的模型和服务端公布尺寸，展示图片模型列表。
2. 等待用户选择模型；每次新生成或编辑都重新查询、选择，选择凭证只能使用一次。同一次请求若在输入预检阶段失败、尚未启动后台任务且凭证未消耗，修正输入后复用当前凭证和已选模型，无需再次查询或选择；已有后台任务先查 `status`。
3. 确定文生图或图生图。用户新上传的参考图优先；“不满意”“把刚才的改成”等纠正意图复用当前任务最近成功的原图，仅发送本次修改要求。带参考图时使用默认 `--mode auto`；即使误传 `--mode generate`，`--image` / `--use-last` 仍按图生图处理。明确要求独立新图时不传这两个参考选项，使用 `--mode generate` 避免自动复用旧图。意图不明确时先询问。
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

`last` 会核对本轮请求：仍在运行、已失败或被拒绝时，不把上一轮保存的图片当成本轮生成结果返回。只有用户明确要求取回历史图片时使用 `last --allow-previous`；返回的 `historical: true` 表示旧图，不代表本轮成功。内容拒绝会明确报告未生成新图，停止自动端点切换；不会自动改写提示词规避拒绝。

## 编辑请求与错误诊断

- 参考图默认 **每张最多 1 MiB、最多 15 张**，不再要求全部合计小于 1 MiB。超过单图预算的图片自动压缩，最长边不超过 2048；合规的小图保留原始字节。16 张及以上会在发请求前拦截，要求用户选择，不自动丢图。模型目录公布更低的张数、单图或总量限制时按更低值执行；只有总量限制确实需要时才从大图开始进一步压缩。
- `413 Request body is too large`：15 张满额图片，Images multipart 请求约 15 MiB，Chat 两份 Base64 约 40 MiB，再加提示词和字段开销。完整请求体的客户端保护上限为 **48 MiB**，不代表 Keylink 保证接受；未知渠道仍可能返回 413。错误按原端点策略报告/回退，不自动丢图或降低输出分辨率。只处理上传副本，保留原始文件、透明度及所选输出尺寸；无法保留合理细节时停止并提示换用更小的参考图。
- 所有参考图按原顺序发送；每次编码都从原图读取，避免对 JPEG 上传副本反复压缩。尺寸、格式、EXIF 方向不合规仍需转换。这些是客户端兼容预算，不代表 OpenAI 官方或 Keylink 公布的统一上限。
- `400 images[].image_url is required`：保留原先兼容的字符串格式；只有明确的 400/422 字段校验错误才在同一个 Chat 端点改用 `{ "image_url": { "url": "..." } }` 重试一次。标准 `messages[].content[].image_url` 始终保留，模型和输出尺寸不变。
- 编辑 prompt 会提取明确包装中的本轮用户要求。清理后仍有歧义或超过 4000 字符时，先由 Codex 精简，再发请求；不盲目截断，也不删除用户指定的画面文字。
- 编辑未指定 `--size` / `--aspect` 时继承原图像素尺寸（考虑 EXIF 方向），不会默认变成方图；压缩上传副本的尺寸与输出尺寸独立。显式尺寸/画幅优先；多张参考图尺寸不同时先选择输出尺寸，非标准/高分辨率原图仍需确认，不自动降档。返回图尺寸与请求不符时通过 `matches_requested_size: false` 和警告说明，保留服务端原始结果，不自动重发或本地放大。
- `reference_images` 记录原始/上传尺寸和字节数；`prompt_info` 记录提示词长度；`request_attempts` 记录每次 POST 的请求体字节数、起始时间、耗时及 HTTP 状态。后台任务保存稳定参考图副本。
- HTTP/API 错误、连接中断、200 无图片、图片下载/解码失败分别报告。支持更多嵌套 URL/Base64/Chat/Gemini 图片结构；不再仅凭响应的 `image/*` 类型就把非图片内容当成成功结果。
- 嵌套响应中的错误码也会在取图前检查，防止把错误响应附带的图片误报成功。有 Pillow 时验证图片完整性并实际解码；没有 Pillow 时仅做基本结构检查，建议安装以识别损坏图片。后台结果先保存发布，再清理计划任务，清理失败不会丢失已生成图片。

## 测试

```sh
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

macOS 也可使用 `python3` 或虚拟环境内的 Python。GitHub Actions 在 Windows 和 macOS 上运行测试；测试使用本地模拟服务，不消耗 Keylink 生图额度。
