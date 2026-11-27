---
name: lymphloop-drama-swap
description: 将任意竞品保健品（滴剂、硬胶囊、软胶囊、片剂、软糖、粉剂）的竖屏短视频广告换成 yesnap LymphLoop 成片（Antigravity 原生看听版，默认零外部 Gemini 调用）。由当前 Agent 直接用 read_file 看视频/帧板与听音轨，配合本地 faster-whisper、LaMa 去字幕与规则表设计，默认走 100x 渠道 `100x-minimax-h3`（`standard` / `768P` / `9:16`，时长按段）生成；穿帮补做默认走人物卡模式；对白改词优先音节数对齐；回剪、盖水印、终检与内置 Range HTTP 看板一条龙完成。
---

# LymphLoop 换品（Antigravity 原生看听 + 100x H3 版）

本插件支持 **macOS 与 Windows / Linux**，为 **Antigravity 原生多模态执行环境**定制：
- **默认零外部 Gemini 模式**（`scripts/agent_native.py`）：当前 Agent 直接用 `read_file` 看视频（`.mp4`）、看帧板（`.jpg`/`.png`）与听音频（`.mp4`/`.wav`），配合本地 `faster-whisper`、OpenCV/Vision 识别与 `carton_check.py` 像素量尺完成全链路（仅显式设 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时回退外部 Gemini）。
- **默认生成渠道**：**100x `100x-minimax-h3`（`standard` / `768P` / `9:16`）**，首次参考视频生成与人物卡补做统一默认直传 100x（不走本地隧道）；禁用 `100x-seedance-2.5`。
- **独立插件副本**：仅在本插件目录内迭代，不要改动其他插件目录；各条片子的 RUN 目录互不混用。

> **读法**：本文件严格控制在 42 KB 以内（确保 Antigravity 一次 `read_file` 完整读完）。细则分布在：[skill-rules-full.md](references/skill-rules-full.md)（完整规则与环境说明）、[skill-steps-full.md](references/skill-steps-full.md)（步骤细则）、[rules-now.md](references/rules-now.md)（换品规则表、五段式、每镜大小参照与人物卡补做）、[workflow.md](references/workflow.md)（命令参数与文件布局）、[watermark.md](references/watermark.md)（漂浮水印）、[file-map.md](references/file-map.md)（文件地图）。

---

## 不可违反的规则

完整说明见 [skill-rules-full.md](references/skill-rules-full.md)。

