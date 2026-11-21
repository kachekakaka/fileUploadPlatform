"""Native setup and status window for the Windows ZIP."""

import argparse
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

from upload_platform.domain.core import Problem
from upload_platform.infrastructure.directory_picker import DirectoryPicker
from upload_platform.portable import ADDRESS, Portable

TITLE = "文件上传平台 · MuMu 便携版"
PICK_RECORDINGS = 101
PICK_DATA = 102
START = 103
STOP = 104
DASHBOARD = 105


def show_window(platform):
    import win32api
    import win32con as c
    import win32gui as gui

    events = SimpleQueue()
    controls = {}
    status = []
    active = False
    hdr = win32api.GetModuleHandle(None)
    signal = c.WM_APP + 1

    def control(kind, text, x, y, width, height, identifier=0, style=0):
        handle = gui.CreateWindowEx(
            0,
            kind,
            text,
            c.WS_CHILD | c.WS_VISIBLE | style,
            x,
            y,
            width,
            height,
            hwnd,
            identifier,
            hdr,
            None,
        )
        gui.SendMessage(handle, c.WM_SETFONT, gui.GetStockObject(17), True)
        return handle

    def notice(text):
        gui.SetWindowText(controls["notice"], text)

    def publish(kind, value):
        events.put((kind, value))
        if gui.IsWindow(hwnd):
            try:
                gui.PostMessage(hwnd, signal, 0, 0)
            except gui.error:
                pass  # The window may close while the background command is finishing.

    def job(body):
        try:
            body()
        except Problem as exc:
            publish("failure", exc.message)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            publish("failure", f"启动或退出失败（{type(exc).__name__}）。请查看日志位置。")

    def input_state():
        for name in ("recordings", "data", PICK_RECORDINGS, PICK_DATA):
            gui.EnableWindow(controls[name], not active and platform.settings() is None)
        for name in (START, STOP):
            gui.EnableWindow(controls[name], not active)

    def do_start(*, confirm):
        nonlocal active
        settings = platform.settings()
        recordings, data = (
            gui.GetWindowText(controls[name]).strip() for name in ("recordings", "data")
        )
        if settings is None:
            if not recordings or not data:
                notice("请选择录像目录和新的数据目录。")
                return
            sessions = Path(recordings) / "sessions"
            if (
                not sessions.is_dir()
                and confirm
                and gui.MessageBox(
                    hwnd,
                    f"首次保存时将创建以下空目录。\n{sessions}\n\n继续保存并启动吗？",
                    TITLE,
                    c.MB_OKCANCEL | c.MB_ICONINFORMATION,
                )
                != c.IDOK
            ):
                return
        active = True
        input_state()
        gui.EnableWindow(controls[DASHBOARD], False)
        notice(f"正在检查设置及后台状态。\n日志位置 {Path(data) / 'logs'}")

        def apply():
            result = platform.start(recordings, data, lambda *values: publish("status", values))
            publish("ready", result)

        Thread(target=job, args=(apply,)).start()

    def event_handler(_hwnd, message, wparam, _lparam):
        nonlocal active
        if message == c.WM_DESTROY:
            gui.PostQuitMessage(0)
            return 0
        if message == c.WM_COMMAND and not active:
            command = wparam & 0xFFFF
            try:
                if command in (PICK_RECORDINGS, PICK_DATA):
                    field = "recordings" if command == PICK_RECORDINGS else "data"
                    label = (
                        "MuMu 录像输出目录" if command == PICK_RECORDINGS else "上传平台数据目录"
                    )
                    current = Path(gui.GetWindowText(controls[field]) or Path.home())
                    chosen = DirectoryPicker().choose(current, title=f"选择{label}", owner=hwnd)
                    if chosen:
                        gui.SetWindowText(controls[field], chosen)
                if command == START:
                    do_start(confirm=True)
                if command == STOP and platform.settings() is not None:
                    active = True
                    input_state()
                    gui.EnableWindow(controls[DASHBOARD], False)

                    def stop():
                        platform.stop(lambda role: publish("stopping", role))
                        publish("stopped", None)

                    Thread(target=job, args=(stop,)).start()
                if command == DASHBOARD:
                    os.startfile(ADDRESS)
            except (Problem, ValueError, OSError) as exc:
                notice(exc.message if isinstance(exc, Problem) else "操作失败，请检查设置和目录。")
            return 0
        if message == signal:
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == "status":
                        for handle, line in zip(status, value, strict=True):
                            gui.SetWindowText(handle, line)
                        continue
                    if kind == "stopping":
                        notice(
                            "正在等待后台上传完成…"
                            if value == "worker"
                            else "正在等待接口上的操作收尾…"
                        )
                        continue
                    active = False
                    input_state()
                    if kind == "ready":
                        build_id, available_accounts, logs = value
                        for handle, line in zip(
                            status,
                            ("API 已就绪", "Worker 心跳正常", f"B 站引擎已就绪 · {build_id}"),
                            strict=True,
                        ):
                            gui.SetWindowText(handle, line)
                        instruction = (
                            "已有可用 B 站账号。"
                            if available_accounts
                            else "首次投稿请从 MuMu 的「自动投稿 → 管理投稿账号」扫码。"
                        )
                        notice(f"MuMu 可以连接  {ADDRESS}\n{instruction}\n日志位置 {logs}")
                        gui.EnableWindow(controls[DASHBOARD], True)
                    elif kind == "stopped":
                        for handle in status:
                            gui.SetWindowText(handle, "")
                        notice("上传平台已退出。")
                    else:
                        notice(value)
            except Empty:
                pass
            except (Problem, OSError):
                notice("无法读取便携版设置，请检查设置文件。")
            return 0
        return gui.DefWindowProc(_hwnd, message, wparam, _lparam)

    window = gui.WNDCLASS()
    window.hInstance = hdr
    window.lpszClassName = "FileUploadPlatformPortable"
    window.lpfnWndProc = event_handler
    window.hCursor = gui.LoadCursor(0, c.IDC_ARROW)
    window.hbrBackground = c.COLOR_BTNFACE + 1
    gui.RegisterClass(window)
    hwnd = gui.CreateWindowEx(
        0,
        window.lpszClassName,
        TITLE,
        c.WS_OVERLAPPED | c.WS_CAPTION | c.WS_SYSMENU | c.WS_MINIMIZEBOX,
        c.CW_USEDEFAULT,
        c.CW_USEDEFAULT,
        720,
        405,
        0,
        0,
        hdr,
        None,
    )
    initial = platform.settings()
    for label, name, pick, default, y in (
        ("MuMu 录像输出目录", "recordings", PICK_RECORDINGS, "", 15),
        ("上传平台数据目录", "data", PICK_DATA, str(platform.default_data), 75),
    ):
        control("STATIC", label, 18, y, 570, 20)
        controls[name] = control(
            "EDIT",
            initial[name] if initial else default,
            18,
            y + 22,
            544,
            24,
            style=c.WS_BORDER | c.ES_AUTOHSCROLL | c.WS_TABSTOP,
        )
        controls[pick] = control("BUTTON", "选择目录…", 572, y + 21, 111, 26, pick, c.WS_TABSTOP)
    for y in (153, 180, 207):
        status.append(control("STATIC", "", 18, y, 670, 22))
    controls["notice"] = control("STATIC", "选择目录后保存并启动。", 18, 239, 670, 72)
    for command, title, x, width in (
        (START, "查看或启动" if initial else "保存并启动", 18, 128),
        (STOP, "退出上传平台", 156, 132),
        (DASHBOARD, "打开面板", 298, 115),
    ):
        controls[command] = control("BUTTON", title, x, 317, width, 28, command, c.WS_TABSTOP)
    gui.EnableWindow(controls[DASHBOARD], False)
    input_state()
    gui.ShowWindow(hwnd, c.SW_SHOW)
    if initial:
        do_start(confirm=False)
    gui.PumpMessages()


def main():
    import win32con
    import win32gui

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        show_window(Portable.for_user(args.bundle))
    except Problem as exc:
        win32gui.MessageBox(0, exc.message, TITLE, win32con.MB_ICONERROR)
    except (OSError, ValueError, KeyError) as exc:
        win32gui.MessageBox(
            0, f"便携版窗口无法启动（{type(exc).__name__}）。", TITLE, win32con.MB_ICONERROR
        )


if __name__ == "__main__":
    main()
