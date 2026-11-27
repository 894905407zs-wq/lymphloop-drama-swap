# 执行细则与文件布局（Antigravity 原生看听 + 100x H3 版）

`$SKILL` = 本 skill 根目录；`$PY` = macOS/Linux 下 `$SKILL/.venv/bin/python`，Windows 下 `$SKILL\.venv\Scripts\python.exe`。
- macOS/Linux 首次建环境：`python3.12 -m venv "$SKILL/.venv" && "$SKILL/.venv/bin/pip" install -r "$SKILL/requirements.txt"`（`ffmpeg` 需带 `drawtext` 滤镜，macOS 用 `brew install ffmpeg-full`）。
- Windows 首次建环境（PowerShell，在 `$SKILL` 目录下）：`py -3.12 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt`；`ffmpeg` 用 `winget install Gyan.FFmpeg`，Node 用 `winget install OpenJS.NodeJS.LTS`；`scripts/oscompat.py` 会在 Windows 下自动切 UTF-8 模式。
- 视觉识别由 `scripts/vision_scan.py` 自动按系统分发：macOS 默认系统 Apple Vision，Windows/Linux 自动用 RapidOCR + OpenCV YuNet（首次自动下载模型）；100x 客户端 `scripts/x100.py` 已内置在包内。所有命令均幂等，已完成产物不会重做。

---

## RUN 目录布局

```text
RUN/state.json                                阶段完成记录
RUN/master-cfr.mp4                            非整数/VFR 原片转严格整数 CFR 后的中间母版
RUN/watermark/{scan.json,cleanup.json}        水印扫描、固定位置与清洗报告
RUN/master-clean.mp4                          工作母版（切镜头、听写与保留原片回剪均用它；送生成参考段直接从原片裁）
RUN/agent_review/                             Antigravity 原生看听打包产物
  ├── overview_grid.jpg                       24 格全片总览图
  ├── transcript.json                         本地 faster-whisper 全片听写
  ├── voice_samples/*.mp4                     3 段 6 秒说话人音色样本
  └── shot_grids/shot_grid_##.jpg             镜头拼接网格（供 detect-pro / screen 看图）
RUN/detect/{competitor.json,pro_nodes.json,overrides.json}
RUN/shots/{shots.json,sheets/S###.png,frames/S###.jpg}
RUN/screen/{S###.json,summary.json}           逐镜头原生筛查结论
RUN/plan/{decisions.json,segments.json,selections.json,swap_design.json}
RUN/plan/{scenes.json,lock_overrides.json,raw_cuts.json,language.json,words/}
RUN/design/obs/                               换品观察网格与导入记录
RUN/clips/G##.hq.clean.ref.mp4 [*.muted-*.mp4]  去字幕去水印参考段（首尾杂帧已裁、末帧定住补到整数秒）
RUN/clips/G##.hq.ref.mp4  G##.hq.mp4          未去字幕参考段（字幕复制 OCR 用）与未补尾原段（审片对照用）
RUN/prompts/G##-a1.txt  G##-a1.final.txt      五段式提示词与提交正文
RUN/jobs/G##-a1.json                          准备好的生成任务定义
RUN/generation/{jobs/*.json,results/*.mp4}    100x（或备选渠道）账本与结果（*.decaptioned.mp4 / G##-aN-c1.mp4）
RUN/cards/{cast.json,*.png}                   补做用 4:3 人物卡
RUN/shots-regen/{rounds/round-N.json,NAME.*}  一轮一批穿帮补做产物
RUN/captions/{style.json,style-preview.jpg}   原样复制/仿制字幕样式与预览
RUN/qa/G##-a1-c1.png  G##-captioned.png       生成段与补字幕抽帧预览
RUN/recut/{edit.json,edit.frozen.json,render/atoms/,brand-atoms/}  按内容缓存的回剪与盖水印块
RUN/final/<master sha16>/{content.json,baseline.json,agent_review/,flashback/,product/,speech/,watermark/}
OUT/master.mp4 (+ .timeline.json, .verification.json, .brand.json)
```

---

## 逐步说明

**单 RUN 单写者**：除只读查询命令外，所有 `swap.py` 写命令自动获取 `RUN/.run.lock`（退出或崩溃自动释放）；被占用时报 `RUN is busy`，不要让第二个进程并发写同一个 RUN，确需排队加 `--lock-wait 秒数`。

### 0–1. `init` / `prepare`
`swap.py init --video V --run RUN --out OUT` → `swap.py prepare`。原片为 VFR 或 29.97 等非整数帧率时，自动无损音轨转为严格整数 CFR（`RUN/master-cfr.mp4` + `.normalization.json`）。

