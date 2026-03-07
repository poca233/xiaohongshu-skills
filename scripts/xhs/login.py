"""登录管理，对应 Go xiaohongshu/login.go。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

_QR_DIR = os.path.join(tempfile.gettempdir(), "xhs")
_QR_FILE = os.path.join(_QR_DIR, "login_qrcode.png")
_QR_BORDER = 16  # 截图时在元素四周留白的像素数

from .cdp import Page
from .errors import RateLimitError
from .human import sleep_random
from .selectors import (
    AGREE_CHECKBOX,
    AGREE_CHECKBOX_CHECKED,
    CODE_INPUT,
    GET_CODE_BUTTON,
    LOGIN_CONTAINER,
    LOGIN_ERR_MSG,
    LOGIN_STATUS,
    LOGOUT_MENU_ITEM,
    LOGOUT_MORE_BUTTON,
    PHONE_INPUT,
    PHONE_LOGIN_SUBMIT,
    QRCODE_IMG,
)
from .urls import EXPLORE_URL

logger = logging.getLogger(__name__)


def _wait_for_auth_ui(page: Page, timeout: float = 8.0) -> None:
    """等待认证 UI 出现，替代固定延迟。

    轮询直到登录状态指示器或登录容器出现为止，避免无谓等待。
    超时后静默返回，由调用方自行处理元素不存在的情况。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.has_element(LOGIN_STATUS) or page.has_element(LOGIN_CONTAINER):
            return
        time.sleep(0.2)


def check_login_status(page: Page) -> bool:
    """检查登录状态。

    Returns:
        True 已登录，False 未登录。
    """
    page.navigate(EXPLORE_URL)
    page.wait_for_load()
    _wait_for_auth_ui(page)

    return page.has_element(LOGIN_STATUS)


def fetch_qrcode(page: Page) -> tuple[bytes, bool]:
    """截取登录二维码图片（CDP 元素截图）。

    Returns:
        (png_bytes, already_logged_in)
        - 如果已登录，返回 (b"", True)
        - 如果未登录，返回 (png_bytes, False)
    """
    page.navigate(EXPLORE_URL)
    page.wait_for_load()
    _wait_for_auth_ui(page)

    if page.has_element(LOGIN_STATUS):
        return b"", True

    # 等待 img.qrcode-img 出现，用浏览器 Canvas 加白边后导出 PNG base64
    page.wait_for_element(QRCODE_IMG, timeout=10.0)
    b64 = page.evaluate(
        f"""
        (() => {{
            const img = document.querySelector({json.dumps(QRCODE_IMG)});
            if (!img) return null;
            const p = {_QR_BORDER};
            const c = document.createElement('canvas');
            c.width  = img.naturalWidth  + p * 2;
            c.height = img.naturalHeight + p * 2;
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, c.width, c.height);
            ctx.drawImage(img, p, p);
            return c.toDataURL('image/png').split(',')[1];
        }})()
        """
    )
    if not b64:
        raise RuntimeError("二维码 Canvas 导出失败")
    import base64
    png_bytes = base64.b64decode(b64)

    return png_bytes, False


def save_qrcode_to_file(png_bytes: bytes) -> str:
    """将二维码 PNG 字节保存到临时文件，返回文件路径。

    Args:
        png_bytes: CDP 截图返回的 PNG 字节。

    Returns:
        file_path: 保存的 PNG 文件绝对路径。
    """
    os.makedirs(_QR_DIR, exist_ok=True)
    with open(_QR_FILE, "wb") as f:
        f.write(png_bytes)
    logger.info("二维码已保存: %s", _QR_FILE)
    return _QR_FILE


def send_phone_code(page: Page, phone: str) -> bool:
    """填写手机号并发送短信验证码。

    适用于无界面服务器场景，全程通过 CDP 操作，无需扫码。

    Args:
        page: CDP 页面对象。
        phone: 手机号（不含国家码，如 13800138000）。

    Returns:
        True 验证码已发送，False 已登录（无需再登录）。

    Raises:
        RuntimeError: 找不到登录表单或手机号输入框。
    """
    page.navigate(EXPLORE_URL)
    page.wait_for_load()
    sleep_random(1500, 2500)

    if page.has_element(LOGIN_STATUS):
        return False

    # 等待登录弹窗出现
    page.wait_for_element(LOGIN_CONTAINER, timeout=15.0)
    sleep_random(500, 800)

    # 点击手机号输入框并逐字输入
    page.click_element(PHONE_INPUT)
    sleep_random(200, 400)
    page.type_text(phone, delay_ms=80)
    sleep_random(500, 800)

    # 先勾选用户协议，再点获取验证码
    if not page.has_element(AGREE_CHECKBOX_CHECKED):
        page.click_element(AGREE_CHECKBOX)
        sleep_random(300, 600)

    # 点击"获取验证码"
    page.click_element(GET_CODE_BUTTON)
    sleep_random(2000, 2500)

    # 检测按钮是否变为倒计时（成功发送后按钮文字会包含数字秒数）
    btn_text = page.get_element_text(GET_CODE_BUTTON) or ""
    if not any(ch.isdigit() for ch in btn_text):
        raise RateLimitError()

    logger.info("验证码已发送至 %s", phone[:3] + "****" + phone[-4:])
    return True


def submit_phone_code(page: Page, code: str) -> bool:
    """填写短信验证码并提交登录。

    Args:
        page: CDP 页面对象。
        code: 收到的短信验证码。

    Returns:
        True 登录成功，False 失败（超时或验证码错误）。
    """
    # 点击验证码输入框并逐字输入
    page.click_element(CODE_INPUT)
    sleep_random(300, 500)
    page.type_text(code, delay_ms=100)
    sleep_random(500, 800)

    # 点击登录按钮
    page.click_element(PHONE_LOGIN_SUBMIT)
    sleep_random(1000, 2000)

    # 检查是否有错误提示
    err = page.get_element_text(LOGIN_ERR_MSG)
    if err and err.strip():
        logger.warning("登录失败: %s", err.strip())
        return False

    return wait_for_login(page, timeout=30.0)


def logout(page: Page) -> bool:
    """通过页面 UI 退出登录（点击"更多"→"退出登录"）。

    Args:
        page: CDP 页面对象。

    Returns:
        True 退出成功，False 未登录或操作失败。
    """
    page.navigate(EXPLORE_URL)
    page.wait_for_load()
    sleep_random(800, 1500)

    if not page.has_element(LOGIN_STATUS):
        logger.info("当前未登录，无需退出")
        return False

    # 点击"更多"按钮展开菜单
    page.click_element(LOGOUT_MORE_BUTTON)
    sleep_random(500, 800)

    # 等待退出菜单项出现并点击
    page.wait_for_element(LOGOUT_MENU_ITEM, timeout=5.0)
    page.click_element(LOGOUT_MENU_ITEM)
    sleep_random(1000, 1500)

    logger.info("已退出登录")
    return True


def wait_for_login(page: Page, timeout: float = 120.0) -> bool:
    """等待扫码登录完成。

    Args:
        page: CDP 页面对象。
        timeout: 超时时间（秒）。

    Returns:
        True 登录成功，False 超时。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.has_element(LOGIN_STATUS):
            logger.info("登录成功")
            return True
        time.sleep(0.5)
    return False
