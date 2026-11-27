---
name: lymphloop-drama-swap
description: 将任意竞品保健品（滴剂、硬胶囊、软胶囊、片剂、软糖、粉剂）的竖屏短视频广告换成 yesnap LymphLoop 成片（Antigravity 原生看听版，默认零外部 Gemini 调用）。由当前 Agent 直接用 read_file 看视频/帧板与听音轨，配合本地 faster-whisper、LaMa 去字幕与规则表设计，默认走 100x 渠道 `100x-minimax-h3`（`standard` / `768P` / `9:16`，时长按段）生成；穿帮补做默认走人物卡模式；对白改词优先音节数对齐；回剪、盖水印、终检与内置 Range HTTP 看板一条龙完成。
---

# LymphLoop 换品（Antigravity 原生看听版）v2.2（2026-11-24：零外部 Gemini 原生模式、原生看视频+听音频全链路、对白音节数对齐、6 项结构化审片自检、内置看板 HTTP 服务；保留 2026-11-23 人物卡补做与 2026-11-22 批次能力）

本插件版为 **Antigravity 原生多模态执行环境**定制：**当前 Agent 本身具备直接看视频（`.mp4`）、看帧板（`.jpg`/`.png`）与听音频（`.mp4`/`.wav`）的能力**。
- **默认零外部 Gemini 模式**（`scripts/agent_native.py`）：`google_api.py`、`speech_map.py`、`scenes.py`、`watermark_brand.py`、`swap.py` 默认完全不调用外部 Gemini API（不再依赖 `GEMINI_API_KEY`）；本地用 `faster-whisper`（经 ffmpeg 解码规避 PyAV 兼容性问题）+ 视觉/音频特征 + Antigravity 原生 `read_file` 承接全部看、听与语义判断。仅当显式设置 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时才回退外部 Gemini。
- **独立插件副本**：仅在本插件目录内迭代，**不要改动** `~/.claude/skills/lymphloop-swap-claude`；各自的 RUN 目录不要混用。

## Antigravity 原生“看 + 听”全链路清单

| 环节 | Antigravity 原生做法（替代外部 Gemini） | 脚本原生支持 |
|---|---|---|
| **1b 建竞品档案** | `read_file` 看 `overview_grid.jpg` + 听 `voice_samples/*.mp4`（记性别、年龄、口音、情绪、语速、BGM）+ 读 `transcript.json`，填 `competitor.json` 后 `--accept` | `swap.py competitor` 自动出 24 格总览图、本地 Whisper 字幕与 3 段 6 秒音色采样片 |
| **3–5 识别与筛查** | `read_file` 批量看拼接网格 `shot_grids/shot_grid_##.jpg`；有疑点短镜/弱露瓶镜头直接 `read_file` 看 `clips/S###.mp4` 动态画面；写出 `pro_nodes.json` 和精简 `verdicts.json` 导入 | `detect-pro --import-nodes`、`screen --import-verdicts` 一键写入并提交状态 |
| **7 切段与锁边** | `scenes.py` 本地色彩直方图 + 音轨 RMS 跌落自动初分切点；`speech_map.py` 本地 Whisper（停顿 ≥ 0.7s 自动断句防合并）+ 停顿标点切分；Agent 看帧板写 `lock_overrides.json` | 零 Flash、零长静默吞句 |
| **8 提示词与音节对齐** | 画内可见嘴部改词**优先按音节数对齐**（如 4 音节 `Lymphoria` → 4 音节 `Yesnap LymphLoop`，避免 2 音节 `LymphLoop` 导致首次生成口型拉扯）；直接听原片采样写精准音色句 | `generate --prepare-only` 自动跑 `check_dialogue_syllables` 预警 |
| **9 提交前听审与黄条检测** | 所有带 `--mute` 的上传参考片，付费前必用 `read_file` **直接听**确认旧词清零且未切到邻词；自动扫描字幕黄色高亮底条残留（避免 LaMa 漏清） | `prepare-only` 输出 `listen_before_submit` 与 `highlight_backplate` 告警 |
| **10 审片（看动图 + 听真声 + 6 项自检）** | 不只看静态帧板！对每个生成段直接 `read_file` **看+听** `generation/results/G##-a1-c1.mp4`，核查口型/节奏/动效/是否整段露侧面/是否缩水；`select` 传 `--audit` 做 6 项结构化穿帮自检，任一项为 `false` 自动拦下进 `goofs.json` | `review` 默认本地 Whisper 词检 + 输出 `agent_watch_and_listen`；`select --audit` 硬卡点 |
| **10c 补做（人物卡模式）** | 首次有穿帮的镜头，第二次起默认人物卡模式（不挂参考视频，彻底脱离旧瓶轮廓与旧口型；不挂场景图，提示词照 ai-drama-ad 写法：固定物件+轴线锚点、机位表写死背景、每阶段身体表演、七样音色句）；切点偏移的无嘴补做自动保留底片音轨（`first_generation`），段尾补做自动防 1 帧旧尾闪回，`--drop` 支持多次重复传参 | `shot_regen.py round open / splice` 原生内置 |
| **12 终检与看板预览** | `final-screen` 跳过 Flash，自动把变动镜头和闪回帧板拼成紧凑大图 `agent_review/changed_shots_##.jpg` + `flashback_##.jpg` 供 `read_file` 逐张看；用 `compare_board.py --serve` 一键起本地 Range HTTP 服务给用户看同步对比看板 | `final-screen` 自动拼图；`compare_board.py --serve [--runs ...]` |

## 固定事实

- **产品**：yesnap LymphLoop，Orange Mango 口味，30 支/盒，每支 8 g 液体条包。青绿色翻盖纸盒（10×7×13 cm 级）+ 青绿色柔性条包（约一根食指长、比拇指指腹略宽，侧边缺口）。**画面里不撕条包**：展示用未开条包（可轻晃），喝的镜头从第一帧就是已开条包闭唇轻抿，一镜到底就中间加硬切（规则见 [controlled-edit.md §4.2](references/controlled-edit.md) 第 6 条）。`assets/product/profile.json → facts` 是事实来源，[product-actions.md](references/product-actions.md) 是写提示词前要看的事实、状态链与动作对照。
- **送模型的产品参考图**（前三张来自真人手持实拍，识别和生成都只用这些和 `RUN/refs/` 里为这条片子做的参考图）：
  - `handheld_photo`：`assets/product/stick-handheld-scale.jpg`，手持密封条包（展示、抽出、拿着讲话）。
  - `opened_packet_hand`：`assets/product/opened-packet-handheld.jpg`，手持已开条包（喝、往水里挤、拿着已开条包的镜头用它，2304×4096 原图）。
  - **纸盒只有两张图，一起给**（用户 2026-11-20："如果是要换纸盒的段落可以用我这套方法，其他关于盒装的参考图都可以移除掉，没什么用了"）：`carton_views` = `assets/product/carton-views.jpg`（同一个纸盒白底九个角度，无手）+ `carton_hand` = `assets/product/carton-hand-scale.jpg`（一只真手拿着纸盒正面，只管纸盒和手的比例、握法）。**换纸盒的段落按 [carton-swap.md](references/carton-swap.md) 的五段式写**（生成目标 / 参考素材职责 / 主体与关系 / 事件脚本 / 保持一致），用 100x `100x-seedance-2.0` 720P 参考视频生成；测试片 G05、G16 用这套一次成功，之前同样镜头按旧规则失败 3–4 轮。旧的四视图拼图、正面单图、顶面图和 carton-sources 都已移除（备份在 `~/.codex/skills-backup/lymphloop-swap-claude-carton-2026-11-20/`）。**提示词里绝不写"与原瓶大小一致"**（003 G09：纸盒被画成瓶子那样细高）。
  - **屏幕上的商品页两张**（用户 2026-11-22：“电脑和手机的页面应该都要放在skill里”）：`phone_page` = `assets/product/landing-page-phone.jpg`（手机上的 Yesnap Health 店铺商品页实拍截图）、`desktop_page` = `assets/product/landing-page-desktop.jpg`（电脑浏览器里的商品页截图）。原片手机或电脑屏幕上显示竞品购物页 / 落地页时，整页换成我方商品页（价格不用管）。规则表 S1 自动判；观察没认出来的，在 `detect/overrides.json` 那一镜写 `"screen": "phone"` 或 `"computer"` 再跑 decide。
  每段先按 [controlled-edit.md §4](references/controlled-edit.md) 判断现实里该用条包还是盒装、条包开没开，再选图。`assets/physical/` 四段实拍视频只给 Agent 看，不上传。
