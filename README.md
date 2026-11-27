# LymphLoop Drama Swap（Antigravity 插件）

把竞品保健品（滴剂、胶囊、片剂、软糖、粉剂）的竖屏短剧广告换成 yesnap LymphLoop 成片的 Antigravity 技能。看片、听音、识别和审片由 Antigravity Agent 自己完成，视频生成走 100x MiniMax H3。

完整流程见 [SKILL.md](skills/lymphloop-drama-swap/SKILL.md)。

## 一键安装

需要先装好 Antigravity 和 Git，目前只支持 macOS（文字识别用的是系统自带的 Vision）。在终端里运行下面这一条命令，插件就会出现在 Antigravity 里，不用重启。

```bash
git clone https://github.com/894905407zs-wq/lymphloop-drama-swap.git ~/.gemini/config/plugins/lymphloop-drama-swap
```

装好后在 Antigravity 的 Customizations 页面能看到 **LymphLoop Drama Swap**，在对话里直接说“用 lymphloop-drama-swap 处理这条原片”即可调用。

## 更新与卸载

更新到最新版：

```bash
git -C ~/.gemini/config/plugins/lymphloop-drama-swap pull
```

卸载：在 Antigravity 的 Customizations 页面里卸载，或直接删除 `~/.gemini/config/plugins/lymphloop-drama-swap` 文件夹。

## 首次使用前要准备的

安装命令只放好了技能文件。真正跑片子之前，每台电脑还要准备下面几样，都不随仓库分发：

| 需要什么 | 用途 | 怎么准备 |
|---|---|---|
| Python 3.12 环境 | 运行脚本 | 在 `skills/lymphloop-drama-swap` 目录下建 `.venv` 并安装 `requirements.txt`，命令见下 |
| ffmpeg 和 ffprobe | 剪辑、抽帧 | 装到 PATH 即可，例如 `brew install ffmpeg` |
| Node.js 22 或更新 | 100x 客户端 | 自行安装 |
| 100x MCP 插件和账号 | 视频生成（付费） | 按 [kezd088/100x-mcp](https://github.com/kezd088/100x-mcp) 的说明安装并登录。默认位置是 `~/.codex/plugin-sources/100x-mcp`，装在别处时把环境变量 `X100_PLUGIN` 指到其中的 `plugins/100x` 目录 |

建环境的命令（在 `skills/lymphloop-drama-swap` 目录下运行）：

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

去字幕用的 LaMa 模型（约 208 MB）和 Whisper 模型会在第一次用到时自动下载。

也可以直接让 Antigravity 来做：安装后对它说“按 SKILL.md 的首次安装说明初始化 lymphloop-drama-swap”。

只有切回 APIMART 渠道时才另外需要 `cloudflared` 和 APIMART 密钥，默认的 100x 渠道用不到。

## 注意

- 密钥、运行环境、模型缓存和任何任务历史都不在仓库里，每个使用者用自己的账号和余额。
- 视频生成会产生费用，由使用者自己的 100x 账号承担。
- 仓库里的 yesnap LymphLoop 产品图、实拍演示视频和竞品识别参考图仅供本技能识别与生成参考，不授予任何商标、图片、人物肖像或原视频的权利。
