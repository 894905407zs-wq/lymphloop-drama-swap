# 竞品与冲突识别（Antigravity 原生零 Gemini 模式）

本插件默认运行在 **Antigravity 原生看听模式**（`agent_native.is_native() == True`），完全不调用外部 Gemini API。识别由“本地脚本提取证据 + 当前 Agent 直接用 `read_file` 看图/看片/听音并导入结论”组成。

---

## 1. 四步识别分工

1. **全片竞品档案（`swap.py competitor`）**
   - 脚本自动产出：24 格全片总览图 `RUN/agent_review/overview_grid.jpg`、本地 `faster-whisper` 全片听写 `RUN/agent_review/transcript.json`、3 段 6 秒说话人音色采样 `RUN/agent_review/voice_samples/*.mp4`，以及 `RUN/detect/competitor.json` 骨架。
   - Agent 直接用 `read_file` 看总览图、听 3 段采样视频（记录说话人年龄/性别/口音/音色/是否有 BGM）、读听写稿，补全 `competitor.json`（重点：`brand_names`、`form`、`container_look`、`dose_unit_look`、`hard_cases`、`watermark_text`、`ingredients_named`），执行 `swap.py competitor --accept --note "..."`。
   - 超出 6 类常见剂型范围（drops / hard_capsule / softgel / tablet / gummy / powder）时，先征得用户同意再加 `--out-of-scope-ok`。

2. **全局冲突节点识别（`swap.py detect-pro`）**
   - 脚本自动将切好的镜头打包成紧凑网格图 `RUN/agent_review/shot_grids/shot_grid_##.jpg` 并确保 `transcript.json` 就绪。
   - Agent 结合听写稿和镜头网格，标出所有需要替换的冲突节点（`visual` 画面竞品、`dialogue` 画内对白冲突、`vo` 画外旁白冲突，以及具体改词建议），写入 `RUN/detect/pro_nodes.json` 后运行：
     ```bash
     "$PY" scripts/swap.py detect-pro --run RUN --import-nodes RUN/detect/pro_nodes.json
     ```
   - 改词口径：只改冲突的品牌名、剂型词、用法词与我方没有的竞品成分；**画内可见嘴部的改词优先音节数对齐**（见 [rules-now.md](rules-now.md)）。我方包装正面只有 7 种合法成分（Dandelion、Cleavers、Burdock Root、Red Clover、Calendula、Echinacea、Bromelain，含对应西语名）。

3. **逐镜头视觉筛查（`swap.py screen`）**
   - Agent 逐张看 `shot_grids/shot_grid_##.jpg`（小物件、弱露瓶、背景/桌面/包口疑似镜头直接用 `read_file` 打开对应 `RUN/clips/S###.mp4` 放大确认），重点对照 `competitor.json → hard_cases` 查：
     - 桌面/台面/抽屉/杯架/车内/画边/镜面/虚焦里的竞品容器；
     - 手里或掌心散落的剂型单元（滴管、胶囊、软糖、药片、粉末/量勺）；
     - 手机或电脑屏幕上的竞品购物页/落地页；
     - 只出现几帧的极短露瓶镜头。
   - 写出精简判定 JSON `{"replace_ids": [...], "unsure_ids": [...], "notes": {...}}`，执行：
     ```bash
     "$PY" scripts/swap.py screen --run RUN --import-verdicts verdicts.json
     ```
   - 脚本会自动展开生成 `RUN/screen/S###.json` 与 `summary.json`。

4. **合并与人工复核覆盖（`swap.py merge` + `overrides.json`）**
   - `merge` 将 `pro_nodes.json` 与 `screen` 合并成 `RUN/plan/decisions.json`。
   - 需修正任何误报、漏报，或标记“画面要改但没有实体竞品（如成分动画/标题卡）”时，编辑 `RUN/detect/overrides.json`：
     - 漏报补标：`{"S012": {"decision": "replace", "kinds": ["visual"], "note": "桌角露琥珀瓶"}}`
     - 误报放行：`{"S020": {"decision": "keep", "note": "实际为水杯，无竞品"}}`
     - 非实体竞品视觉镜头（防规则表误塞产品）：`{"S045": {"decision": "replace", "kinds": ["visual", "vo"], "physical_competitor": false, "note": "成分动画换我方植物"}}`
   - `overrides.json` 是唯一能把 `replace` 改回 `keep` 的合法途径。修改后重跑 `merge → plan`。

---

## 2. 切点分级与终检门禁

- **切点分级（`scenes.py`）**：原生模式下基于本地画面直方图差分与静音谷检测（`quiet_cuts`）划分段落锁边与镜头锁边，可在 `RUN/plan/lock_overrides.json` 中手动微调（`{"S019": "shot", "S020": "paragraph", "S007": "none"}`）。
- **终检多道硬门禁（`swap.py final-screen`）**：
  - **水印残留检查**：按固定位置模板/OCR 复扫，要求 `watermark_left == []`；
  - **口播听写门禁（`speech_gate.py`）**：本地 `faster-whisper` 复听改动区间，要求 `speech_left == []`（无竞品品牌名、无我方没有的成分词）；
  - **闪回关（`cutscan.py` + `flashback-review`）**：对所有接缝、拼接点与 ≤0.5 秒碎镜头出帧板，Agent 逐张 `read_file` 看完后执行 `swap.py flashback-review --run RUN --clean`（有漏网则 `--hit A-B`）；
  - **产品帧板关（`product-review`）**：对含纸盒的生成段按原尺寸每秒 3 帧裁出产品帧板，Agent 逐张 `read_file` 看完后执行 `swap.py product-review --run RUN --clean`（有穿帮则 `--hit "秒-秒:说明"`）。

> **外部 Gemini 兼容开关（默认关闭）**：仅当环境变量显式设 `LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` 时，脚本才会调用 `google_api.py` / `flash_screen.py`；默认 Antigravity 原生链路不需要也不使用任何外部 Gemini 密钥。
