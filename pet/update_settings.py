# -*- coding: utf-8 -*-
"""Modern settings page for online updates."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from . import __version__, updater
# 本地定制版：作者账号区块（settings_about.build_author_section）已按需求移除。


class UpdatePage(QWidget):
    """检查、下载并安装更新；不写入 Config，避免把一次性命令变成设置项。"""

    check_finished = Signal(object)
    download_finished = Signal(object)
    download_progress = Signal(int, int)

    def __init__(self, config, *, include_chat: bool = True, parent=None):
        super().__init__(parent)
        self.config = config
        self.include_chat = bool(include_chat)
        self.release: dict | None = None
        self._busy = False
        self._build_ui()
        self.check_finished.connect(self._on_check_finished)
        self.download_finished.connect(self._on_download_finished)
        self.download_progress.connect(self._on_download_progress)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        intro = QLabel(
            f"当前版本 v{__version__}。更新功能已在本版本停用（本地定制版）："
            "在线升级会覆盖本地新增的语音输入法等功能，故检查与下载均已写死禁用。",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("updateIntro")
        layout.addWidget(intro)

        status_card = QFrame(self)
        status_card.setObjectName("updateStatusCard")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(16, 14, 16, 14)
        status_layout.setSpacing(8)
        self.version_label = QLabel(f"当前版本  v{__version__}", status_card)
        self.version_label.setObjectName("updateCurrentVersion")
        self.status_label = QLabel("更新功能已停用（本地定制版）", status_card)
        self.status_label.setObjectName("updateStatus")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.version_label)
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_card)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.check_button = QPushButton("检查更新", self)
        self.check_button.setObjectName("checkUpdateButton")
        self.check_button.clicked.connect(self.check_for_updates)
        self.check_button.setEnabled(False)  # 本地定制版：写死停用
        self.install_button = QPushButton("下载并安装", self)
        self.install_button.setObjectName("installUpdateButton")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.download_and_install)
        self.manual_button = QPushButton("打开下载页", self)
        self.manual_button.setObjectName("manualUpdateButton")
        self.manual_button.setEnabled(False)  # 本地定制版：升级指向已清空
        self.manual_button.clicked.connect(self.open_download_page)
        action_row.addWidget(self.check_button)
        action_row.addWidget(self.install_button)
        action_row.addWidget(self.manual_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.progress = QProgressBar(self)
        self.progress.setObjectName("updateProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.hide()
        layout.addWidget(self.progress)

        notes_card = QFrame(self)
        notes_card.setObjectName("updateNotesCard")
        notes_layout = QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(16, 14, 16, 14)
        notes_layout.setSpacing(6)
        notes_title = QLabel("新版本说明", notes_card)
        notes_title.setObjectName("updateNotesTitle")
        self.notes_label = QLabel("检查到新版本后，这里会显示版本说明。", notes_card)
        self.notes_label.setObjectName("updateNotes")
        self.notes_label.setWordWrap(True)
        self.notes_label.setTextInteractionFlags(
            self.notes_label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.notes_label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        notes_layout.addWidget(notes_title)
        notes_layout.addWidget(self.notes_label)
        layout.addWidget(notes_card)
        # 本地定制版：作者账号区块已按需求移除。
        layout.addStretch(1)

    def check_for_updates(self) -> None:
        # 本地定制版：写死停用，不发起网络请求。
        self._set_state("更新功能已停用（本地定制版）。", checking=False)

    def _on_check_finished(self, result) -> None:
        self._busy = False
        if isinstance(result, Exception) or result is None:
            self.release = None
            self._set_state("无法连接更新服务，请检查网络后重试。", checking=False)
            self.manual_button.setEnabled(True)
            return
        self.release = result
        tag = str(result.get("version") or "")
        self.manual_button.setEnabled(bool(result.get("html_url")))
        if not updater.is_newer(tag):
            self.install_button.setEnabled(False)
            self._set_state(f"已经是最新版本（v{__version__}）。", checking=False)
            self.notes_label.setText("当前没有可用更新。")
            return
        asset = updater.select_windows_installer(result, include_chat=self.include_chat)
        notes = str(result.get("notes") or "暂无版本说明。")
        self.notes_label.setText(notes)
        integrity_ready = bool(asset and asset.get("size") is not None and asset.get("sha256"))
        self.install_button.setEnabled(integrity_ready and updater.can_auto_install())
        if asset is None:
            state = f"发现 v{tag}，但没有找到匹配的 Windows 安装包，请打开下载页。"
        elif not integrity_ready:
            state = f"发现 v{tag}，但安装包缺少完整性校验信息，请打开下载页手动更新。"
        elif not updater.can_auto_install():
            state = f"发现 v{tag}。当前运行方式不支持自动安装，请打开下载页手动更新。"
        else:
            state = f"发现新版本 v{tag}，下载后会自动关闭并重启安装。"
        self._set_state(state, checking=False)

    def download_and_install(self) -> None:
        # 本地定制版：写死停用。
        self._set_state("更新功能已停用（本地定制版）。", checking=False)

    def _on_download_progress(self, current: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(min(100, int(current * 100 / total)))
            self.progress.setFormat(f"{current / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")
        else:
            self.progress.setFormat(f"已下载 {current / 1024 / 1024:.1f} MB")

    def _on_download_finished(self, result) -> None:
        self._busy = False
        self.check_button.setEnabled(True)
        if isinstance(result, Exception):
            self.install_button.setEnabled(True)
            self._set_state(f"下载或校验失败：{result}", checking=False)
            return
        try:
            updater.start_installer(result)
        except Exception as exc:  # noqa: BLE001
            self.install_button.setEnabled(True)
            self._set_state(f"无法启动安装器：{exc}", checking=False)
            return
        self._set_state("安装器已启动，软件将关闭并自动重启。", checking=False)
        self.install_button.setEnabled(False)
        self.window().close()

    def open_download_page(self) -> None:
        url = str((self.release or {}).get("html_url") or updater.REPO_URL)
        QDesktopServices.openUrl(QUrl(url))

    def _set_state(self, text: str, *, checking: bool) -> None:
        self.status_label.setText(text)
        self.check_button.setEnabled(not checking and not self._busy)


__all__ = ["UpdatePage"]
