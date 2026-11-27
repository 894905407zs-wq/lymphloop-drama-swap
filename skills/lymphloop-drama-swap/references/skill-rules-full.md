# 规则、守则、固定事实、并行、交付：完整说明

本文件是 `SKILL.md` 对应章节的完整细则（均已统一到 **Antigravity 原生零 Gemini + 100x `100x-minimax-h3` (`standard` / `768P`) + 五段式 + 人物卡补做** 现行单轨口径，并完整覆盖 macOS 与 Windows / Linux 环境）。

---

## 不可违反的规则

1. **宁多换不漏换**。第 4 步冲突节点、第 5 步逐镜头筛查、看帧板时任一处判“有/拿不准”的镜头均进入替换清单；只有亲眼看过帧板并写入 `RUN/detect/overrides.json` 才能改回 keep。
2. **提示词由 Agent 写，审片由 Agent 定**。脚本自动检查只是辅助，绝不把“接口返回成功”等同于“内容合格”。每段写提示词前必须看高清原帧，生成后必须用 `read_file` 直接看并听 `.mp4` 审片。
3. **物理优先**：每段先按 [controlled-edit.md §4](controlled-edit.md) 判断现实中用条包还是纸盒能自然完成动作，绝不迁就原动作写违背物理的画面（条包不自站立、不弯折拉长、画面里不撕条包、服用一律单手闭唇轻抿）。
3b. **动作设计避开参考片照抄，统一五段式 + 每镜大小参照**：
    - 参考片只去字幕和漂浮水印，**原片竞品原样保留，不抹除、不定格替换**（LaMa 抹瓶易留糊块或误伤人脸/手部）。
    - 提示词一律按 [rules-now.md](rules-now.md) 五段式写：在 `【参考素材职责】` 写“完全移除原片中的<具体竞品>及字幕”和“产品大小不沿用原片被替换容器大小和手部张开幅度”，在 `【事件脚本】` 每镜正向描写我方产品动作与 `大小参照：……`（优先对照人脸、握持的手与近旁实际物件，严禁拿竞品作参照）。
    - **严禁旧写法**：`prepare` 直接拒绝 `将@视频1中…更换为…`、`这不是视频编辑`、`动作照@视频1 / 照它`、厘米数、勺子词、画面否定句、参数/时间戳进正文。
    - 手机/电脑屏幕上的竞品购物页按规则 S1 传 `phone_page` / `desktop_page`；小而远、背景旧瓶、多镜反打等高风险动作按 [controlled-edit.md §4.8](controlled-edit.md) 处理。
3c. **禁止贴图补救生成段**：严禁贴色块/贴产品图/局部抠图跟踪/调色染绿/LaMa 整框抹除生成画面里的产品或道具穿帮；严禁放大裁切生成画面或用原片放大重取景顶替生成镜头；严禁自写 ffmpeg 拼生成段。`region_inpaint.py` 在生成段帧范围拒绝无门控整框填充，仅允许 `:purple`（残留字幕）与 `:ink`（道具旧印刷字）。允许的免费本地手段仅限整帧级处理（裁掉补尾与 ≤0.5 秒坏闪帧、借同素材兄弟片整镜、`select` 自动去残留字幕、补满 3 轮后无嘴镜用同段干净帧放慢填补）。
3d. **不许删原片内容、不许跳过要换的段**：成片里原片每个镜头与每句台词都必须在，要么是原片，要么是换好的生成段（`recut` 拒绝比原段短超 0.5 秒的选片，除非用户明确同意 `--user-approved-cut` 或通过完整内容逐镜核验 `--content-preserved-review`）。原片人物（含卡通/特效造型）照原片生成；参考片不得把人物裁出画（`ref_framing.py` 自动检查拦截）。
4. **首轮原片参考每段 1 次 + 穿帮必走人物卡定点补做**：
    - 首轮（`a1`）每段挂去字幕参考视频生成 1 次。未产生付费结果的失败（失败退款、校验被拒未扣费）由脚本自动重提最多 3 次并记入 `unpaid_attempts`；网络超时/状态不明绝不盲目重提，先查 100x 任务状态。
    - 审片（6 项 `--audit`、`carton_check.py` 像素量尺、终检闪回帧板与产品帧板）查出八类穿帮时，统一按第 10c 步**一轮一批**走**人物卡模式**定点补做（不挂参考视频，只挂人物卡 + 产品图 + 说话人音色，同一处最多补做 3 轮；第 2 轮范围必须更短，第 3 轮不长于最窄轮）。
    - 同一处补满 3 轮仍未过时，不卡死流程，照常 `finish` 并写入交付说明交用户一次性裁定（破例第 4 轮 `--again --user-quote "..."` 或无嘴镜本地修复）。
