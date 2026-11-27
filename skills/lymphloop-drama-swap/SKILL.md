---
name: lymphloop-drama-swap
description: 将任意竞品保健品（滴剂、硬胶囊、软胶囊、片剂、软糖、粉剂）的竖屏短视频广告换成 yesnap LymphLoop 成片（Antigravity 原生看听版，默认零外部 Gemini 调用）。由当前 Agent 直接用 read_file 看视频/帧板与听音轨，配合本地 faster-whisper、LaMa 去字幕与规则表设计，默认走 100x 渠道 `100x-minimax-h3`（`standard` / `768P` / `9:16`，时长按段）生成；穿帮补做默认走人物卡模式；对白改词优先音节数对齐；回剪、盖水印、终检与内置 Range HTTP 看板一条龙完成。
---

# LymphLoop 换品（Antigravity 原生看听版）v2.2（2026-11-24：零外部 Gemini 原生模式、原生看视频+听音频全链路、对白音节数对齐、6 项结构化审片自检、内置看板 HTTP 服务；保留 2026-11-23 人物卡补做与 2026-11-22 批次能力）

本插件版为 **Antigravity 原生多模态执行环境**定制：**当前 Agent 本身具备直接看视频（`.mp4`）、看帧板（`.jpg`/`.png`）与听音频（`.mp4`/`.wav`）的能力**。
- **默认零外部 Gemini 模式**（`scripts/agent_native.py`）：`google_api.py`、`speech_map.py`、`scenes.py`、`watermark_brand.py`、`swap.py` 默认完全不调用外部 Gemini API（不再依赖 `GEMINI_API_KEY`）；本地用 `faster-whisper`（经 ffmpeg 解码规避 PyAV 兼容性问题）+ 视觉/音频特征 + Antigravity 原生 `read_file` 承接全部看、听与语义判断。仅当显式设置 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时才回退外部 Gemini。
- **独立插件副本**：仅在本插件目录内迭代，**不要改动** `~/.claude/skills/lymphloop-swap-claude`；各自的 RUN 目录不要混用。

> **读法**：这份文件必须能被 Antigravity 一次读完（它读文件一次最多 46 KB；改完用 `wc -c SKILL.md` 核对，保持在 42000 以内）。这里每一条都是摘要，做某一步之前先读对应的细节文件：[skill-rules-full.md](references/skill-rules-full.md)（规则、守则、固定事实、并行、交付的完整原文）、[skill-steps-full.md](references/skill-steps-full.md)（每一步的完整说明、切段、审片自动关）、[watermark.md](references/watermark.md)（漂浮水印）、[file-map.md](references/file-map.md)（文件地图）。摘要和细节文件有出入时以细节文件为准。**新增的细节、实测数据和用户原话写进细节文件，不往这里加。**

## 不可违反的规则

每条的来由、例外的完整写法和实测见 [skill-rules-full.md](references/skill-rules-full.md)。

