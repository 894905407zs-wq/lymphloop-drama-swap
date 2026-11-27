# 执行细则与文件布局

`$SKILL` = 本 skill 根目录；`$PY` = `$SKILL/.venv/bin/python`（Windows：`$SKILL\.venv\Scripts\python.exe`），带 opencv 的解释器（首次建：`"$PYTHON" -m venv $SKILL/.venv && $SKILL/.venv/bin/pip install opencv-python-headless numpy Pillow faster-whisper onnxruntime`）。所有命令都可重复执行，已完成的产物不会重做。

## RUN 目录

```
RUN/state.json              阶段记录
RUN/master-cfr.mp4          仅在原片非整数帧率时存在
RUN/watermark/{scan.json,cleanup.json}   水印扫描与清洗报告
RUN/master-clean.mp4        工作母版（识别、切镜头、听写和回剪里保留原片的部分用它；送 Seedance 的参考段从原始源文件裁）
RUN/shots/shots.json  sheets/S###.png  frames/S###.jpg
RUN/detect/{timecoded.mp4,pro_raw.md,pro_nodes.json,overrides.json}
RUN/screen/S###.json  summary.json      逐镜头筛查结果（Agent 的判定经 screen --import-verdicts 展开）
RUN/plan/{decisions.json,segments.json,selections.json}
RUN/plan/{scenes.json,lock_overrides.json,scene_strips/,speech/}   切点分级、听写缓存
RUN/detect/competitor.json  competitor/anchor-*.jpg   本条竞品档案与识别锚点帧（第 1b 步）
RUN/clips/G##.hq.clean.ref.mp4 [*.muted-*.mp4]  送 Seedance 的 720P 参考段（从源文件一次编码；字幕和水印已去掉，首尾杂帧已裁，末帧定住补到整数秒；消音版只动音轨）
RUN/clips/G##.hq.ref.mp4  G##.hq.mp4      同一段没去字幕的版本（OCR 用）和不补尾的原段（审片对照用）
RUN/prompts/G##-a1.txt  G##-a1.final.txt  G##.range.json
RUN/jobs/G##-a1.json                    生成任务定义
RUN/generation/{jobs/*.json,results/*.mp4}   APIMART Seedance 账本与结果（`*.decaptioned.mp4` 抹掉残留字幕的版本，`*.srcaudio.mp4` 人工补救时配原片声音的版本，`G##-aN-c1.mp4` N>1 是本地拼接）
RUN/plan/words/G##.json [G##.generated.json]   词级时间戳（scripts/words.py）
RUN/frames/G##.jpg  G##-out.jpg         每段原片密帧板与生成片帧板（scripts/seg_sheets.py）
RUN/apimart/image_uploads.json          图片上传缓存（72 小时链接）
RUN/qa/G##-a1-c1.png  G##-a1-c1.json   生成片帧板；.json 是外部 Flash 的 QA 记录，默认不调外部 Gemini 时没有
RUN/recut/{edit.json,edit.frozen.json,render/}
RUN/final/<master sha16>/{shots/,screen/,clips/}   成片终检（按成片字节分目录，重渲染的成片一定重新筛）
OUT/master.mp4(+.timeline.json,+.verification.json)
```

## 逐步说明

**一个 RUN 只能有一个写入者**：除 `status`、`review --no-flash` 外，每个 `swap.py` 命令都先拿 `RUN/.run.lock`（进程退出或崩溃即自动释放）；被占用时报 `RUN is busy ... held by pid N: swap.py generate`，不要让第二个 agent 指向同一个 RUN，确需排队就加 `--lock-wait 秒数`。

### 0–1 init / prepare
`swap.py init --video V --run RUN --out OUT`，`swap.py prepare`。VFR 或 29.97 之类非整数帧率会自动 `normalize` 成整数 CFR（`RUN/master-cfr.mp4` + `.normalization.json`）。

### 1b competitor（Antigravity 原生看听模式）
`swap.py competitor`：原生模式下**不调用外部 Gemini**，自动生成 24 格全片总览图 `RUN/agent_review/overview_grid.jpg`、本地 Whisper 全片听写 `RUN/agent_review/transcript.json` 与 3 段 6 秒音色采样视频 `RUN/agent_review/voice_samples/*.mp4`，并写入 `RUN/detect/competitor.json` 骨架。**用 `read_file` 看总览图、听 3 段采样视频（记下说话人性别/年龄/口音/语气/BGM）、读听写稿**，填好 `competitor.json`（尤其 `brand_names`、`form`、`container_look`、`dose_unit_look`、`hard_cases`、`watermark_text`），然后 `swap.py competitor --accept --note "已看总览图并听取音色样本……"`。剂型超出范围同样需用户同意才加 `--out-of-scope-ok`。`LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 可临时切回旧 Flash 模式。

### 2 watermark
`swap.py watermark scan` 先试存好的水印模板（`assets/watermarks/<目标词>-fixed.*`，见 [rules-now.md](rules-now.md#水印模板用户-2026-11-21扫水印这件事太慢了都非常占-cpu选固定位置有水印才盖)）：比对得上就直接写出 `scan.json`（`method: template`，每帧每处的分数存在 `RUN/watermark/template/template_scores.npy`），不跑下面的 OCR、边缘补扫、位置补扫和 0.8 秒补帧；比对不上（终端提示）或加了 `--ocr-scan` 才走 OCR：

`swap.py watermark scan [--fade-pad N]`（和 `detect-pro`、`screen` 同时后台跑：这三步共享读 RUN、各写各的，`state.json` 合并写入，用户 2026-11-19）：先每 4 帧扫一次、命中处前后 12 帧逐帧补扫，4 个进程分段同时扫（`watermark_brand.fast_scan`；10 分钟片子 39 秒，命中与逐帧全扫一致），用 `scan_watermark.swift`（swiftc 编译到 RUN）对 CFR 母版全帧 OCR，命中本条竞品水印词的框（目标词默认取竞品档案，`--target WORD` 可改；**模糊匹配**取目标词里不属于 LYMPHLOOP/YESNAP 的任意 4 个连续字母，LYMPHORIA 就是 PHOR/HORI/RHOR/MPHO，淡水印常被认成 LYMPHORTA、LYMRHORIA、MPHORIA；包装手写体 LymphLoop 读成 LyMPHOR 的不算）自动标为 `floating_overlay`（OCR 写的 `unreviewed` 也会被改掉），框按整行取并向前扩出**水滴图标**（用户 2026-10-25：图标和文字整个都是水印），其它文字标 `printed_label_keep`；接着 `watermark_brand.py` 的**边缘补扫**每 3 帧把左右两条 20% 宽的竖条转正（顺、逆时针各一次）、上下两条 15% 高的横条，放大到长边约 2600 px，原样和加对比各认一次，命中的框换算回原帧坐标并入 `scan.json`（`pass` 字段记是哪一路；`edge_scan.json` 是明细；`--no-edge-pass` 可跳过）；淡入淡出时 OCR 认不出，**每个水印按位置各自**（框按重叠连成轨迹，不按整帧算——同屏另有字幕或第二个水印时不会把它的淡入吞掉）前后各补 0.8 秒（30 fps 即 24 帧，`--fade-pad N` 可改帧数；305 实测淡入约 0.6 秒，旧的 10 帧盖不住）、中间 ≤ 该帧数的空档补齐；字幕/招牌里提到竞品的整行不补（它们是硬切出现的，补了会在没字的帧上糊一条、还会被当成水印盖字）（框外扩 4 px，`synthetic: fade_pad/gap_fill`）。补帧之前先定**固定位置**（用户 2026-11-20）：真实命中（不含补帧、不含字幕行）按位置聚类，每处用命中帧的“每帧都在的边缘”量出真实范围，同一个水印的各处统一成同一大小，写进 `scan.json → positions`；再用 `watermark_brand.position_scan` 只读这几处、整片补扫（先每 4 帧、命中前后逐帧；各处裁出转正放大，原样 / 加对比 / 高通三种拼成一张图 OCR；10 分钟片子约 1–2 分钟），新命中并入 `scan.json`（`pass: position`，`--no-position-pass` 可跳过）。所以默认 `scan` 后直接 `apply` 即可，不必手改。输出里有命中帧数和前几帧号——用 `shots.py frames RUN/master-cfr.mp4 --out RUN/watermark/check --fps 1 --max 20` 导几张看一眼框得对不对；要改分类直接编辑 `scan.json`。然后 `swap.py watermark apply --note "已看 N 帧，框选正确"`。确认整片没有漂浮水印就 `swap.py watermark none --note "..."`。OCR 用不了时可用任何工具产出同 schema 的 JSON，`watermark scan --scan-json X.json` 导入。

### 3 shots
`swap.py shots [--threshold 0.3 --min-shot-seconds 0.4 --fps-sample 2]`。帧板每格烧有源帧号和时间，格宽约 480px。**看完全部帧板**再往下走，边看边记下可疑镜头号。看不清的用 `shots.py frames` 放大。

### 4 detect-pro（原生零 Gemini 模式）
`swap.py detect-pro`：原生模式下自动打包 `RUN/agent_review/shot_grids/shot_grid_##.jpg` 并确保 `RUN/agent_review/transcript.json` 就绪；Agent 用 `read_file` 读听写稿 + 逐批看镜头网格（可疑镜头直接 `read_file` 看 `RUN/clips/S###.mp4`），写出 `pro_nodes.json` 后执行 `swap.py detect-pro --run RUN --import-nodes RUN/detect/pro_nodes.json` 提交阶段记录。画内可见嘴部改词优先遵循 [rules-now.md](rules-now.md) 的**音节数对齐**原则。

### 5 screen（原生零 Gemini 模式）
`swap.py screen`：原生模式下输出 `RUN/agent_review/shot_grids/shot_grid_##.jpg` 与 `RUN/clips/` 路径；Agent 亲自看网格（短镜、弱露瓶镜直接看 `.mp4`）写出精简判定 JSON `{"replace_ids": [...], "unsure_ids": [...], "notes": {...}}`，再跑 `swap.py screen --run RUN --import-verdicts verdicts.json` 自动展开生成 `RUN/screen/S###.json` 与 `summary.json` 并提交阶段记录。

### 6 merge
`swap.py merge` 产出 `plan/decisions.json`。规则：节点（`pro_nodes.json`）与镜头有重叠 → 该镜头继承其 kinds（visual/dialogue/vo）和改写；逐镜头筛查 verdict=replace → visual；override 可 `{"S012":{"decision":"replace","kinds":["visual"],"note":"帧板 F340 瓶子在桌角"}}` 或 `{"S020":{"decision":"keep","note":"筛查时误把水杯当瓶子，帧板确认无竞品"}}`。override 是唯一能把 replace 改成 keep 的途径。

### 7 plan
`swap.py plan [--dry-run] [--no-speech] [--no-scenes]`：按**锁边**切生成段（用户 2026-10-24 定）。**本 skill 的参考段保持小于 15 秒**，上限取 14.9 秒；**上限不是目标**；生成模型只管段内质量，段的起止切错了，回剪永远对不上。规则（`scripts/scenes.py`、`scripts/speech_map.py`）：
1. **每个硬切点分级**（`scenes.cut_levels`，结果和理由缓存在 `RUN/plan/scenes.json`）：每个镜头拼一张首、中、尾三帧的条图（`RUN/plan/scene_strips/`），本地比较切点两侧条图的色彩直方图（HSV 相关系数），不调模型。要分清两件事：下一个镜头是不是同一个物理环境，是不是同一时刻的连续动作。不同环境 = **段落锁边**（相关 < 0.45）；同一环境但没有连续性 = **镜头锁边**（相关 0.45–0.65）；其余是普通硬切（相关 ≥ 0.65）。切点处音轨掉到几乎无声（低于局部电平 8% 且低于全片电平 5%）时升一级：相关 < 0.65 算段落锁边，≥ 0.65 算镜头锁边。直方图只是粗分，下一条的复核不能省。
2. **Agent 复核**：对照帧板看 `scenes.py RUN` 打印的分级，判错的在 `RUN/plan/lock_overrides.json` 改（`{"S019": "shot", "S020": "paragraph", "S007": "none"}`，键是切点后面那个镜头）。每个切点都写了时直接用写的分级。
3. **块**（`speech_map.lock_blocks`）：每个段落（两个段落锁边之间）用本地 Whisper 听写一次（缓存在 `RUN/plan/speech/`），再按镜头锁边分块。块里有产品镜头或要改的台词（`needs_edit`）就整块生成。一句话跨过镜头锁边就合并两块（不超上限时）；跨过段落锁边不合并，写进 `block_notes`。两句间停顿不到 0.35 秒算同一句没说完（听写会把 "Six weeks. You're dancing…" 拆成两句，中间只有 0.28 秒）。
4. **块内切**：每一处 ≥0.8 秒、里面有硬切点的停顿是候选段落边界，切在下一句开口前的最后一个硬切点；这些停顿和下一条的强制切点一起过一次本地的停顿和标点判断（问句后面不切；停顿 ≥ 0.55 秒，或句子以句号、叹号结尾才切），判为一来一回中间（问答）的停顿整段不再算边界；道歉与回应这类本地判断不出的一来一回，Agent 看 `plan` 的输出时并回去，只要合并后仍在上限内。短于 3 秒的段并入相邻段，并入后仍在上限内才并。
   - 段落超过 30 秒时按约 30 秒、落在硬切点上的块分段听写（`transcribe_chunked`，每块前后多送 2 秒音频，句子归中点所在的块）：v025 的 162 秒花店段一次听写时间戳漂约 0.5 秒，把 "Four herbs" 之前的切点判成切到台词。
   - 切点处音轨 ±0.1 秒内掉到局部电平 12% 以下（`scenes.quiet_cuts`）时，听写的句首句尾离切点 0.5 秒以内算听写误差（v025 166.38 秒：听写说 "Four" 早 0.26 秒开口，音轨在切点掉到 3%，测试 2 的参考段从这里切也完整含 "Four herbs"）；切点在句子中间的安静处仍算切到台词。
5. 仍超过上限的段，用最少的切点拆开：候选是不跨句的硬切点，先用同一套本地判断挑出可切的点（问答、道歉与回应这类一来一回、列举、品牌名和它后面的说明都不拆开，本地判断不出的由 Agent 复核时改），优先切在这些点上，其次切在停顿最长的点。实在只能切进句子时，段上带 `warning`，Agent 必须看。
6. 切完后既没有产品镜头、也没有要改台词的段（不满足 `needs_edit`）不生成，原片照放，记在 `segments.json → skipped_product_free`。短段参考用末帧和静音补到约 4.1 秒。成片按审过的有效画声时长顺剪。
7. **单个镜头本身就超过上限时**（少见，`speech_map.split_long_shots`）：在镜头内部的句间停顿中点切开，选切点最少、停顿最长的方案，子镜头记为 `S012.1`、`S012.2`；真实硬切点永远优先。`trim` 记录原片停顿中可考虑的切点。回看生成的同期画声后，再以实际发话边界决定是否手动剪气口。没有可用停顿时平均切开并带 `warning`。

每段在 `segments.json` 里带 `product_shots`（哪些镜头真有产品；其余镜头在提示词模板里预填"看不到产品，不要添加任何产品"）、`lock_in`（这段开头是段落锁边还是镜头锁边、理由）、`block_notes`、`speech`（段内整句，秒数从段起点算）、`cut_in`、`output_seconds`（向上取整、最少 2 秒）、`trim`（镜头内切点的气口）；文件顶层还有 `locks`（全部锁边）和 `skipped_product_free`。`--no-scenes` 不做切点分级，整条当一个块。

`--runs-only` 是旧规则（只把连续产品镜头并成段），只用于对照：长段落测试上会切断 4 句台词、切出 2.1 秒和 3.5 秒的碎段。

**段边界吸附**（用户 2026-11-21，`cutscan.snap_edges`，细节见 rules-now.md“杜绝竞品闪回”）：切完后每条边界对齐到工作母版的逐帧切点，边界外 0.5 秒内的碎镜头并进生成段，只往外不往里，不超 14.9 秒；打印和写进段里的 `edge_snap`，带“check the seam”的要亲自看接缝。`--no-edge-snap` 关掉。

直接正式运行（用户 2026-11-19：先预演再正式出，听写跑两遍；正式运行同样打印分段，要改就改 override 重跑，听写有缓存）。`--dry-run` 只在不想写文件时用。切点分级只问替换镜头前后 20 秒内的切点（`scenes.ZONE_S`，6 个请求同时发），更远的切点记为段落锁边（`far: true`），不影响切段。正式运行只写 `segments.json` 和提示词模板 `RUN/prompts/G##-a1.txt`；参考段在 `generate --prepare-only` 时才裁。

实测：长段落测试（`lymphloop-runs/_长段落测试`）自动切出 S001–S003、S004–S007、S008–S014、S015–S018、S019 五段，美发店没有产品不生成；v025 自动切出用户指定的 159.04–166.38 秒（到 "Four herbs" 之前）和 166.38–179.38 秒。

### 8 写提示词
`plan` 一出就后台跑 `swap.py generate --refs-only`：所有参考片先去字幕、去水印、裁首尾、补整秒（3 路同时，和写提示词同时进行；它和别的命令共享 RUN，不占写锁），之后 `--prepare-only` 直接用缓存。提示词一律五段式（用户 2026-11-20，[rules-now.md](rules-now.md)）：`plan` 写骨架，`swap_design.py observe` + `decide` 按设计表把产品部分填好（只重写没人动过的骨架），再看高清单帧和帧板填满【写…】。产品事实和动作对照见 [product-actions.md](product-actions.md)。`generate` 把去掉 `#` 注释后的正文存为 `G##-a1.final.txt` 提交：五段式检查、设计表核对、画面否定句、参数进正文、勺子和厘米数都在这一步拒绝并列出原因；每段都交 APIMART seedance-2.0（9:16、720p、时长按段，全是参数）。

### 9 generate

**竞品留在参考片里**（用户 2026-11-19）：`--erase` 已停用。准备后 Agent 自己看参考片找出每处竞品，核对它们所在镜头那一行都有 `将@视频1中…更换为…` 替换句（五段式提示词是【参考素材职责】写“完全移除原片中的…”、那一阶段写出我方产品）；脚本不再自动列出竞品（`reference_competitor.items` 只在打开外部 Gemini 时才有）。专门的参考图放 `RUN/refs/`，提示词 `# views:` 写 `run:文件名`。

**用户要求重生成时**（用户 2026-11-19）：`generate --segment G06,G07 --prepare-only --user-approved-regen G06 --user-approved-regen G07 --user-quote "用户原话" [--mute …]` 读 `prompts/G06-a2.txt`（G07 的 a2 已被本地拼接占用时读 `G07-a3.txt`），准备出 `jobs/G06-a2.json`；看完上传帧板后只带 `--segment`、`--user-approved-regen`、`--user-quote` 提交（`--mute` 只在准备时给，提交时给会被拒）。之后 `review --segment G06` 会把 a1 和 a2 都出帧板，`select --job G06-a2 --candidate c1` 选新的那条，`captions` 读所选任务自己的提示词。


1. 运行 `swap.py generate --prepare-only [--segment G01,G02] [--mute G01:1.50-2.28]`。只在本地做，不调用 APIMART：从源文件裁出参考片，用 LaMa 去掉字幕和漂浮水印（`caption_lama.py`，12 秒的段第一次约 3 分钟，两段并行；修补结果有缓存，同样的画面再做一次约 20 秒，见 rules-now.md“去字幕缓存”），裁掉首尾混进来的邻镜头帧，末帧定住补到输出的整数秒；落盘最终提示词和 `RUN/jobs/G##-a1.json`，返回 `upload_video`、帧板、`output_seconds`、去字幕的帧数、清完后仍读得出字的帧数（`frames_left`，超过 2% 直接报错）和被取整的时间戳个数。需要改词的段指定消音窗口。
2. 逐段打开 `upload_video` 与帧板亲自看：字幕和水印去干净了、修补处看不出痕迹（字幕压在脸或产品上的镜头重点看）、首尾没有邻镜头的帧、结尾定住的是本段最后一帧。复听消音处和相邻词。发现问题改消音窗口、切点或提示词，然后重新 `--prepare-only`。用 `# views:` 绑定实际上传的图，顺序为 `@图片1`、`@图片2` 等；`# views: none` 用于只有画外改词、没有产品的段。
3. 运行 `swap.py generate [--segment G01,G02]` 提交已检查的同一批输入。脚本使用准备时的文件与 SHA-256，输入发生变化时会拒绝提交。请求字段：`model: seedance-2.0`、`size: 9:16`、`resolution: 720p`、`duration` = 段长向上取整的整数秒（4–15）、`video_urls`（参考片，传了就是参考视频生成）、`image_urls`、`generate_audio: true`（[APIMART 接口说明](https://docs.apimart.ai/cn/api-reference/videos/seedance-2-0/generation)，2026-11-20 核对；接口页没列价目，首批任务后从账本读实付）。画幅、时长、分辨率只走参数，提示词里不写（用户 2026-11-20）。
4. 每段最多一次付费生成（用户 2026-11-11）。参考片经 APIMART 视频上传（`/v1/uploads/videos`，2026-11-20 起；隧道 `hostd.py` 只作备用），产品图经 APIMART 图片上传，脚本并行提交、轮询和下载。请求发出前落盘提交状态和请求体。没有产生付费结果的尝试（任务失败已全额退款、提交时被拒且未扣费、参考片没发布出去）由脚本自动用同样输入重提，每段最多 3 次，全部记进账本 `unpaid_attempts`；状态不明（超时、断线）可能已被接收扣费，绝不盲目重提，先查 APIMART；401/402/403（密钥、余额）要用户处理（11-11 G10：第一次 invalid_parameter 退款，同样输入第二次成功）。已有任务只按原 task_id 查询和下载。结果在 `RUN/generation/results/`，提交状态、任务号和费用在 `RUN/generation/jobs/G##-a1.json`。

### 10 review / select（原生看片听音 + 6 项结构化自检）
`review` 同时审 4 段（`REVIEW_WORKERS`），`captions` 同时补 4 段（`CAPTION_WORKERS`，共用一个 Whisper 模型）。
`swap.py review [--segment G01] [--fps 2]`：原生模式下默认不调用 Flash QA，但**照常执行本地 `faster-whisper` 台词核对（`words`）**、残留字幕检测（`captions`）、水印/叠加文字检测（`overlay`），并输出每个生成片的 `agent_watch_and_listen` 路径。**Agent 必须直接用 `read_file` 看并听生成片 `.mp4`**（不只看 2 fps 静态帧板），把动效、比例、朝向、口型、音色一次查透。
`swap.py select --job G01-a1 --candidate c1 --audit "competitor_clean=1,front_face_ok=1,scale_ok=1,physics_ok=1,speech_lip_ok=1,text_clean=1" [--clip-in 0 --clip-out 7.5] [--no-caption-clean] [--sound candidate|source] --note "..."`。传入 `--audit` 时，任一项为 `false` 会自动拦截并要求把穿帮写进 `goofs.json` 走 `shot_regen.py round open` 补做（确需保留并记录瑕疵时加 `--allow-audit-fail`）。选用的画面带着它自己生成的声音（默认，不会自动换原片声音；用户 2026-11-21："这种带有口型同步的替换段，不论有没有改口播，都应该要用生成段自带的音频"）；回执里的 `sound_note` 只说明台词相对原片的时间变了，口型看片判断，声音和它自己的嘴对不上按穿帮补做那一镜。`--sound source`（及 `swap.py source-sound` 得到的 `*.srcaudio.mp4`）只能用在看不到说话嘴的画面上，有画内对白的段要加 `--no-mouth`。输出是整数秒、比本段长，不写 `--clip-out` 时只取本段自己的时长（后面是参考片补尾）。写提示词前和审片时用 `scripts/seg_sheets.py RUN [G01,G02] [--generated]` 看整段的密帧板，用 `scripts/words.py RUN [G01,G02] [--generated]` 看每个词的起止时间（定消音窗口、核对生成片的台词）。每段都要 select，生成结果一律选用；计划里有、但决定不提交的段用 `select --job G##-a1 --keep-source-picture --note "..."`（不需要账本和 `--candidate`，原片画声照放，记为 `generated: false`）；生成片上残留的字幕（和本段台词同词、在字幕位置的文字框多于 5% 的帧）会被自动去掉，选用的文件变成 `*.decaptioned.mp4`，记录在 `caption_clean`。`select` 默认取完整候选。看听过实际成片后，可用 `--clip-in/--clip-out`（生成文件内的秒）排除坏镜头、参考补尾或多余气口。对白音画用同一组切点，确认句首句尾完整。
需要用现有素材本地拼接时用 `swap.py splice --segment G05 --out-attempt a7 --picture G05-a1-c1:0-167 --picture G06-a1-c1:168-240 [--audio G05-a1-c1] [--audio-window G09-a1-c1:3.18-4.28@3.72] [--mute-window 2.10-2.30] [--hold-last N] --note "..."`：画面按顺序拼各源的帧段（含首尾帧，各源自己的帧号；一次编码 libx264 crf 12、yuv420p、第一段的帧率与尺寸），默认给每段画面取同一源、同一时间窗的同期声。`--audio` 或 `--audio-window 源:起-止@落点` 只用于没有说话口型的画面（20 ms 交叉淡化）：凡是声音不是那段画面同一来源、同一时刻的地方，都要用 `--no-mouth A-B`（新片秒数，看过那里没有说话的嘴）标出，否则拒绝（用户 2026-11-21）；`--mute-window` 消音；输出 `generation/results/G05-a7-c1.mp4` + `local_splice` 账本，`review` 给它出帧板，再 `select --job G05-a7 --candidate c1`。源名可以是 `G##-aN-cK`、`results/` 里的文件名或路径。
不合格时保留原审片结论。用现有画面或同一音色的现有音频本地修补，复查后记录仍未达标的项目。

### 10b captions（补字幕，用户 2026-11-13；2026-11-19 改为原样复制）
**默认复制原片字幕**（`scripts/caption_matte.py`）：`clips/G##.hq.ref.mp4`（没清字幕）和 `clips/G##.hq.clean.ref.mp4`（LaMa 清过）逐帧相减，差值只在 OCR 识别出的字幕行和标题卡框里取，得到原片字幕的像素和透明度，任何样式都照原样。时间：每句字幕按它的词在生成片逐词听写里出现的时刻平移（单句平移限制在全段中位数 ±0.25 秒，最多 ±1.6 秒），标题卡和用原声的段不平移。哪些文字算字幕：在全段字幕的中心高度带 ±7% 里、和本段原片台词有共同的词（3 个字母以上）；算标题卡：字高 ≥ 画面 3.5%、居中、不是产品用词、停留 ≥ 12 帧。含被替换词（改写前后不同的词、竞品品牌名）的句子不复制，在它的时间窗里按下面量出的样式新画。逐词听写用多语种 Whisper（`small`），按 RUN 识别一次语言（`plan/language.json`；11-19 之前用的英文专用模型把西语片听成英语）。`--draw-all` 退回全部新画。

`swap.py captions --run RUN [--segment G##] [--font "字体名"] [--remeasure]`，在 `select`（和所有 `splice`）之后、`recut` 之前跑。本地、免费、可重复跑（每次都从没有字幕的那份片段重新烧）。

- **样式和位置照原片**：从本片各段没清字幕的参考片（`clips/G##.hq.ref.mp4` 和它的 OCR）量出字幕块的中心高度（每段用自己那段的高度）、字号、行距、最宽一行、填充色、描边色和宽度、句首是否小写、有没有句号逗号连字符、哪些词保持大写（人名），再从本机字体里挑字形最接近的一款。结果在 `RUN/captions/style.json`，对比图 `RUN/captions/style-preview.jpg`（左原片、右仿制）。
- **内容是生成段实际说出来的话**：把选用片段在 `clip_in–clip_out` 内听写一遍（逐词时间按声音收紧），拼写和标点取提示词里的台词；提示词里有、但没说出来的词不写（回执 `in_prompt_but_not_said` 列出）；听写把品牌名听成别的词时按提示词写。一句话一条字幕，停顿 0.7 秒以上另起一条，超过两行就在靠中间的逗号或词处分开。
- **不处理的段**：沿用原片画面的段、片段上已经有字幕的段（把原片接回来的 `splice`），它们本来就带原字幕。没有台词的段也不处理。
- **看过再回剪**：看 `style-preview.jpg`（字体、粗细、描边像不像）和每段的 `RUN/qa/G##-captioned.png`（字对不对、位置和大小与前后原片镜头是否一致）。字体不像就 `--font "Avenir Next Demi Bold"`（名字见 `style.json → font_alternatives`，或本机任意一款无衬线字体）；其他数值直接改 `style.json` 再跑。
- 局限：只能用本机已有的字体，放大看字形有差别；逐词高亮（当前词带色块）的字幕样式还没做，遇到这种片子先告诉用户；原片的标题卡（WEEK 1 之类）不补。
- 选片改了（重新 `select` 或 `splice`）要重跑；`recut` 发现有台词的生成段没补字幕会在终端提醒。

### 7c reuse（批次里画面相同的片子）
`swap.py reuse --run RUN --from 代表片RUN [--from …] [--dry-run] [--tolerance 2] [--unfinished-donor]`：在 `plan` 之后、写提示词之前跑。逐帧比对后把代表片的审定结果导入成 `G9nn`（`reused_segments`），本片落在同一画面上的计划段撤到 `dropped_for_reuse`；比对用的小图缓存在 `RUN/reuse/prints/`。重跑从头算（先撤掉上一次导入）。规则和实测见 rules-now.md“跨 RUN 共用结果”。

### 10c 单镜头重生成（用户 2026-11-19）

**补做用人物卡模式**（用户 2026-11-23："第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式"）：`round open` 默认不裁参考片上传，而是写人物卡模式骨架（`# cards` / `# views` / `# voice`），生成时只挂人物卡、产品图和说话人的声音；先 `shot_regen.py cards build --run RUN` 出卡。细节见 rules-now.md“补做一律用人物卡模式”。下面的参考片做法只在 `--mode reference`（用户点名）时用。

**整条片的穿帮一轮一批做**（用户 2026-11-22，细节见 rules-now.md“补穿帮一轮一批”）：`shot_regen.py round open --run RUN --file goofs.json` → 改提示词 → `round submit`（后台）→ 看 `NAME.result.png` → `round splice [--drop …] [--finish]`；`round status` 看各轮状态，`reasons RUN…` 汇总穿帮原因。下面是单处的手工步骤，`round` 内部就是按它做的；单处 `prepare --goof` 现在必须带 `--why 原因 --note 说明`，`splice --into` 可以一次给多个 `NAME=起-止`。

**2026-11-20 起也用于穿帮补做**（用户 2026-11-20："如果首次生成的整段中出现了穿帮，对照原片看穿帮的是那一镜，然后单独针对穿帮的镜头重写提示词生成。"）：先 `shot_regen.py locate --run RUN --seg G## --at 秒数 [--from-s 停顿秒数]` 找出穿帮镜头和对应的原片帧，它会判断生成片是否左右镜像（镜像就 `ref --hflip`），列出这一镜的台词和改词，并给出 `splice --into 起-止帧`（按生成片自己的切点精确盖住穿帮帧，不按时间换算）。补做后还有穿帮就缩到单个动作（`--from-s/--to-s`）再补做一次（用户 2026-11-20 补充："二次生成如果还有穿帮，继续缩减范围，甚至缩减到单镜头动作继续第三次生成"）；同一处最多补做三次（用户 2026-11-21："后续只要skill进程中检查出来了，都可以执行第三次补做"），第二次必须更短，第三次不能比之前最窄的一次长，`prepare` 按原片秒数核对。
只在用户点名要重做某几个镜头时用（整段一次付费之外的重生成，`prepare` 必须带 `--user-quote` 用户原话）。11-19 测试片 G01–G16 共 14 个镜头这样做，临时脚本散在 RUN 里，现在固化为 `scripts/shot_regen.py`：

1. `shot_regen.py ref --run RUN --seg G05 --name S170 --frames 342-402`：帧号是该段去字幕参考片 `clips/G##.hq.clean.ref.mp4` 的帧（结束帧不含），看 `RUN/shots/sheets/` 和段帧板定。不足 4 秒放慢到正好 4 秒、无声（生成后加速塞回，声音用段落自己的）；4 秒以上原速、末帧定格补整秒、保留原声；多个 `--frames` 是同一人物反复切回的产品镜头，原速首尾相接一起生成（第一块留声、其余静音，`--keep-sound 0,1` 改），回剪时拆开放回各自位置。产出 `shots-regen/NAME.ref.mp4`、帧条 `NAME.strip.png`（先看：首帧、最后一帧真画面、定格帧）和提示词骨架 `NAME.txt`。参考片里的竞品保留（不抹除，用户 11-19）。
2. 填 `NAME.txt`：五段式（[rules-now.md](rules-now.md)）；有设计表就用 `swap_design.py skeleton --shots … --name NAME --force` 覆盖它，第一行 `# views:`（只传该段设计表需要的注册参考图；APIMART seedance-2.0 官方最多 9 张。纸盒双图与密封、已开条包同段时需 4 张，不因旧的三张限制漏图）。画幅、时长、分辨率不写进正文。
3. `shot_regen.py prepare --run RUN --name S170 --user-quote "用户原话"`：只收五段式，拒绝没填的【写】、画面否定句、勺子、正文里的画幅/时长/分辨率/时间戳、图没有职责句；出任务 `RUN/jobs/G05-S170.json`（APIMART seedance-2.0，9:16、720p、时长都是参数）。同一镜头再试一次用新名字（`S170b`），不覆盖已生成的任务。
4. 用户同意后 `apimart_wan.py run-batch RUN/jobs/G05-S170.json …`（提交、轮询、下载并写账本）。多个镜头一次提交，同时生成。用户点名 100x 时才 `prepare --channel x100`，再 `x100_gen.py quote/submit/poll`。
5. `shot_regen.py splice --run RUN --seg G05 --out-attempt a3 S167 S168 S170`：按两边帧率把每个镜头映射回该段已选生成片的同一帧范围，调用 `swap.py splice` 的 `SRC:A-B@N` 变速塞回，放慢的镜头用该段已选片的声音并自动标 `--no-mouth`；原速生成的镜头一律用它自己的同期声（用户 2026-11-21，不再需要 `--shot-sound`）。`--dry-run` 只打印命令。然后 `swap.py select --job G05-a3`，再 `finish.py`。

### 11 recut

- 时长启发式误报：原生 `local_splice` 比原窗口短超过 0.5 秒，但已逐镜、逐句确认原片内容完整时，可用 `recut --content-preserved-review RUN内审核.json`。审核必须精确绑定 RUN、清洗原片 SHA、计划 SHA、当前完整 selection、原生拼接台账与输入输出 SHA、每个原镜头及原句、实际有效时长和同 RUN 实看/听写证据；必须确认画内同窗同期声、无变速/补尾。任何变化使审核失效。这不是删除内容的授权，不替代缺句/缺镜补做，也不使用 `--user-approved-cut` 冒称用户同意删内容。
- `swap.py recut` 按 `selections.json` 冻结并渲染到 `OUT/master.mp4`。替换段的时长取选定片段的实际时长，长了后续画声一起顺延，短了就前移。全程原速，按输出帧网格取整。原时间轴用于定位替换窗口。
- 渲染按块缓存（用户 2026-11-22）：每块的钥匙是内容（文件字节、取的帧或秒、输出格式、编码代码），不含在成片里的位置，块在 `RUN/recut/render/atoms/`；时间轴每段记 `atom_key` / `atom_video`，`brand` 和 `final-screen` 靠它只处理变了的块。细节见 rules-now.md“成片按内容缓存”。
- 有画内对白时，以同一文件、同一时间窗的生成画面和同期声为一组剪，不论台词改没改（用户 2026-11-21）。缺句或生成口型本身有问题时，按穿帮补做那一镜（`lip_sync`，画声一起生成）；原声只能落在无说话口型的镜头上。保留原旁白时，同样先确认画外音完整，再与所选画面在本地配成一组。`--audio-only` 只用于无口型的旁白修补，且原速台词要能放进保留的画面；更长的旁白可与合适的画面一起顺剪。
- 复查替换段接点、完整台词与音画，核对 `master.mp4.timeline.json` 的源帧和成片帧映射，按新时间码交付问题清单。

### 11b brand
`swap.py brand`：按复核过的 `scan.json` 把原水印每一帧的框连成轨迹（同一帧里全帧扫描和边缘补扫的框按四条边各取中值合并（不取并集，否则某一路框松了字就盖歪、盖高）、逐帧中值平滑），竖排轨迹的读向：有模板的位置用模板记录的方向，其余默认向下读，放大截图存到 `RUN/watermark/direction/` 给 Agent 核对（方向反了在 `scan.json` 那一处的命中上写 `orient` 再跑 `brand`），然后在 recut 成片上逐帧先把每个框修补干净（OpenCV Telea，原水印和生成段里带出的淡残影都不会从字缝里露出来），再盖白色不透明 LYMPHLOOP；字幕或招牌里提到竞品的整行（OCR 行里还有别的词，如 "LYMPHORIA 4 HERBS"）只清除、不盖字（也不补淡入淡出帧）；LYMPHLOOP 填满原水印（水滴图标 + 文字）的范围、字母比例不变、方向和原水印一致，一次编码（libx264 crf 12，音轨直接拷贝），写回 `OUT/master.mp4`，未盖版保存为 `OUT/master.unbranded.mp4`，回执 `OUT/master.brand.json`（含抽检：抽 60 帧检查字是不是白的）。重跑会从未盖版重新盖，不会盖两层。原片没有漂浮水印时记为 skipped。recut 按 `master.mp4.timeline.json` 将原水印的位置移到成片对应帧，补查实际生成画面中的残留。

### 12 final-screen & 看板服务
`swap.py final-screen`：原生模式下跳过外部 Flash 筛查，自动把变动镜头帧板与闪回帧板拼成紧凑大图（`final/<sha>/agent_review/changed_shots_##.jpg` 与 `flashback_##.jpg`）供 Antigravity 用 `read_file` 集中审完，同时完整运行本地水印检测、`speech_gate.py` 全片口播检查与 `cutscan.py` 闪回关。
生成完对比看板后，直接用内置 Range HTTP 服务让用户在浏览器稳定拖拽预览（避免 `file://` 中文路径限制）：
- 单条片：`"$PY" scripts/compare_board.py --run RUN --serve --port 8765`
- 多条片导航页：`"$PY" scripts/compare_board.py --runs RUN1 RUN2 --serve --port 8765`

（旧版 `final-screen` 细节与定点修补同下：）
`swap.py final-screen`：筛的是 `brand` 之后的成片（原片有水印却没跑 `brand` 会拒绝）。成片重新切镜头，变动镜头的帧板拼成大图由 Agent 筛查（目录按成片 sha 分开，重新 recut 后不会复用旧镜头片段），再重查一遍竞品水印词（`watermark_target`）：默认只读 `brand_plan.json → positions` 的固定位置、整条成片都读（用户 2026-11-20；旧的全帧 + 四边补扫每轮约 10 分钟，`--full-watermark` 仍可用）；`watermark_left` 列出还残留的时间，必须为空；有残留就 `swap.py watermark add-final`（把 `final/<sha>/watermark/left.json` 的残留框及淡入淡出范围存为成片帧坐标；OCR 认不出、但自己看到的残留用 `--manual 起帧-止帧:X,Y,W,H` 逐帧补进去，不需要先跑终检）→ `swap.py brand` → `swap.py final-screen`，`brand` 自己会修补再盖字，所以不用重新 recut（`--no-watermark-check` 只用于原片确实没有水印的情况）。打开外部 Gemini 时，某个镜头 Flash 连续答不出 JSON 会记进 `errors` / `unsure`，其余镜头照常筛，重跑会重试它。`flagged` 非空就看对应帧板：是漏换 → 在 `overrides.json` 标 replace，核对该原片时间窗是否已提交。首次发现的漏换段走准备与检查；已经提交的时间窗做本地修补；是误报 → 记录理由。

**只查新内容**（用户 2026-11-22）：每轮把成片每块的内容签名写进 `final/<sha>/content.json`，和上一次终检比，只筛新内容的镜头、只听新内容的窗口、只出新内容的闪回帧板（上一版看过闪回帧板时）；上次有未了结问题的块照样重查。回执 `scope.baseline` 记上一版 sha、没变的块数、新内容是哪些段。`--no-baseline` 查全部生成段。

**闪回关**（用户 2026-11-21，细节见 rules-now.md“杜绝竞品闪回”）：终检只出帧板（接缝、拼接点、短于 0.5 秒的碎镜头），不调 Gemini；agent 逐张打开 `final/<sha>/flashback/sheets/spot_*.jpg` 亲自看，看完 `swap.py flashback-review --run RUN --clean`，看到竞品就 `--hit A-B --note "…"` 并按穿帮修，修完重跑 final-screen 再看。`--no-flashback-check` 只在没有竞品画面的片子用。

**默认只查改动处**（用户 2026-11-19：整条再筛、再逐帧 OCR、再整条听写共 45 分钟）：生成段、换了声音的段、保留原画面的计划段，以及原片漂浮水印所在的帧，各前后 1 秒（`swap.changed_ranges`）；只筛和这些范围重叠的镜头，水印词只扫这些帧，听写只听这些时间。没动过的原片镜头在第 5 步已经筛过、画面逐帧相同。`--full` 照旧查整条（例如源片换过、或想再保险一次）。成片听写（`scripts/speech_gate.py`，离线 faster-whisper，结果在 `final/<sha>/speech/report.json`）：`speech_left` 列出还听得到的竞品名（按相似度匹配，听写把 Nuflos 写成 New Flows 也算）和我们产品没有的竞品成分，必须为空；`speech_check` 列出本片改词里被换掉的词在别的时间又出现的地方，逐个看是不是漏改。有命中就回到对应原片时间窗：没提交过的走准备与检查，提交过的做本地修补。`--no-speech-check` 可关掉；没装 faster-whisper 时回执里写 `speech_gate: skipped`。

已知误报：成片里提到目标品名的台词（`speech_target_product` 备注，不再计为 replace）、目标纸盒上的 "LYMPHATIC DRAINAGE SUPPORT" 字样、草药特写（cleavers 等同时是竞品与目标的成分）、青绿色条包或浅色饮品被当成竞品（2026-11-12 三条 Nuflos）。某个镜头接口报错（HTTP 500/503）时重跑终检，或对照上一遍里该镜头的结果。

局部残留的定点修复（不重跑 recut）：`scripts/region_inpaint.py OUT/master.mp4 OUT/master.fix.mp4 --box X,Y,W,H[:purple|ink] --frames A,B [--box ... --frames ...] [--gate purple] --report OUT/master.fix.json`（门控按框：`--box X,Y,W,H:purple` 或每个 `--box` 各给一个 `--gate`；只给一个 `--gate` = 所有没自带门控的框）。用途：保留原片画面的段里字幕词与新音轨不符（`--gate purple` 只擦字幕高亮色+白字，范围可放宽）；水印追踪器漏掉的淡影（位置只出现一两次、OCR 认不出）。生成段里道具（卡片、标签）上留下的竞品字用 `:ink`：只抹框内浅色表面上的灰黑印刷字，手指和卡片边缘不动，框可以画得比字大一圈；整框（不带门控）只用于原片画面（保留的原片镜头里的字幕残块、淡水印、原片里的小瓶）；**落在生成段帧范围里的整框填充会被拒绝**（用户 2026-11-14 禁止贴图补救生成段：生成画面里的产品、道具、徽章穿帮只能裁掉、借兄弟片整镜，或报告用户决定是否付费重生成；判断生成段靠 `master.mp4.timeline.json`）。盒子要贴着文字画，半径 2–3；修完把原 `master.mp4` 改名 `master.pre-*.mp4` 再换入，并在交付说明里写明帧段。

### 附：改词消音与回剪

- 改词由 Seedance 按参考片里原说话人的音色重新生成。只改旁白的段用 `select --audio-only`，保留原画面。具体成分词改了且画面也有该成分时，按对应镜头同时改画面。
- 窗口大小（2026-11-13 整批实测，详见 controlled-edit.md §5）：旁白改词消整句；画内旧词在句中、消掉后和停顿连成 0.8 秒以上空白的消整句；品牌名消 "It's called ___" 整个分句并盖住原词结尾的 s；句尾单个替换词只消这个词。
- 用 `generate --prepare-only --mute G##:1.50-2.28` 消音（只动音轨）。准备时会对照 `RUN/plan/words/G##.json`（先跑 `words.py RUN`）检查每个窗口有没有切到旧词、留下听得到的一截，有就在终端提示该放宽到哪里；按提示改窗口重新准备。听原片并对照波形选窗口，复听实际上传文件确认相邻词保留，段尾改词要保全整句，以实际同期画声决定成片切点。提示词里的静音起止与准备参数一致。
- 旁白缺词时可用同一音色的现有合格片段做本地补音，用 `splice --audio-window` 加 `--no-mouth` 并复听衔接。画内对白缺词不借音，补做那一镜（`lip_sync`）。无法修好时注明缺词。

## 恢复
所有命令幂等。`state.json` 记每步完成时间；`generation/jobs/*.json` 记每个任务的 task_id、下载状态和花费，重跑 `generate` 时，已记录任务只查询和下载。无 task_id 且已发出请求时查 APIMART 任务历史，保留状态和账本。共享托管看 `hostd.py status` 和 `hostd.log`（`~/.claude` 下这份用 `~/.cache/lymphloop-host-claude/`，Codex 那份用 `~/.cache/lymphloop-host/`，`LYMPHLOOP_HOST_ROOT` 可改）。ffmpeg/ffprobe 查找顺序：`FFMPEG_BIN`/`FFPROBE_BIN` → `$SKILL/bin` → PATH → `~/.codex/tools/ffmpeg`；cloudflared：`CLOUDFLARED_BIN` → `$SKILL/bin` → `~/.codex/tools/wan-media/2026.10.0`。改了上游（比如 overrides、lock_overrides）后重跑 `merge → plan`。