5. **完整语义分段**：段边界不切断一句话、不拆开产品镜头与对应台词、不跨段落锁边；参考段严格 `< 15` 秒（上限 14.9 秒，非目标）。
6. **去字幕参考 + 原样复刻字幕**：送生成的参考片和生成结果不带字幕、标题卡、水印或竞品字；选片后由 `captions` 原样复制原片字幕（仅改词句按量出样式仿制重画），未重做的原片镜头保留原字幕。
7. **台词与音节对齐**：全段听得到的台词逐句以 `{原词}` 写出，未冲突词一字不改；画内可见嘴部改词优先**音节数对齐**（如 4 音节 `Lymphoria` 换 `Yesnap LymphLoop`）；正文绝不出现被换掉的旧词或音标；**永远不用 TTS**；冲突词提交前 `--mute` 消音（旁白/句中 ≥0.8s 空档消整句，品牌名消 `"It's called ___"` 分句，句尾单词可单消）。
8. **费用透明**：默认走 100x `100x-minimax-h3`（`standard` / `768P` / `9:16`，约 3.89 积分/秒）；生成前向用户报段数与总秒数，交付按账本如实汇报总积分与人物卡费用。
9. **如实汇报局限**：自动检查是兜底不是绝对保证，768P 生成段与高码率原片画质偶有细微落差，达上限未解的瑕疵如实列新时间码交付。

---

## Agent 执行守则

1. **亲自看图听声**：帧板、总览图、上传参考片、生成片 `.mp4`（`read_file` 看动图+听真声）、`carton_check` 对照图、闪回帧板与产品帧板一律亲自看听；只看 JSON 即选片/判 pass 属违规。
2. **长步骤放后台**：`watermark scan`、`generate`、`recut`、`final-screen`、`finish.py` 放后台运行并留日志，通过 `swap.py status` 和 `RUN/generation/jobs/*.json` 查进度。
3. **只动自己的 RUN**：每条视频独立建 RUN/OUT 目录；`swap.py` 自带跨平台文件锁（POSIX `flock` / Win32 `LockFileEx`），遇占用提示时等待或用 `--lock-wait`，绝不删锁。
4. **不误杀其他进程**：禁止无差别 `pkill -f python`；提交统一走 `swap.py generate` 与 `shot_regen.py round submit`，不自写绕过账本的提交脚本。
5. **按实际时长顺剪，说话镜头只用自己的同期声**：画面看得到人张嘴说话的地方，声音必须是同一文件、同一时刻自带音轨，不论台词改没改；`--sound source` / `splice --audio-window` 仅允许用在无嘴画面并标 `--no-mouth`。口型错位直接补做该镜（`lip_sync`），不做本地贴音或口型对齐。
6. **次数记账**：首次生成 `a1`、补做 `shots-regen/rounds/`、本地拼接 `local_splice`、未付费重试 `unpaid_attempts`，全量落盘可追溯。
7. **凭据安全**：密钥/登录态仅从环境变量、系统凭据库或 100x CLI 读取，绝不打印或写进日志/提示词。
8. **完整汇报**：交付写明成片路径、分辨率/帧率/时长、生成渠道与模型、段数/首轮通过率/补做轮次、总消耗积分/费用、终检结论、已知瑕疵（新时间码）与对比看板访问地址。
9. **批次不中途停摆**：单段/单条遇异常或补满 3 轮时，记录复现信息或瑕疵明细并继续推进其余段/片子，整批完成后一并汇总交用户裁定。