### 1b. `competitor`（Antigravity 原生看听）
`swap.py competitor` 自动产出 `overview_grid.jpg`、`transcript.json`、`voice_samples/*.mp4` 与 `competitor.json` 骨架。Agent 用 `read_file` 看总览图、听 3 段采样视频（记下性别/年龄/口音/音色/BGM）、读听写稿，填写 `competitor.json` 后执行：
```bash
"$PY" scripts/swap.py competitor --run RUN --accept --note "已看总览图并听取音色样本……"
```
剂型超出 6 类支持范围时，需用户同意后再加 `--out-of-scope-ok`。

### 2. `watermark`
- `swap.py watermark scan [--fade-pad N]`：优先比对 `assets/watermarks/<目标词>-fixed.*` 笔画模板（10 分钟片约 25 秒）；匹配不上或传 `--ocr-scan` 时自动退回 4 进程 Vision OCR + 边缘补扫 + 固定位置聚类（`positions`）+ 位置补扫，并对轨迹前后补 0.8 秒淡入淡出（字幕/招牌行不补）。
- 抽帧确认框选后执行 `swap.py watermark apply --run RUN --note "已抽检 N 帧，框选准确"`（整片无漂浮水印则 `swap.py watermark none --run RUN --note "全片无漂浮水印"`）。

### 3. `shots`
`swap.py shots [--threshold 0.3 --min-shot-seconds 0.4 --fps-sample 2]`：切分镜头并产出 `RUN/shots/sheets/S###.png`（带源帧号与时间戳）。

### 4. `detect-pro`（原生零 Gemini）
`swap.py detect-pro` 打包 `RUN/agent_review/shot_grids/shot_grid_##.jpg` 并确保 `transcript.json` 就绪。Agent 读听写稿 + 看网格图（疑镜直接 `read_file` 看 `RUN/clips/S###.mp4`），写出 `RUN/detect/pro_nodes.json`（画内可见嘴改词遵循 [rules-now.md](rules-now.md) 音节数对齐），然后导入：
```bash
"$PY" scripts/swap.py detect-pro --run RUN --import-nodes RUN/detect/pro_nodes.json
```

### 5. `screen`（原生零 Gemini）
`swap.py screen` 提示网格与镜头切片路径。Agent 逐张看网格并核查弱露瓶/小物件/背景瓶，写出 `verdicts.json`（`{"replace_ids": [...], "unsure_ids": [...], "notes": {...}}`），然后导入：
```bash
"$PY" scripts/swap.py screen --run RUN --import-verdicts verdicts.json
```

### 6. `merge`
`swap.py merge` 合并 `pro_nodes.json` 与 `screen` 产出 `RUN/plan/decisions.json`。人工修正漏报/误报或标非实体竞品镜头（`"physical_competitor": false`）编辑 `RUN/detect/overrides.json` 后重跑 `merge`。

### 7. `plan`（锁边切段 + 边界吸附）
`swap.py plan [--dry-run] [--no-speech] [--no-scenes] [--no-edge-snap]`：
- 参考段严格 `< 15` 秒（上限 14.9 秒，上限不是目标），优先切在真实硬切与句间停顿处。
- `scenes.py` 基于本地直方图差分与静音谷（`quiet_cuts`）划分段落锁边与镜头锁边；需微调时写 `RUN/plan/lock_overrides.json`（`{"S019": "shot", "S020": "paragraph", "S007": "none"}`）。
- 既无产品也无冲突台词的段不生成，保留原片（`skipped_product_free`）。
- **段边界吸附（`cutscan.snap_edges`）**：每段边界自动向外吸附到 0.5 秒内最近的逐帧切点（从不往里收，且不超 14.9 秒），并吞并边界外 ≤0.5 秒碎镜头，杜绝接缝竞品闪回。带 `check the seam` 提醒的边界需亲自核查。
- 产出 `RUN/plan/segments.json` 与初始 `RUN/prompts/G##-a1.txt`。

### 7c. `reuse`（同素材批次共用代表片）
批次内多条片子共用主体画面时，代表片完整终检后，在其余片 `plan` 之后执行：
```bash
"$PY" scripts/swap.py reuse --run RUN --from 代表片RUN --dry-run
"$PY" scripts/swap.py reuse --run RUN --from 代表片RUN
```
逐帧比对匹配上的段直接零费用导入为 `G9nn`（`reused_segments`），仅保留独有开头/结尾照常生成。重编码差异大时可加 `--tolerance 2` 并逐个核查接缝。