1. **宁多换不漏换**。第 4 步的节点、第 5 步的逐镜头筛查、看帧板时任一处判"有/拿不准"的镜头都进替换清单；只有亲眼看过帧板并写了 override 才能改回 keep。
2. **提示词由 Agent 写，审片由 Agent 定**。脚本的自动关只是辅助；不把"API 成功"当"内容合格"。每段先写清楚原片里发生了什么，再写要生成什么。
3. **物理优先**：每段先判断原片动作在现实里用条包还是盒装做得到，绝不为了迁就原动作让产品弯折、拉长、自己立着或被捏扁；同一场戏里产品形态和位置前后连续；画面里不撕条包。
3b. **动作设计避开参考片照抄，竞品靠替换句和参考图换掉**：模型会照抄参考片的姿态、道具和口型。**参考片只去字幕和水印，竞品原样保留，不抹、不定格替换**。每一镜里看得见的每一处竞品都写替换句：`将@视频1中<竞品的颜色、形状、位置、谁拿着>更换为@图片N的<我方产品、状态、比例>`，竞品在做条包做不到的动作（倒、滴、拧、舀）时连动作一起换；替换句之后照常正向写新产品的样子、正面朝镜头和比例句。原物离手立着的那一拍条包不离手；提示词里不出现任何勺子；产品不被手整个包住；竞品品牌名不写进提示词，用外观描述；产品只写在参考片原本有竞品的位置。`generate` 准备后 Agent 自己看参考片找出每处竞品，核对每处所在镜头那一行都有替换句，缺了就补上（脚本不再自动列出竞品）。手机购物页、整箱产品这类特殊呈现给专门的参考图：放在 `RUN/refs/`，`# views:` 写 `run:文件名`。小而远的镜头、反复切镜的短段落和无产品镜头按 [controlled-edit.md §4.8](references/controlled-edit.md) 处理；后期修补只是兜底。
3c. **禁止贴图补救生成段**：生成出来的画面里有穿帮，不许贴色块 / 色条、贴产品图、跟踪贴片、从别的帧或别的片子抠一块贴进来、局部调色，不许用 LaMa / inpaint 把画面里的产品或道具抹掉、改画，不许用原片画面放大重取景或裁切别段的生成画面去顶替生成镜头，不许自己写 ffmpeg 脚本拼生成段。能做的只有整帧级处理（去掉 0.5 秒以内的闪帧、借同一素材兄弟片的整镜连同期声接在硬切上、消音或补音）和文字类处理（去残留字幕和标题卡、`:ink` 抹道具上的旧品牌字、原水印位置盖白色 LYMPHLOOP）；都救不了时写进交付说明并报告用户，由用户决定是否付费重新生成。`recut` 和 `splice` 只接受账本里登记的生成结果和原片整帧。没有重新生成的原片画面不在此列。
3d. **不许删原片内容、不许跳过要换的段**：成片里原片的每个镜头、每句台词都要在，要么是原片，要么是换好的生成段。不剪掉镜头或整句来躲开穿帮，不把有竞品的段留着不做，不把选片剪得比原段短（`recut` 拒绝短 0.5 秒以上的选片）；只有用户明确同意删某段时才加 `recut --user-approved-cut G##`。**原片人物照原片生成**：卡通人物、骷髅角色这类 AI 造型都按参考片原样生成，不把人物镜头换成静物、不删人。**参考片也不许把人裁出画**（`generate` 提交前用 `ref_framing.py` 检查；只有裁掉的是海报、包装上的人像时才加 `--allow-reference-crop G##`）。
4. **一段最多一次付费生成，结果一律采用**。审片不过不重做。没有产生付费结果的尝试（任务失败已全额退款、提交时被拒且未扣费、参考片没发布出去）由脚本自动用同样输入重提，每段最多 3 次，全部记进账本 `unpaid_attempts`；状态不明（超时、断线）可能已被接收扣费，绝不盲目重提，先去生成渠道查原任务；401/402/403（密钥、余额）要用户处理。例外只有两种：**用户在对话里明确要求重生成**（先找出上次失败的原因，改参考片和提示词，写到 `prompts/G##-a2.txt`，`generate --prepare-only --user-approved-regen G## --user-quote "用户原话"`，看过上传帧板再提交）；**审片发现穿帮的那一镜按第 10c 步单独补做**（同一处最多三次，一次比一次范围小，只重做这一镜、按帧拼回，不整段重生成）。
5. **完整语义分段**：段的切点不切断一句话，不把产品镜头和它所配的那句台词拆开，不跨段落锁边；相邻替换段直接相接。
6. 送去生成的参考片和生成结果都不带字幕、标题卡、水印或原品牌文字（字幕会把台词和口型绑死，换不成新词）；选片后由 `captions` 在本地按原片字幕的样式和位置把字幕补回生成段，内容只写生成段实际说出来的词（没说出来的词不写）。没有被重新生成的原片镜头保持原样（含其字幕）。
7. **台词**：整段的台词连口型由模型重新生成，提示词里逐句写出；没有冲突的台词一字不改；冲突只有产品名（任何竞品名）、剂型（drops、capsules、softgels、gummies、powder……）、用量用法和我的产品没有的具体成分（草药数量、泛泛的功效说法不算，见 [controlled-edit.md §2](references/controlled-edit.md) 第 4 条）；冲突台词只改冲突词，提示词正文里绝不写被替换的旧词；生成片台词要听写回读，不信 QA 字幕。**永远不用 TTS**：对白、旁白、成分改词都由生成模型按参考片里的原音色重新生成。有冲突词的段在准备时消音（`generate --prepare-only --mute G##:起-止[,起-止]`，只动音轨）；窗口怎么定见 skill-rules-full.md 的同一条和 workflow.md 附录。
8. **费用**。APIMART seedance-2.0 的接口页没有列价目，生成前向用户报段数和总输出秒数；首批任务完成后从账本 `cost_usd`（任务返回的 `cost`）读实付单价，之后按它估算，交付按账本报告实际费用。
9. 做不到就说做不到：三层识别仍可能漏，终检是兜底不是保证；720p 生成段与原片清晰度可能有落差。

