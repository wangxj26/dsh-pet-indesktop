"""语音输入法（长按大肥鱼 → 语音转文字 → 输入到当前窗口）。

设计原则（对齐 dsh-pet-indesktop 的「低侵入」约束）：

- 不改动 ``window.py`` 内部的拖拽/点击状态机：长按检测用**应用级事件过滤器**
  （挂在 ``QApplication`` 上，只处理属于桌宠窗口及其子控件——即播放动画的
  那个 QLabel——的鼠标事件），从而在「大肥鱼」身上也能稳定触发。
- ``vosk`` / ``PySide6.QtMultimedia`` 全部**惰性导入**（用到才 import），
  首次启动即便还没装 ``vosk``，主程序也不会因 import 失败而崩。
- 文字交付逻辑全部在 ``voice_ime_delivery.py``（纯逻辑、可单测、跨平台安全），
  本模块只负责「采集 + 识别 + 唤起面板 + 调度交付」。
- 唯一新增的第三方依赖是 ``vosk``（离线中文识别，约 40MB 模型首启联网下载）。

交互：按住大肥鱼不放（默认 600ms）→ 唤起语音面板并开始聆听；松开 →
把识别到的中文逐字「键入」回你刚才正在编辑的窗口（SendInput），失败时回退
到剪贴板（Ctrl+V 粘贴）。托盘还有「语音输入」（手动唤起）与「语音输入法设置」。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QTimer,
    QThread,
    Qt,
    Signal,
)
from PySide6.QtGui import QCursor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .voice_ime_delivery import (
    copy_to_clipboard,
    get_foreground_hwnd,
    get_window_title,
    hwnd_belongs_to_current_process,
    is_valid_hwnd,
    send_unicode_text,
)

logger = logging.getLogger(__name__)

# 中文离线模型（Vosk 官方，MIT/Apache 许可，自包含、断网可用）。
MODEL_NAME = "vosk-model-small-cn-0.22"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"

# 配置键（无 schema 约束，新增零风险；缺失时回落默认值）。
CFG_ENABLED = "voice_ime_enabled"
CFG_LONG_PRESS_MS = "voice_ime_long_press_ms"
CFG_DELIVERY = "voice_ime_delivery"  # "type"（默认键入，失败回退剪贴板）| "clipboard"
CFG_ENGINE = "voice_ime_engine"  # "auto"（SenseVoice 优先，缺失回退 Vosk）| "vosk"

# SenseVoice（阿里通义，Apache-2.0）：中文精度远超 Vosk 小模型且自带标点。
# int8 约 226MB，CPU 实时；经 hf-mirror 国内直连下载。
SENSEVOICE_DIR_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SENSEVOICE_BASE = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/resolve/main"
)
SENSEVOICE_MODEL_URL = SENSEVOICE_BASE + "/model.int8.onnx"
SENSEVOICE_TOKENS_URL = SENSEVOICE_BASE + "/tokens.txt"


# --------------------------------------------------------------------------- #
# 配置与模型路径辅助
# --------------------------------------------------------------------------- #
def voice_ime_model_dir(config) -> Path:
    """语音模型存放目录：``<数据目录>/vosk_models``。"""
    return Path(str(config.dir)) / "vosk_models"


def voice_ime_model_path(config) -> Path:
    return voice_ime_model_dir(config) / MODEL_NAME


def model_folder_ready(model_path: Path) -> bool:
    """判断模型是否已解压就绪（Vosk 需要 am/conf/graph 等子目录）。"""
    if not model_path.is_dir():
        return False
    return any(
        (model_path / sub).exists()
        for sub in ("am", "conf", "graph", "ivector", "res")
    )


def is_model_ready(config) -> bool:
    return model_folder_ready(voice_ime_model_path(config))


def model_status_text(config) -> str:
    if is_model_ready(config):
        return "已就绪（离线中文识别可用）"
    return "未下载（首次使用需联网下载约 40MB）"


def is_enabled(config) -> bool:
    return bool(config.get(CFG_ENABLED, True))


def long_press_ms(config) -> int:
    try:
        return max(200, min(2000, int(config.get(CFG_LONG_PRESS_MS, 600))))
    except (TypeError, ValueError):
        return 600


def delivery_mode(config) -> str:
    mode = str(config.get(CFG_DELIVERY, "type"))
    return mode if mode in ("type", "clipboard") else "type"


def engine_mode(config) -> str:
    """识别引擎："auto"（SenseVoice 优先，模型缺失回退 Vosk）| "vosk"。"""
    return "vosk" if str(config.get(CFG_ENGINE, "auto")) == "vosk" else "auto"


def sensevoice_dir(config) -> Path:
    return Path(str(config.dir)) / "sherpa_models" / SENSEVOICE_DIR_NAME


def is_sensevoice_ready(config) -> bool:
    d = sensevoice_dir(config)
    return (d / "model.int8.onnx").is_file() and (d / "tokens.txt").is_file()


# --------------------------------------------------------------------------- #
# 长按检测器（应用级事件过滤器）
# --------------------------------------------------------------------------- #
def _global_pos(ev: QMouseEvent) -> QPoint:
    try:
        return ev.globalPosition().toPoint()
    except Exception:  # pragma: no cover - 旧 API 兜底
        return ev.globalPos()


class LongPressDetector(QObject):
    """挂在 QApplication 上的事件过滤器，仅对属于 ``widget`` 的鼠标事件做长按判定。

    不消费鼠标事件（除「已触发长按后的抬起」外一律返回 False），因此完全不影响
    桌宠原有的拖拽/点击/抛出等行为。
    """

    long_press_started = Signal()
    long_press_ended = Signal()

    def __init__(self, widget: QWidget, threshold_ms: int = 600) -> None:
        app = QApplication.instance() or QApplication([])
        super().__init__(app)
        self._widget = widget
        self._threshold = int(threshold_ms)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._active = False
        self._fired = False
        self._press_pos: QPoint | None = None
        app.installEventFilter(self)
        # 被观察窗口销毁后自摘过滤器：应用级过滤器仍会收到事件，
        # 对已删 C++ 对象调 isAncestorOf 会抛 RuntimeError。
        widget.destroyed.connect(self._on_widget_destroyed)

    def _on_widget_destroyed(self, *_a) -> None:
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:  # noqa: BLE001
            pass

    def set_threshold(self, ms: int) -> None:
        self._threshold = max(200, min(2000, int(ms)))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 约定
        try:
            return self._event_filter_impl(obj, event)
        except RuntimeError:
            # 被观察窗口已销毁（PySide 内部对象失效）：自摘过滤器，静默退出。
            try:
                app = QApplication.instance()
                if app is not None:
                    app.removeEventFilter(self)
            except Exception:  # noqa: BLE001
                pass
            return False

    def _event_filter_impl(self, obj, event) -> bool:
        # 应用级过滤器收到的是**所有 QObject** 的事件（含 QApplication、定时器等
        # 非 QWidget 对象）：先做类型闸，否则 isAncestorOf 对非 QWidget 抛
        # TypeError（离屏实测踩中）。只处理落在桌宠窗口（含其子控件）上的鼠标。
        if not isinstance(obj, QWidget):
            return False
        if not (obj is self._widget or self._widget.isAncestorOf(obj)):
            return False
        if not isinstance(event, QMouseEvent):
            return False

        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._active = True
                self._fired = False
                self._press_pos = _global_pos(event)
                self._timer.start(self._threshold)
            return False

        if etype == QEvent.Type.MouseMove:
            if self._active and not self._fired and self._press_pos is not None:
                if (_global_pos(event) - self._press_pos).manhattanLength() > 12:
                    # 移动超过阈值：这是拖拽，不是长按，取消判定。
                    self._timer.stop()
                    self._active = False
            return False

        if etype == QEvent.Type.MouseButtonRelease:
            if self._active:
                self._timer.stop()
                was_fired = self._fired
                self._active = False
                self._fired = False
                if was_fired:
                    self.long_press_ended.emit()
                    # 不吞抬起：overlay 拓扑的 mouseReleaseEvent 还负责收尾
                    # _mouse_grab/_press_pos 等拖拽状态，吞掉会让抓取卡死；
                    # 放行的代价只是长按松手时多一次无害的「点击反应」。
                    return False
            return False

        return False

    def _on_timeout(self) -> None:
        if self._active:
            self._fired = True
            self.long_press_started.emit()


# --------------------------------------------------------------------------- #
# 语音面板（半透明悬浮小窗）
# --------------------------------------------------------------------------- #
class VoicePanel(QWidget):
    commit_requested = Signal()
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 关键：面板绝不抢前台焦点——否则用户正在编辑的聊天框会失去激活，
        # 交付时 SendInput 就打不进去（本 bug 的诱因之一）。面板上的按钮
        # 仍可正常点击（点击只被抑制「激活」，不抑制鼠标事件）。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        # 尺寸预算（150% DPI 下同样成立）：12+标题24+6+状态18+6+文本46+6+
        # 按钮行33+12 = 163 ≤ 190，留余量防止按钮被挤到文字截断（历史 bug）。
        self.setFixedSize(384, 190)
        self._final = ""
        self._partial = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        self.title = QLabel("🎤 语音输入法")
        self.title.setStyleSheet("font-weight:600;font-size:14px;color:#cfe0ff;")
        self.status = QLabel("")
        self.status.setStyleSheet("color:#9fb3d1;font-size:12px;")
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.text.setStyleSheet("color:#f2f2f2;font-size:14px;")
        self.text.setFixedHeight(46)

        # 按钮横排（各占一半）：竖排会让面板超高、按钮被布局压扁到文字截断。
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.b_commit = QPushButton("完成并输入")
        self.b_cancel = QPushButton("取消")
        self.b_commit.setStyleSheet(
            "QPushButton{background:#3b6cff;color:#fff;border:none;"
            "border-radius:8px;padding:7px 12px;font-size:13px;}"
            "QPushButton:hover{background:#5a82ff;}"
        )
        self.b_cancel.setStyleSheet(
            "QPushButton{background:#2a2f3a;color:#cdd6e4;border:none;"
            "border-radius:8px;padding:7px 12px;font-size:13px;}"
            "QPushButton:hover{background:#353b48;}"
        )
        self.b_commit.clicked.connect(self.commit_requested.emit)
        self.b_cancel.clicked.connect(self.cancel_requested.emit)
        btn_row.addWidget(self.b_commit, 1)
        btn_row.addWidget(self.b_cancel, 1)

        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.text, 1)
        layout.addLayout(btn_row)

    # -- 外部接口 --------------------------------------------------------- #
    def set_status(self, s: str) -> None:
        self.status.setText(s)

    def set_final(self, t: str) -> None:
        self._final = t
        self._refresh()

    def set_partial(self, t: str) -> None:
        self._partial = t
        self._refresh()

    def get_text(self) -> str:
        return self._final.strip()

    def show_delivered(self, ok: bool, title: str = "", via: str = "typed") -> None:
        if ok and via == "typed":
            self.set_status(f"已输入到「{title}」✅" if title else "已输入到当前窗口 ✅")
        elif ok:
            self.set_status("已复制到剪贴板，请 Ctrl+V 粘贴")
        else:
            self.set_status("交付失败，请重试")
        self.b_commit.setEnabled(False)
        QTimer.singleShot(1800, self.close)

    def _refresh(self) -> None:
        sep = " " if self._final and self._partial else ""
        self.text.setText((self._final + sep + self._partial).strip())

    # -- 绘制圆角半透明背景 ---------------------------------------------- #
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        from PySide6.QtGui import QColor

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 14, 14)
        p.fillPath(path, QColor(26, 28, 36, 238))
        p.setPen(QPen(QColor(120, 160, 255, 170), 1))
        p.drawPath(path)


# --------------------------------------------------------------------------- #
# 模型下载器（后台线程，带进度）
# --------------------------------------------------------------------------- #
class ModelDownloader(QThread):
    progress = Signal(int, int, str)  # done, total, status_text
    finished = Signal(bool, str)      # success, message

    def __init__(self, model_dir: Path, url: str, model_name: str) -> None:
        super().__init__()
        self.model_dir = model_dir
        self.url = url
        self.model_name = model_name

    def run(self) -> None:  # pragma: no cover - 联网下载，沙箱不可达
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.model_dir / (self.model_name + ".zip")

            def _hook(block_num, block_size, total_size):
                if total_size and total_size > 0:
                    pct = min(100, int(block_num * block_size * 100 / total_size))
                    self.progress.emit(pct, 100, f"下载中… {pct}%")

            self.progress.emit(0, 100, "开始下载语音模型…")
            urllib.request.urlretrieve(self.url, str(tmp), _hook)
            self.progress.emit(100, 100, "解压中…")
            with zipfile.ZipFile(str(tmp)) as zf:
                zf.extractall(str(self.model_dir))
            try:
                tmp.unlink()
            except OSError:
                pass
            target = self.model_dir / self.model_name
            if model_folder_ready(target):
                self.finished.emit(True, "模型已就绪（离线中文识别可用）")
            else:
                self.finished.emit(False, "解压后未找到模型文件，请手动下载")
        except Exception as exc:  # noqa: BLE001 - 下载失败需明确告知用户
            logger.warning("语音模型下载失败: %s", exc)
            self.finished.emit(False, f"下载失败：{exc}")


class FileDownloader(QThread):
    """单文件下载器（带进度）：用于 SenseVoice 模型等大文件。

    写入 ``.part`` 临时名、完成后原子改名——中断不会留下半截的正式文件。
    """

    progress = Signal(int, int, str)
    done = Signal(bool, str)

    def __init__(self, url: str, dest: Path, label: str = "") -> None:
        super().__init__()
        self.url = url
        self.dest = dest
        self.label = label or url.rsplit("/", 1)[-1]

    def run(self) -> None:  # pragma: no cover - 联网下载
        try:
            self.dest.parent.mkdir(parents=True, exist_ok=True)
            part = self.dest.with_suffix(self.dest.suffix + ".part")
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, \
                    open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    self.progress.emit(
                        pct, 100, f"{self.label} {done >> 20}MB/{total >> 20}MB")
            part.rename(self.dest)
            self.done.emit(True, f"{self.label} 下载完成")
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载失败 %s: %s", self.url, exc)
            self.done.emit(False, f"{self.label} 下载失败：{exc}")


# --------------------------------------------------------------------------- #
# 语音输入法管理器（编排：模型 + 录音 + 识别 + 面板 + 交付）
# --------------------------------------------------------------------------- #
class VoiceIMEManager(QObject):
    def __init__(self, config, shell=None) -> None:
        super().__init__()
        self.config = config
        self.shell = shell
        self._model = None
        self._recognizer = None
        self._audio_source = None
        self._io = None
        self._panel: VoicePanel | None = None
        self._hwnd = None
        self._pet_widget: QWidget | None = None
        self._anchor = None
        # 双引擎："sensevoice"（非流式：缓存 PCM、松开整句转写）| "vosk"（流式）。
        self._engine = None
        self._pcm = bytearray()
        self._sv_recognizer = None
        self._rec_started_at = 0.0
        # 交付目标窗口的「外部前台缓存」：长按大肥鱼时 overlay 可能已把前台抢走，
        # 按下瞬间 GetForegroundWindow 拿到的会是桌宠自己——所以平时就低频轮询
        # 记住「最近一个不属于本进程的前台窗口」（通常是用户正在编辑的聊天框）。
        self._last_foreign_hwnd = None
        self._fg_timer = QTimer(self)
        self._fg_timer.setInterval(500)
        self._fg_timer.timeout.connect(self._cache_foreign_foreground)
        self._fg_timer.start()
        self._listening = False
        self._session_active = False
        self._final_text = ""

    # -- 入口 ------------------------------------------------------------- #
    def _cache_foreign_foreground(self) -> None:
        """低频记录最近的外部（非本进程）前台窗口，供交付时回溯目标。"""
        try:
            hwnd = get_foreground_hwnd()
            if hwnd and not hwnd_belongs_to_current_process(hwnd):
                self._last_foreign_hwnd = hwnd
        except Exception:  # noqa: BLE001
            pass

    def _resolve_target_hwnd(self):
        """解析交付目标：外部前台优先，其次最近的外部前台缓存。"""
        try:
            hwnd = get_foreground_hwnd()
            if hwnd and not hwnd_belongs_to_current_process(hwnd):
                self._last_foreign_hwnd = hwnd
                return hwnd
        except Exception:  # noqa: BLE001
            pass
        cached = self._last_foreign_hwnd
        if cached and is_valid_hwnd(cached) and not hwnd_belongs_to_current_process(cached):
            return cached
        return None

    def on_long_press_started(self, pet_widget: QWidget | None = None) -> None:
        if not is_enabled(self.config):
            return
        if self._session_active:
            return
        # 在按下那一刻抓取前台窗口：通常是用户正在编辑的窗口（按下尚未让其激活）。
        # 面板锚点用按下时的光标位置（overlay 窗铺满整屏，窗几何不适合做锚）。
        try:
            anchor = QCursor.pos()
        except Exception:  # noqa: BLE001
            anchor = None
        # 交付目标：优先「当前外部前台」，否则回退最近缓存（见 _resolve_target_hwnd）。
        self.start(self._resolve_target_hwnd(), pet_widget=pet_widget, anchor=anchor)

    def on_long_press_ended(self) -> None:
        if self._session_active:
            self.stop_and_deliver()

    def start(self, hwnd=None, pet_widget: QWidget | None = None,
              anchor=None) -> None:
        if self._session_active:
            if self._panel is not None:
                self._panel.raise_()
            return
        self._hwnd = hwnd
        self._pet_widget = pet_widget
        self._anchor = anchor
        self._session_active = True
        self._final_text = ""
        self._show_panel()
        if self._panel is not None:
            self._panel.set_status("准备中…")
        if not is_model_ready(self.config):
            if self._panel is not None:
                self._panel.set_status("模型未下载，请在「语音输入法设置」中下载")
            self._session_active = False
            return
        # 让「准备中/加载模型」先画出来，再同步加载（约 1s 冻结一次，可接受）。
        QTimer.singleShot(30, self._load_and_begin)

    def _load_and_begin(self) -> None:
        # 引擎选择：默认 auto = 优先 SenseVoice（识别准、带标点），模型缺失或
        # 显式选 "vosk" 时回退 Vosk 流式识别。
        if engine_mode(self.config) != "vosk" and is_sensevoice_ready(self.config):
            try:
                self._engine = "sensevoice"
                self._load_sensevoice()
                self._begin_capture_sv()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("SenseVoice 引擎加载失败，回退 Vosk: %s", exc)
        self._engine = "vosk"
        try:
            from vosk import Model

            mp = voice_ime_model_path(self.config)
            self._model = Model(str(mp))
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载语音模型失败: %s", exc)
            if self._panel is not None:
                self._panel.set_status(f"模型加载失败：{exc}")
            self._session_active = False
            return
        self._begin_capture()

    # -- 录音开路（两引擎共用） -------------------------------------------- #
    def _open_audio(self):
        """打开 16kHz 单声道 Int16 麦克风流。返回 (source, io, device)。"""
        from PySide6.QtMultimedia import (
            QAudioFormat,
            QAudioSource,
            QMediaDevices,
        )

        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise RuntimeError("未检测到麦克风输入设备")
        source = QAudioSource(device, fmt, self)
        io = source.start()
        io.readyRead.connect(self._on_audio_ready)
        return source, io, device

    def _begin_capture(self) -> None:  # pragma: no cover - 需要音频设备
        """Vosk 流式路径：边录边出部分结果。"""
        try:
            from vosk import KaldiRecognizer

            self._audio_source, self._io, device = self._open_audio()
            self._recognizer = KaldiRecognizer(self._model, 16000)
            self._recognizer.SetWords(False)
            self._listening = True
            self._rec_started_at = time.monotonic()
            logger.info("语音输入法: 开始录音（Vosk, 设备=%s, 实际采样率=%d）",
                        device.description(),
                        self._audio_source.format().sampleRate())
            if self._panel is not None:
                self._panel.set_status("正在聆听…（松开即输入）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动录音失败: %s", exc)
            if self._panel is not None:
                self._panel.set_status(f"无法启动麦克风：{exc}")
            self._session_active = False

    def _begin_capture_sv(self) -> None:  # pragma: no cover - 需要音频设备
        """SenseVoice 路径：缓存 PCM，松开后整句转写（无中间部分结果）。"""
        try:
            self._audio_source, self._io, device = self._open_audio()
            self._pcm = bytearray()
            self._listening = True
            self._rec_started_at = time.monotonic()
            logger.info("语音输入法: 开始录音（SenseVoice, 设备=%s）",
                        device.description())
            if self._panel is not None:
                self._panel.set_status("正在聆听…（SenseVoice，松开即转写）")
            self._start_elapsed_ticker()
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动录音失败: %s", exc)
            if self._panel is not None:
                self._panel.set_status(f"无法启动麦克风：{exc}")
            self._session_active = False

    def _start_elapsed_ticker(self) -> None:
        """非流式引擎没有部分结果：用秒数计时给用户「正在录」的反馈。"""
        if getattr(self, "_elapsed_timer", None) is None:
            self._elapsed_timer = QTimer(self)
            self._elapsed_timer.setInterval(500)
            self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._elapsed_timer.start()

    def _tick_elapsed(self) -> None:
        if not self._listening or self._panel is None:
            return
        secs = int(time.monotonic() - self._rec_started_at)
        self._panel.set_status(f"正在聆听… {secs}s（SenseVoice，松开即转写）")

    def _load_sensevoice(self) -> None:
        import sherpa_onnx

        d = sensevoice_dir(self.config)
        if self._panel is not None:
            self._panel.set_status("加载 SenseVoice 模型…")
        QApplication.processEvents()
        self._sv_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=2,
            use_itn=True,
        )
        logger.info("语音输入法: SenseVoice 模型已加载（%s）", d.name)

    def _finalize_sensevoice(self) -> str:
        """把缓存的 PCM 整句交给 SenseVoice 转写，返回文本（可空）。"""
        if self._sv_recognizer is None or not self._pcm:
            return ""
        import numpy as np

        samples = np.frombuffer(bytes(self._pcm), dtype=np.int16).astype(
            np.float32) / 32768.0
        self._pcm = bytearray()
        if samples.size == 0:
            return ""
        # 静音防护：峰值近似为 0（误触长按/麦克风没声）时直接不转写——
        # 否则语音模型会对纯静音「幻觉」出「嗯」之类的字被打进文档。
        if float(np.max(np.abs(samples))) < 0.01:  # ≈ -40dB
            logger.info("语音输入法: SenseVoice 判定静音（峰值过低），跳过转写")
            return ""
        if self._panel is not None:
            self._panel.set_status("识别中…（SenseVoice）")
        QApplication.processEvents()
        stream = self._sv_recognizer.create_stream()
        stream.accept_waveform(16000, samples.tolist())
        try:
            self._sv_recognizer.decode_streams([stream])
        except AttributeError:  # 旧版 API 兼容
            self._sv_recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        logger.info("语音输入法: SenseVoice 转写完成（%d 字）", len(text))
        return text

    def _on_audio_ready(self) -> None:  # pragma: no cover - 需要音频设备
        if self._io is None:
            return
        data = self._io.readAll().data()
        if not data:
            return
        if self._engine == "sensevoice":
            self._pcm.extend(data)
            return
        if self._recognizer is None:
            return
        try:
            if self._recognizer.AcceptWaveform(data):
                res = json.loads(self._recognizer.Result())
                txt = (res.get("text") or "").strip()
                if txt:
                    self._final_text = (
                        (self._final_text + " " + txt).strip()
                        if self._final_text
                        else txt
                    )
                    if self._panel is not None:
                        self._panel.set_final(self._final_text)
            else:
                res = json.loads(self._recognizer.PartialResult())
                if self._panel is not None:
                    self._panel.set_partial(res.get("partial") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("识别中间结果异常（已忽略）: %s", exc)

    # -- 结束与交付 ------------------------------------------------------- #
    def stop_and_deliver(self) -> None:
        self._stop_capture()
        if self._engine == "sensevoice":
            self._final_text = self._finalize_sensevoice()
        elif self._recognizer is not None:
            try:
                res = json.loads(self._recognizer.FinalResult())
                tail = (res.get("text") or "").strip()
                if tail:
                    self._final_text = (
                        (self._final_text + " " + tail).strip()
                        if self._final_text
                        else tail
                    )
            except Exception:  # noqa: BLE001
                pass
        text = self._final_text.strip()
        logger.info("语音输入法: 停止并交付（文本 %d 字）", len(text))
        if text:
            ok, via = self.deliver(text)
            title = get_window_title(self._hwnd) if via == "typed" and self._hwnd else ""
            if self._panel is not None:
                self._panel.show_delivered(ok, title, via)
        else:
            if self._panel is not None:
                self._panel.set_status("未识别到语音")
            QTimer.singleShot(900, self._safe_close_panel)
        self._session_active = False

    def cancel(self) -> None:
        self._stop_capture()
        self._session_active = False
        self._safe_close_panel()

    def deliver(self, text: str) -> tuple[bool, str]:
        """交付文本。返回 (是否成功, 通道)："typed" | "clipboard" | ""（全失败）。"""
        mode = delivery_mode(self.config)
        hwnd = self._hwnd
        # 目标窗口已关/无效时重解析一次；仍拿不到就只走剪贴板兜底。
        if hwnd and not is_valid_hwnd(hwnd):
            hwnd = self._hwnd = self._resolve_target_hwnd()
        if mode != "clipboard" and hwnd is not None:
            if send_unicode_text(text, hwnd):
                logger.info("语音输入法交付: 键入成功 hwnd=0x%X title=%r",
                            hwnd, get_window_title(hwnd))
                return True, "typed"
            logger.warning("语音输入法交付: 键入失败（SendInput），回退剪贴板")
        else:
            logger.info("语音输入法交付: 无有效目标窗口或模式=clipboard，直接剪贴板")
        ok = copy_to_clipboard(text)
        logger.info("语音输入法交付: 剪贴板兜底=%s", ok)
        return ok, ("clipboard" if ok else "")

    def _stop_capture(self) -> None:  # pragma: no cover - 需要音频设备
        if self._audio_source is not None:
            try:
                self._audio_source.stop()
            except Exception:  # noqa: BLE001
                pass
            self._audio_source = None
        self._io = None
        self._listening = False
        elapsed = getattr(self, "_elapsed_timer", None)
        if elapsed is not None:
            elapsed.stop()

    # -- 面板 ------------------------------------------------------------- #
    def _show_panel(self) -> None:
        if self._panel is None:
            self._panel = VoicePanel()
            self._panel.commit_requested.connect(self.stop_and_deliver)
            self._panel.cancel_requested.connect(self.cancel)
        # 定位：按下时的光标位置（面板悬在手指/光标上方）优先；否则用桌宠窗。
        anchor = getattr(self, "_anchor", None)
        if anchor is not None:
            try:
                x = anchor.x() - self._panel.width() // 2
                y = anchor.y() - self._panel.height() - 24
                self._panel.move(max(8, x), max(8, y))
            except Exception:  # noqa: BLE001
                pass
        elif self._pet_widget is not None and self._pet_widget.isVisible():
            try:
                geo = self._pet_widget.geometry()
                x = geo.center().x() - self._panel.width() // 2
                y = geo.top() - self._panel.height() - 10
                self._panel.move(max(8, x), max(8, y))
            except Exception:  # noqa: BLE001
                pass
        self._panel.show()
        self._panel.raise_()

    def _safe_close_panel(self) -> None:
        if self._panel is not None:
            try:
                self._panel.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 安装入口（由 app.py 在每窗创建时调用）
# --------------------------------------------------------------------------- #
def install_voice_ime(win: QWidget, instance) -> VoiceIMEManager:
    """在 ``win``（桌宠窗口）上安装长按→语音输入法，并返回管理器。

    管理器挂在 ``instance`` 上以保证进程生命期内不被回收；长按检测器挂在
    ``QApplication`` 上（应用级），只对本窗口的鼠标事件生效。
    """
    mgr = getattr(instance, "_voice_ime_manager", None)
    if mgr is None:
        mgr = VoiceIMEManager(instance.config, instance.shell)
        instance._voice_ime_manager = mgr

    detector = LongPressDetector(win, threshold_ms=long_press_ms(instance.config))
    detector.long_press_started.connect(
        lambda: mgr.on_long_press_started(win)
    )
    detector.long_press_ended.connect(mgr.on_long_press_ended)
    # 保活：挂在窗口上，避免被 GC（事件过滤器依赖它活着）。
    win._voice_ime_detector = detector
    win._voice_ime_manager = mgr
    logger.info("语音输入法: 长按检测已挂载到 %r（阈值 %dms）",
                type(win).__name__, long_press_ms(instance.config))
    return mgr
