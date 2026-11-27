# 流程每一步、切段与审片自动关：完整说明

本文件展开 `SKILL.md` 的步骤表、切段规则与审片自动关细则，已统一为 **Antigravity 原生零 Gemini + 100x `100x-minimax-h3`（`standard` / `768P` / `9:16`）+ 人物卡补做** 现行标准。命令参数与目录布局详见 [workflow.md](workflow.md)。

---

## 流程（每条视频一个 RUN 目录，`scripts/swap.py` 子命令按序）

| 步 | 命令 | 详细说明 |
|---|---|---|
| 0 | `init --video V --run RUN --out OUT` | 记录源片路径、哈希与探帧信息 |
| 1 | `prepare` | VFR 或 29.97 等非整数帧率自动无损音轨规范化为严格整数 CFR `master-cfr.mp4` |
| 1b | `competitor` → 看图听音填 JSON → `competitor --accept` | 自动生成 24 格全片总览图 `overview_grid.jpg`、本地 Whisper 全片听写 `transcript.json` 与 3 段 6 秒音色样本 `voice_samples/*.mp4`；Agent 用 `read_file` 看听后补齐 `competitor.json` 并 `--accept` |
| 2 | `watermark scan` → 核查 → `watermark apply`（或 `watermark none`） | 优先比对 `assets/watermarks/<目标词>-fixed.*` 笔画模板（10 分钟片 ~25 秒）；匹配不上自动退回多进程 OCR + 四边旋转放大补扫 + 固定位置聚类 + 位置补扫；无模板时看 `RUN/watermark/place_kind/` 区分真水印与产品标签。可与第 4、5 步同时后台跑 |
| 3 | `shots` | 切分镜头并生成带源帧号/时间码的帧板 `RUN/shots/sheets/S###.png` |
| 4 | `detect-pro`（与第 2、5 步并行） | 自动打包 `shot_grids/shot_grid_##.jpg`；Agent 读听写稿 + 看拼图（疑镜直接看 `clips/S###.mp4`）写出 `pro_nodes.json` 后 `--import-nodes` 导入 |
| 5 | `screen`（与第 2、4 步并行） | Agent 逐批看 `shot_grids` 筛查弱露瓶/小物件/背景瓶，写出 `{replace_ids, unsure_ids, notes}` 后 `--import-verdicts` 导入 |
| 6 | `merge` | 合并冲突节点与逐镜筛查结果产出 `plan/decisions.json`；人工纠正漏报/误报或标记非实体竞品（`"physical_competitor": false`）写 `detect/overrides.json` 后重跑 |
| 7 | `scenes.py RUN` → 复核 → `plan` | 本地色彩直方图 + 音轨静音谷分级（可写 `lock_overrides.json` 覆盖）→ 按锁边与句间停顿切 `<15s` 段 → `cutscan.py` 将边界向外吸附到逐帧切点并并入 ≤0.5 秒碎镜头防闪回；带 `check the seam` 的边界亲自核查 |
| 7b | `swap_design.py observe` → `decide` | Agent 看观察拼图导入客观事实，规则表自动决定产品形态、参考图、数量与五段式骨架（详见 [rules-now.md](rules-now.md)） |
| 7c | `reuse --from 代表片RUN [--dry-run]` | 同素材批次逐帧比对导入代表片审定结果（`G9nn`，零生成费）；共用段有穿帮回代表片修完重跑 `reuse` |
| 8 | 后台 `generate --refs-only`；填完五段式提示词 | `--refs-only` 3 路并行 LaMa 去字幕去水印（带跨 RUN 内容块缓存）；Agent 对照高清帧补全所有 `【写…】`（环境、人物、音色、每镜 `大小参照：`、终态、音节对齐台词） |
| 9 | `generate --prepare-only [--mute ...]` → 看帧板听消音 → `generate` | 本地准备并过全量门禁（五段式、设计表一致、大小参照、禁旧写法/勺子/厘米数/否定句/参数进正文、去字幕残留 ≤2%、消音切词检查）；核查无误后 `generate` 走 `x100_gen.py run-batch` 一次性并行提交至默认 **`100x-minimax-h3`（`standard` / `768P` / `9:16`）**，参考片直传 100x |
| 10 | `review` → `read_file` 看听 `.mp4` → `select --audit "..."` | `review` 本地 `faster-whisper` 词检 + 残留字幕/叠加字检测；Agent 必须直接用 `read_file` 看并听每个 `.mp4` 并传 6 项 `--audit`；`select` 自动跑 `carton_check.py` 像素量尺（超线拦截出并排对照图）、自动清理残留字幕、自动裁段尾定格帧并保护 H3 句尾真实台词不截断 |
| 10b | `captions`（4 段并行，按需重做变动段） | 原样复制原片字幕（差值提取透明度与颜色，含标题卡），仅含被替换词的句子按本片量出样式仿制重画；检查 `qa/G##-captioned.png` |
| 10c | `cards build` → `round open` → 写人物卡五段式 → `round submit` → `round splice` | 审完所有段后建 `goofs.json`（登记八类 `why` 原因），一轮一批默认**人物卡模式**补做（不挂参考视频，只挂人物卡 + 产品图 + 说话人 `# voice:`，走 `100x-minimax-h3` / `768P`）；说话镜头原速自带声，无嘴镜可放慢。同一处最多补做 3 轮（第 2 轮更短，第 3 轮不长于最窄轮） |
| 11 | `recut` | 按选片实际时长原速顺剪到 `OUT/master.mp4`；每块按内容签名缓存在 `RUN/recut/render/atoms/`，改单镜只重编变动块 |
| 11b | `brand` | 按成片时间轴逐块修补原水印并覆盖白色不透明 `LYMPHLOOP`（每处固定一框一读向，按块缓存在 `brand-atoms/`，无水印自动跳过） |
| 12 | `final-screen` → `flashback-review` & `product-review` | 增量只查改动块与上次未结问题块（`--full` 查全片）：复扫水印（`watermark_left == []`）、口播听写（`speech_left == []`）、输出**闪回帧板**（接缝/拼接点/≤0.5s 碎镜）与**产品原尺寸帧板**（纸盒段每秒 3 帧）；Agent 逐张 `read_file` 看完并执行 `flashback-review --clean` 与 `product-review --clean` |

