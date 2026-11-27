# 只替换已完成成片中的一至两个镜头

入口：`skill-run lymphloop-swap-claude finished_patch.py prepare --recipe RECIPE.json --directory PATCH_DIR`，确认冻结清单后再 `finished_patch.py render --directory PATCH_DIR --output master.carton-local.mp4`。

`prepare` 只校验本地数据并调用现有 `media_pipeline.freeze`；`render` 只调用现有 `media_pipeline.render`，不运行生成、字幕、水印、brand、整条 select/recut/finish 或全片 AI 终检。不得覆盖已有成片。原生成账本、全片旧 owner 和旧 brand/final-screen 状态保持历史事实。

配方格式（所有 ref 均为绝对 `path` 加实际 `sha256`）：

```json
{
  "schema_version": 1,
  "type": "lymphloop-finished-master-local-patch",
  "source": {"path": ".../versions/before_carton_scale_20261122/master.mp4", "sha256": "..."},
  "source_timeline": {"path": ".../versions/before_carton_scale_20261122/master.mp4.timeline.json", "sha256": "..."},
  "source_owner": {"path": ".../原已审owner.json", "sha256": "..."},
  "replacements": [{
    "id": "S180", "segment": "G01", "old_frames": [10000, 10040],
    "raw_media": {"path": ".../新单镜raw.mp4", "sha256": "..."},
    "media": {"path": ".../仅该单镜已烧字幕.mp4", "sha256": "..."},
    "generation_ledger": {"path": ".../generation/jobs/单镜.json", "sha256": "..."},
    "candidate": "c1", "clip_seconds": ["0", "97/24"],
    "review": {"path": ".../该单镜审核.json", "sha256": "..."}
  }]
}
```

示例帧号只是 schema 占位，不能套到实际片子。`old_frames` 是旧**成片**原生帧率下的半开窗，必须按实际镜头锁边。新 `clip_seconds` 支持精确有理数，画面与自带声音始终同文件同时间窗。

单镜审核 JSON 必须包含 `reviewer:"Claude"`、`reviewed:true`、与配方逐字段相同的 `raw_media`、`media`、`clip_seconds`，`own_same_time_audio:true`，非空 `evidence:[{path,sha256}]`（真实亲看资料），以及 `caption_audio_packet_sha256:{before:实际音频packet哈希,after:相同哈希}`。字幕仅使用现有母版字幕烧录，保声音 packet；此审核不冒称全片新审。

`receipt.json`（prepared）绑定完整源 SHA、旧 owner、旧时间线、原生 frozen seal、每个 old/new 半开帧窗、输出帧数与48k音频时钟、原速 unchanged interval 映射和接点。`receipt.rendered.json` 另绑定实际新 MP4/原生局部时间线/技术验证 SHA，状态为等待局部 owner。所有未改内容只取同一个旧 finished source、保持顺序及速度1；尾段仅整体平移。

原生导出会将各 atom 用 libx264 fast CRF18 重编码并串接，音频解码到48k PCM再AAC192k；不承诺尾段字节/packet不变。旧字幕、品牌标字已在画面内，直接继承，不重新绘制或重跑检测。完整解码/帧数/音频时钟是技术验证，不能称为新的全片语义审片。

最终局部 owner 只审新镜、接点和实际需要的短音窗；未改区域明确引用旧 finished SHA 的完整旧审片。确认后可在 `state.stages.local_final_patch` 登记 rendered receipt 与局部 owner、在 `state.stages.current_delivery` 指向新文件；不改旧 `recut/brand/final_screen/flashback_review` 来假装新全审。

局部时间线坐标是 `旧finished → 新finished`，不是 `原始source → 新finished`。对比看板及原片到新版完整索引必须合成旧 source-to-finished 映射和本次 patch 映射。重生成场景内部的线性位置只作导航近似；精确局部 old/new 帧窗单独公布。播放器两侧均保持用户选择的原速，不能按映射斜率加速有声侧。
