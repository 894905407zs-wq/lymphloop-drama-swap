# 文件地图

本技能目录内所有脚本、参考文档与视觉资产一览（路径相对 `skills/lymphloop-drama-swap/`）。

## 核心主链路脚本

| 文件 | 作用 |
|---|---|
| `scripts/swap.py` | 全流程主入口子命令（`init` 到 `final-screen` / `flashback-review` / `product-review`） |
| `scripts/agent_native.py` | Antigravity 原生零 Gemini 调度：总览图、镜头拼图、音色采样、默认渠道路由（`x100`）、音节数对齐检查 |
| `scripts/x100_gen.py` | 默认视频生成通道：`100x-minimax-h3`（`standard` / `768P` / `9:16`，亦支持 `100x-seedance-2.0` `720P`），支持首轮与人物卡补做 |
| `scripts/x100.py` | 内置 100x API 客户端，调用本机 100x 插件桥（Node 22+）完成上传、询价、提交与下载 |
| `scripts/swap_design.py` | 换品设计：Agent 观察事实（`observe` 出拼图、`--import-obs` 导入）→ 规则表定换法 → 五段式骨架与一致性/大小参照校验 |
| `scripts/shot_regen.py` | 单镜头与一轮一批穿帮补做（默认人物卡模式：`round open/submit/splice`、`locate`、`retrim` 裁定格、`reasons` 汇总） |
| `scripts/cards.py` | 人物卡生成：`RUN/cards/cast.json` → 从原片截正脸 → `nano-banana-pro` 4:3 2K 人物卡 |
| `scripts/finish.py` | 选片后一键收尾：`retrim` → `captions` → `recut` → `brand` → `final-screen` → 对比看板（支持 `--from` 续跑） |
| `scripts/compare_board.py` | 交付对比看板生成与内置 Range HTTP 预览服务（`--serve`，支持多片 `--runs` 导航页） |
| `scripts/oscompat.py` | 跨平台系统抽象：文件锁（POSIX `flock` / Win32 `LockFileEx`）、后台进程、系统字体、`.exe` 查找、Windows 自动 UTF-8 |
| `scripts/vision_scan.py` | 跨平台识别统一入口：macOS 调用编译好的 Apple Vision Swift 扫描器，Windows/Linux 自动用 RapidOCR + OpenCV YuNet |

## 媒体处理、识别与质检脚本

| 文件 | 作用 |
|---|---|
| `scripts/media_pipeline.py` | 探帧、VFR→CFR 规范化、冻结、按内容缓存回剪渲染（`atoms/`）与 `frozen_tail` 定格/缓漂检测 |
| `scripts/competitor.py` | 本条竞品档案骨架、总览图、水印词与模糊匹配规则 |
| `scripts/shots.py` | 切镜头、帧板与高清导帧 |
| `scripts/scenes.py` | 本地切点分级（色彩直方图 + 音轨静音谷 → 段落锁边/镜头锁边） |
| `scripts/speech_map.py` | 本地 Whisper 听写与按锁边/停顿语义切段 |
| `scripts/words.py` | 词级时间戳（本地多语种 Whisper 缓存，绑定段范围防脏缓存）：定消音窗口与核对生成台词 |
| `scripts/speech_gate.py` | 成片增量听写门禁，查残留竞品品牌名、多余成分与漏改词 |
| `scripts/cutscan.py` | 逐帧切点（不合并短镜）、段边界吸附、闪回关取点与大格帧板生成 |
| `scripts/carton_check.py` | 纸盒像素量尺：青绿色区域 + OCR 找 `LYMPHATIC`，量正面高:宽与外形长:短，超线出并排对照图并拦截 `select` |
| `scripts/caption_lama.py` | 参考片去字幕/去水印（LaMa ONNX 模型 + 跨 RUN 内容块缓存） |
| `scripts/caption_clean.py` + `scan_text.swift` | 字幕 OCR、生成片残留字幕自动清理与残留文字检查 |
| `scripts/caption_matte.py` / `caption_burn.py` | 原片字幕原样复制（差值提取透明度）与改词句仿制样式重画 |
| `scripts/watermark_template.py` + `assets/watermarks/` | 固定位置水印笔画模板比对（不走 OCR，10 分钟片 ~25 秒） |
| `scripts/watermark_brand.py` / `watermark_cleanup.py` + `scan_watermark.swift` | 漂浮水印扫描、四边补扫、固定位置聚类、按块盖白色 `LYMPHLOOP` 与抽检 |
| `scripts/ref_framing.py` + `scan_people.swift` | 提交前检查参考片是否将原片人物裁出画 |
| `scripts/region_inpaint.py` | 原片局部残留与生成段文字门控修复（`:purple` 字幕 / `:ink` 道具印刷字）；拒绝生成段无门控整框抹除 |
| `scripts/reuse.py` / `batch_cluster.py` | 同素材改版片逐帧比对分组与零费用跨 RUN 复用代表片结果 |
| `scripts/seg_sheets.py` | 每段原片密帧板与生成片帧板（`--generated`） |
| `scripts/sound_sync.py` / `audio_verbatim.py` / `source_audio_facts.py` | 音画偏移测算与音频事实辅助 |

## 显式备选 / 兼容脚本（默认主链路不调用）

| 文件 | 作用 |
|---|---|
| `scripts/apimart_wan.py` | 显式切 `LYMPHLOOP_GEN_CHANNEL=apimart` 时使用（APIMART `seedance-2.0` `720p`） |
| `scripts/google_api.py` / `flash_screen.py` | 仅设 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 兼容外部 Gemini 时使用；主链路共用 `google_api` 的原子读写与哈希工具 |
| `scripts/hostd.py` / `finished_patch.py` / `account_preflight.py` | 历史兼容脚本（默认 100x 直传与 `shot_regen` 链路无需调用） |

## 参考文档（`references/`）

| 文件 | 作用 |
|---|---|
| [rules-now.md](rules-now.md) | 现行全部核心规则：换品规则表、五段式校验、每镜大小参照表、人物卡补做、声音归属、定格裁剪、缓存与跨 RUN 共用 |
| [workflow.md](workflow.md) | 逐步命令参数、RUN 目录布局、字幕复制、水印盖字、终检、看板服务与幂等恢复 |
| [skill-rules-full.md](skill-rules-full.md) | 不可违反规则、Agent 守则、跨平台运行环境与首次安装细则 |
| [skill-steps-full.md](skill-steps-full.md) | 流程每一步详述、切段规则与审片自动关 |
| [product-actions.md](product-actions.md) | LymphLoop 物理事实、状态链、5 类竞品剂型动作映射表与 7 种合法成分可见描写 |
| [controlled-edit.md](controlled-edit.md) | 物理优先选型、防照抄动作设计表（§4.8）、台词消音细则（§5）、禁贴图与不删原片红线（§6） |
| [carton-swap.md](carton-swap.md) | 纸盒双图规范、五段式完整范例、人物卡补做与数字量尺标准 |
| [detection.md](detection.md) | Antigravity 原生零 Gemini 四步识别与终检门禁 |
| [qa-checklist.md](qa-checklist.md) | 八类穿帮清单、纸盒量尺、产品帧板终检、6 项 `--audit` 与豁免清单 |
| [watermark.md](watermark.md) | 漂浮水印模板比对、固定位置聚类、覆盖与终检验收完整说明 |