### 8. 换品设计与五段式提示词
1. `plan` 完成后立即后台预跑 `swap.py generate --run RUN --refs-only`（3 路并行 LaMa 去字幕去水印，带跨 RUN 缓存，不占写锁）。
2. 运行 `swap_design.py observe --run RUN` 打包观察网格 → Agent `read_file` 看网格导入观察结果 → `swap_design.py decide --run RUN` 按规则表生成 `plan/swap_design.json` 并重写未编辑过的 `prompts/G##-a1.txt` 五段式骨架。
3. Agent 对照高清帧与 [product-actions.md](product-actions.md) 补全所有 `【写…】`：光线环境、人物外貌、音色句、每镜 `大小参照：……`（严禁拿竞品作参照）、每镜终态及音节对齐后的台词。

### 9. `generate`（默认 100x `100x-minimax-h3` / `standard` / `768P`）
1. **本地准备与强校验**：
   ```bash
   "$PY" scripts/swap.py generate --run RUN --prepare-only [--segment G01,G02] [--mute G01:1.50-2.28]
   ```
   本地裁出参考片、用 LaMa 清除字幕和水印（`frames_left > 2%` 自动报错）、裁首尾杂帧、末帧定住补到整数秒，并执行全量提示词门禁（五段式、设计表一致性、大小参照、禁旧写法、禁勺子/厘米数/画面否定句/参数进正文、消音窗口切词检查、音节对齐提醒）。
2. **核查上传输入**：用 `read_file` 看每段 `upload_video` 帧板确认字幕/水印干净、首尾无杂帧，听消音窗口确认旧词盖净且邻词完整。
3. **正式提交**：
   ```bash
   "$PY" scripts/swap.py generate --run RUN [--segment G01,G02]
   ```
   - 默认渠道 `x100`（`scripts/x100_gen.py`）：`model: 100x-minimax-h3`、`tier: standard`、`resolution: 768P`、`aspect_ratio: 9:16`、`duration` = 段长向上取整整数秒（4–15）、`generate_audio: true`。媒体直传 100x，不走本地隧道。
   - 显式备选：仅用户点名时设 `LYMPHLOOP_X100_MODEL=100x-seedance-2.0`（`720P`）或 `LYMPHLOOP_GEN_CHANNEL=apimart`（APIMART `seedance-2.0` / `720p`）。
   - 请求前落盘账本，已提交任务重跑只查询和下载，绝不重复扣费。

### 10. `review` / `select`（原生看听 + 6 项自检 + 纸盒尺子）
1. `swap.py review --run RUN [--segment G01]`：4 路并行执行本地 `faster-whisper` 词检（`words`）、残留字幕检查（`captions`）与水印/叠加字检查（`overlay`），输出 `agent_watch_and_listen` 路径。
2. **Agent 必须用 `read_file` 直接看并听每段 `.mp4`**，再执行选片：
   ```bash
   "$PY" scripts/swap.py select --run RUN --job G01-a1 --candidate c1 \
     --audit "competitor_clean=1,front_face_ok=1,scale_ok=1,physics_ok=1,speech_lip_ok=1,text_clean=1" \
     [--clip-in 0 --clip-out 7.5] [--note "..."]
   ```
   - `select` 自动执行：① 残留字幕清理（`*.decaptioned.mp4`）；② `carton_check.py` 纸盒像素比例与正面检测（超线则出并排对照图并拒绝，必须 `read_file` 看过对照图后再决定补做或 `--carton-ok`）；③ `frozen_tail` 段尾静帧/缓漂定格自动裁剪，同时当末尾超过 `seg['duration_s']` 处仍有真实台词收尾时自动保尾不截断。
   - 画面看得到说话嘴的段一律自带声音；无嘴画面才允许 `--sound source --no-mouth`。计划内决定沿用原片画面的段用 `select --job G##-a1 --keep-source-picture --note "..."`。
   - 需本地整帧拼接时用 `swap.py splice`（非同源同窗声音必须标 `--no-mouth A-B`）。

