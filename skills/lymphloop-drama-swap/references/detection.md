# 识别：三层取并集，偏召回

过去漏识别的原因是只信一次全片模型扫描，而"agent 逐帧复核"实际做不到。这里改成三层互相兜底，并且**成片之后再筛一遍**。

## 第零层：竞品档案（`competitor`，2026-11-11 起换任意竞品）
- Flash 把整条工作母版看一遍，写出 `RUN/detect/competitor.json`：品牌名、剂型（`drops / hard_capsule / softgel / tablet / gummy / powder`，其他算超出范围）、容器和剂型单元的长相、出现过的服用方式、难例（局部、背景、镜面、散粒）、点到的成分、漂浮水印文字，以及最多 8 个产品清楚可见的时间点；这些时间点的整帧存在 `RUN/detect/competitor/`，作为本条的识别锚点图（只进识别请求，永不作生成参考）。
- 档案的 `scope_note` 写明这条广告卖的是什么功效。不是淋巴排毒品类时（少见，用户 2026-11-11），识别范围扩大到所有与 yesnap LymphLoop 矛盾的元素：点名的成分（改成 LymphLoop 自己的成分）、成分画面、包装图和片尾上的功效徽章、别的品类的功效说法；改后的机制是否讲得通不用管。
- agent 看每张锚点帧，对照帧板改正或补全 JSON（漏了的难例、错的剂型、几种剂型同时出现），再 `competitor --accept --note`。剂型不在范围内时 `--accept` 会拒绝，先告诉用户。
- 接受后：Flash 逐镜头筛查、生成片 QA、终检用它描述竞品并附它的锚点图；水印扫描的目标词默认取它的 `watermark_text`（否则品牌名）。

## 第一层：Gemini 3.1 Pro 固定契约（`detect-pro`）
- 输入：白底黑字时间码母版（清洗版）、`handheld_photo` + `carton_views`（profile → analysis_default_reference_views）、用户原契约全文（SHA 锁定）+ 运行说明 + 本次帧率/帧数。
- 产出：三栏节点（VO / Dialogue / Visual）与最小改词。这是**台词改写的唯一来源**；画面召回只当候选。
- 解析：`pro_nodes.json`。时间码结束帧按包含处理（+1 成半开区间）。

## 第二层：Gemini 3.8 Flash 逐镜头（`screen`）
- 每个镜头单独一段视频（带音轨）提问，JSON 结构化回答。镜头短、问题具体，比全片一次扫描可靠得多。
- 提示词里带本条竞品档案（`competitor.json`）的长相描述和**难例清单**，并附它的锚点帧；没有档案的旧 RUN 用内置的 Lymphoria 描述和 `assets/competitors/` 锚点图。提示词明确：容器握在拳头里看不到标签也算 yes；掌心的散粒、桌上的一颗软糖、勺里的粉（不连包装）也算竞品。锚点图只进识别请求，永不作生成参考。
- 判定偏召回：`competitor_visible ∈ {yes, unsure}`、服用动作是竞品剂型（dropper、bottle、capsule_or_tablet、softgel、gummy、powder_or_drink_mix；喝白水、咖啡算 other）、非 LymphLoop 品牌文字、语音提到产品（yes/unsure）→ replace。
- 成本低，可以对 `unsure` 镜头换个采样率再跑一次。

## 第三层：Claude 看帧板（`shots` 的 sheets）
- 每个镜头一张帧板，每格有帧号。看的时候专门找：桌面/背景/镜面里的容器、手里一闪而过的滴管或胶囊、掌心或碗里的散粒、成分特写、包装特写、字幕里出现的品牌名。
- 发现 Pro 和 Flash 都没标的 → `overrides.json` 标 replace，写清帧号。
- 认为 Flash 误报（例如水杯、化妆品瓶）→ 只有看过帧板和 `frames` 放大图后才能标 keep，并写理由。

## 合并（`merge`）
镜头 = 决策单位。任一层说换就换；kinds 取并集；Pro 的改写挂到对应镜头。`decisions.json` 是后续 plan 的唯一输入。

## 终检（`final-screen`）
成片重新切镜头 + Flash 筛查。标记非空时先看帧板：
- 真漏换 → 该镜头 override replace，回到 plan/generate 只补这一段，再 recut。
- 生成段里模型自己画出了瓶子/胶囊/文字 → 记录候选审片漏项和时间码，用现有素材局部修补；不重新生成（一次成片）。
- 误报 → 记录理由，交付时说明。

## 边界
- 三层并用仍不是零漏检保证；向用户说"已三层筛查 + 终检"，不说"100%"。
- 一个镜头里产品反复被遮挡不拆成多段；镜头内任一帧露出即整镜头替换。
- 叠化/闪白转场找不到硬切时，把转场整个并入替换段。

- **override 里的 `physical_competitor`**（2026-11-22）：镜头标了 `visual` 是因为成分画面、标题字要改，而画面里没有实体竞品（瓶、滴管、胶囊、包装）时，加 `"physical_competitor": false`。不加的话换品设计的 R0 会当成“人手边有小件竞品”，往这一镜里塞一支手捏条包。