1. **宁多换不漏换**。第 4 步冲突节点、第 5 步逐镜头筛查、看帧板时任一处判“有/拿不准”的镜头都进替换清单；只有亲眼看过帧板并写了 `overrides.json` 才能改回 keep。
2. **提示词由 Agent 写，审片由 Agent 定**。脚本自动关只是辅助；不把“API 成功”当“内容合格”。每段先看清原片帧再写提示词，生成完必须用 `read_file` 直接看并听 `.mp4` 审片。
3. **物理优先**：每段先判断原片动作在现实里用条包还是盒装做得到，绝不为了迁就原动作让产品弯折、拉长、自己立着或被捏扁；同一场戏里产品形态和位置前后连续；画面里不撕条包。
3b. **动作设计避开参考片照抄，统一五段式 + 每镜大小参照**：参考片只去字幕和水印，竞品原样保留（不抹、不定格替换）。按 [rules-now.md](references/rules-now.md) 五段式写法：在 `【参考素材职责】` 写“完全移除原片中的<具体竞品>及字幕”与“不沿用原容器大小和手部张开幅度”，在 `【事件脚本】` 每镜正向描写我方产品与 `大小参照：……`（严禁拿竞品作参照）。**严禁旧写法**（不得出现 `将@视频1中…更换为…`、`这不是视频编辑`、`动作照@视频1`、厘米数、勺子词、画面否定句、参数进正文）。手机/电脑商品页按规则 S1 传 `phone_page`/`desktop_page`；小而远/反打等照抄高危镜按 [controlled-edit.md §4.8](references/controlled-edit.md) 处理。
3c. **禁止贴图补救生成段**：严禁贴色块/贴产品图/局部涂抹生成段内的产品穿帮，严禁放大裁切生成画面或用原片放大顶替生成镜头，严禁自写 ffmpeg 拼生成段。仅允许整帧级处理（≤0.5 秒坏闪帧、借同素材兄弟片整镜、补满 3 轮后无嘴镜放慢填补）及文字类处理（自动去残留字幕、`:purple`、道具旧印刷字 `:ink`、`brand` 盖白色 `LYMPHLOOP`）。
3d. **不许删原片内容、不许跳过要换的段**：成片里原片每个镜头与每句台词都必须在（`recut` 拒绝短超 0.5 秒的选片，除非用户明确同意 `--user-approved-cut`）；原片人物（含卡通/特效人物）照原片生成，参考片不得将人物裁出画（`ref_framing.py` 自动拦截）。
4. **首轮每段 1 次原片参考生成 + 穿帮必走人物卡定点补做**：每段首次生成（`a1`）挂去字幕参考视频；审片（含 6 项 `--audit`、`carton_check.py` 像素量尺、终检闪回帧板与产品帧板）查出八类穿帮后，统一按**一轮一批**走第 10c 步**人物卡模式**定点补做（不挂参考视频，只挂人物卡 + 产品图 + 说话人音色，同一处最多补做 3 轮，第 2 轮更短、第 3 轮不长于最窄轮）。未扣费的失败由脚本自动重提（最多 3 次，记 `unpaid_attempts`）；状态不明绝不盲目重提，先查任务状态；补满 3 轮仍未过才如实写入交付说明交用户裁定。
5. **完整语义分段**：切点不切断一句话、不拆开产品镜头与对应台词、不跨段落锁边；参考段严格 `< 15` 秒（上限 14.9 秒，非目标）。
6. **去字幕参考 + 原样复刻字幕**：送生成的参考片和生成结果不带字幕、标题卡、水印或竞品字；选片后由 `captions` 按原片字幕原样复制（含改词句按量出样式仿制重画）贴回生成段，保留原片镜头自带原字幕。
7. **台词与音节对齐**：全段听得到的台词逐句以 `{原词}` 写出，未冲突词一字不改；画内可见嘴部改词优先**音节数对齐**（如 4 音节 `Lymphoria` 换 `Yesnap LymphLoop`）；正文严禁出现被替换旧词与音标；**永远不用 TTS**；冲突词提交前 `--mute` 消音（旁白/句中长空档消整句，品牌名消 `"It's called ___"` 分句）。
8. **费用透明**：默认走 100x 积分计费（`100x-minimax-h3` / `standard` / `768P` 约 3.89 积分/秒，人物卡按张计费）；生成前向用户报段数与总秒数，交付按账本如实报告消耗积分/费用。
9. **如实汇报局限**：自动检查是兜底不是绝对保证，768P 生成段与高码率原片画质偶有细微落差，达 3 轮上限未解的瑕疵如实列时间码交付。

---

## Agent 执行守则

完整说明见 [skill-rules-full.md](references/skill-rules-full.md)。

1. **亲自看图听声**：总览图、镜头网格、上传参考帧板、生成片 `.mp4`（`read_file` 看动图+听真声）、`carton_check` 并排对照图、闪回帧板与产品帧板，一律亲自看听；只看 JSON 就判 pass 属违规。
2. **长步骤放后台**：`watermark scan`、`generate`、`recut`、`final-screen`、`finish.py` 放后台运行并留日志，通过 `swap.py status` 与账本查进度，避免超时中断。
3. **单 RUN 单写者**：一条视频独立一个 RUN/OUT 目录；`swap.py` 自带跨平台 RUN 文件锁（POSIX `flock` / Windows `LockFileEx`），严禁多进程并发写同一 RUN 或手动删锁。
4. **不误杀其他进程**：禁止无差别杀 `python` 进程；提交统一走 `swap.py generate` 与 `shot_regen.py round submit`，不自写绕过账本的提交脚本。
5. **按实际时长顺剪，说话镜头只用自己的同期声**：可见说话嘴的画面，声音必须是**同一文件、同一时刻**的自带音轨，不论台词改没改；`--sound source` 或 `splice --audio-window` 只允许用于无说话嘴的画面且必须标 `--no-mouth`。口型错位直接补做该镜（`lip_sync`），不借音、不做本地口型对齐。
6. **次数记账**：每段首次生成记 `a1`，人物卡补做记 `shots-regen/rounds/`，本地拼接记 `local_splice`；未付费失败由脚本自动重提并记 `unpaid_attempts`。
7. **凭据安全**：密钥/登录态仅由环境变量、系统凭据库或 100x CLI 读取，绝不打印或写入日志/提示词。
8. **完整交付汇报**：交付须写明成片路径、分辨率/帧率/时长、渠道模型、首轮段数与通过率、补做轮次与总消耗、终检各项结论、已知瑕疵（新时间码）及对比看板 HTTP 访问地址。
9. **批次不中途卡死**：遇到单段/单条异常或补满 3 轮的穿帮，记录明细并继续推进其余段/其余片子，交付时一并汇总交用户裁定。

---

## Antigravity 原生“看 + 听”全链路清单