### 10c. 穿帮一轮一批人物卡补做（`shot_regen.py`）
审完所有段后再汇总穿帮清单 `RUN/shots-regen/goofs.json`：
```json
[{"seg": "G05", "at": "3.2", "why": "competitor", "note": "台面左侧旧瓶还在"}]
```
1. 写 `RUN/cards/cast.json`（英文 `identity` 把体重体型写最前 + 服装 + 全片最清正脸 `srcs`）→ `shot_regen.py cards build --run RUN`（4:3 2K，逐张 `read_file` 验收）。
2. `shot_regen.py round open --run RUN --file RUN/shots-regen/goofs.json`：默认人物卡模式（`# cards` + `# views` + 说话镜头 `# voice`），不上传参考视频。
3. 补齐每个 `NAME.txt`（光线、3–6 固定物件、轴线锚点、机位表、每镜大小参照、7 要素音色句）。
4. `shot_regen.py round submit --run RUN`（全部校验通过后一次提交到默认 `100x-minimax-h3` / `standard` / `768P`，下载并产出 `NAME.result.png`）。
5. `read_file` 看 `NAME.result.png` 与生成 `.mp4`，确认干净后执行 `shot_regen.py round splice --run RUN [--drop 仍有穿帮的NAME]`（自动拼回并重新 `select`）。同一处最多补 3 轮。

### 11–12. 一键收尾与终检交付（`finish.py`）
`python scripts/finish.py --run RUN` 依次串行执行以下各步（均支持 `--from <step>` 断点续跑）：
1. **`retrim`**：`shot_regen.py retrim --run RUN --all`，彻底裁掉补做镜与段尾残余的静态/缓漂定格帧。
2. **`captions`**：默认原样复制原片字幕（`caption_matte.py`，对含改词句按量出的本片样式自动仿制重画），多语种 Whisper 按实际说出的词对齐时间。核查 `RUN/captions/style-preview.jpg` 与 `RUN/qa/G##-captioned.png`；字体偏差可用 `--font "字体名"` 重跑。
3. **`recut`**：按内容缓存（`RUN/recut/render/atoms/`）无损顺剪输出 `OUT/master.mp4` 与 `OUT/master.mp4.timeline.json`。改一个镜头只重编变动块，其余块直接复用。
4. **`brand`**：按块盖水印（`RUN/recut/brand-atoms/`），先 Telea 修补原水印及残影，再按固定位置/统一尺寸/一致读向覆盖白色不透明 `LYMPHLOOP`（未盖版留存为 `OUT/master.unbranded.mp4`）。无水印片自动跳过。
5. **`final-screen`**：基于 `content.json` / `baseline.json` 增量只查新内容块（`--full` 查全片）：
   - 固定位置水印复扫（要求 `watermark_left == []`；有残留用 `swap.py watermark add-final` → `brand` → 重跑终检）；
   - 本地 `faster-whisper` 全口播门禁（`speech_gate.py`，要求 `speech_left == []`）；
   - 生成**闪回帧板**（`final/<sha>/flashback/sheets/`）与**产品原尺寸帧板**（`final/<sha>/product/`）。
6. **Agent 人工复核门禁**：`read_file` 逐张看完闪回帧板与产品帧板后执行：
   ```bash
   "$PY" scripts/swap.py flashback-review --run RUN --clean   # 有问题用 --hit 起帧-止帧 --note "..."
   "$PY" scripts/swap.py product-review   --run RUN --clean   # 有问题用 --hit "秒-秒:说明"
   ```
   完成后重跑 `finish.py --run RUN --from board` 产出对比看板。

### 13. 启动内置 Range HTTP 看板服务
避免 `file://` 下中文路径和视频拖拽受限，直接启动内置服务预览与交付：
```bash
"$PY" scripts/compare_board.py --run RUN --serve --port 8765
# 多条片汇总导航页：
"$PY" scripts/compare_board.py --runs RUN1 RUN2 --serve --port 8765
```

### 附：局部文字定点修复（`region_inpaint.py`）
仅用于原片画面残留字幕/淡水印，或生成段道具上的旧品牌印刷字（`:ink`）与残留紫框字幕（`:purple`）：
```bash
"$PY" scripts/region_inpaint.py OUT/master.mp4 OUT/master.fix.mp4 \
  --box X,Y,W,H:ink --frames A,B --report OUT/master.fix.json
```
生成段帧范围内的整框（无门控）填充会被脚本直接拒绝，防止贴图补救产品穿帮。

---

## 恢复与幂等机制
- 任何一步中断后重跑同一命令即可自动续接：`state.json` 记录阶段进度，`generation/jobs/*.json` 登记 100x 任务 ID 与下载状态，绝不重复提交扣费。
- 调整上游配置（`overrides.json` / `lock_overrides.json`）后重跑 `merge → plan`，听写与 LaMa 去字幕缓存自动命中。
- `ffmpeg` / `ffprobe` 查找顺序：`FFMPEG_BIN` / `FFPROBE_BIN` → `$SKILL/bin` → `PATH`。
