"""The Windows tray is the launcher UI; browser windows do not own its lifetime."""

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .launcher import DesktopLauncher


def icon_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=15, fill="#182825")
    draw.line((20, 17, 20, 46, 45, 46), fill="#a9d4bd", width=7)
    draw.ellipse((39, 15, 48, 24), fill="#a9d4bd")
    return image


class WindowsTray:
    def __init__(self):
        self._icon = None
        self._stopped = threading.Event()

    def run(self, controller: "DesktopLauncher") -> None:
        import pystray

        def notify_failure(message: str) -> None:
            if self._icon is not None:
                self._icon.notify(message, "LingNexus")

        def open_browser(icon, item):
            try:
                controller.open_browser()
            except Exception:  # noqa: BLE001 - browser boundary, never display secret-bearing URLs
                notify_failure("无法打开浏览器，请检查 Windows 的默认浏览器设置。")

        def background_text(item):
            enabled = controller.background_enabled()
            if enabled is None:
                return "后台活动（当前不可用）"
            return "暂停后台活动" if enabled else "恢复后台活动"

        def toggle_background(icon, item):
            enabled = controller.background_enabled()
            if enabled is None or not controller.set_background_enabled(not enabled):
                notify_failure("后台活动设置暂时不可用。")
            icon.update_menu()

        def toggle_startup(icon, item):
            try:
                controller.startup.set_enabled(not controller.startup.enabled())
            except OSError:
                notify_failure("无法更改当前用户的 Windows 登录启动设置。")
            icon.update_menu()

        def quit_app(icon, item):
            controller.request_quit()

        menu = pystray.Menu(
            pystray.MenuItem("打开 LingNexus", open_browser, default=True),
            pystray.MenuItem(
                background_text,
                toggle_background,
                # pystray documents dynamic enabled callbacks, but annotates it as bool.
                enabled=lambda item: controller.background_enabled() is not None,  # type: ignore[arg-type]
            ),
            pystray.MenuItem(
                "登录 Windows 时启动",
                toggle_startup,
                checked=lambda item: controller.startup.enabled(),
                enabled=controller.startup.available,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 LingNexus", quit_app),
        )
        self._icon = pystray.Icon("LingNexus", icon_image(), "LingNexus", menu)

        def setup(icon):
            if self._stopped.is_set():
                icon.stop()
            else:
                icon.visible = True

        try:
            self._icon.run(setup=setup)
        finally:
            self._icon = None

    def stop(self) -> None:
        self._stopped.set()
        icon = self._icon
        if icon is not None:
            icon.stop()
