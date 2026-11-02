# 第三方内容与授权

本仓库的代码用 MIT 协议，下面这些内容不属于 MIT，按各自的授权使用。

## Live2D 模型（seed/data/avatars，每个模型文件夹里都有 许可与署名.txt）

| 模型 | 作者 | 授权 |
|---|---|---|
| PinkSakura | Ariakage（live2d-agent-kit） | CC BY 4.0，贴图从 8192 缩到 4096 |
| FILI | MjGjVtube | CC0 |
| LittleCat | Ezri | 需署名，CC BY-ND，不得改作 |

## Live2D Cubism Core

`live2dcubismcore.min.js` 是 Live2D 公司的闭源组件，**不包含在本仓库和 exe 里**，第一次运行时从 Live2D 官方地址下载，
使用时受 [Live2D Proprietary Software 使用授权协议](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_cn.html) 约束。
如果把本软件用于商业用途，还要自行确认 [Live2D 的出版许可](https://www.live2d.com/sdk/license/) 要求。

## 场景照片

`src/scene_chat_*.webp` 来自 Unsplash（[Unsplash License](https://unsplash.com/license)）。

## 前端库

- PixiJS（MIT）
- pixi-live2d-display（MIT）

## 运行时用到的 Python 库

numpy、openai、httpx、edge-tts、sounddevice、imageio-ffmpeg（内含 FFmpeg，LGPL/GPL）、pywebview、PyYAML、cryptography、pypinyin、Pillow，各自遵循其开源协议。