| 环节 | Antigravity 原生做法（默认零外部 Gemini） | 脚本支持 |
|---|---|---|
| **1b 建竞品档案** | `read_file` 看 `overview_grid.jpg` + 听 `voice_samples/*.mp4` + 读 `transcript.json`，填 `competitor.json` 后 `--accept` | `swap.py competitor` 自动出总览图、本地 Whisper 字幕与 3 段音色样本 |
| **3–5 识别与筛查** | `read_file` 看 `shot_grids/shot_grid_##.jpg`（疑镜看 `clips/S###.mp4`），导入 `pro_nodes.json` 与 `verdicts.json` | `detect-pro --import-nodes`、`screen --import-verdicts` |
| **7 切段与锁边** | `scenes.py` 本地直方图 + 音轨跌落分级；`speech_map.py` 本地 Whisper 断句；`cutscan.py` 段边界向外吸附防闪回 | `lock_overrides.json` 可人工微调 |
| **8 提示词与设计表** | `swap_design.py observe` 看拼图导入 → `decide` 规则表填五段式骨架；补每镜 `大小参照：` 与音节对齐台词 | `generate --prepare-only` 自动校验五段式/大小参照/音节差/去字幕残留 |
| **9 生成（100x H3）** | 听审 `--mute` 上传片后，`swap.py generate` 一次性并行提交至 `100x-minimax-h3` (`standard` / `768P` / `9:16`) | `x100_gen.py run-batch` 直传 100x，写统一账本 |
| **10 审片与选片** | `read_file` 直接看并听每个 `.mp4`；`select --audit` 做 6 项穿帮自检，自动跑 `carton_check.py` 尺子与定格裁剪 | `select` 自动保尾防 H3 截词、自动去残留字幕 |
| **10c 人物卡补做** | 审完所有段建 `goofs.json` → `cards build` → `round open`（默认人物卡模式）→ `round submit` → `round splice` | 默认 `100x-minimax-h3` / `768P`，同一处最多 3 轮 |
| **11–12 终检与看板** | `finish.py` 跑 `retrim → captions → recut → brand → final-screen`；`read_file` 审完闪回帧板与产品帧板后起 Range HTTP 看板 | `flashback-review` + `product-review` 双门禁；`compare_board.py --serve` |

---

## 固定事实

完整说明见 [skill-rules-full.md](references/skill-rules-full.md)。

- **产品**：yesnap LymphLoop（Orange Mango，30 支/盒，8 g/支液体条包）。青绿色翻盖纸盒（10×7×13 cm 级，宽正面印 `LYMPHATIC`）+ 青绿色柔性条包（约食指长、略宽于拇指指腹）。画面里不撕条包；服用一律单手持已开条包闭唇轻抿。7 种合法成分与动作映射见 [product-actions.md](references/product-actions.md)。
- **产品参考图**：
  - `handheld_photo`：`assets/product/stick-handheld-scale.jpg`（未开条包）
  - `opened_packet_hand`：`assets/product/opened-packet-handheld.jpg`（已开条包）
  - **纸盒双图必一起给**：`carton_views`（`assets/product/carton-views.jpg`）+ `carton_hand`（`assets/product/carton-hand-scale.jpg`），详见 [carton-swap.md](references/carton-swap.md)
  - **屏幕商品页**：`phone_page`（`assets/product/landing-page-phone.jpg`）、`desktop_page`（`assets/product/landing-page-desktop.jpg`）
- **生成渠道与模型**：默认 **100x `100x-minimax-h3`（`standard` / `768P` / `9:16`）**，主流程首次生成与人物卡补做一致，时长取目标段向上取整整数秒（`4–15`），全走请求参数不进正文。备选：`LYMPHLOOP_X100_MODEL=100x-seedance-2.0`（`720P`）或 `LYMPHLOOP_GEN_CHANNEL=apimart`（APIMART `seedance-2.0` / `720p`）；**禁用 `100x-seedance-2.5`**。
- **跨平台运行环境（macOS / Windows / Linux）**：
  - Python 3.12 虚拟环境：macOS/Linux 用 `$PY="$SKILL/.venv/bin/python"`，Windows 用 `$PY="$SKILL\.venv\Scripts\python.exe"`，通过 `pip install -r "$SKILL/requirements.txt"` 安装依赖；`ffmpeg`/`ffprobe` 需带 `drawtext` 滤镜（macOS 用 `brew install ffmpeg-full`，Windows 用 `winget install Gyan.FFmpeg`）。
  - 视觉引擎按平台自动切换（`scripts/vision_scan.py`）：macOS 默认系统 Apple Vision（Swift），Windows/Linux 自动用 RapidOCR + OpenCV YuNet（模型首次自动下载），输出完全一致的 JSON；系统差异集中由 `scripts/oscompat.py` 处理（含 Windows 自动 UTF-8 模式与 Win32 文件锁）。
  - 100x 客户端 `scripts/x100.py` 已内置在包内，需本机 Node.js ≥ 22 与已登录的 100x 插件（默认 `~/.codex/plugin-sources/100x-mcp/plugins/100x`，可用 `X100_PLUGIN` 指定）。

