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

- 每次生成或编辑前调用 `GET /v1/models`，同一步询问模型、生成数量（1～4 张，默认 1 张）、设计内容/风格，以及克制、大师或专业模式，已有信息直接沿用。
- 按所选模式保留或优化提示词；专业模式只在花瓣、搞定设计、Pinterest、Dribbble 检索，每批展示 10 张风格参考，支持按序号选择或“换一批”，再与用户素材图一起生成。返回图片时附上实际发送的提示词，后台恢复也可查看。
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
2. 读取 CCSwitch 已保存的配置。兼容旧版当前 Provider、新版路由模式和聚合模式；聚合模式从已启用的成员中查找 API 地址匹配的 Keylink Provider，不把默认的 OpenAI 官方账号当作 Keylink。**软件关闭或无法检测运行状态时，只要保存的密钥匹配 Keylink 地址，仍可读取并直连使用。** 多个不同的 Keylink 密钥且默认项无法确定选择时，要求用户选择 Keylink 默认 Provider 或手动设置 `KEYLINK_API_KEY`；不会猜测。旧版仅有本地转发地址、无法确认供应商域名的配置仍要求 CCSwitch 确认运行。
3. 没有可用的 CCSwitch 密钥且运行状态已确定时，检查 Codex/Codex++ 是否保存了账号登录状态。**已登录账号：不从 `auth.json` 或 `OPENAI_API_KEY` 兜底**；提示用户配置并保存 Keylink 密钥（聚合模式需加入成员），或设置 `KEYLINK_API_KEY`。`--confirm-codex-auth` 不能绕过账号登录保护。
4. **未检测到账号登录：** 才检查 Codex/Codex++ 的 API Key 配置。无论 CCSwitch 未运行还是运行中但未找到密钥，都可以进入这个兜底。发现候选后先询问“这个凭据是否是 Keylink 的 Key”，用户确认后使用 `--confirm-codex-auth` 继续；否认或不确定则要求手动配置。

CCSwitch 只提供真实供应商密钥，图片请求仍直连 `https://keylinkclub.com`。新版写入客户端的 `PROXY_MANAGED` 是本地路由占位值，不能用于直连；OAuth 登录令牌也不会作为 API Key 使用。Skill 只读取 CCSwitch 和 Codex 配置，不覆盖账号登录文件。

**CCSwitch 已关闭或状态无法检测：** 客户端优先使用与目标 API 地址匹配的已保存密钥；来源为 `ccswitch-saved-provider` 或 `ccswitch-saved-aggregation-provider`，不表示软件正在运行。没有这种可用密钥且沙箱又无法查询进程/监听端口时，才返回 `ccswitch_detection_required` / `requires_host_execution`，状态为 `null`。Skill 通过客户端批准流程在宿主环境重试；权限仍不可用时提示在自己的终端执行。不会仅凭数据库文件存在就认定 CCSwitch 正在运行。

| 登录状态 | CCSwitch 状态 | 处理方式（未显式设置 `KEYLINK_API_KEY` 时） |
| --- | --- | --- |
| 已登录或未登录 Codex 账号 | 找到匹配地址的已保存 Keylink 密钥，不要求软件运行 | 优先使用 CCSwitch 密钥，不读取账号凭据 |
| 任意登录状态 | 多个匹配密钥无法确定选择 | 提示选择 Keylink 默认 Provider 或设置 `KEYLINK_API_KEY` |
| 已登录 Codex 账号 | 运行状态已知，但找不到可用 Keylink 密钥 | 报错并提示手动配置，不使用 `auth.json` 兜底 |
| 未登录 Codex 账号 | 运行状态已知，但找不到可用 Keylink 密钥 | 检查 API-key-only 配置，用户确认属于 Keylink 后使用 |
| 任意登录状态 | 没有匹配密钥，运行状态也无法检查 | 报告检测权限限制，申请宿主重试 |

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
2. 同时询问模型、生成数量（1～4 张，默认 1 张）和模式，只展示一次三种模式说明；内容/用途与风格可选填且沿用已有信息。主体、模型、模式明确后，克制原文不动，大师按既有流程重写，专业模式先检索并让用户选择风格参考图。每个新生成/编辑请求查询一次目录，用户回复选择、补全设计及修复未提交的输入/命令错误都属于同一请求，复用已有目录与未消耗的凭证，不重新获取令牌。真正新请求、用户要求刷新或令牌缺失/被替换/已消耗/主机改变时才重新查询并选择；已有后台任务先查 `status`。
3. 确定文生图或图生图。用户新上传的参考图优先；“不满意”“把刚才的改成”等纠正意图复用当前任务最近成功的原图，仅发送本次修改要求。带参考图时使用默认 `--mode auto`；即使误传 `--mode generate`，`--image` / `--use-last` 仍按图生图处理。明确要求独立新图时不传这两个参考选项，使用 `--mode generate` 避免自动复用旧图。意图不明确时先询问。
4. 确认尺寸。优先 `1024x1024`、`1536x1024`、`1024x1536`；高分辨率先展示服务端尺寸和可尝试候选，用户确认后才请求。
5. 多张输出以及 `2560x1440`、`3840x2160` 使用后台作业：启动后返回 job ID，轮询状态直到保存完成。后台作业继续接收响应，即使 Codex 当前任务中断，也不能重复提交。若任务中途被中断，恢复后先执行 `status`，不要重新执行 `run`。
6. 自动模式下按下表调用。首个端点出现 HTTP 错误、200 但没有图片、图片下载或解码失败时，使用同一模型、提示词、尺寸和参考图尝试 Chat。
7. 成功后保存本地原图，展示图片并返回原图链接、实际模型、端点、检测到的像素尺寸，以及所选模式和实际发送的完整提示词。两个端点都失败才汇总错误并询问是否换模型。

