# 文件地图

每个脚本和资源是做什么的（从 `SKILL.md` 挪过来，2026-11-27，一字未改；路径都相对技能目录）。


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
