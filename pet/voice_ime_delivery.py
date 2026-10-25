"""语音输入法：文本交付层（Windows）。

把识别出的文字「输入」到当前光标所在窗口：
- 优先用 Win32 ``SendInput`` + ``KEYEVENTF_UNICODE`` 模拟键盘逐字打入
  （像真输入法一样送进正在编辑的窗口）；
- 失败（或配置为仅剪贴板）时回退为「复制到剪贴板」，由调用方提示粘贴。

所有 Win32 调用都包在 try/except 里，非 Windows / 调用失败时返回 ``False``，
保证主流程永不因交付失败而崩溃。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32
_kernel32 = getattr(ctypes, "windll", None) and ctypes.windll.kernel32

# --- Win32 常量 ---
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    """必须进联合体：真实 Win32 INPUT 的 union 以 **最大成员 MOUSEINPUT** 定长，
    x64 下 sizeof(INPUT) 应为 40。只放 KEYBDINPUT 会得到 32——SendInput 校验
    cbSize 不符直接拒绝（返回 0），键入永远失败（实测踩中）。"""


    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("__u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("__u", _INPUTUNION),
    ]


def _build_inputs(text: str) -> list[INPUT]:
    """把一段文本展开成 SendInput 所需的 INPUT 数组（每个字符一次按下+抬起）。

    基本多文种平面（BMP, <0x10000）字符一次 Unicode 按键即可；
    代理对（emoji 等）拆成高低代理两项，否则 Windows 会吞掉。
    """
    inputs: list[INPUT] = []
    for ch in text:
        code = ord(ch)
        units = (
            (code,)
            if code < 0x10000
            else ((0xD800 + ((code - 0x10000) >> 10)) & 0xFFFF,
                  0xDC00 + ((code - 0x10000) & 0x3FF))
        )
        for unit in units:
            down = INPUT()
            down.type = INPUT_KEYBOARD
            down.ki.wScan = unit  # type: ignore[attr-defined]
            down.ki.dwFlags = KEYEVENTF_UNICODE  # type: ignore[attr-defined]
            up = INPUT()
            up.type = INPUT_KEYBOARD
            up.ki.wScan = unit  # type: ignore[attr-defined]
            up.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP  # type: ignore[attr-defined]
            inputs.append(down)
            inputs.append(up)
    return inputs


def send_unicode_text(text: str, hwnd=None) -> bool:
    """把 ``text`` 打入前台窗口。成功返回 True。

    ``hwnd`` 为目标窗口句柄；若给定会先 ``SetForegroundWindow`` 把焦点还回去，
    再发送按键，从而保证文字进入用户原本正在编辑的窗口。
    """
    if not _user32 or not text:
        return False
    try:
        if hwnd and is_valid_hwnd(hwnd):
            _user32.SetForegroundWindow(int(hwnd))
            # 给目标窗口一点时间完成激活切换，否则首字符可能落空。
            time.sleep(0.03)
        inputs = _build_inputs(text)
        if not inputs:
            return False
        arr = (INPUT * len(inputs))(*inputs)
        n = _user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(INPUT))
        return n == len(inputs)
    except Exception:
        return False


def get_foreground_hwnd():
    """当前前台窗口句柄（best-effort）。"""
    if not _user32:
        return None
    try:
        return _user32.GetForegroundWindow()
    except Exception:
        return None


def hwnd_belongs_to_current_process(hwnd) -> bool:
    """判断 hwnd 是否属于本进程（桌宠自己的窗口不能当交付目标）。"""
    if not _user32 or not _kernel32 or not hwnd:
        return False
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return pid.value == _kernel32.GetCurrentProcessId()
    except Exception:
        return False


def is_valid_hwnd(hwnd) -> bool:
    """窗口句柄仍然有效（未被关闭）。"""
    if not _user32 or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(int(hwnd)))
    except Exception:
        return False


def get_window_title(hwnd) -> str:
    """窗口标题（用于交付后的反馈提示），失败返回空串。"""
    if not _user32 or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = _user32.GetWindowTextW(int(hwnd), buf, 256)
        return (buf.value or "").strip() if n else ""
    except Exception:
        return ""


def copy_to_clipboard(text: str) -> bool:
    """用 Qt 剪贴板复制文本（不依赖前台窗口焦点）。"""
    try:
        from PySide6.QtWidgets import QApplication

        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
            return True
    except Exception:
        pass
    return False