---

## 固定事实与跨平台环境

- **产品**：yesnap LymphLoop，Orange Mango 口味，30 支/盒，每支 8 g 液体条包。青绿色翻盖纸盒（10×7×13 cm 级，宽正面印 `LYMPHATIC`）+ 青绿色柔性条包（约一根食指长、略宽于拇指指腹）。动作与 7 种合法成分见 [product-actions.md](product-actions.md)。
- **送模型的参考图**：`handheld_photo`（未开条包）、`opened_packet_hand`（已开条包）、纸盒双图 `carton_views,carton_hand`（必一起传，见 [carton-swap.md](carton-swap.md)）、屏幕商品页 `phone_page` / `desktop_page`（规则 S1）。`assets/physical/` 实拍视频仅给 Agent 看，不上传。
- **生成路线**：默认 **100x `100x-minimax-h3`（`standard` / `768P` / `9:16`）**，首次参考视频生成与人物卡补做一致，时长取目标段向上取整整数秒（`4–15`），全走请求参数不进正文，参考片与音色音频均直传 100x（不走任何本地隧道）。临时切换：`LYMPHLOOP_X100_MODEL=100x-seedance-2.0`（`720P`）或 `LYMPHLOOP_GEN_CHANNEL=apimart`（APIMART `seedance-2.0` `720p`）；禁用 `100x-seedance-2.5`。选片时 `select` 自动检查 H3 末词是否超出原段时长并放宽 `clip_out` 防止截词。
- **跨平台运行环境（macOS / Windows / Linux）**：
  - 视觉引擎按平台自动选择（`scripts/vision_scan.py`）：macOS 用系统 Apple Vision（Swift 扫描器），Windows / Linux 自动用 RapidOCR + OpenCV YuNet（模型首次自动下载到 `~/.cache/lymphloop-models/`；macOS 也可设 `LYMPHLOOP_OCR_ENGINE=rapidocr` 切换），输出同格式 JSON。
  - 系统差异集中由 `scripts/oscompat.py` 处理（文件锁 `flock` / Win32 `LockFileEx`、进程管理、系统字体扫描、Windows 自动 UTF-8 模式）。
  - **首次安装（在 `$SKILL` 目录下运行）**：
    - macOS / Linux：`python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`；`ffmpeg` 需带 `drawtext`（`brew install ffmpeg-full` 并加进 PATH）。
    - Windows（PowerShell）：`py -3.12 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt`；`ffmpeg` 用 `winget install Gyan.FFmpeg`，Node 用 `winget install OpenJS.NodeJS.LTS`。
    - 自检：`$PY scripts/oscompat.py`（核查系统、编码、字体）与 `$PY scripts/watermark_cleanup.py preflight`（核查识别引擎）。
  - 100x 客户端 `scripts/x100.py` 已内置在仓库中，调用本机 100x 插件桥（默认 `~/.codex/plugin-sources/100x-mcp/plugins/100x`，可用 `X100_PLUGIN` 指定），需 Node 22+ 与已登录的 100x 账号。

---

## 并行与交付

- **并行提速**：一条视频独立一个 RUN；单条内第 2 / 4 / 5 步可同时后台跑，`plan` 后立即后台 `generate --refs-only`（LaMa 带跨 RUN 缓存），`generate` / `round submit` / `review` / `captions` 均自动多线程并行；同素材批次用 `swap.py reuse` 零费用复用代表片结果。
- **交付清单**：`OUT/master.mp4` + `master.mp4.timeline.json` + `master.mp4.verification.json` + `OUT/对比看板/index.html`（内置 Range HTTP 服务 `compare_board.py --run RUN --serve --port 8765`）+ 完整交付汇报（换段明细、新旧时间码、总积分/费用、终检结论、已知瑕疵）。