| 操作 | 第一次请求 | 失败后请求 |
| --- | --- | --- |
| 文生图 | `POST /v1/images/generations`，JSON `prompt` | `POST /v1/chat/completions` |
| 上传参考图 / 继续修改 | `POST /v1/images/edits`，multipart `image` 文件字段和 `prompt` | `POST /v1/chat/completions`，同时发送 `messages[].content[].image_url` 和 `images[].image_url` |

以上顺序对表中全部模型、变体及其他透传模型统一适用。用户明确指定 `--endpoint images`、`--endpoint chat` 或自定义端点时只调用指定端点，不自动切换。

输出数量通过 `--count 1|2|3|4` 设置，默认 1，超过 4 会在请求前拦截；这是 Skill 的本地限制。Images 文生图/编辑传入 `n`，Chat 按张请求，始终保持用户选择的模型、提示词、尺寸和参考图。多张使用后台 `start` 并查询同一个 job；已保存图片后不自动补发或重跑其他端点。平台少返回或后续出错时保留已有图片，通过 `requested_count` / `returned_count` / `count_complete` 报告实际完成数量，不把部分结果称为全部完成。输入参考图仍最多 15 张，与输出最多 4 张互相独立。

### 提示词模式与设计指引

| 模式 | 行为 | 参数 |
| --- | --- | --- |
| 克制模式 | 原文原样发送，不补词、不修改文字/标点/空白，适合已有完整提示词 | `--prompt-mode restrained` |
| 大师模式 | 保留目标、指定文案和限制，重写为完整视觉方案，统筹概念、布局与文字层级 | `--prompt-mode master` |
| 专业模式 | 先检索并选择风格参考图，再调优提示词，与用户素材图一起作为参考生成 | `--prompt-mode professional` |

查询模型后只在同一条提问中介绍三种模式，模型和模式独立选择，不做固定套餐。已有主题时回复“模型名，1 张，专业模式”就可继续；专业模式会在花瓣、搞定设计、Pinterest、Dribbble 中检索 10 张参考供选择，也可以换一批，大师沿用既有完整设计流程，克制原文不动。专业模式允许用主题相关品牌和相邻概念扩展搜索词，不要求与原句完全一致；成稿的背景、光线和辅助元素与大师模式对齐。模型/模式缺失时仅补问必要选择；主题全无、关键冲突、必需事实/精确文案缺失或高分辨率仍需确认，不编造事实。三种模式的流程见 [提示词流程](skills/keylink-image/references/prompting.md)，专业模式的检索和压缩见 [专业模式](skills/keylink-image/references/professional-mode.md)。

模型查询的网络权限由运行环境决定：环境明确限制联网且允许申请主机执行时，首次就使用批准流程，不先发送必然失败的请求；同一批准范围内沿用可用执行方式。未知限制时按普通执行，遇到明确套接字拒绝才最多申请一次只读重试，不把 HTTP 401/403、超时或连接失败都称为沙箱错误。真实权限提示不能由 Skill 取消；不会关闭沙箱、修改代理或自动提权。

