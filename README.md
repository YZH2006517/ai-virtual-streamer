# AI虚拟主播

Windows 上的 AI 数字人唠嗑直播助手：一个 Live2D 主播看着抖音弹幕陪观众聊天，还能跳舞、玩弹幕小游戏。

**不需要显卡**，下载一个 exe 就能用。模型在云端，用你自己的大模型 API Key。

## 能做什么

- **人设**：内置多个主播人设，性格、声音、场景一键换，也可以自己写
- **读弹幕回答**：读抖音直播间网页的留言，排队回答，留言多时合并回答，支持屏蔽词
- **舞台跳舞**：跟着背景音乐的节拍跳
- **弹幕小游戏**：猜数字、答题挑战（大模型现场出题，可选类别和难度），有积分榜和排行榜大屏
- **礼物心愿**：礼物攒够了自动跳舞或开一局游戏
- **画面可调**：每个场景的组件、位置、图层都能单独调
- **账本**：今天花了多少 token、礼物进了多少

## 怎么用

1. 到 [Releases](../../releases/latest) 下载最新的 `.exe`，双击运行
   - Windows 可能提示「已保护你的电脑」：这是因为 exe 没有代码签名，点「更多信息 → 仍要运行」
   - 第一次启动要解压，等十几秒
2. 按提示填大模型 API Key（推荐 [DeepSeek](https://platform.deepseek.com/)，也支持其他 OpenAI 兼容接口）
3. 在「活动」「人设」里选好，点「控制 → 抖音弹幕」填直播间地址
4. 在「输出」页复制直播画面地址，用 OBS 或直播伴侣采集

## 运行要求

- Windows 10 / 11（64 位），需要 [WebView2](https://developer.microsoft.com/microsoft-edge/webview2/)（Win11 自带）
- 需要联网：大模型、语音（微软 Edge 在线语音）都在云端
- 你的数据都在本机 `%LOCALAPPDATA%\AI虚拟主播`，API Key 用 Windows DPAPI 加密保存

## 使用须知

- 大模型按量计费，费用由你自己的账号承担
- Edge 语音是微软的在线服务，读弹幕是读取直播间网页，都不是官方开放接口，可能随时失效；请自行确认是否符合平台规则
- 直播时画面上的「AI 生成」标识请保留，这是平台和法规的要求
- 不要诱导观众打赏，未成年人不得打赏

## 从源码运行

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe src\main.py
```

打包：`powershell -File build.ps1`，生成 `release\AI虚拟主播.exe`。

## 授权

代码：[MIT](LICENSE)。Live2D 模型、照片等第三方内容各有授权，见 [THIRD_PARTY.md](THIRD_PARTY.md)。

本项目与抖音、DeepSeek、微软、Live2D 公司没有关联。