**一键收尾**：`finish.py --run RUN` 自动串行跑 `retrim → captions → recut → brand → final-screen → compare_board`（支持 `--from <步>` 断点续跑）。若尚未完成 `flashback-review` / `product-review` 或记录了未修穿帮，`finish.py` 会停在 `NEEDS_AGENT_REVIEW`，核完或修完后 `finish.py --from board` 出看板并启动 `compare_board.py --serve` 交付。

---

## 切段规则（第 7 步）

| 锁边级别 | 触发条件 | 切段作用 |
|---|---|---|
| **段落锁边** | 下一镜头切换至完全不同的物理环境 | 段落绝不跨过；向前后探索边界至此停止 |
| **镜头锁边** | 同环境但无时间/动作连续性（时间跳跃、换装、新动作） | 低于段落锁边；不跨句时在此锁边 |
| 普通硬切 | 同一时刻连续镜头（正反打、特写、插入镜、场景内动画） | 仅当两锁之间超过 14.9 秒时在此拆分 |

1. 两锁之间为一个块，含产品镜头或冲突台词（`needs_edit`）则整块生成；无产品无改词的段原片照放（`skipped_product_free`）。
2. 台词跨镜头锁边时合并两块（不超 14.9 秒）；跨段落锁边不合并并提示复核。
3. 块超 14.9 秒优先切在停顿 ≥0.55 秒且非问句后的硬切点，避免拆开一问一答。
4. 14.9 秒是上限非目标；单镜头自身超 14.9 秒则在镜内句间停顿切开。

---

## 审片自动关与穿帮处置（第 10 步）

- **字幕关**：逐帧 OCR，与本段台词同词的文字算残留字幕（产品包装印刷不算），`select` 自动清理。
- **台词关**：本地 `faster-whisper` 听写生成片，核查新词是否完整说出、无旧竞品词或乱码。
- **声音与口型**：可见说话嘴镜头只用同一文件同一时刻自带音轨；口型与声音对不上属 `speech` 穿帮，直接进第 10c 步补做（`lip_sync`），不换原声。
- **物理关**：Agent 看生成片核查条包无支撑自站立、画面撕包、服用空手/张嘴等物理穿帮（`--audit` 的 `physics_ok`），为 `false` 直接进补做。
- **残留文字关**：参考片原有竞品水印、标题卡或旧标签残留于生成画面任何位置均判不合格。
- **纸盒量尺关（硬卡点）**：送了纸盒图的段，`select` 自动运行 `carton_check.py` 按每 0.25 秒量像素（正面高:宽 ≤ 1.65、外形长:短 ≤ 2.3、竖立 ≥0.5 秒不漏 `LYMPHATIC` 正面）；超线立即拦截并生成三栏并排对照图，Agent 必须 `read_file` 看图，真穿帮填 `scale_ok=0` / `front_face_ok=0` 进人物卡补做，确认误量才加 `--carton-ok "看过：原因"` 放行。
- 详细八类穿帮与豁免标准见 [qa-checklist.md](qa-checklist.md)。