## Agent 执行守则（2026-10-24 Claude 与 Codex 同批对比实跑后定，Antigravity 照用）

每条的完整原文见 [skill-rules-full.md](references/skill-rules-full.md)。

1. **亲自看图**：帧板（`RUN/shots/sheets/`）、生成片帧板（`RUN/qa/`）、拼接前后的对比图，一律用图片查看工具打开亲自看；只看 JSON 或 OCR 结论就选片、判 keep，算违规。每段选片前至少看过一张生成片帧板。
2. **长步骤放后台**：`watermark scan`、`generate`、`recut`、`final-screen` 可能跑 5–30 分钟（`detect-pro`、`screen` 现在只出拼图和导入结果，几秒就完）。先 `mkdir -p RUN/logs`，再用 `nohup "$PY" scripts/swap.py <步> --run RUN … > RUN/logs/<步>.log 2>&1 &` 后台跑，用 `tail -n 30 RUN/logs/<步>.log`、`"$PY" scripts/swap.py status --run RUN`、`RUN/generation/jobs/*.json` 查进度。前台命令被超时杀掉会在账本里留下 submitting 状态，必须先去生成渠道查任务再处理，绝不盲目重提（会重复扣费）。
3. **只动自己的 RUN**：开跑前新建自己的 RUN/OUT 目录；绝不往别人的 RUN 写提示词、提交生成、改 `selections.json` 或回剪。`swap.py` 的写操作带 RUN 锁，碰到"被另一个进程占用"的报错说明有人在用这个 RUN，换自己的目录，不要删锁。
4. **不杀别人的进程**：要停进程先 `ps -axww | grep -E "swap.py|apimart_wan|x100_gen|hostd"` 看完整命令和 RUN 路径，只停自己启动的。`hostd.py` 是所有 RUN 共享的托管，不要 `stop`，慢不要重启；不要自己另起隧道、文件服务或第三方上传。提交交给 `swap.py generate`，不要自己分批、限并发或另写提交脚本。
5. **按实际时长顺剪，画面和它生成的声音一起用**。提示词里每一句台词都写出来，不写"原声照原片"。选用的画面带着它自己的声音进出，不拆开；所选段长了，后续画声一起顺延，短了就前移；保持原速，不为卡原时长补帧、截句或变速。**画面里看得到有人说话的地方，声音只能是同一个文件、同一时刻的自带声音，不论台词改没改**：原片声音、首次生成的声音、借来的词只能放在看不到嘴的画面上（旁白、背影、空镜、插入镜头），`splice` 和 `select --sound source` 要求 Agent 看过后用 `--no-mouth` 标出这些地方，否则拒绝。生成的声音和它自己的嘴对不上是穿帮，补做那一镜，不靠换声音。
6. **次数记账**。每个目标段只有 a1 和 c1，最多一次付费结果（用户明确要求重生成的段另开 a2，见规则 4）。未付费的失败由脚本自动重提（`unpaid_attempts` 记每一次），不要自己写重提脚本；状态不明的提交恢复时只查询原任务或下载结果。本地拼接由 `swap.py splice` 记为 `local_splice`。
7. **凭据**：密钥只从环境变量或钥匙串读，不打印、不写进日志或提示词；沙箱禁网、禁钥匙串时请用户切到完全访问再跑。
8. **汇报**：每条视频交付时写清段数、各段用了第几次生成或哪种拼接、总花费、终检结果、已知瑕疵。
9. **批次里报了母版问题，不要整批停下**。问题写进维护者记录（复现命令 + 报错原文），其余段和片子照常推进到交付；母版修好后直接续跑原命令。补满三次的穿帮也不停、不问，先当瑕疵交付，最后汇总成一张清单交用户一次裁定。批次经验见 references/rules-now.md 的"批次运行经验"和"上限瑕疵的破例与本地修复"。

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