海报、UI/UX、人物、商品、信息图等配方位于 [设计指引](skills/keylink-image/references/design-recipes.md)，模式边界见 [提示词流程](skills/keylink-image/references/prompting.md)。新增 [自媒体设计指引](skills/keylink-image/references/social-media.md)：覆盖小红书封面/笔记配图、抖音/视频号封面与视频插图、公众号头图/正文图、B 站封面、知乎知识配图，以及生活方式、教程科普、人物 IP、产品种草等方向。专业模式检索到的图片只作为用户选择后的参考，按交付物与主题调优；平台名不自动触发额外尺寸或批量请求。

大师模式增加“概念选择 → 平面排版 → 辅助元素 → 场景风格 → 成稿自检”流程。海报和封面强调字体、网格、留白与图文关系，辅助线、图标、纹样或拼贴须来自主题并承担分组、引导或叙事功能。设计指引提供 12 类场景的风格选择依据，不把科技、文化、生活方式等题材统一套成一种模板；纯摄影、无字素材和局部编辑不强加海报装饰。这是提示词设计方法的增强，实际审美效果仍需以生成结果判断。

参考 [awesome-gpt-image-2](https://github.com/freestylefly/awesome-gpt-image-2) 的分类与排版方法，以及 [editorial-allure-prompting](https://github.com/we1jia/editorial-allure-prompting) 中适当的摄影/服装描述，来源与 MIT 许可见 [sources.md](skills/keylink-image/references/sources.md)。自媒体扩展为本 Skill 的设计指导，不宣称是平台官方规范。不引入保证过审或拒绝后自动改写的流程。

指定文案、人物身份、参考图作用和编辑保留项优先；大师模式也不会将“只改标题”变为整图重设计。克制模式不做词汇替换。大师/专业模式只将适当的模糊审美描述转为具体视觉语言，不改变不允许的意图来绕过审核。

`models` 返回 `prompt_mode_choices`；`run` 的成功或已收到服务端响应后的端点失败结果返回 `prompt_mode` 和 `sent_prompt`，主机网络权限在响应前阻断时返回 `prepared_prompt` 并保留选择令牌供一次原命令重试，`last` 和完成的 `status` 也保留它们。`start` 或发送前的本地体积拦截只返回 `prepared_prompt`，表示准备的输入，不能证明已提交。`sent_prompt` 也不代表服务端已成功接收或生成。旧记录缺少提示词时不会补造。结果展示原文/优化稿时直接使用保存的 `sent_prompt`，不再重写。

带模式的编辑命令若混入会话包装，会在本地拒绝并要求干净的视觉提示词，避免悄悄改变原文；旧命令不带 `--prompt-mode` 时仍兼容原来的包装清理。手动调用必须自行准备最终 `--prompt`，仅添加模式参数不会让 Python 自动优化。

多行、引号、精确空白或 shell 特殊字符使用 UTF-8 文件输入：将最终提示词原样写入本地文件，通过 `--prompt-file "<文件路径>"` 替代 `--prompt`，避免命令行转义改变文字；文件内容不会作为 shell 命令执行。文件读取忽略可选的 UTF-8 BOM 编码标记，保留正文、空白和 CRLF/LF；空文件或只有编码标记的文件会被本地拦截。后台作业保存文本快照，不依赖原提示词文件继续存在，提示词以短横线开头也可正常传递。

### 高分辨率与等待

“高清”“超高清”“更高分辨率”“2K”“4K”“UHD”以及更大的像素尺寸都会进入高分辨率确认流程。16:9 的 2K 级候选为 `2560x1440`，4K 为 `3840x2160`；服务端未公布的候选会注明渠道可能不支持，不假定支持 `2048x2048`。

4K 请求可能耗时数分钟。发送 `3840x2160` 前，Codex 会提示用户耐心等待，并通过后台 job 持续等待同一个请求完成，避免重复提交。端点重试不降低尺寸、不更换用户选择的模型，也不本地放大。Chat 不保证遵循像素尺寸；结果会报告实际检测值，不将低分辨率图片称为 4K。

### macOS 调用示例

```sh
sh "$skill_dir/scripts/keylink-image.sh" models
# 用户从本次返回的模型列表选择模型和数量后，再使用返回的 selection_token：
sh "$skill_dir/scripts/keylink-image.sh" run \
  --prompt "海边的灯塔" --prompt-mode restrained --model "gpt-image-2" \
  --selection-token "<本次查询返回的凭证>" --count 1 --aspect landscape

# 2K/4K 使用后台作业；start 返回 job_id，随后查询 status
sh "$skill_dir/scripts/keylink-image.sh" start \
  --prompt "黑洞" --prompt-mode restrained --model "gpt-image-2.5" \
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
