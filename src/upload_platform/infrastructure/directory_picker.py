import ctypes
from ctypes import wintypes
import os
from threading import Lock

from upload_platform.domain.core import Problem

BIF_NEWDIALOGSTYLE = 0x0040
BIF_NONEWFOLDERBUTTON = 0x0200
BFFM_SETSELECTIONW = 0x0467


class DirectoryPicker:
    def __init__(self):
        self._open = Lock()

    def choose(self, initial_directory):
        if os.name != "nt":
            raise Problem("picker_unavailable", "原生选择器需要 Windows，请手动输入路径", 503)
        desktop = ctypes.windll.user32
        desktop.OpenInputDesktop.restype = wintypes.HANDLE
        desktop.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = desktop.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not handle:
            raise Problem("picker_unavailable", "服务未运行在可交互桌面，请手动输入路径", 503)
        desktop.CloseDesktop(handle)
        if not self._open.acquire(blocking=False):
            raise Problem("picker_busy", "已有目录选择窗口，请先完成或取消", 409)
        try:
            return self._dialog(initial_directory)
        finally:
            self._open.release()

    @staticmethod
    def _dialog(initial_directory):
        import pythoncom
        import win32gui
        from win32com.shell import shell, shellcon

        title = "文件上传平台 · 选择作品目录"

        def show_title(hwnd, event, _value, _context):
            if event == shellcon.BFFM_INITIALIZED:
                win32gui.SetWindowText(hwnd, title)
                win32gui.SendMessage(hwnd, BFFM_SETSELECTIONW, 1, str(initial_directory))
            return 0

        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            try:
                flags = (
                    shellcon.BIF_RETURNONLYFSDIRS
                    | shellcon.BIF_EDITBOX
                    | BIF_NEWDIALOGSTYLE
                    | BIF_NONEWFOLDERBUTTON
                )
                folder, _, _ = shell.SHBrowseForFolder(0, None, title, flags, show_title)
                return shell.SHGetPathFromIDListW(folder) if folder is not None else None
            finally:
                pythoncom.CoUninitialize()
        except (pythoncom.com_error, shell.error) as exc:
            raise Problem("picker_unavailable", "无法打开目录选择器，请手动输入路径", 503) from exc