完整原文（含来由和实测）见 [skill-rules-full.md](references/skill-rules-full.md)。

- **产品**：yesnap LymphLoop，Orange Mango 口味，30 支/盒，每支 8 g 液体条包。青绿色翻盖纸盒（10×7×13 cm 级）+ 青绿色柔性条包（约一根食指长、比拇指指腹略宽，侧边缺口）。**画面里不撕条包**：展示用未开条包（可轻晃），喝的镜头从第一帧就是已开条包闭唇轻抿，一镜到底就中间加硬切（规则见 [controlled-edit.md §4.2](references/controlled-edit.md) 第 6 条）。`assets/product/profile.json → facts` 是事实来源，[product-actions.md](references/product-actions.md) 是写提示词前要看的事实、状态链与动作对照。
- **送模型的产品参考图**（前三张来自真人手持实拍，识别和生成都只用这些和 `RUN/refs/` 里为这条片子做的参考图）：
  - `handheld_photo`：`assets/product/stick-handheld-scale.jpg`，手持密封条包（展示、抽出、拿着讲话）。
  - `opened_packet_hand`：`assets/product/opened-packet-handheld.jpg`，手持已开条包（喝、往水里挤、拿着已开条包的镜头用它，2304×4096 原图）。
  - **纸盒只有两张图，一起给**：`carton_views` = `assets/product/carton-views.jpg`（同一个纸盒白底九个角度，无手）+ `carton_hand` = `assets/product/carton-hand-scale.jpg`（一只真手拿着纸盒正面，只管纸盒和手的比例、握法）。**换纸盒的段落按 [carton-swap.md](references/carton-swap.md) 的五段式写**。**提示词里绝不写"与原瓶大小一致"**（纸盒会被画成瓶子那样细高）。
  - **屏幕上的商品页两张**：`phone_page` = `assets/product/landing-page-phone.jpg`、`desktop_page` = `assets/product/landing-page-desktop.jpg`。原片手机或电脑屏幕上显示竞品购物页 / 落地页时，整页换成我方商品页（价格不用管）。规则表 S1 自动判；观察没认出来的，在 `detect/overrides.json` 那一镜写 `"screen": "phone"` 或 `"computer"` 再跑 decide。
  每段先按 [controlled-edit.md §4](references/controlled-edit.md) 判断现实里该用条包还是盒装、条包开没开，再选图。`assets/physical/` 四段实拍视频只给 Agent 看，不上传。
