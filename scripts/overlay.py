"""把 GlassDesk 变成一个常驻的小悬浮窗。

做法（**零新增依赖**）：

1. 检查后端是否在跑，没跑就用隐藏窗口把它拉起来；
2. 用 Edge 的 ``--app`` 模式打开一个**没有地址栏、没有标签页**的小窗口；
3. 用 Windows API（``SetWindowPos``）把它设为**置顶**，浮在邮件和网页上面。

为什么用 Edge 而不是内嵌浏览器：系统自带，不用下载任何东西，
``--app`` 模式打开后就是一个独立窗口，看起来就是个小应用。
代价是拖到屏幕边缘不会自动吸附——这个只有自绘窗口才能做到。

用法::

    .venv\\Scripts\\pythonw.exe scripts/overlay.py          # 无控制台窗口
    .venv\\Scripts\\python.exe  scripts/overlay.py --debug  # 看日志
    .venv\\Scripts\\python.exe  scripts/overlay.py --size 460x720
    .venv\\Scripts\\python.exe  scripts/overlay.py --no-topmost
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

APP_URL = "http://127.0.0.1:8000"
WINDOW_TITLE_HINT = "GlassDesk"
PROFILE_DIR = ROOT / "outputs" / "edge-profile"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

user32 = ctypes.windll.user32

# ----------------------------------------------------------------------
# 必须显式声明参数类型。
#
# HWND_TOPMOST 是 -1（一个伪句柄）。不声明 argtypes 时 ctypes 把它按 32 位 int 传，
# 64 位下被零扩展成 0x00000000FFFFFFFF 而不是全 1，SetWindowPos 于是
# **静默地不置顶**——返回成功、窗口位置也对，就是不上浮。
# 这个坑很费时间：没有任何错误提示，只能靠读回 WS_EX_TOPMOST 才发现。
# ----------------------------------------------------------------------
_set_window_pos = user32.SetWindowPos
_set_window_pos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_set_window_pos.restype = wt.BOOL

_get_window_long = user32.GetWindowLongPtrW
_get_window_long.argtypes = [wt.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_void_p

_ALL_ONES = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
HWND_TOPMOST = wt.HWND(_ALL_ONES)      # (HWND)-1
HWND_NOTOPMOST = wt.HWND(0)
GWLP_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

#: 浏览器主窗口的类名。Edge 和 Chrome 都是这个。
#: 只按标题匹配会抓到 160x28 的隐藏辅助窗口（实测踩过），必须加类名和尺寸过滤。
BROWSER_CLASS = "Chrome_WidgetWin_1"
MIN_WINDOW_SIDE = 200


def log(message: str, debug: bool = True) -> None:
    if debug:
        print(message, flush=True)


# ======================================================================
# 后端
# ======================================================================
def server_alive(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_server() -> subprocess.Popen | None:
    """用当前解释器起后端。启动约 7 秒（含模型预热）。"""
    if server_alive():
        return None
    serve = ROOT / "scripts" / "serve.py"
    creation = 0x08000000  # CREATE_NO_WINDOW，避免弹出黑框
    process = subprocess.Popen(
        [sys.executable, str(serve), "--port", "8000"],
        cwd=str(ROOT), creationflags=creation,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return process


def wait_for_server(seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if server_alive():
            return True
        time.sleep(0.6)
    return False


# ======================================================================
# 窗口
# ======================================================================
def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _window_size(hwnd: int) -> tuple[int, int]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def find_windows(keyword: str = WINDOW_TITLE_HINT) -> list[int]:
    """按标题 + 类名 + 尺寸找所有匹配的浏览器主窗口。

    只按标题匹配不行：Edge 会创建若干标题里也带应用名的隐藏辅助窗口
    （实测抓到一个 160x28、坐标 -32000 的），置顶它会毫无效果。
    所以必须同时验收类名、可见性、以及窗口得像个正常窗口那么大。
    """

    def callback(hwnd: int, _param) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if keyword.lower() not in buffer.value.lower():
            return True
        if _class_name(hwnd) != BROWSER_CLASS:
            return True
        width, height = _window_size(hwnd)
        if width < MIN_WINDOW_SIDE or height < MIN_WINDOW_SIDE:
            return True
        found.append(hwnd)
        return True      # 继续枚举，收集全部

    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(callback)
    user32.EnumWindows(enum_proc, 0)
    return found


def apply_window(hwnd: int, x: int | None = None, y: int | None = None,
                 width: int | None = None, height: int | None = None,
                 topmost: bool = True) -> bool:
    """一次性设置位置、尺寸与层级。

    为什么要自己设尺寸：**Edge 会记住 profile 里上次的窗口大小，
    并忽略命令行传的 --window-size**。实测过：参数写 420x660，
    实际开出来 1003x661（上一次会话的尺寸）。所以必须在窗口出现后自己设。

    位置、尺寸、置顶放在同一个 SetWindowPos 里：分两次调用会产生可见跳动，
    而且第二次若带 SWP_NOZORDER 会把刚设好的置顶状态搞乱。
    """
    flags = SWP_SHOWWINDOW
    if x is None or y is None:
        flags |= SWP_NOMOVE
    if width is None or height is None:
        flags |= SWP_NOSIZE
    return bool(_set_window_pos(
        wt.HWND(hwnd), HWND_TOPMOST if topmost else HWND_NOTOPMOST,
        x or 0, y or 0, width or 0, height or 0, flags,
    ))


def is_topmost(hwnd: int) -> bool:
    """读回 WS_EX_TOPMOST 自检。

    SetWindowPos 的返回值**不能证明置顶成功**——参数传错时它照样返回非零。
    所以必须读扩展样式确认，否则会出现"日志说已置顶、实际没置顶"。
    """
    style = _get_window_long(wt.HWND(hwnd), GWLP_EXSTYLE) or 0
    return bool(style & WS_EX_TOPMOST)


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def window_alive(hwnd: int) -> bool:
    return bool(user32.IsWindow(wt.HWND(hwnd)))


# ======================================================================
def parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"尺寸格式应为 420x660，收到 {text!r}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassDesk 悬浮窗")
    parser.add_argument("--size", type=parse_size, default=(420, 660), help="WxH，默认 420x660")
    parser.add_argument("--position", default="right", choices=["right", "left", "center"])
    parser.add_argument("--no-topmost", action="store_true", help="不置顶（普通窗口）")
    parser.add_argument("--keep-server", action="store_true", help="关闭时不停后端")
    parser.add_argument("--question", default="", help="打开后自动提问（演示用）")
    parser.add_argument("--debug", action="store_true", help="打印日志")
    args = parser.parse_args()
    debug = args.debug

    edge = next((p for p in EDGE_CANDIDATES if Path(p).is_file()), None)
    if edge is None:
        print("找不到 Edge 或 Chrome，无法创建应用窗口。", file=sys.stderr)
        return 1

    log("检查后端…", debug)
    server = start_server()
    if not wait_for_server():
        print("后端 90 秒内没有起来，看看 scripts/serve.py 是不是报错了。", file=sys.stderr)
        if server:
            server.terminate()
        return 1
    log("后端就绪。", debug)

    width, height = args.size
    screen_w, screen_h = screen_size()
    margin = 40
    x = {"right": screen_w - width - margin,
         "left": margin,
         "center": (screen_w - width) // 2}[args.position]
    y = max(margin, (screen_h - height) // 2 - 60)

    app_url = APP_URL
    if args.question:
        from urllib.parse import quote
        app_url = f"{APP_URL}/?q={quote(args.question)}"

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        str(edge),
        f"--app={app_url}",   # 带 ?q= 时打开即问，方便演示
        f"--window-size={width},{height}",
        f"--window-position={x},{y}",
        f"--user-data-dir={PROFILE_DIR}",   # 独立配置目录：不并入日常浏览器窗口
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2",
    ]
    # 启动前先记下已经存在的同标题窗口。
    # 不能只按标题匹配：用户自己的 Edge 里如果开着一个 GlassDesk 标签页，
    # 就会被误抓——实测把用户整个浏览器窗口设成了 516x660 还置顶了。
    # 只接受"启动之后新出现的"窗口。
    preexisting = set(find_windows())
    if preexisting:
        log(f"启动前已存在 {len(preexisting)} 个同标题窗口，将忽略", debug)

    log("打开悬浮窗…", debug)
    window_process = subprocess.Popen(command)

    # 主循环：**每轮重新找窗口**。
    #
    # 不能缓存句柄后一直用。Edge 在启动过程中会替换窗口（先一个占位窗口，
    # 加载完再换真正的），我们现在置顶的那个句柄会失效；
    # 更糟的是循环若以"句柄死掉"为退出条件，就会直接结束，
    # 于是新窗口从没被置顶过——现象是"日志说已置顶，实际不浮"。
    positioned = False
    seen_any = False
    misses = 0
    failures = 0
    appear_deadline = time.time() + 40   # 从启动算起，40 秒内必须出现窗口
    try:
        while True:
            fresh = [h for h in find_windows() if h not in preexisting]
            current = fresh[0] if fresh else 0
            if current:
                seen_any = True
                misses = 0
                if not positioned:
                    apply_window(current, x, y, width, height, topmost=not args.no_topmost)
                    positioned = True
                    if args.no_topmost:
                        log(f"窗口就绪（未置顶）：{current}", debug)
                    elif is_topmost(current):
                        log(f"窗口就绪并已置顶：{current}", debug)
                    else:
                        print("警告：置顶未生效（WS_EX_TOPMOST 未设置）", file=sys.stderr)
                elif not args.no_topmost and not is_topmost(current):
                    # 换了新窗口，或层级被别人抢走 —— 重新压上去
                    apply_window(current, topmost=True)
                    log(f"重新置顶：{current}", debug)
            else:
                if seen_any:
                    # Edge 在加载过程中会短暂销毁并重建窗口，中间有 1–2 秒找不到。
                    # 只失手一次就退出是错的（实测会导致"刚开就退出"）。
                    # 连续 6 秒都找不到，才认为用户真的关掉了。
                    misses += 1
                    if misses >= 4:
                        log("窗口已关闭，退出。", debug)
                        break
                elif time.time() > appear_deadline:
                    print("窗口没有出现（可能被浏览器策略拦了）。", file=sys.stderr)
                    failures = 1
                    break
            time.sleep(1.5)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.keep_server and server is not None:
            log("关闭后端…", debug)
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
        if window_process.poll() is None:
            window_process.terminate()
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
