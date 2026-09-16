# 懒得笔记｜本地视频自动转写入库工具（Windows 11 版）

> 把本地视频文件夹变成 Obsidian 笔记：丢进视频，自动转写成文字并存为 Markdown。

简体中文 | [English](./README.en.md)

## 这是什么

懒得笔记（Video2Obsidian）是一个在本机运行的小工具。它监听你指定的视频文件夹，把新增视频转写成文字，经过错词修正和分段整理后，保存为 Markdown 笔记。

笔记可以写入你的 Obsidian 库（按原视频的目录结构存放），也可以只生成在数据目录里，不写入任何现有库。整个过程**完全离线**：不调用云 API、不产生 API 费用、不联网下载模型。

## 核心功能

- **监听文件夹，自动处理**：填好视频文件夹并点开始监听，把视频丢进去即可，后台排队逐个处理，不用守在页面前。
- **本机 GPU 转写**：用本机 Python 环境里的 `faster-whisper`（CTranslate2 后端，NVIDIA CUDA）把声音转成文字，中间用随包 `ffmpeg` 提取音频。
- **错词修正**：在控制台词汇区维护“错词→正词”（比如人名、术语），新转写自动应用，也能一键重跑已有结果。
- **成稿与入库**：转写结果会整理成分段正文并生成 Markdown；填了笔记库目录就按目录镜像写入，不填就只保留在数据目录。
- **任务可见可重试**：任务列表显示发现→听写→整理→成稿→入库的进度，失败的任务可以单独重试，也能预览正文、一键在资源管理器或 Obsidian 里打开。
- **红线保护**：已存在的笔记一个字节都不覆盖（No-Clobber）；GPU 不可用时明确报错、绝不静默降级 CPU；断网才能用，绝不偷偷联网下载。

## 适合谁

- 用 Obsidian 记笔记，同时有课程录像、会议录像、采访素材需要转文字的人。
- 希望数据留在本机、不想把视频上传到云服务的人。
- 使用 Windows 11 + NVIDIA 显卡，并能按下面步骤准备环境的人。

## 快速开始

双击仓库根目录的 **`start.bat`**（或在 PowerShell 里执行 `.\start.ps1`）。启动脚本会自动完成：

1. 用官方 Python 3.12 创建 `.venv`（没有就自动建）；
2. 按 `requirements.txt` 锁版本安装依赖；
3. 检查端口占用（只提示、绝不杀别人的进程）；
4. 启动本机控制台并打开：

```text
http://127.0.0.1:8899/
```

页面里按顺序做三件事：

1. 填**视频文件夹**（必填，本机绝对路径）。
2. 填**笔记库目录**（选填；不填也能转，只是不写入 Obsidian）。
3. 点**开始监听**，然后把视频文件放进视频文件夹。

完成后在任务列表点对应行查看正文。

## 安装

### 前置要求

- Windows 11（64 位）。
- 官方 **Python 3.12**（命令行可用 `py -3.12`；`start.ps1` 会用它建 `.venv`）。
- **NVIDIA 显卡 + CUDA 运行库**：驱动装好、`nvidia-smi` 能看到卡。GPU 不可用时程序会明确报错停下，**不会自动降级 CPU**；确需 CPU 兜底，在页面显式勾选「CPU int8 兜底」（明显变慢）。
- **ffmpeg 随包**：交付包自带 `third_party\ffmpeg\bin\ffmpeg.exe`。若没有，也可以用环境变量 `V2O_FFMPEG` 指定绝对路径，或让 PATH 里的 `ffmpeg` 兜底（日志会如实标注用的是哪一个）。
- Obsidian 库目录是选填的；不写也能跑通全流程。

### 模型冻结（首次使用前，在能联网的机器上做一次）

本程序**离线才能用**，模型不联网下载，需要先在联网机上把 CT2 模型备好并登记：

1. 在联网机下载 `Systran/faster-whisper-large-v3-turbo` 的 CTranslate2 checkpoint（人工下载，本工具不代下），记下它的来源 URL 与 revision（commit）。
2. 把模型目录拷到本机（如 `D:\models\faster-whisper-large-v3-turbo`）。
3. 在本机 venv 里生成清单（SHA-256 校验一并写入）：