- **竞品不限于 Lymphoria**（用户 2026-11-11）：滴剂、硬胶囊、软胶囊、片剂、软糖、粉剂都换。每条视频先 `swap.py competitor`，脚本出 24 格全片总览图、本地听写和声音采样，Agent 看图、听采样后写出本条的竞品档案 `RUN/detect/competitor.json`（品牌、剂型、容器与剂型单元长相、服用方式、难例、漂浮水印文字），核对后 `--accept`；识别、审片、终检和水印扫描都以它为准。外用、器械类不在范围内，`--accept` 会拒绝，先告诉用户。`assets/competitors/` 只是内置的 Lymphoria 锚点图（没有档案的旧 RUN 用），只用于识别，永远不作生成参考。
- **识别契约** `assets/prompts/detect-conflicts.txt` 是用户原文，SHA 锁定，不改。
- **生成路线**：每一段（纸盒、条包、只改台词的段、人物卡补做都一样）默认走 **100x 渠道 `100x-minimax-h3`（`standard` / `768P` / `9:16`）** 参考生成（用户 2026-11-26 实测确认并切为默认；画幅固定 `9:16`、分辨率固定 `768P`、线路 `standard`、时长 = 段长向上取整的整数秒 `4–15`，全部走接口参数，提示词里不写；单价约 3.89 积分/秒，参考片直传 100x 不经隧道）。如需临时切回 `100x-seedance-2.0`（`720P`）可设 `LYMPHLOOP_X100_MODEL=100x-seedance-2.0`，如需切回 APIMART `seedance-2.0`（`720p`）可设 `LYMPHLOOP_GEN_CHANNEL=apimart` 或改 `~/.cache/lymphloop-models/local-slots/channel.txt`。100x `seedance-2.5` 无 `9:16` 参数，不用。参考片是去掉字幕和水印的原段，末帧定住补到同样的整数秒；按本 skill 的锁边规则切段，不超过 15 秒。选片时 `select` 会自动检查 H3 末词是否超出原段时长并放宽 `clip_out` 防止截词。详见 [执行细则](references/workflow.md#9-generate)。
- **逐段写提示词，一律五段式**（用户 2026-11-20："统一成五段式"；来自用户在 Gemini 里用 clone-seedance-video skill 写、G05/G16 一次成功的提示词，规则见 [rules-now.md](references/rules-now.md)）：【生成目标】【参考素材职责】【主体与关系】【事件脚本】【保持一致】。`plan` 写出骨架，`swap_design.py decide` 按设计表把形态、图、数量、移除清单和每镜动作填好（只重写没人动过的骨架）；agent 只填【写…】：场景、人物、音色、每阶段的景别机位和终态、台词语气。一个镜头一个阶段，每阶段以“结束时……”收尾；台词写成 `{原词}`，带语气和语言；【事件脚本】只写正向，“完全移除 / 不采用 / 仅”只写在另外几段；正文不写时间戳、秒数、画幅、时长、分辨率（这些全由接口参数定，用户 2026-11-20），也不写镜头号、规则号；每张上传的图在【参考素材职责】里单独一行写职责。`generate --prepare-only` 只收五段式，并核对设计表、阶段数和终态、台词花括号、参数不进正文、否定句、勺子、厘米数，不过就拒绝。
- **一次成片**（用户 2026-11-11）：每个目标段**最多一次付费生成**，**生成什么都要用**，没有第二次付费。没有产生付费结果的尝试（任务失败已全额退款、提交时被拒且未扣费、参考片没发布出去）由脚本自动用同样输入重提，每段最多 3 次，全部记进账本 `unpaid_attempts`；状态不明（超时、断线）可能已被接收扣费，绝不盲目重提，先查 APIMART；401/402/403（密钥、余额）要用户处理。成功率全靠提交前做对：参考片干净（无字幕、无水印、首尾不带邻镜头的帧）；竞品逐镜头找到每一处并写成目标产品；目标动作按原片手的运动轨迹选；服用一律闭唇轻抿；提示词按官方写法。审片只记录缺陷和做免费的本地处理（裁切、`select` 自动去字幕、拼接、定点修补）。
- **提交前检查实际上传的参考片**。先用 `generate --prepare-only` 产出准确文件和帧板，亲自看：字幕和水印去干净了、修补处看不出痕迹、首尾没有邻镜头的帧、结尾定住的是本段最后一帧；复听有消音的窗口，再按 controlled-edit.md §7 逐条自查提示词，然后提交该段（付费只此一次）。
- **凭据**：默认零外部 Gemini 模式无需 `GEMINI_API_KEY`（仅 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时读取）；APIMART 用 `APIMART_API_KEY` 或钥匙串 `codex-seedance-apimart/api-key`（钥匙串条目沿用旧名）；都不出现在日志。
- **运行环境**：macOS 或 Windows（Linux 同 Windows 的做法）。文字和人脸识别按系统选引擎，输出同一种 JSON，调用方不用区分：macOS 用系统 Vision（三个 `.swift`，首次用时编译），Windows / Linux 用 RapidOCR + OpenCV YuNet（`scripts/vision_scan.py`；`LYMPHLOOP_OCR_ENGINE=rapidocr` 可在 Mac 上也选它）。系统差异（文件锁、进程、字体、Windows 上的 UTF-8）都收在 `scripts/oscompat.py`。Python 3.12 venv 在 `$SKILL/.venv`（numpy、opencv-python-headless、Pillow、faster-whisper、onnxruntime，Windows / Linux 另加 rapidocr）；去字幕用的 LaMa 模型（208 MB）第一次用时自动下载到 `~/.cache/lymphloop-models/`，所有命令用 `$PY=$SKILL/.venv/bin/python` 运行（Windows 是 `$SKILL\.venv\Scripts\python.exe`，文档里其余写法不变）；ffmpeg / ffprobe 找 `$SKILL/bin/` → PATH → `~/.codex/tools/ffmpeg/`，cloudflared 找 `$SKILL/bin/` → `~/.codex/tools/wan-media/2026.10.0/`；公网托管缓存在 `~/.cache/lymphloop-host-claude/`。需要外网（100x 生成接口、首次下载模型；切回 APIMART 渠道时另需 APIMART 和 Cloudflare；默认不连外部 Gemini）和系统凭据库读取权限（macOS 钥匙串；Windows 凭据管理器，或直接用环境变量）。Windows 上人脸模型（232 KB）第一次用时自动下载到同一个模型目录。
- **首次安装（仓库版）**：`.venv` 和 `bin/` 不随仓库分发。`$SKILL/.venv` 不存在时先建环境：`python3.12 -m venv "$SKILL/.venv" && "$SKILL/.venv/bin/pip" install -r "$SKILL/requirements.txt"`；ffmpeg / ffprobe 装到 PATH 即可，但要带 drawtext 滤镜（烧时间码用；Homebrew 的普通 `ffmpeg` 不带，用 `brew install ffmpeg-full` 并把 `$(brew --prefix ffmpeg-full)/bin` 放进 PATH）；默认 100x 渠道不需要 cloudflared。**Windows**（PowerShell，在 `$SKILL` 目录下）：`py -3.12 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt`，ffmpeg 用 `winget install Gyan.FFmpeg`，Node 用 `winget install OpenJS.NodeJS.LTS`；脚本在 Windows 上会自己切到 UTF-8 模式，不用另外设置。装好后 `$PY scripts/oscompat.py` 打印本机情况（系统、编码、字体），`$PY scripts/watermark_cleanup.py preflight` 打印识别引擎。100x 客户端 `scripts/x100.py` 已随包提供，它调用本机安装的 100x MCP 桥（默认 `~/.codex/plugin-sources/100x-mcp/plugins/100x`，可用 `X100_PLUGIN` 指到别处），需要 Node 22+ 和已登录的 100x 账号。

## 流程（每条视频一个 RUN 目录，`scripts/swap.py` 子命令按序）

| 步 | 命令 | Agent 要做的事 |
|---|---|---|
| 0 | `init --video V --run RUN --out OUT` | 记录源与探帧 |
| 1 | `prepare` | 非整数帧率自动规范化 |
| 1b | `competitor` → 看总览图、听声音采样、填 JSON → `competitor --accept` | 本条竞品档案：品牌、剂型、容器与剂型单元、难例、水印词；剂型超出范围先告诉用户 |
| 2 | `watermark scan` → 看 `scan.json` 摘要与几张帧 → `watermark apply`（或确认无水印用 `watermark none`） | **有存好的水印模板时不做 OCR**（用户 2026-11-21："固定位置+有水印才盖"）：LYMPHORIA.CO 的片子水印都在同样 6 处（15 条实测差 ≤ 3 px），`assets/watermarks/lymphoria-fixed.*` 存了这 6 处和每处的笔画模板，`scan` 逐帧做模板比对（10 分钟片约 25 秒，CPU 只占这一小段，云端一样快），摘要 `method: template`，看 `places` 每处的帧数和 `scores` 即可 apply；比对不上（别的竞品或别的水印）自动退回下面的 OCR 扫描，`--ocr-scan` 可强制 OCR。OCR 扫描时：确认框选的是漂浮水印（图标 + 竞品水印词整个，Lymphoria 片子是**水滴图标 + LYMPHORIA**），不是包装印刷或字幕；扫描是模糊匹配 + 四边旋转放大补扫（见下），先每 4 帧扫、命中处前后逐帧补扫，4 个进程分段同时扫（10 分钟片子约 1 分钟）；随后定出水印的**固定位置**，每个位置的截图存到 `RUN/watermark/place_kind/`，Agent 逐个看是真水印还是产品标签（瓶标只算标签，用户 2026-11-21；确认是漂浮水印的位置写进 `RUN/watermark/agent_positions.json`，字段见 [rules-now.md](references/rules-now.md)“水印候选先分清是不是产品标签”，不在其中的命中按包装印刷保留），按像素量出真实大小，再只读这几个位置补扫整片（`scan.json → positions`，摘要里有 `places`，看一眼位置和大小对不对）；**`watermark scan`、`detect-pro`、`screen` 三步同时后台跑**（它们只共享读 RUN，各写各的，用户 2026-11-19）；去水印母版用于识别和回剪里保留原片的部分 |
| 3 | `shots` | 得到 `RUN/shots/sheets/S###.png`，**每张都要看** |
| 4 | `detect-pro`（和第 2、5 步同时跑） | Agent 读全片听写、逐批看镜头拼图 `shot_grids`（可疑短镜直接看 `clips/S###.mp4`），按固定契约写出三栏节点与最小改词 `pro_nodes.json`，`detect-pro --import-nodes` 导入 |
| 5 | `screen`（和第 2、4 步同时跑） | Agent 逐批看镜头拼图逐镜头筛查，写出 `{replace_ids, unsure_ids, notes}`，`screen --import-verdicts` 导入 |
| 6 | `merge` | 看帧板的结论、第 4 步的节点、第 5 步的逐镜头筛查取并集；要推翻时写 `RUN/detect/overrides.json`，再 merge |
| 7 | `scenes.py RUN` → 复核 → `plan`（直接出，不再先 `--dry-run`；要改就改 override 重跑，听写有缓存） | 只给替换镜头前后 20 秒内的切点分级（本地比较切点两侧的色彩直方图和音轨跌落，不调模型），更远的切点一律当段落锁边；看每个切点的锁边分级，判错的写进 `RUN/plan/lock_overrides.json`；再按下一节的规则切段，生成提示词模板 `RUN/prompts/G##-a1.txt`。段边界自动对齐到逐帧切点（`cutscan.py`），边界外 0.5 秒内的碎镜头并进生成段；`edge_snap` 里写“check the seam”的边界要亲自看（用户 2026-11-21：杜绝滴剂瓶闪回） |
| 7b | `swap_design.py observe` → `decide` | **换品设计，agent 不临场判断**（用户 2026-11-20：“我最终是要让agent自己能够判断，不要给我审”）：Agent 看 `observe` 出的拼图（可疑镜头直接看片段），对每个替换镜头只回答客观问题，`observe --import-obs` 导入，规则表决定换成纸盒 / 未开条包 / 已开条包、动作照原片还是重新设计、数量和参考图，写进 `RUN/plan/swap_design.json`。规则表和流程见 [rules-now.md](references/rules-now.md)，它优先于下面各文档里零散的形态规则 |
| 7c | **批次里画面相同的片子共用结果**：`reuse --from 代表片RUN [--from 另一条] [--dry-run]`（这条片先照常做到 `plan`；代表片先做完） | 用户 2026-11-22（上一批 26 条里 15 条是同一组画面的改版，靠手工共用）。逐帧比对两条片的画面：代表片每个生成段在这条片里找到同样的帧，就把它审定过的选用片、字幕、瑕疵记录原样导入成 `G9nn` 段（账本 `reused_generation`，零次提交），这条片计划里落在同一段画面上的段撤下（`dropped_for_reuse`）；有代表片里没有的画面的段留下，照常生成。先 `--dry-run` 看报告（`adopted` / `set_aside` / `kept` / `notes`），再正式跑。共用段不写提示词、不做参考片、不补字幕；回剪、盖水印、终检、看板照常包含它。共用段里发现穿帮回代表片的 RUN 修，修完在这条片重跑同一条命令（每次从头算）。细节见 rules-now.md“跨 RUN 共用结果” |
| 8 | `plan` 一出就后台跑 `generate --refs-only`；7b 的 `decide` 已把骨架按设计表填好，亲自补完每段 | `--refs-only` 先把所有参考片去字幕、去水印、补到整秒（和写提示词同时进行，准备时直接用缓存）；`RUN/prompts/G##-a1.txt` 是五段式骨架（[rules-now.md](references/rules-now.md)），`# views:` / `# design:` 和产品部分来自设计表，不改；看该段高清单帧和帧板后填满所有【写…】（场景、人物、每个说话人的音色、每阶段景别机位和“结束时”终态、台词语气和语言；**每个有产品的阶段写“大小参照：”**，用这一镜里看得见的手、脸或身边物件说清产品多大，前后镜头一致，用户 2026-11-25，见 rules-now.md“每镜大小参照”），骨架注释里写着每个阶段对应的镜头和原片时间；有改词的段定好消音窗口。设计表没覆盖的产品镜头（骨架会写出来）先 `swap_design.py observe --shots S###` 再 `decide`，不自己定形态 |
| 9 | `generate --prepare-only` → 检查实际上传参考片 → `generate` | 先在本地准备，不调用接口：五段式检查和设计表核对不过就拒绝；消音窗口切到旧词时终端会提示放宽，照改再准备；参考片去字幕、去水印、裁掉首尾杂帧、补到整数秒（12 秒的段约 3 分钟）；有改词的段用 `--mute G##:起-止`（只动音轨）。参考片里的竞品原样保留（`--erase` 已停用，用户 2026-11-19）；Agent 自己看一遍参考片，找出每处竞品所在的镜头，逐条核对：【参考素材职责】有“完全移除原片中的…”，且每处所在镜头的那一阶段写出了我方纸盒或条包，缺了就补上（脚本不再自动列出竞品，也不会因此拒绝）。看帧板确认干净、复听消音处后，已准备的整批一次性交 APIMART seedance-2.0（9:16、720p、时长按段，都是参数）；**提交前先看隧道上行**：APIMART seedance-2.0 约 35–50 秒取不到参考片就拒（11-20 2210：隧道 22–80 KB/s，9 段两轮全拒，未扣费但用掉免费重提），`generate` 按 `hostd` 测速估算最大参考片 30 秒内传不完就拒绝提交；这时等隧道变快，或报 `x100_gen.py quote` 的免费报价请用户同意改走 100x 直传（参数同样 9:16、720P、按段时长）；每段最多一次付费生成，未付费的失败脚本自动重提 |
| 10 | `review` → 看生成片帧板，并直接看、听生成片 → `select`（`--audit` 六项自检） | 每段都 select，结果一律采用；**只找穿帮**（用户 2026-11-20："只要比例差不多正确，不要大幅度偏差都不算瑕疵。重点要找的是穿帮"；清单见 [qa-checklist.md](references/qa-checklist.md) 开头），有穿帮按第 10c 步补做，比例差不多、包装小字、纸盒透视这些不记瑕疵；**纸盒比例不靠看视频判**（2026-11-24）：`select` 自动跑 `carton_check.py` 量（正面高:宽 > 1.65、外形 > 2.3、竖着站看不到正面就拦），超线时用 read_file 看并排对照图再定，`--audit` 的 `scale_ok` / `front_face_ok` 不能和量出来的结果矛盾；定裁切；残留字幕 `select` 自动从生成片上去掉；`sound_drift` 记下声音相对口型的偏移 |
| 10b | `captions`（4 段同时；只重做选用片、台词或样式变了的段，`--force` 全部重做） | **原片字幕原样复制**（用户 2026-11-19："字幕是不固定的，每一条字幕可能都不一样"）：没清字幕的参考片和清过的参考片相减，在字幕行和标题卡的框里就是原片字幕本身（字体、颜色、逐词高亮、描边、位置都照原样），按生成片里同一句话说出的时刻平移后叠回去；标题卡（WEEK 1、SEMANA 5、片尾标语）也一起回来。含被替换词的那几句才按量出的样式新画（`style.json`，字体不像用 `--font` 换）。字幕行只收原片字幕高度带里、和本段台词同词的文字，瓶标、席卡、徽章、手机页面上的字不会被复制。挪动过画面的本地拼接段退回全部新画；`--draw-all` 强制全部新画。看每段的 `qa/G##-captioned.png` 再回剪 |
| 10c | **补做用人物卡模式**（用户 2026-11-23："第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式"）：首次生成照旧挂原片参考视频；穿帮的补做不挂参考视频，只挂人物卡 + 产品图（说话镜头再挂说话人的一段声音），场景、机位、站位、动作、台词全写进提示词。先写 `RUN/cards/cast.json`（人物在整条片里最清楚的正脸），`shot_regen.py cards build --run RUN` 出卡并亲自看；`round open` 默认就是这个模式，骨架开头 `# cards:` / `# views:` / `# voice:` 三行，场景只用文字搭，写法照 ai-drama-ad prompting.md（用户 2026-11-24）：环境写固定物件和轴线锚点，机位表写死背景，原片几镜写几个阶段，每阶段写身体表演，说话人写七样音色句；不挂场景图。详见 [rules-now.md](references/rules-now.md)“补做一律用人物卡模式”；`--mode reference` 只在用户点名要旧做法时用。**穿帮镜头单独补做，一轮一批**（用户 2026-11-22）：审完**所有段**再动手，把整条片的穿帮列成清单 → `shot_regen.py round open --run RUN --file goofs.json`（每处自动定位、裁参考片、出帧条和已填好的提示词）→ 改每个 `shots-regen/NAME.txt` → `round submit`（后台；全部检查通过后一次提交，只等一次）→ 看每张 `NAME.result.png` → `round splice [--drop 仍有穿帮的]`（每段一次拼回并选用）→ `finish.py --from captions`。单处手工流程（`locate` → `ref` → `prepare --goof --why 原因 --note 说明` → 生成 → `splice --into` → `select`）仍可用 | **不要发现一处做一处**：三批 26 条实测，145 次补做里集中一批做的片子 12–24 分钟做完，一处一处做的拖 3–6 小时（每次都要等约 7 分钟生成、再跑一遍收尾）。`goofs.json` 每处写 `{"seg": "G05", "at": "生成片里的秒数", "why": "原因", "note": "看到了什么"}`；`why` 是 qa-checklist 穿帮清单的八类之一（`competitor` 竞品还在 / `count` 数量 / `shape` 形态 / `side` 侧面当正面 / `physics` 物理 / `identity` 人或场景变了 / `speech` 台词口型 / `text` 字幕水印，或写序号 1–8），**每次补做都要登记原因**（`shot_regen.py reasons RUN…` 按类别汇总，用来改规则表，提高首次成功率）。提示词骨架直接取这一段已写好的提示词、只留这一镜的阶段，agent 只改针对穿帮的那几句。同一段的多处补做一次拼回；`round splice` 之后补字幕只重做选用片变了的段，回剪、盖水印、终检只处理变了的块。还有穿帮的（`--drop`）进下一轮，范围缩到单个动作（清单里给 `from_s` / `to_s`），同一处最多三轮。用户 2026-11-20："如果首次生成的整段中出现了穿帮，对照原片看穿帮的是那一镜，然后单独针对穿帮的镜头重写提示词生成。"审片发现某一段有穿帮（竞品残留、背景被参考图带偏、产品形态错、侧面当正面等），不整段重生成：`locate` 在生成片里找出穿帮所在的那一镜（按硬切），对照原片同一时间的镜头，判断是否左右镜像、这一镜里有哪些台词和改词，给出参考片帧号和拼回帧号；单镜头长镜头时用 `--from-s` 从穿帮前的句间停顿开始。针对穿帮原因重写这一镜的五段式提示词（例：侧袋里的竞品写成“侧袋口只露出青绿色条包的上端”；纸盒一律两张图（特写例外已撤回））。补做只做一个镜头（`prepare --goof` 检查参考片里没有硬切）；声音（用户 2026-11-21，详见 rules-now.md“说话镜头只用自己的声音”）：这一镜看得到有人说话，就原速补做、连口型一起生成，只用它自己的声音（`ref --real-speed`，短镜头也原速，末帧定格补到 4 秒）；看不到嘴的镜头才可以放慢补做，下面铺原片声音或首次生成的声音（`splice --source-sound` 只对放慢的镜头有效，自动标 `--no-mouth`）。**补做后还有穿帮就继续缩小范围再做**（用户 2026-11-20 补充："二次生成如果还有穿帮，继续缩减范围，甚至缩减到单镜头动作继续第三次生成"）：第二次补做只做还出错的那个动作（`locate --from-s/--to-s` 把范围收窄到单个动作），必须比上一次短；同一处（按原片秒数算，跨段也算）最多补做三次，即连首次在内最多生成四次（用户 2026-11-21："后续只要skill进程中检查出来了，都可以执行第三次补做"）；第二次必须比第一次短，第三次不能比之前最窄的一次长；`prepare` 按原片秒数核对，超出就拒绝，再做必须用户明确要求 `--again`；`--user-quote` 写这条规则原文。生成后看帧条确认穿帮没了、接口两侧逐帧干净，再选用；详见 [workflow.md §10c](references/workflow.md) |
| 11 | `recut` | 以审过的片段时长顺剪到 `OUT/master.mp4`。有画内对白时，画面和同期声从同一素材取同一时间窗。**按内容缓存**（用户 2026-11-22）：每块按内容编码，补一个镜头只重编那一块，后面的块原样无损拼到新位置 |
| 11b | `brand` | 按成片时间轴映射原水印位置并盖上**白色不透明 LYMPHLOOP**（原位置、原大小、原方向，连水滴图标一起盖；**每个固定位置整条片只用一个框、一个方向，字不忽大忽小**），写回 `OUT/master.mp4`（未盖版留作 `master.unbranded.mp4`），逐帧抽检确认盖上了；按块盖，没有水印的块不重编，盖过的块按内容缓存 |
| 12 | `final-screen`（默认只查改动处；`--full` 查整条） | 只查生成段、保留原画面的计划段、原水印所在帧（各前后 1 秒）；**再和上一次终检的成片按内容比**（用户 2026-11-22），只查新内容和上次有未了结问题的块，闪回帧板也只出新内容的（`--no-baseline` 关掉）：没动过的原片镜头在第 5 步已经筛过（用户 2026-11-19：整条再筛 45 分钟，改后约 6 分钟）。并重新查竞品水印词（必须 0 帧，`watermark_left` 列出残留时间；模板扫描的片子用同一模板逐帧比对整条成片、盖了字的帧不算，约 30 秒；默认只读 `brand_plan.json → positions` 的几个固定位置、整条成片都读，约 1–2 分钟，`--full-watermark` 回到全帧 + 四边补扫）；成片整条听写查口播（`speech_left` 竞品名和我们没有的成分必须为空，`speech_check` 逐个看）；原片有水印却没跑 `brand` 时拒绝；**闪回关**（用户 2026-11-21；只出帧板、不调 Gemini，agent 自己看）：每个接缝、每个拼接点前后 0.5 秒每 3 帧取一帧（加接点两帧），每个不超过 0.5 秒的碎镜头逐帧，一处一张、300 px 大格的帧板在 `final/<sha>/flashback/sheets/`；agent 逐张打开，看手里、包口、口袋、桌面、画边有没有不是我方条包/纸盒的东西（滴剂瓶多半只露出米白或琥珀色的一截圆柱），可疑就把那一处前后逐帧放大看；看完 `swap.py flashback-review --run RUN --clean`，看到就 `--hit 帧-帧 --note "…"` 并当穿帮修；有标记时复查。新发现的漏换段可首次提交，已提交段做本地修补或记录瑕疵。**产品帧板**（2026-11-24，用户："可是我看着还是有产品穿帮镜头"）：送了纸盒图的生成段里每一次出现的产品按原尺寸裁出来，每秒 3 张（`final/<sha>/product/`，回执 `product_sheets`），量尺量不到的被挡住、很小、在动的盒子靠这一步；用 read_file 逐张看形状，看完 `swap.py product-review --run RUN --clean` 或 `--hit "秒-秒:看到了什么"` 并补做；补做后整段再看，不只看补的那一镜 |

**第 10b–12 步和看板一条命令跑完**：`nohup "$PY" scripts/finish.py --run RUN > RUN/logs/finish.log 2>&1 &`（retrim → captions → recut → brand → final-screen → compare_board，每步单独日志 `RUN/logs/finish-<步>.log`，出错即停并打出日志尾；原片无水印自动跳过 brand；改完某段可从 `--from recut` 等处接着跑）。**第一步 retrim 把每段选片里的定格帧裁掉**（用户 2026-11-23：“回剪的时候把定格帧裁掉”）：补做镜头参考片末帧定住补到 4 秒，模型照抄出定格，拼接里的和段尾的都在这里裁，免费、画面声音一起裁、不短于原镜头、说话处不裁；`recut` 发现还有定格帧就拒绝。不用真实画面补满 4 秒（用户否决）。11-19 测试片每轮修完都手敲这五步，约 12 分钟，其中终检 9 分钟。 **出看板之前**，这条成片的闪回帧板和产品帧板都要用 `flashback-review` / `product-review` 记录，产品帧板记了未修的穿帮时 `finish.py` 停在 `NEEDS_AGENT_REVIEW`，修好重出或用户明确同意后才 `finish.py --from board`（2026-11-24）。

**生成通道**：默认 **100x `100x-minimax-h3`（`standard` / `768P` / `9:16`）**（用户 2026-11-26："现在把这个skill默认的生成渠道也改为100x-minimax-h3 (standard/768P)"），`generate` 和 `shot_regen.py round submit` 直接走 `scripts/x100_gen.py run-batch`（参考片直传、按 100x 积分计费、结果写进统一账本，后续 select / splice / captions / recut 完全无缝）。也支持 `LYMPHLOOP_X100_MODEL=100x-seedance-2.0`（`720P`）或切回 APIMART `seedance-2.0`（`LYMPHLOOP_GEN_CHANNEL=apimart`）；100x `seedance-2.5` 没有 9:16 参数，不用。付费前同样要用户同意。

详细参数、文件布局、改词消音与本地修补和恢复方式见 [workflow.md](references/workflow.md)。

### 切段（第 7 步，用户 2026-10-23/24 定的规则）

每个硬切点先分级（`scripts/scenes.py`：本地比较切点两侧镜头的色彩直方图，加音轨跌落作线索，每个镜头首、中、尾三帧的条图存在 `RUN/plan/scene_strips/`；Agent 对照条图和帧板复核，可在 `RUN/plan/lock_overrides.json` 改）：

| 级别 | 什么情况 | 作用 |
|---|---|---|
| **段落锁边** | 下一个镜头换到完全不同的环境（花店变客厅、厨房变美发店） | 段落不跨过它；往前往后探索段的边界，探到这里就到头 |
| **镜头锁边** | 同一个环境，但下一个镜头和上一个完全没有连续性（时间跳跃、换了衣服、不相干的新动作，比如 WEEK 1 的特写） | 级别低于段落锁边；段的边界也锁在这里 |
| 普通硬切 | 同一时刻的连续镜头：正反打、特写、同一段对话的插入镜头；隔着门的交叉对话、插进场景里的 3D 动画也算 | 只在两个锁之间超过 14.9 秒时才在这里切 |

标题卡和音轨断开只是分级的辅助线索，不是判据。

1. **两个锁之间是一个块**；块里有产品镜头或要改的台词（`needs_edit`）就整块生成，反应镜头、另一个说话人都带上。
2. **台词不能被切断**：一句话跨过镜头锁边就两块合并（不超上限时）；跨过段落锁边不合并，标出来给 Agent 看。
3. **块超过 14.9 秒才在块内切**：切在句间停顿里的硬切点，先过本地的停顿和标点判断（问句后面不切；停顿 ≥ 0.55 秒，或句子以句号、叹号结尾才切）；问答、道歉与回应这类一来一回不拆开，Agent 复核时把切错的并回去。
4. **既没有产品镜头、也没有要改台词的段不生成**，原片照放（`segments.json → skipped_product_free`）；只改旁白、画面没有产品的段生成后 `select --audio-only`。计划里有、但只因一两帧或角落一小块入选的段可以不提交：`select --keep-source-picture`，残留在成片上定点修补（[controlled-edit.md §4.7](references/controlled-edit.md)）。
5. **不切碎、不凑满**：14.9 秒是上限不是目标；短段先将参考补足接口最短时长。单个镜头超过 14.9 秒就在句间停顿切开，审片时在生成结果上找合适的接点。

细则（听写分块、安静切点、单镜头切开、实测结果）见 [workflow.md §7](references/workflow.md)。

### 漂浮水印（第 2 步和第 11b 步，用户 2026-10-25 定）

- 漂浮水印的目标词来自竞品档案（`watermark_text`，否则品牌名；`watermark scan --target WORD` 可改）。Lymphoria 这批片子的漂浮水印是**水滴图标 + LYMPHORIA**；任何竞品的水印都按整个图标加文字的范围清除和覆盖。下面以 LYMPHORIA 为例，模糊匹配对任何目标词按同样规则生成（目标词里不属于 LYMPHLOOP/YESNAP 的任意 4 个连续字母）。
- **存好的模板优先**（用户 2026-11-21："扫水印这件事太慢了……都非常占 CPU"，选了"固定位置+有水印才盖"）：`scripts/watermark_template.py` + `assets/watermarks/<词>-fixed.json/.npz`。水印是固定的半透明图，有没有出现是笔画比对，不用读字：解码一遍（灰度 720×1280），每处在 ±6 px 内用高通后的笔画模板做归一化相关，≥ 0.30 是水印、相邻 ≥ 0.20 的帧并进来、8 帧内的空档补齐、两头各补 6 帧（水印随硬切出现和消失，开头 1–3 帧分数只有 0.10–0.16 但看得见）。用 2 条片子的 OCR 命中帧建模板，另 6 条验证：OCR 读到的帧全部覆盖（36 处 0 漏），另外多找到 OCR 漏读的帧（亲眼看过，都是真水印：被背景压住的和白底上很淡的）；1506620285858447 没水印的帧最高 0.16。仍然**只在有水印的帧**清除并盖 LYMPHLOOP，不整条挂字、不整条模糊（实测：横排那两处落在头发和脸上，模糊框一直看得见）。参考片去水印和终检也用同一模板。新竞品的水印也固定时，用几条 OCR 扫过的片子 `watermark_template.py build --film 视频 scan.json …` 建模板。
- **找全**（没有模板时的 OCR 扫描）：OCR 认淡水印常认错（LYMPHORTA、LYMRHORIA、MPHORIA、@LYMPHOIN），扫描按 PHOR/HORI/RHOR/MPHO 模糊匹配，排除包装上手写体 LymphLoop（读成 LyMPHOR）和盖上去的 LYMPHLOOP；再把左右两条边竖条（转正、放大、加对比）和上下两条边横条每 3 帧补扫一遍，竖排、很淡、贴角的水印靠这一步（2026-10-25：这批 7 条成片带着全帧扫描漏掉的水印交付）。
- **固定位置**（用户 2026-11-20："整条片下来基本都是在同一个位置"、"lymphloop 字样会忽大忽小，很丑，所以我想要固定住它"）：同一条片子的水印只出现在几个固定位置（Claude 30542865505302802：四角竖排 + 偏中间对角两个横排，共 6 处，12 次闪现合计 23 秒，占全片 3.3%）。扫描后把命中框按位置聚类（`watermark_brand.positions`），每处在命中的帧里取“每帧都在的边缘”量出真实范围（水印不动、背景在变；OCR 框不可靠：横排的行框本来就含水滴，旧的补图标又往左多扩 16 px；竖排全帧框两头都扩），同一个水印的各处统一成同一大小（`measure_places`），再只读这几处、每帧补扫一遍整片（`position_scan`：各处裁出、转正、放大，原样 / 加对比 / 高通三种各一份，拼成一张图一次 OCR）。这一遍在 Claude 那条片子 265 秒找到了全帧和四边补扫都漏掉的一次右上角水印。**不整条从头盖到尾**：原片 97% 的时间那里没有字，整条盖会多出一直挂着的白字，两处还在画面中间；省时靠终检只读固定位置。OCR 在水印淡入淡出时认不出，每个水印按位置各自前后补 0.8 秒（字幕行不补）。
- **覆盖**：`recut` 之后跑 `brand`，原水印出现的每一帧先把该位置修补干净（连生成段里可能带出的淡水印一起），再在同一位置盖上白色不透明的 LYMPHLOOP，大小填满原水印（图标 + 文字）的范围、字母比例不变；**每个固定位置整条片只用量出来的那一个框**（`fix_stamps`；Claude 那条片子同一处的盖字在 16×52 和 18×68 之间跳，原因是 OCR 只认出半个词的帧和淡入淡出补帧的框外扩 4 px），读向按该处多数票统一，每帧原水印的框仍然一起修补干净；方向和原水印一致（竖排的读向 OCR 判断不了：有模板的位置用模板记录的方向；没有模板的默认按向下读，放大截图存在 `RUN/watermark/direction/`，Agent 看截图核对，`brand` 的回执 `orientations` 列出每处的方向和来源；方向反了就在 `scan.json` 那一处的命中上写 `"orient": "v_up"` 再跑 `brand`）；生成段也照样盖，因为盖的是原片水印的位置。字幕或招牌里提到 LYMPHORIA 的整行文字（如 "LYMPHORIA 4 HERBS"）照样清除，但不盖 LYMPHLOOP（它不是漂浮水印）。
- **验收**：`final-screen` 在盖过的成片上重新做模糊匹配 + 边缘补扫，LYMPHORIA 必须 0 帧；有残留就 `watermark add-final`（把残留帧并入扫描）→ `brand` → `final-screen`，不用重新 recut；`brand` 的回执里有逐帧抽检结果（盖上的字是否是白色）。

### 审片自动关（第 10 步）

- **字幕关**：逐帧 OCR，和本段台词同词的文字算残留字幕；包装印刷不算。
- **台词关**：听写生成片，新词必须出现，被替换的旧词不能出现。
- **声音**：`sound_drift` 只是生成的台词相对原片的时间变化，不是口型检查（生成的嘴跟着生成的声音走）。口型看片判断：生成的声音和它自己的嘴对不上，按穿帮补做那一镜（`shot_regen` 的 `lip_sync`），不换声音。
- **物理关**（2026-11-11 加）：Agent 看生成片时把"条包无支撑竖立"和"服用镜头手里没有条包"这两类镜头逐帧看（`--audit` 的 `physics_ok`），都记为失败、归到 controlled-edit.md §4.8 的类别，写进交付说明（不剪掉镜头，见规则 3d）。
- **残留文字关**：参考段上原有的文字（竞品水印、标题卡、竞品标签）只要还出现在生成片画面任何位置就不过。
- **纸盒关**（2026-10-25 加，送了纸盒图的段）：`select` 自动跑 `carton_check.py` 量比例和朝向；Agent 对照这一段送的九视图 `carton_views`，按 10 × 13 × 7 cm 看纸盒是否偏细、偏扁、偏小（被指尖捏着、不比手指长）、偏大、前后不一致、多出来或变形；结果只记录（一次成片后不再拦 `select`，写进 selections 的 `defects` 和交付说明）。根因和写法见 [controlled-edit.md §4](references/controlled-edit.md)：原片容器的轮廓、数量和握法可能残留，只能靠提交前把比例句、宽度句和数量写全。

自动关通过后仍须看帧板。未通过时按 [controlled-edit.md §6](references/controlled-edit.md#6-审片与本地修补) 做免费的本地处理并记录缺陷，不重新生成。

## 并行（用户 2026-10-24 要求；2026-11-19 提速）

- **时长目标**：10 分钟的片子从拿到视频到交付约 2 小时（11-19 测试片原来 4 小时 20 分）。机器步骤能同时跑的都同时跑：第 2、4、5 步一起后台跑；`plan` 后立刻 `generate --refs-only`；审片（`review`）、补字幕（`captions`）、准备时的竞品检查都按段并行。

- **一条视频一个 RUN 目录、一个处理进程**，互不读写；一条出错不影响其他条。多条视频同时跑时，每条视频开一个 Agent 会话（或在一个会话里给每条视频各起一条后台命令链），各自从第 0 步跑到交付。
- **一条视频的段全部并行**：`generate` 先锁定全部段、检查全部提示词，再一次性提交（`apimart_wan.py run-batch`，默认 16 个线程提交、轮询、下载）；消音段和普通段可以在同一批里。APIMART 账号支持至少 600 个并发任务；5 条视频 × 约 25 段 × 1 个任务 ≈ 125 个任务。
- **参考片上传到 APIMART**（用户 2026-11-20 "隧道问题怎么解决"）：每段的参考视频先用 APIMART 的视频上传接口 `POST /v1/uploads/videos?retention_days=3`（multipart 字段 `file`，MP4 ≤ 50 MB，3 天保存在账号免费额度内）传上去，请求里的 `video_urls` 填它返回的 CDN 地址；按内容哈希缓存在 `RUN/apimart/video_uploads.json`，剩一天以上就复用，重提不重传。原因：11-20 片子 2210 走本机 Cloudflare 临时隧道时 9 段两轮全被拒（"Failed to probe reference video duration"），隧道只有 22–80 KB/s，seedance-2.0 取片 35–50 秒就放弃；同一个 1.5 MB 文件直传 APIMART 只要 2.2 秒。产品图照旧走 `/v1/uploads/images`。`scripts/hostd.py`（全机共享的本地文件服务 + Cloudflare 隧道 + 取片闸门）只在上传接口不可用时手动打开：`LYMPHLOOP_VIDEO_HOST=tunnel`；这时 `hostd.py status` 看测速，慢不要重启隧道（会打断别的会话），全机同时取片最多 8 个（`LYMPHLOOP_FETCH_SLOTS`）。第三方公网托管（`LYMPHLOOP_HOST_MODE=litterbox|tempfile`）和自建地址（`LYMPHLOOP_PUBLIC_BASE`）只在用户同意时手动打开。
- 共享的只有这个托管进程和生成渠道（100x 或 APIMART）的账号额度；每条视频的缓存（听写、切点分级、OCR、图片上传记录、生成账本）都在各自 RUN 里。

## Agent 执行守则（2026-10-24 Claude 与 Codex 同批对比实跑后定，Antigravity 照用）

1. **亲自看图**：帧板（`RUN/shots/sheets/`）、生成片帧板（`RUN/qa/`）、拼接前后的对比图，一律用图片查看工具打开亲自看；只看 JSON 或 OCR 结论就选片、判 keep，算违规。每段选片前至少看过一张生成片帧板。
2. **长步骤放后台**：`watermark scan`、`generate`、`recut`、`final-screen` 可能跑 5–30 分钟（`detect-pro`、`screen` 现在只出拼图和导入结果，几秒就完）。先 `mkdir -p RUN/logs`，再用 `nohup "$PY" scripts/swap.py <步> --run RUN … > RUN/logs/<步>.log 2>&1 &` 后台跑，再用 `tail -n 30 RUN/logs/<步>.log`、`"$PY" scripts/swap.py status --run RUN`、`RUN/generation/jobs/*.json` 查进度。不要让前台命令卡到超时被杀：被杀的提交会在账本里留下 submitting 状态，必须先去 APIMART 查任务再处理，绝不盲目重提（会重复扣费）。
3. **只动自己的 RUN**：开跑前新建自己的 RUN/OUT 目录；绝不往别人的 RUN 写提示词、提交生成、改 `selections.json` 或回剪。`swap.py` 的写操作带 RUN 锁，碰到"被另一个进程占用"的报错说明有人在用这个 RUN，换自己的目录，不要删锁。
4. **不杀别人的进程**：要停进程先 `ps -axww | grep -E "swap.py|apimart_wan|hostd"` 看完整命令和 RUN 路径，只停自己启动的。`hostd.py` 是这份 skill 所有 RUN 共享的托管，不要 `stop`；只在托管真正不通（`hostd.py status` 显示不健康）时才用 `hostd.py restart-tunnel` 换隧道，**慢不要重启**（每次重启会打断所有会话正在取片的任务）；不要自己另起隧道、文件服务或第三方上传。提交交给 `swap.py generate`：它经全机取片闸门排队，**不要**自己分批、限并发、绕开 `apimart_wan.run_batch` 写提交脚本。
5. **按实际时长顺剪，画面和它生成的声音一起用**。整段的台词和口型由模型重新生成、一起出（用户 2026-11-11），提示词里每一句都写出来，不写"原声照原片"。选用的画面带着它自己的声音进出，不拆开；所选段长了，后续画声一起顺延，短了就前移。保持原速，避免为卡原时长补帧、截句或变速。**画面里看得到有人说话的地方，声音只能是同一个文件、同一时刻的自带声音，不论台词改没改**（用户 2026-11-21："这种带有口型同步的替换段，不论有没有改口播，都应该要用生成段自带的音频"）：不自动换原片声音、不把首次生成或别的片段的声音贴到说话的嘴上、不借词。原片声音、首次生成的声音、借来的词只能放在看不到嘴的画面上（旁白、背影、空镜、插入镜头），`splice` 和 `select --sound source` 要求 agent 看过后用 `--no-mouth` 标出这些地方，否则拒绝。生成的声音和它自己的嘴对不上是穿帮，补做那一镜，不靠换声音。
6. **次数记账**。每个目标段只有 a1 和 c1，最多一次付费结果（用户明确要求重生成的段另开 a2，见规则 4）。未付费的失败由 `apimart_wan` 自动重提（`unpaid_attempts` 记每一次），不要自己写重提脚本；状态不明的提交恢复时只查询原任务或下载结果。本地拼接由 `swap.py splice` 记为 `local_splice`。
7. **凭据**：密钥只从环境变量或钥匙串读，不打印、不写进日志或提示词；沙箱禁网、禁钥匙串时请用户切到完全访问再跑。
8. **汇报**：每条视频交付时写清段数、各段用了第几次生成或哪种拼接、总花费、终检结果、已知瑕疵。
9. **批次里报了母版问题，不要整批停下**（11-22 一个线程报完问题就停住，9 条只交了 3 条）。问题写进维护者记录（复现命令 + 报错原文），其余段和片子照常推进到交付；母版修好后直接续跑原命令。补满三次的穿帮也不停、不问，先当瑕疵交付，最后汇总成一张清单交用户一次裁定。批次经验见 references/rules-now.md 的"批次运行经验"和"上限瑕疵的破例与本地修复"。

## 不可违反的规则

1. **宁多换不漏换**。第 4 步的节点、第 5 步的逐镜头筛查、看帧板时任一处判"有/拿不准"的镜头都进替换清单；只有亲眼看过帧板并写了 override 才能改回 keep。
2. **提示词由 Agent 写，审片由 Agent 定**。脚本的自动关只是辅助；不把"API 成功"当"内容合格"。每段先写清楚原片里发生了什么，再写要生成什么。
3. **物理优先**：每段先判断原片动作在现实里用条包还是盒装做得到，绝不为了迁就原动作让产品弯折、拉长、自己立着或被捏扁；同一场戏里产品形态和位置前后连续；画面里不撕条包。
3b. **动作设计避开参考片照抄，竞品靠替换句和参考图换掉**（用户 2026-11-14：后期补穿帮太多，要提高一次成功率；2026-11-19：“不能用抹除原片竞品的方式，因为模糊地带会被2.5给照搬回来，甚至抹除到人脸的部分，还会被重塑人物……应该从提示词和参考图下手”）：模型会照抄参考片的姿态、道具和口型。原物离手立着的那一拍条包不离手；提示词里不出现任何勺子；产品不被手整个包住。**参考片只去字幕和水印，竞品原样保留，不抹、不定格替换**（11-19 测试片 6 段抹过的参考片：LaMa 留下糊块、半透明瓶影，框到下巴和手）。每一镜里看得见的每一处竞品都写替换句：`将@视频1中<竞品的颜色、形状、位置、谁拿着>更换为@图片N的<我方产品、状态、比例>`，竞品在做条包做不到的动作（倒、滴、拧、舀）时连动作一起换：`将@视频1中…的动作更换为：…`；替换句之后照常正向写新产品的样子、正面朝镜头和比例句。原片里产品的特殊呈现（手机购物页、整箱产品）给专门的参考图：放在 `RUN/refs/`，`# views:` 写 `run:文件名`（11-19 用户给了我方商品页截图）。`generate` 准备后 Agent 自己看参考片找出每处竞品，核对每处所在镜头那一行都有替换句，缺了就补上（脚本不再自动列出竞品）。竞品品牌名不写进提示词（会被画成字），用外观描述。中远景里偏小的产品改成人物手里一直捏着已开封条包；短段落里反复切镜、产品时有时无的，把同一人物拿产品的镜头裁出来接成一条单独生成，再拆开拼回原位（controlled-edit.md §4.8 第 21、22 类，用户 2026-11-19）。产品只写在参考片原本有竞品的位置，不往原片没有产品的地方加（会凭空显形、变成贴纸）。小而远的镜头和无产品镜头按 [controlled-edit.md §4.8](references/controlled-edit.md) 处理；后期修补只是兜底。
3c. **禁止贴图补救生成段**（用户 2026-11-14）：生成出来的画面里有穿帮（产品变形、立着、残留瓶子或勺子、手糊），不许在画面上贴色块 / 色条、贴产品图、跟踪贴片、从别的帧或别的片子抠一块贴进来、局部调色把竞品染成产品色，也不许用 LaMa / inpaint 把画面里的产品或道具抹掉、改画。能做的只有整帧级处理：去掉 0.5 秒以内的闪帧、借同一素材兄弟片的整镜（画面和同期声一起，接在硬切上）、消音或补音；这些都救不了时，写进交付说明并报告用户，由用户决定是否付费重新生成这一段。`region_inpaint.py` 会拒绝在生成段帧范围里用整框填充（gate none）。**也不许用原片画面放大重取景、裁切别段的生成画面去顶替生成镜头，不许自己写 ffmpeg 脚本拼生成段**（11-14 NV0560：G01 车边交接换成原片约 6 倍放大的头部，另一位女士和递条包动作没了；G05 末镜两人宽景换成一人头部放大，等于删了原片内容）：`recut` 只接受账本里登记的生成结果（付费结果、`swap.py splice` 结果）及 `select` / `captions` 自动做的去字幕、加字幕版本；`splice` 只接受本 RUN 的生成结果和原片整帧，别的文件一律拒绝。仍然允许的只有文字类处理：去残留字幕和标题卡（`select` 自动去字幕、`:purple`）、抹道具上的旧品牌字（`:ink`），以及原水印位置盖白色 LYMPHLOOP（`brand`，用户已认可）。没有重新生成的原片画面（去字幕、去水印、原片里残留的小瓶）不在此列。
3d. **不许删原片内容、不许跳过要换的段**（用户 2026-11-14："codex 会擅自把原片的一些段落给删掉，直接不做，这是不允许的"）：成片里原片的每个镜头、每句台词都要在，要么是原片，要么是换好的生成段。不剪掉镜头或整句来躲开穿帮，不把有竞品的段留着不做，不把选片剪得比原段短（`recut` 拒绝短 0.5 秒以上的选片）。生成段有穿帮就报告用户，由用户决定是否付费重生成；只有用户明确同意删某段时才加 `recut --user-approved-cut G##`。用户最在意的是产品变形和画面穿帮，品牌名念得略偏是小问题。**原片人物照原片生成**（用户 2026-11-14："照原片生成人物"）：卡通人物、骷髅角色、"婴儿身体成年人"这类 AI 造型，都按参考片原样生成，不自行把人物镜头换成静物、不删人（11-14 NV0467 把 30 个人物镜头换成纸盒静物，用户否决）。**参考片也不许把人裁出画**：去竞品时缩放裁切参考片、把人物或这一镜的主体裁掉，模型会换人、换衣服、换场景（11-14 batch6 18 段）；`generate` 提交前用 `ref_framing.py` 检查，原片里的脸被裁出参考画面就拒绝（只有裁掉的是海报、包装上的人像时才加 `--allow-reference-crop G##`）。
4. **一段最多一次付费生成，结果一律采用**（用户 2026-11-11："每段实际付费只能一次"）。审片不过不重做。没有产生付费结果的尝试（任务失败已全额退款、提交时被拒且未扣费、参考片没发布出去）由脚本自动用同样输入重提，每段最多 3 次，全部记进账本 `unpaid_attempts`；状态不明（超时、断线）可能已被接收扣费，绝不盲目重提，先查 APIMART；401/402/403（密钥、余额）要用户处理。保留账本，优先用整帧级的本地处理（借兄弟片整镜、消音补音、0.5 秒以内的闪帧；不贴图、不删内容，见 3c、3d），其余写进交付说明并报告用户。**唯一的例外是用户在对话里明确要求重生成**（用户 2026-11-19："把失败的镜头重新生成一遍"）：先按 controlled-edit.md §4.8 找出上一次失败的原因（通常是参考片里还看得见竞品），改参考片和提示词，写到 `prompts/G##-a2.txt`（编号跟在已有账本后面，本地拼接占过的号自动跳过），再 `generate --prepare-only --user-approved-regen G## --user-quote "用户原话"` → 看上传帧板 → 只带 `--segment`、`--user-approved-regen`、`--user-quote` 提交（`--mute` 只在准备时给）；用户原话记进任务账本，脚本没有用户原话不接受 a1 以外的编号。 例外（用户 2026-11-20 补充："二次生成如果还有穿帮，继续缩减范围，甚至缩减到单镜头动作继续第三次生成"；用户 2026-11-20："如果首次生成的整段中出现了穿帮，对照原片看穿帮的是那一镜，然后单独针对穿帮的镜头重写提示词生成。"）：审片发现穿帮的那一镜可以按第 10c 步单独补做，还有穿帮再缩到单个动作补做，检查出来的还可以补第三次（同一处最多三次），只重做这一镜、按帧拼回，不整段重生成。
5. **完整语义分段**：段的切点不切断一句话，不把产品镜头和它所配的那句台词拆开，不跨段落锁边；相邻替换段直接相接。
6. 送去生成的参考片和生成结果都不带字幕、标题卡、水印或原品牌文字（字幕会把台词和口型绑死，换不成新词）；选片后由 `captions` 在本地按原片字幕的样式和位置把字幕补回生成段，内容只写生成段实际说出来的词（没说出来的词不写）。没有被重新生成的原片镜头保持原样（含其字幕）。
7. **台词**：整段的台词连口型由模型重新生成，提示词里逐句写出；没有冲突的台词一字不改；冲突只有产品名（任何竞品名）、剂型（drops、capsules、softgels、gummies、powder……）、用量用法和我的产品没有的具体成分（草药数量、泛泛的功效说法不算，见 [controlled-edit.md §2](references/controlled-edit.md) 第 4 条）；冲突台词只改冲突词，提示词正文里绝不写被替换的旧词；生成片台词要听写回读，不信 QA 字幕。**永远不用 TTS**：对白、旁白、成分改词都由 Seedance 按参考片里的原音色重新生成。有冲突词的段在准备时消音（`generate --prepare-only --mute G##:起-止[,起-止]`，只动音轨）：旁白改词消整句，画内旧词在句中且会留下 0.8 秒以上空白的消整句，品牌名消 "It's called ___" 整个分句，句尾单个替换词只消这个词；提示词写法见 controlled-edit.md §5，窗口怎么定、段尾改词、拼接和空档补音见 workflow.md 附录）。
8. **费用**。APIMART seedance-2.0 的接口页没有列价目，生成前向用户报段数和总输出秒数；首批任务完成后从账本 `cost_usd`（任务返回的 `cost`）读实付单价，之后按它估算，交付按账本报告实际费用。
9. 做不到就说做不到：三层识别仍可能漏，终检是兜底不是保证；720p 生成段与原片清晰度可能有落差。

## 交付

`OUT/master.mp4` + `master.mp4.timeline.json`（源→成片映射）+ `master.mp4.verification.json`（技术核验）+ `RUN/plan/decisions.json`（哪些镜头为何被换）+ `RUN/plan/segments.json`（怎么切的、为什么）+ `RUN/generation/jobs/*.json`（每次生成的提示词、任务号、花费，含 local_splice）。向用户汇报：换了哪些段、新旧时长及时间码、花了多少钱、终检结果、已知瑕疵。

**对比看板**（用户 2026-11-19："做一个像这样的看板，和原片对比，我要看和原片差多少，然后哪里改了我也要在轴上能看出来"）：`finish.py` 最后一步自动生成（单独跑是 `scripts/compare_board.py --run RUN`），输出 `OUT/对比看板/index.html` + `media/`（整个文件夹离线可用）。审片发现的瑕疵写进 `RUN/plan/board_notes.json`（`{G##: {"issue": "…", "note": "…", "fix": true}}`），有 issue 的段在轴上标红；重剪过的片子把上一版存到 `OUT/versions/` 再用 `--previous … --previous-timeline …` 放进看板，右边可切换对照。

## 文件地图

| 文件 | 作用 |
|---|---|
| `scripts/swap.py` | 全流程子命令 |
| `scripts/finish.py` | 选片之后一条命令：retrim（裁定格帧）→ captions → recut → brand → final-screen → 对比看板 |
| `scripts/swap_design.py` | 换品设计：Agent 观察事实（`observe` 出拼图，`--import-obs` 导入）→ 规则表定换法 → 五段式骨架；`check_against_design` 核对提示词和设计一致 |
| `references/rules-now.md` | 现行规则一页：规则表、流程、提示词写法、旧写法黑名单（2026-11-20 起以此为准） |
| `scripts/reuse.py` | 两条片逐帧画面比对（`swap.py reuse` 用）：每帧缩成 18×32 灰度小图，找代表片的段在这条片里的位置 |
| `scripts/cards.py` | 人物卡：`RUN/cards/cast.json` → 从原片截脸 → nano-banana-pro 4:3 卡（补做的人物卡模式用；`shot_regen.py cards build` 调它） |
| `scripts/carton_check.py` | 量纸盒比例：青绿色区域 + OCR 找 LYMPHATIC，正面高:宽、外形长:短、侧面当正面；超线出并排对照图（生成的盒子 vs 同高真盒），`select` 自动跑并拦截；`batch 批次目录` 回查整批 |
| `scripts/shot_regen.py` | 单镜头重生成：按帧号裁参考片（放慢 / 定格 / 多镜头接成一条）、检查提示词出任务、按原帧数拼回；`round` 把整条片的穿帮一轮一批补做，`reasons` 汇总穿帮原因 |
| `scripts/x100_gen.py` | 备用生成通道，只在用户点名 100x 时用：`shot_regen.py prepare --channel x100` 准备的任务走 100x seedance-2.0 720P 9:16，写回同一账本 |
| `scripts/compare_board.py` | 交付对比看板：原片和成片并排同步播放，轴上标出换了哪段、改词、水印、时间差，可切到上一版（用户 2026-11-19） |
| `scripts/competitor.py` | 本条竞品档案（Agent 看总览图、听采样后填写）、总览图、水印词与模糊匹配 |
| `scripts/scenes.py` | 切点分级（段落锁边、镜头锁边） |
| `scripts/speech_map.py` | 听写、按锁边和语义切段 |
| `scripts/caption_burn.py` | 量原片字幕样式、给生成段补字幕 |
| `scripts/speech_gate.py` | 成片整条听写，查竞品名、多余成分和漏改的词 |
| `scripts/words.py` | 逐词时间（定消音窗口、核对生成台词）、消音窗口切词检查 |
| `scripts/apimart_wan.py` | APIMART seedance-2.0 视频参考生成的批量提交（9:16、720p、时长按段，全是请求参数；沿用脚本文件名） |
| `scripts/hostd.py` | 备用：本地文件服务 + Cloudflare 隧道（`LYMPHLOOP_VIDEO_HOST=tunnel` 时才用；默认参考片直传 APIMART） |
| `scripts/caption_clean.py` + `scan_text.swift`（macOS）/ `vision_scan.py`（Windows、Linux） | 字幕 OCR、把残留字幕从生成片上抹掉、字幕关和残留文字关 |
| `scripts/caption_lama.py` | 参考片送模型前去字幕、去水印（LaMa 修补模型，整行文字框遮罩；修补结果按内容缓存，重做参考片不再重算） |
| `scripts/sound_sync.py` | 量生成声音相对原片的偏移；人工补救时把原片声音配到生成画面下 |
| `scripts/words.py` | 词级时间戳（本地 Whisper）：定消音窗口、核对生成片每个词的时间 |
| `scripts/seg_sheets.py` | 每段原片的密帧板（带镜头窗口、台词、改词）和生成片的同版式帧板（`--generated`），写提示词和审片时看 |
| `scripts/flash_screen.py` | 外部 Gemini Flash 的逐镜头筛查、生成片 QA（默认停用，只在 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时调用） |
| `scripts/google_api.py` | 外部 Gemini 接口（默认停用，同上）；其余脚本共用它的读写、哈希和文件锁 |
| `scripts/shots.py` | 切镜头、帧板、导帧 |
| `scripts/cutscan.py` | 逐帧切点（不合并短镜）、段边界吸附、闪回关取点与给 agent 看的大格帧板 |
| `scripts/media_pipeline.py` | 探帧、规范化、冻结与回剪渲染（每块按内容缓存） |
| `scripts/watermark_cleanup.py` + `scan_watermark.swift`（macOS）/ `vision_scan.py`（Windows、Linux） | 漂浮水印扫描与清除 |
| `scripts/vision_scan.py` | 识别引擎的统一入口：给调用方一条扫描命令（macOS 是编译好的 Vision 扫描器，其余系统是本文件的 RapidOCR / YuNet 实现），三种扫描的 JSON 格式两边一致 |
| `scripts/oscompat.py` | 系统差异集中处：文件锁（flock / LockFileEx）、进程存活与后台启动、字体目录、可执行文件名、Windows 上自动切 UTF-8 模式；每个脚本第一行导入它 |
| `scripts/watermark_template.py` + `assets/watermarks/` | 存好的水印模板：固定位置逐帧笔画比对（不 OCR），扫描、参考片、终检共用；`build` 从 OCR 扫过的片子建模板 |
| `scripts/watermark_brand.py` | 边缘补扫（旋转放大的四边条）、白色 LYMPHLOOP 覆盖与抽检（按块盖、按内容缓存） |
| `scripts/region_inpaint.py` | 成片里原片画面的局部残留、生成段里的残留文字（`:purple` / `:ink`）；拒绝在生成段上整框填充（3c） |
| `scripts/ref_framing.py` + `scan_people.swift`（macOS）/ `vision_scan.py`（Windows、Linux，只找人脸） | 提交前检查参考片有没有把原片里的人裁出画（3d） |
| `references/carton-swap.md` | 换纸盒的段落：两张图、五段式范例（用户 2026-11-20） |
| `references/controlled-edit.md` | 产品物理、动作对照、片源规则、审片的背景知识和历次实测；提示词写法以 rules-now.md 为准 |
| `references/history-edit-mode.md` | 编辑模式时期（到 2026-11-11）的模板和实测记录，只供查阅 |
| `references/product-actions.md` | 产品事实、状态链与动作对照 |
| `references/workflow.md` | 逐步参数、切段细则、文件布局、改词消音、恢复 |
| `references/detection.md`、`qa-checklist.md` | 识别细则、审片打分 |