---

## 流程（每条视频一个 RUN 目录，`scripts/swap.py` 子命令按序）

详细参数与实测见 [skill-steps-full.md](references/skill-steps-full.md) 与 [workflow.md](references/workflow.md)。

| 步 | 命令 | Agent 要做的事 |
|---|---|---|
| 0 | `init --video V --run RUN --out OUT` | 记录源片与探帧 |
| 1 | `prepare` | 非整数帧率/VFR 自动规范化为严格整数 CFR |
| 1b | `competitor` → 看总览图、听音色采样、填 JSON → `competitor --accept` | 建立本条竞品档案（品牌、剂型、容器/单元外观、难例、水印词） |
| 2 | `watermark scan` → 核查摘要/抽帧 → `watermark apply`（无水印用 `watermark none`） | 优先命中固定水印模板（~25s）；无模板则自动 OCR + 固定位置聚类，可与第 4、5 步并行 |
| 3 | `shots` | 切分镜头并生成帧板 `RUN/shots/sheets/S###.png` |
| 4 | `detect-pro`（可与第 2、5 步并行） | 读听写稿 + 看 `shot_grids` 写出 `pro_nodes.json`，`detect-pro --import-nodes` 导入 |
| 5 | `screen`（可与第 2、4 步并行） | 看 `shot_grids`（疑镜看 `clips/S###.mp4`）写出 `verdicts.json`，`screen --import-verdicts` 导入 |
| 6 | `merge` | 合并节点与筛查结果产出 `decisions.json`；人工纠正写 `overrides.json` 后重跑 |
| 7 | `scenes.py RUN` → 复核 → `plan` | 本地切点分级（可写 `lock_overrides.json` 微调）+ 按锁边/停顿切 `<15s` 段 + 自动边界吸附防闪回 |
| 7b | `swap_design.py observe` → `decide` | Agent 看观察拼图导入事实，规则表自动定产品形态、参考图与五段式骨架（见 [rules-now.md](references/rules-now.md)） |
| 7c | `reuse --from 代表片RUN [--dry-run]` | 同素材批次复用代表片审定结果（零生成费），仅生成独有片段 |
| 8 | 后台 `generate --refs-only`；填完每段五段式提示词 | 并行 LaMa 去字幕（带缓存）；补全场景、人物、音色、每镜 `大小参照：`、终态及音节对齐台词 |
| 9 | `generate --prepare-only [--mute ...]` → 核查参考片与消音 → `generate` | 全量门禁校验通过后，一次性并行提交至默认 `100x-minimax-h3`（`standard` / `768P` / `9:16`） |
| 10 | `review` → `read_file` 看听 `.mp4` → `select --audit "..."` | 6 项结构化审片自检 + `carton_check.py` 自动尺子（超线必看对照图）+ 定格裁剪与 H3 尾词保尾 + 自动去残留字幕 |
| 10b | `captions`（通常由 `finish.py` 统一跑） | 原样复制原片字幕（含改词句按量出样式仿制重画），检查 `qa/G##-captioned.png` |
| 10c | `cards build` → `round open` → 填人物卡五段式 → `round submit` → `round splice` | 审完所有段建 `goofs.json`，一轮一批默认**人物卡模式**补做穿帮（`100x-minimax-h3 / 768P`，同处最多 3 轮） |
| 11–12 | `finish.py --run RUN` → 审闪回与产品帧板 → `flashback-review` & `product-review` → `finish.py --from board` | `retrim` 裁定格 → `captions` → `recut` 按内容缓存顺剪 → `brand` 固定框盖白色 `LYMPHLOOP` → `final-screen` 增量终检 → `compare_board.py --serve` 起 Range HTTP 看板交付 |

### 切段与终检要点

- **切段**：段落锁边（换环境）不跨过；镜头锁边（同环境时间/动作跳跃）跨句可合并；块超 14.9 秒才在句间停顿切开；无产品无改词的段原片照放；边界外 ≤0.5 秒碎镜头自动并入防闪回。
- **漂浮水印**：优先模板比对，其次固定位置 OCR；只在有水印帧清除并盖白色 `LYMPHLOOP`，每处整片固定一框一读向，详见 [watermark.md](references/watermark.md)。
- **终检四关**：水印残留 `watermark_left == []`、口播听写 `speech_left == []`、闪回帧板 `flashback-review --clean`、产品原尺寸帧板 `product-review --clean`，四关全过由 `finish.py` 出看板交付。