```powershell
.\.venv\Scripts\python.exe tools\freeze_model_manifest.py ^
    --model-dir D:\models\faster-whisper-large-v3-turbo ^
    --revision <checkpoint 的 commit> ^
    --source https://huggingface.co/Systran/faster-whisper-large-v3-turbo ^
    --license MIT --license-file LICENSE
```

清单落在 `models\MODEL_MANIFEST.json`。启动监听前程序会逐文件校验 SHA-256，对不上就明确报错，绝不带病转写。

### 启动方式

`start.ps1` 的行为（`start.bat` 只是它的薄包装）：

- venv：`.venv\Scripts\python.exe`（缺了自动用 `py -3.12` 建）。
- 依赖：按 `requirements.txt` 锁版本安装。
- 缺 `faster_whisper` / `ctranslate2`：控制台照常打开，但点开始转写会报 `PRECHECK_ASR_BACKEND_MISSING`，补好依赖重起即可。
- 端口默认 `127.0.0.1:8899`，只监听本机；换端口：`$env:V2O_PORT=8900; .\start.ps1`。端口被占用时退出并提示，**不杀占用进程、不改绑 0.0.0.0**。
- 默认数据目录：`%LOCALAPPDATA%\Video2Obsidian\data`。

## 使用方法

### 最小路径

```powershell
.\start.ps1
# 打开 http://127.0.0.1:8899/
# 填视频文件夹 → 点开始监听 → 丢视频进去 → 任务列表看结果
```

支持的输入格式（按监听端实际接收的后缀）：`.mp4` `.mov` `.mkv` `.m4v` `.avi` `.webm`。

### 词汇修正

在**词汇**区加一行“错词→正词”即可对新转写生效。存量笔记想统一改，用重跑功能重新应用一次。

## 配置

| 页面上的项 | 必填 | 说明 |
| --- | --- | --- |
| 视频文件夹 | 是 | 要监听的本机绝对路径，粘贴带引号的路径会自动去引号。 |
| 笔记库目录 | 否 | 为空时只生成到数据目录，不写入 Obsidian。 |
| 数据目录 | 否 | 高级选项；默认 `%LOCALAPPDATA%\Video2Obsidian\data`。 |

## 交付与验收文档

- [Windows 交接与真机验收清单](./docs/WINDOWS-HANDOFF.md)：给 Windows 端接续者/用户的真机待验清单、验收命令与红线。

## 已知限制

- 目标设备是 Windows 11 + NVIDIA CUDA；其他平台（含 macOS/Linux）本仓库未验证。
- 缺 `faster_whisper` / `ctranslate2`、缺 CUDA 运行库或模型清单未冻结时，转写不可用，控制台会明确报错；**绝不静默降级 CPU、绝不静默联网下载**。
- 服务重启后监听状态不会自动恢复，需要在页面手动重新开始监听。重启后已完成任务自动跳过、半截任务自动重转，不会重复出稿。
- **往监听目录拷入视频后，需等文件写稳（约 7 秒无变化写入）才会被识别入队**；拷贝/下载过程中若长时间停顿（超过约 7 秒）再继续写，会先按当时内容判一次「源在变」，该条会拦下不发布，等写完后自动按最新内容重新排队；极端情况下可能先出一篇**不完整稿**，而完整稿会因 No-Clobber 保护（已存在笔记不覆盖）被挡下。遇到这种不完整稿，请**删除该 md 后重新放入视频**即可正常出稿。
- Windows 侧的 真 GPU 转写、Defender/UAC、`obsidian://` 协议、长路径注册表策略等**尚未在真机验证**，逐条清单见 [WINDOWS-HANDOFF](./docs/WINDOWS-HANDOFF.md)。
- 本仓库没有 License 文件，默认按“保留所有权利”理解；公开使用或分发前请先补 License。

## License

当前仓库未提供 License 文件。如需开源分发，请先添加并在此处链接。