- **竞品不限于 Lymphoria**（用户 2026-11-11）：滴剂、硬胶囊、软胶囊、片剂、软糖、粉剂都换。每条视频先 `swap.py competitor`，脚本出 24 格全片总览图、本地听写和声音采样，Agent 看图、听采样后写出本条的竞品档案 `RUN/detect/competitor.json`（品牌、剂型、容器与剂型单元长相、服用方式、难例、漂浮水印文字），核对后 `--accept`；识别、审片、终检和水印扫描都以它为准。外用、器械类不在范围内，`--accept` 会拒绝，先告诉用户。`assets/competitors/` 只是内置的 Lymphoria 锚点图（没有档案的旧 RUN 用），只用于识别，永远不作生成参考。
- **识别契约** `assets/prompts/detect-conflicts.txt` 是用户原文，SHA 锁定，不改。
- **生成路线**：每一段（纸盒、条包、只改台词的段、人物卡补做都一样）默认走 **100x 渠道 `100x-minimax-h3`（`standard` / `768P` / `9:16`）** 参考生成；画幅、分辨率、线路和时长（段长向上取整的整数秒 `4–15`）全部走接口参数，提示词里不写。`generate` 和 `shot_regen.py round submit` 直接走 `scripts/x100_gen.py run-batch`，参考片直传 100x。临时切换：`LYMPHLOOP_X100_MODEL=100x-seedance-2.0`（`720P`），或 `LYMPHLOOP_GEN_CHANNEL=apimart`（也可改 `~/.cache/lymphloop-models/local-slots/channel.txt`）；100x `seedance-2.5` 无 `9:16` 参数，不用。参考片是去掉字幕和水印的原段，末帧定住补到同样的整数秒，不超过 15 秒。付费前要用户同意。详见 [执行细则](references/workflow.md#9-generate)。
- **逐段写提示词，一律五段式**：【生成目标】【参考素材职责】【主体与关系】【事件脚本】【保持一致】（规则见 [rules-now.md](references/rules-now.md)）。`plan` 写出骨架，`swap_design.py decide` 按设计表把形态、图、数量、移除清单和每镜动作填好；Agent 只填【写…】：场景、人物、音色、每阶段的景别机位和终态、台词语气。一个镜头一个阶段，每阶段以“结束时……”收尾；台词写成 `{原词}`，带语气和语言；【事件脚本】只写正向，“完全移除 / 不采用 / 仅”只写在另外几段；正文不写时间戳、秒数、画幅、时长、分辨率，也不写镜头号、规则号；每张上传的图在【参考素材职责】里单独一行写职责。`generate --prepare-only` 只收五段式，不过就拒绝。
- **一次成片**：每个目标段最多一次付费生成，生成什么都要用（规则 4）。成功率全靠提交前做对：参考片干净（无字幕、无水印、首尾不带邻镜头的帧）；竞品逐镜头找到每一处并写成目标产品；目标动作按原片手的运动轨迹选；服用一律闭唇轻抿。审片只记录缺陷和做免费的本地处理（裁切、`select` 自动去字幕、拼接、定点修补）。
- **提交前检查实际上传的参考片**。先用 `generate --prepare-only` 产出准确文件和帧板，亲自看：字幕和水印去干净了、修补处看不出痕迹、首尾没有邻镜头的帧、结尾定住的是本段最后一帧；复听有消音的窗口，再按 controlled-edit.md §7 逐条自查提示词，然后提交该段（付费只此一次）。
- **凭据**：默认零外部 Gemini 模式无需 `GEMINI_API_KEY`（仅 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时读取）；APIMART 用 `APIMART_API_KEY` 或钥匙串 `codex-seedance-apimart/api-key`（钥匙串条目沿用旧名）；都不出现在日志。
- **运行环境和首次安装**：macOS 或 Windows（Linux 同 Windows 的做法）。所有命令用 `$PY=$SKILL/.venv/bin/python` 运行（Windows 是 `$SKILL\.venv\Scripts\python.exe`，文档里其余写法不变）。`$SKILL/.venv` 不存在时先建环境：`python3.12 -m venv "$SKILL/.venv" && "$SKILL/.venv/bin/pip" install -r "$SKILL/requirements.txt"`；ffmpeg 要带 drawtext 滤镜（macOS 用 `brew install ffmpeg-full`）。识别引擎、Windows 的安装命令、100x 客户端和各缓存目录见 skill-rules-full.md 的“运行环境”“首次安装”两条。

## 流程（每条视频一个 RUN 目录，`scripts/swap.py` 子命令按序）

| 步 | 命令 | Agent 要做的事 |
|---|---|---|
| 0 | `init --video V --run RUN --out OUT` | 记录源与探帧 |
| 1 | `prepare` | 非整数帧率自动规范化 |
| 1b | `competitor` → 看总览图、听声音采样、填 JSON → `competitor --accept` | 本条竞品档案：品牌、剂型、容器与剂型单元、难例、水印词；剂型超出范围先告诉用户 |
| 2 | `watermark scan` → 看 `scan.json` 摘要与几张帧 → `watermark apply`（或确认无水印用 `watermark none`） | 有存好的水印模板时逐帧模板比对，不做 OCR（Lymphoria 的片子水印在同样 6 处），看 `places` 每处的帧数和 `scores` 即可 apply；比对不上自动退回 OCR 扫描，这时确认框选的是漂浮水印（图标 + 竞品水印词整个），不是包装印刷或字幕，每个固定位置的截图在 `RUN/watermark/place_kind/`，逐个看是真水印还是产品标签。和第 4、5 步同时后台跑。见下面“漂浮水印” |
| 3 | `shots` | 得到 `RUN/shots/sheets/S###.png`，**每张都要看** |
| 4 | `detect-pro`（和第 2、5 步同时跑） | Agent 读全片听写、逐批看镜头拼图 `shot_grids`（可疑短镜直接看 `clips/S###.mp4`），按固定契约写出三栏节点与最小改词 `pro_nodes.json`，`detect-pro --import-nodes` 导入 |
| 5 | `screen`（和第 2、4 步同时跑） | Agent 逐批看镜头拼图逐镜头筛查，写出 `{replace_ids, unsure_ids, notes}`，`screen --import-verdicts` 导入 |
| 6 | `merge` | 看帧板的结论、第 4 步的节点、第 5 步的逐镜头筛查取并集；要推翻时写 `RUN/detect/overrides.json`，再 merge |
| 7 | `scenes.py RUN` → 复核 → `plan`（直接出；要改就改 override 重跑，听写有缓存） | 只给替换镜头前后 20 秒内的切点分级（本地比较色彩直方图和音轨跌落，不调模型），更远的切点一律当段落锁边；看每个切点的锁边分级，判错的写进 `RUN/plan/lock_overrides.json`；再按下一节的规则切段，生成提示词模板 `RUN/prompts/G##-a1.txt`。`edge_snap` 里写“check the seam”的边界要亲自看（杜绝滴剂瓶闪回） |
| 7b | `swap_design.py observe` → `decide` | **换品设计，Agent 不临场判断**：Agent 看 `observe` 出的拼图（可疑镜头直接看片段），对每个替换镜头只回答客观问题，`observe --import-obs` 导入；规则表决定换成纸盒 / 未开条包 / 已开条包、动作照原片还是重新设计、数量和参考图，写进 `RUN/plan/swap_design.json`。规则表和流程见 [rules-now.md](references/rules-now.md)，它优先于其他文档里零散的形态规则 |
| 7c | `reuse --from 代表片RUN [--from 另一条] [--dry-run]`（这条片先照常做到 `plan`；代表片先做完） | **批次里画面相同的片子共用结果**：先 `--dry-run` 看报告（`adopted` / `set_aside` / `kept` / `notes`），再正式跑；共用段不写提示词、不做参考片、不补字幕，发现穿帮回代表片的 RUN 修，修完在这条片重跑同一条命令。细节见 rules-now.md“跨 RUN 共用结果” |
| 8 | `plan` 一出就后台跑 `generate --refs-only`；7b 的 `decide` 已把骨架按设计表填好，亲自补完每段 | `RUN/prompts/G##-a1.txt` 是五段式骨架，`# views:` / `# design:` 和产品部分来自设计表，不改；看该段高清单帧和帧板后填满所有【写…】（场景、人物、每个说话人的音色、每阶段景别机位和“结束时”终态、台词语气和语言）；**每个有产品的阶段写“大小参照：”**，用这一镜里看得见的手、脸或身边物件说清产品多大，前后镜头一致（见 rules-now.md“每镜大小参照”）；有改词的段定好消音窗口。设计表没覆盖的产品镜头先 `swap_design.py observe --shots S###` 再 `decide`，不自己定形态 |
| 9 | `generate --prepare-only` → 检查实际上传参考片 → `generate` | 先在本地准备，不调用接口：五段式检查和设计表核对不过就拒绝；有改词的段用 `--mute G##:起-止`（只动音轨）。参考片里的竞品原样保留；Agent 自己看一遍参考片，找出每处竞品所在的镜头，逐条核对：【参考素材职责】有“完全移除原片中的…”，且每处所在镜头的那一阶段写出了我方纸盒或条包，缺了就补上（脚本不再自动列出竞品，也不会因此拒绝）。看帧板确认干净、复听消音处后，已准备的整批一次性提交；每段最多一次付费生成，未付费的失败脚本自动重提 |
| 10 | `review` → 看生成片帧板，并直接看、听生成片 → `select`（`--audit` 六项自检） | 每段都 select，结果一律采用；**只找穿帮**（清单见 [qa-checklist.md](references/qa-checklist.md) 开头），有穿帮按第 10c 步补做，比例差不多、包装小字、纸盒透视这些不记瑕疵；**纸盒比例不靠看视频判**：`select` 自动跑 `carton_check.py` 量（正面高:宽 > 1.65、外形 > 2.3、竖着站看不到正面就拦），超线时用 read_file 看并排对照图再定，`--audit` 的 `scale_ok` / `front_face_ok` 不能和量出来的结果矛盾；定裁切；残留字幕 `select` 自动从生成片上去掉 |
| 10b | `captions`（4 段同时；只重做选用片、台词或样式变了的段，`--force` 全部重做） | **原片字幕原样复制**到生成段（按生成片里同一句话说出的时刻平移后叠回去，标题卡一起回来）；含被替换词的那几句才按量出的样式新画。看每段的 `qa/G##-captioned.png` 再回剪 |
| 10c | `shot_regen.py cards build --run RUN` → `round open --run RUN --file goofs.json` → 改每个 `shots-regen/NAME.txt` → `round submit` → 看每张 `NAME.result.png` → `round splice [--drop 仍有穿帮的]` → `finish.py --from captions` | **补做用人物卡模式**：首次生成照旧挂原片参考视频；穿帮的补做不挂参考视频，只挂人物卡 + 产品图（说话镜头再挂说话人的一段声音），场景、机位、站位、动作、台词全写进提示词；先写 `RUN/cards/cast.json`，出卡后亲自看。**穿帮镜头单独补做，一轮一批**：审完所有段再动手，不要发现一处做一处；只做穿帮的那一个镜头，不整段重生成；`goofs.json` 每处写 `{"seg": "G05", "at": "生成片里的秒数", "why": "原因", "note": "看到了什么"}`，`why` 是 qa-checklist 穿帮清单的八类之一，每次补做都要登记原因。看得到有人说话的镜头原速补做、连口型一起生成，只用它自己的声音。补做后还有穿帮就缩小范围再做，第二次必须比第一次短，同一处最多三次。详见 rules-now.md“补做一律用人物卡模式”和 [workflow.md §10c](references/workflow.md) |
| 11 | `recut` | 以审过的片段时长顺剪到 `OUT/master.mp4`。有画内对白时，画面和同期声从同一素材取同一时间窗。**按内容缓存**（用户 2026-11-22）：每块按内容编码，补一个镜头只重编那一块，后面的块原样无损拼到新位置 |
| 11b | `brand` | 按成片时间轴映射原水印位置并盖上**白色不透明 LYMPHLOOP**（原位置、原大小、原方向，连水滴图标一起盖；**每个固定位置整条片只用一个框、一个方向，字不忽大忽小**），写回 `OUT/master.mp4`（未盖版留作 `master.unbranded.mp4`），逐帧抽检确认盖上了；按块盖，没有水印的块不重编，盖过的块按内容缓存 |
| 12 | `final-screen`（默认只查改动处；`--full` 查整条） | 只查生成段、保留原画面的计划段、原水印所在帧，并和上一次终检的成片按内容比。变动镜头和闪回帧板拼成大图（`final/<sha>/agent_review/`），Agent 逐张看；重新查竞品水印词（必须 0 帧，`watermark_left` 列出残留时间）；成片整条听写查口播（`speech_left` 竞品名和我们没有的成分必须为空）；原片有水印却没跑 `brand` 时拒绝。**闪回关**：逐张看接缝、拼接点和碎镜头的帧板，看手里、包口、口袋、桌面、画边有没有不是我方条包/纸盒的东西，看完 `swap.py flashback-review --run RUN --clean`，看到就 `--hit 帧-帧 --note "…"` 并当穿帮修。**产品帧板**（`final/<sha>/product/`）：逐张看产品形状，看完 `swap.py product-review --run RUN --clean` 或 `--hit "秒-秒:看到了什么"` 并补做 |

**第 10b–12 步和看板一条命令跑完**：`nohup "$PY" scripts/finish.py --run RUN > RUN/logs/finish.log 2>&1 &`（retrim → captions → recut → brand → final-screen → compare_board，每步单独日志 `RUN/logs/finish-<步>.log`，出错即停；原片无水印自动跳过 brand；改完某段可从 `--from recut` 等处接着跑）。第一步 retrim 把每段选片里的定格帧裁掉。**出看板之前**，这条成片的闪回帧板和产品帧板都要用 `flashback-review` / `product-review` 记录，产品帧板记了未修的穿帮时 `finish.py` 停在 `NEEDS_AGENT_REVIEW`，修好重出或用户明确同意后才 `finish.py --from board`。

每一步的参数、耗时、实测和用户原话见 [skill-steps-full.md](references/skill-steps-full.md)；文件布局、改词消音与本地修补和恢复方式见 [workflow.md](references/workflow.md)。

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

### 漂浮水印（第 2 步和第 11b 步）

- 目标词来自竞品档案（`watermark_text`，否则品牌名；`watermark scan --target WORD` 可改）。水印按整个图标加文字的范围清除和覆盖。
- **存好的模板优先**（`scripts/watermark_template.py` + `assets/watermarks/<词>-fixed.json/.npz`）：固定位置逐帧做笔画比对，不读字；参考片去水印和终检也用同一模板。没有模板时用 OCR 模糊匹配，再加四边旋转放大补扫，然后定出固定位置、只读这几处补扫整片。
- **只在有水印的帧**清除并盖 LYMPHLOOP，不整条挂字、不整条模糊；每个固定位置整条片只用量出来的那一个框、一个方向。
- **没有模板的位置要 Agent 自己看两样截图**：`RUN/watermark/place_kind/`（是真水印还是产品标签；瓶标只算标签）和 `RUN/watermark/direction/`（竖排水印的读向，默认按向下读，`brand` 的回执 `orientations` 列出每处的方向和来源）。确认结果的写法见 watermark.md 和 rules-now.md“水印候选先分清是不是产品标签”。
- **验收**：`final-screen` 在盖过的成片上重新查，竞品水印词必须 0 帧；有残留就 `watermark add-final` → `brand` → `final-screen`，不用重新 recut。

完整说明见 [watermark.md](references/watermark.md)。

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
- **一条视频的段全部并行**：`generate` 先锁定全部段、检查全部提示词，再一次性提交；消音段和普通段可以在同一批里。
- 切回 APIMART 渠道时参考片的上传方式、隧道和托管进程见 skill-rules-full.md 的“并行”一节。
- 共享的只有这个托管进程和生成渠道（100x 或 APIMART）的账号额度；每条视频的缓存（听写、切点分级、OCR、图片上传记录、生成账本）都在各自 RUN 里。

## 交付

`OUT/master.mp4` + `master.mp4.timeline.json`（源→成片映射）+ `master.mp4.verification.json`（技术核验）+ `RUN/plan/decisions.json`（哪些镜头为何被换）+ `RUN/plan/segments.json`（怎么切的、为什么）+ `RUN/generation/jobs/*.json`（每次生成的提示词、任务号、花费，含 local_splice）。向用户汇报：换了哪些段、新旧时长及时间码、花了多少钱、终检结果、已知瑕疵。

**对比看板**：`finish.py` 最后一步自动生成（单独跑是 `scripts/compare_board.py --run RUN`），输出 `OUT/对比看板/index.html` + `media/`（整个文件夹离线可用）。审片发现的瑕疵写进 `RUN/plan/board_notes.json`（`{G##: {"issue": "…", "note": "…", "fix": true}}`），有 issue 的段在轴上标红；重剪过的片子用 `--previous … --previous-timeline …` 把上一版放进看板对照。

## 文件地图

每个脚本和资源是做什么的见 [file-map.md](references/file-map.md)。
