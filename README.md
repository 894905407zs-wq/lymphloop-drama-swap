# LymphLoop Drama Swap（Antigravity 插件）

把竞品保健品（滴剂、胶囊、片剂、软糖、粉剂）的竖屏短剧广告换成 yesnap LymphLoop 成片的 Antigravity 技能。看片、听音、识别和审片由 Antigravity Agent 自己完成，视频生成走 100x MiniMax H3。

完整流程见 [SKILL.md](skills/lymphloop-drama-swap/SKILL.md)。

## 一键安装

支持 macOS 和 Windows。需要先装好 Antigravity 和 Git，然后在终端里运行下面这一条命令，插件就会出现在 Antigravity 里，不用重启。

macOS / Linux：

```bash
git clone https://github.com/894905407zs-wq/lymphloop-drama-swap.git ~/.gemini/config/plugins/lymphloop-drama-swap
```

Windows（PowerShell）：

```powershell
git clone https://github.com/894905407zs-wq/lymphloop-drama-swap.git "$env:USERPROFILE\.gemini\config\plugins\lymphloop-drama-swap"
```

装好后在 Antigravity 的 Customizations 页面能看到 **LymphLoop Drama Swap**，在对话里直接说“用 lymphloop-drama-swap 处理这条原片”即可调用。

## 更新与卸载

更新到最新版：

```bash
git -C ~/.gemini/config/plugins/lymphloop-drama-swap pull
```

Windows 上把路径换成 `"$env:USERPROFILE\.gemini\config\plugins\lymphloop-drama-swap"`。

卸载：在 Antigravity 的 Customizations 页面里卸载，或直接删除上面这个文件夹。

## 首次使用前要准备的

安装命令只放好了技能文件。真正跑片子之前，每台电脑还要准备下面几样，都不随仓库分发：

| 需要什么 | 用途 | macOS | Windows |
|---|---|---|---|
| Python 3.12 环境 | 运行脚本 | 见下面的命令 | 见下面的命令 |
| ffmpeg 和 ffprobe | 剪辑、抽帧 | `brew install ffmpeg` | `winget install Gyan.FFmpeg` |
| Node.js 22 或更新 | 100x 客户端 | 自行安装 | `winget install OpenJS.NodeJS.LTS` |
| 100x MCP 插件和账号 | 视频生成（付费） | 按 [kezd088/100x-mcp](https://github.com/kezd088/100x-mcp) 的说明安装并登录 | 同左 |

100x 插件默认找 `~/.codex/plugin-sources/100x-mcp`，装在别处时把环境变量 `X100_PLUGIN` 指到其中的 `plugins/100x` 目录。

建 Python 环境的命令，在插件里的 `skills/lymphloop-drama-swap` 目录下运行。

macOS / Linux：

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Windows（PowerShell）：

```powershell
py -3.12 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt
```

去字幕用的 LaMa 模型（约 208 MB）和 Whisper 模型会在第一次用到时自动下载。

也可以直接让 Antigravity 来做：安装后对它说“按 SKILL.md 的首次安装说明初始化 lymphloop-drama-swap”。

只有切回 APIMART 渠道时才另外需要 `cloudflared` 和 APIMART 密钥，默认的 100x 渠道用不到。

## macOS 和 Windows 的差别

流程和命令两边一样，差别只在底层：

| | macOS | Windows |
|---|---|---|
| 文字识别（字幕、水印） | 系统自带的 Vision | RapidOCR，随 `requirements.txt` 安装 |
| 人脸检测（检查参考片有没有把人裁掉） | 系统自带的 Vision | OpenCV YuNet，模型 232 KB，第一次用时自动下载 |
| Python 解释器 | `.venv/bin/python` | `.venv\Scripts\python.exe` |
| 密钥存放 | 钥匙串或环境变量 | 环境变量，或装了 `keyring` 时用凭据管理器 |

两种识别引擎输出同样格式的结果。RapidOCR 的文字框大小已按 Vision 校准，但两者认字的结果不会逐字相同，极淡的水印在 Windows 上可能需要多看一眼扫描结果。

仓库的 [Actions](https://github.com/894905407zs-wq/lymphloop-drama-swap/actions) 页面会在 macOS 和 Windows 上各跑一遍技能自带的测试。

## 注意

- 密钥、运行环境、模型缓存和任何任务历史都不在仓库里，每个使用者用自己的账号和余额。
- 视频生成会产生费用，由使用者自己的 100x 账号承担。
- 仓库里的 yesnap LymphLoop 产品图、实拍演示视频和竞品识别参考图仅供本技能识别与生成参考，不授予任何商标、图片、人物肖像或原视频的权利。
- 第三方组件：RapidOCR（Apache-2.0，含 PP-OCR 模型）、OpenCV Zoo YuNet 人脸模型（MIT）、LaMa 修补模型，均由各自的安装包或首次使用时下载，不随仓库分发。
