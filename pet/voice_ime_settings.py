"""语音输入法设置对话框（独立悬浮窗，不侵入 ModernSettingsDialog 的域导航）。

仅读写少数配置键，改动即时 ``config.set`` + ``config.save``：

- ``voice_ime_enabled``        是否启用长按语音输入法
- ``voice_ime_long_press_ms``  长按触发阈值（毫秒）
- ``voice_ime_delivery``       "type"（键入当前窗口，失败回退剪贴板）| "clipboard"（仅剪贴板）
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .voice_ime import (
    CFG_DELIVERY,
    CFG_ENABLED,
    CFG_ENGINE,
    CFG_LONG_PRESS_MS,
    MODEL_NAME,
    MODEL_URL,
    ModelDownloader,
    FileDownloader,
    SENSEVOICE_MODEL_URL,
    SENSEVOICE_TOKENS_URL,
    is_model_ready,
    is_sensevoice_ready,
    model_status_text,
    sensevoice_dir,
    voice_ime_model_dir,
)

logger = logging.getLogger(__name__)


class VoiceIMESettingsDialog(QWidget):
    def __init__(self, config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._downloader: ModelDownloader | None = None

        self.setWindowTitle("语音输入法设置")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.resize(460, 500)
        self.setMinimumSize(430, 460)
        self._sv_downloading = False
        self._sv_queue = []
        self._build_ui()
        self._refresh_model_status()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # —— 启用开关 ——
        row_enable = QHBoxLayout()
        self.enable_check = QPushButton()
        self.enable_check.setCheckable(True)
        enabled = bool(self.config.get(CFG_ENABLED, True))
        self.enable_check.setChecked(enabled)
        self.enable_check.toggled.connect(self._on_enabled_toggled)
        self._style_toggle(self.enable_check, enabled)
        row_enable.addWidget(QLabel("启用长按语音输入法"))
        row_enable.addStretch(1)
        row_enable.addWidget(self.enable_check)
        root.addLayout(row_enable)

        # —— 长按时长 ——
        row_lp = QVBoxLayout()
        lp_head = QHBoxLayout()
        self.lp_label = QLabel()
        lp_head.addWidget(QLabel("长按触发时长"))
        lp_head.addStretch(1)
        lp_head.addWidget(self.lp_label)
        self.lp_slider = QSlider(Qt.Orientation.Horizontal)
        self.lp_slider.setRange(200, 2000)
        self.lp_slider.setSingleStep(50)
        ms = int(self.config.get(CFG_LONG_PRESS_MS, 600))
        self.lp_slider.setValue(ms)
        self._update_lp_label(ms)
        self.lp_slider.valueChanged.connect(self._on_lp_changed)
        row_lp.addLayout(lp_head)
        row_lp.addWidget(self.lp_slider)
        root.addLayout(row_lp)

        # —— 交付方式 ——
        row_delivery = QHBoxLayout()
        self.delivery_combo = QComboBox()
        self.delivery_combo.addItem("键入当前窗口（失败回退剪贴板）", "type")
        self.delivery_combo.addItem("仅复制到剪贴板", "clipboard")
        cur = str(self.config.get(CFG_DELIVERY, "type"))
        idx = self.delivery_combo.findData(cur)
        self.delivery_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.delivery_combo.currentIndexChanged.connect(self._on_delivery_changed)
        row_delivery.addWidget(QLabel("文字交付方式"))
        row_delivery.addStretch(1)
        row_delivery.addWidget(self.delivery_combo)
        root.addLayout(row_delivery)

        # —— 识别引擎 ——
        row_engine = QHBoxLayout()
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("自动（SenseVoice，识别更准，推荐）", "auto")
        self.engine_combo.addItem("仅 Vosk（轻量小模型）", "vosk")
        _cur_e = "vosk" if str(self.config.get(CFG_ENGINE, "auto")) == "vosk" else "auto"
        _idx_e = self.engine_combo.findData(_cur_e)
        self.engine_combo.setCurrentIndex(_idx_e if _idx_e >= 0 else 0)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        row_engine.addWidget(QLabel("识别引擎"))
        row_engine.addStretch(1)
        row_engine.addWidget(self.engine_combo)
        root.addLayout(row_engine)

        # —— 模型管理（Vosk 回退引擎） ——
        model_box = QVBoxLayout()
        model_box.setSpacing(8)
        self.model_status = QLabel()
        model_box.addWidget(self.model_status)

        dl_row = QHBoxLayout()
        self.download_btn = QPushButton("下载语音模型")
        self.download_btn.clicked.connect(self._on_download)
        dl_row.addWidget(self.download_btn)
        dl_row.addStretch(1)
        model_box.addLayout(dl_row)

        self.model_hint = QLabel(
            "模型为 Vosk 离线中文识别包（约 40MB），首次需联网下载，"
            "之后断网可用。\n手动下载地址：\n" + MODEL_URL + "\n"
            "解压到：" + str(voice_ime_model_dir(self.config) / MODEL_NAME)
        )
        self.model_hint.setWordWrap(True)
        self.model_hint.setStyleSheet("color:#9fb3d1;font-size:11px;")
        model_box.addWidget(self.model_hint)

        root.addLayout(model_box)

        # —— SenseVoice 精确模型（默认引擎） ——
        sv_box = QVBoxLayout()
        sv_box.setSpacing(8)
        self.sv_status = QLabel()
        sv_box.addWidget(self.sv_status)
        sv_row = QHBoxLayout()
        self.sv_download_btn = QPushButton("下载 SenseVoice 模型（约 226MB）")
        self.sv_download_btn.clicked.connect(self._on_download_sv)
        sv_row.addWidget(self.sv_download_btn)
        sv_row.addStretch(1)
        sv_box.addLayout(sv_row)
        self.sv_hint = QLabel(
            "SenseVoice（阿里通义，Apache-2.0）：中文识别精度显著高于 Vosk 小模型，"
            "自带标点。下载完成后自动作为默认引擎。"
        )
        self.sv_hint.setWordWrap(True)
        self.sv_hint.setStyleSheet("color:#9fb3d1;font-size:11px;")
        sv_box.addWidget(self.sv_hint)
        root.addLayout(sv_box)

        root.addStretch(1)

    # ------------------------------------------------------------------ #
    def _style_toggle(self, btn: QPushButton, on: bool) -> None:
        btn.setText("开" if on else "关")
        color = "#2ecc71" if on else "#7a828e"
        btn.setStyleSheet(
            f"QPushButton{{background:{color};color:#fff;border:none;"
            f"border-radius:8px;padding:4px 14px;font-weight:600;}}"
        )

    def _update_lp_label(self, ms: int) -> None:
        self.lp_label.setText(f"{ms} ms")

    # -- 配置写入 -------------------------------------------------------- #
    def _on_enabled_toggled(self, on: bool) -> None:
        self.config.set(CFG_ENABLED, bool(on))
        self.config.save()
        self._style_toggle(self.enable_check, on)

    def _on_lp_changed(self, ms: int) -> None:
        self._update_lp_label(ms)
        self.config.set(CFG_LONG_PRESS_MS, int(ms))
        self.config.save()

    def _on_delivery_changed(self, index: int) -> None:
        mode = self.delivery_combo.itemData(index)
        self.config.set(CFG_DELIVERY, str(mode))
        self.config.save()

    def _on_engine_changed(self, index: int) -> None:
        self.config.set(CFG_ENGINE, str(self.engine_combo.itemData(index)))
        self.config.save()

    # -- 模型下载 -------------------------------------------------------- #
    def _refresh_model_status(self) -> None:
        ready = is_model_ready(self.config)
        self.model_status.setText("模型状态：" + model_status_text(self.config))
        self.download_btn.setText("重新下载" if ready else "下载语音模型")
        self.download_btn.setEnabled(True)
        self._refresh_sv_status()

    def _refresh_sv_status(self) -> None:
        ready = is_sensevoice_ready(self.config)
        if self._sv_downloading:
            return  # 下载中：状态由进度回调驱动
        self.sv_status.setText(
            "SenseVoice 模型：已就绪（当前默认引擎）" if ready
            else "SenseVoice 模型：未下载（下载后自动作为默认引擎，Vosk 暂顶）"
        )
        self.sv_download_btn.setText("重新下载" if ready else "下载 SenseVoice 模型（约 226MB）")
        self.sv_download_btn.setEnabled(True)

    def _on_download_sv(self) -> None:
        if self._sv_downloading:
            return
        d = sensevoice_dir(self.config)
        d.mkdir(parents=True, exist_ok=True)
        self._sv_downloading = True
        self.sv_download_btn.setEnabled(False)
        self._sv_queue = [
            (SENSEVOICE_TOKENS_URL, d / "tokens.txt", "词表"),
            (SENSEVOICE_MODEL_URL, d / "model.int8.onnx", "SenseVoice 模型"),
        ]
        self._start_next_sv_download()

    def _start_next_sv_download(self) -> None:
        if not self._sv_queue:
            self._sv_downloading = False
            self._refresh_sv_status()
            return
        url, dest, label = self._sv_queue.pop(0)
        self.sv_status.setText(f"SenseVoice 模型：准备下载 {label}…")
        dler = FileDownloader(url, dest, label)
        dler.progress.connect(
            lambda _p, _t, msg: self.sv_status.setText(f"SenseVoice 模型：{msg}")
        )
        dler.done.connect(self._on_sv_file_done)
        self._sv_dl = dler  # 保活
        dler.start()

    def _on_sv_file_done(self, ok: bool, msg: str) -> None:
        if not ok:
            self.sv_status.setText(f"SenseVoice 模型：{msg}")
            self._sv_downloading = False
            self.sv_download_btn.setEnabled(True)
            return
        self.sv_status.setText(f"SenseVoice 模型：{msg}")
        self._start_next_sv_download()

    def _on_download(self) -> None:
        if self._downloader is not None and self._downloader.isRunning():
            return
        self.download_btn.setEnabled(False)
        self.model_status.setText("模型状态：准备下载…")
        d = ModelDownloader(
            voice_ime_model_dir(self.config), MODEL_URL, MODEL_NAME
        )
        d.progress.connect(
            lambda done, total, msg: self.model_status.setText(
                f"模型状态：{msg}"
            )
        )
        d.finished.connect(self._on_download_finished)
        self._downloader = d
        d.start()

    def _on_download_finished(self, ok: bool, msg: str) -> None:
        self.model_status.setText("模型状态：" + msg)
        self._refresh_model_status()
