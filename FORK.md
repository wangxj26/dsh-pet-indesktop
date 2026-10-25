# 本仓库说明（Fork 版本）

> 本仓库是 [**MerZlin/dsh-pet-indesktop**](https://github.com/MerZlin/dsh-pet-indesktop) 的 fork，
> 在原项目基础上新增了**离线语音输入法**（长按桌宠说话 → 本地识别 → 自动键入当前窗口）。
> 桌宠本体、动画素材、菜单系统、聊天窗口等全部来自原项目，**版权归原作者 MerZlin 所有**。

## 这是什么

「鱼语输入法」= 一只住在 Windows 桌面上的大肥鱼桌宠 + 内置的离线语音输入法：

- **桌宠**：106 段动画，会自己游动/做随机动作，点击有反应，右键菜单可编排；
- **语音输入**：鼠标长按桌宠约 0.6 秒开始录音，松开后 1–2 秒本地转写，带标点的文字自动键入光标所在窗口；
- **完全离线**：识别用 SenseVoice-Small（int8）+ sherpa-onnx，在本机 CPU 推理，录音不出本机。

## 相对上游的改动

### 新增文件

| 文件 | 作用 |
| --- | --- |
| `pet/voice_ime.py` | 语音输入法核心：录音、SenseVoice/sherpa-onnx 推理、引擎分发、模型下载管理 |
| `pet/voice_ime_delivery.py` | 文字交付：模拟键入当前窗口，失败回退剪贴板 |
| `pet/voice_ime_settings.py` | 独立设置页（长按时长、交付方式、引擎切换、模型下载） |

### 修改文件

| 文件 | 改动 |
| --- | --- |
| `pet/app.py` | 接入长按手势、语音面板、托盘「语音输入」入口；托盘图标固定为应用 logo |
| `pet/overlay_shell.py` | 同上（overlay 拓扑）；托盘提示文案 |
| `pet/context_menus/registry.py` | 右键菜单新增语音输入相关入口 |
| `pet/update_settings.py`、`pet/updater.py` | 本地构建默认关闭在线更新（避免被上游版本覆盖） |
| `pet/__init__.py` | 版本号与包说明 |
| `requirements.txt`、`requirements_nowinrt.txt` | 新增 `sherpa-onnx`；`winrt-*` 在 Python 3.13 下的安装说明 |
| `scripts/build_onedir.ps1` | 收集 `sherpa_onnx` / `winrt`；本地构建跳过 frameseq 门禁 |
| `packaging/dsh-pet.iss` | 补 `VersionInfo*`，应用显示名与版本注入 |
| `assets/icon.ico`、`assets/icon-preview.png` | 应用图标 |
| `.gitignore` | 忽略本地虚拟环境 `.venv/` |

> 数据目录、包名、开机自启注册表项保持上游的 `dsh-pet-standalone*` 标识不变——改名会让老用户的配置、模型与自启项失联。

## 构建

沿用上游流程（详见上游 README）：

```powershell
# 1) 依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2) onedir 构建（会产出 dist-onedir\<name>\ 与 portable.zip）
powershell -ExecutionPolicy Bypass -File scripts\build_onedir.ps1 -Variant webm-chat

# 3) 安装包（需 Inno Setup 6 与简体中文语言文件 ChineseSimplified.isl）
ISCC.exe /DMyAppDisplay=鱼语输入法 /DMyAppVersion=5.0 packaging\dsh-pet.iss
```

语音模型（约 226 MB）不随仓库分发，首次使用时在「语音输入法设置」页内一键下载。

## 许可证与致谢

- 原项目：**MIT License**，Copyright (c) 2026 Merzlin，见仓库根目录 [`LICENSE`](LICENSE)（本 fork 原样保留，未改动）。
- 本 fork 新增的语音输入相关代码同样以 **MIT** 发布。
- 语音识别：**SenseVoice-Small**（阿里通义实验室，Apache-2.0）+ **sherpa-onnx**。
- 其他第三方组件与素材许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

如果你要基于本 fork 再分发，请保留上述版权声明与许可证文本。

## 已知限制

- 仅 Windows 10/11（64 位）；语音输入依赖当前系统的模拟键入能力。
- 识别为**整句转写**（松开后出结果），不是实时流式逐字上屏。
- 目标程序拒绝模拟输入时（部分游戏/受保护窗口），自动回退剪贴板。
- 首次使用需下载 226 MB 模型；之后纯离线。
