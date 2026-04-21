"""搜索 Feeds，对应 Go xiaohongshu/search.go。"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoFeedsError
from .human import sleep_random
from .selectors import FILTER_BUTTON, FILTER_PANEL
from .types import Feed, FilterOption
from .urls import make_search_url

logger = logging.getLogger(__name__)

# 筛选选项映射表：{分组标题: [选项文本, ...]}
_FILTER_OPTIONS: dict[str, list[str]] = {
    "排序依据": ["综合", "最新", "最多点赞", "最多评论", "最多收藏"],
    "笔记类型": ["不限", "视频", "图文"],
    "发布时间": ["不限", "一天内", "一周内", "半年内"],
    "搜索范围": ["不限", "已看过", "未看过", "已关注"],
    "位置距离": ["不限", "同城", "附近"],
}

# 从 __INITIAL_STATE__ 提取搜索结果的 JS
_EXTRACT_SEARCH_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.search &&
        window.__INITIAL_STATE__.search.feeds) {
        const feeds = window.__INITIAL_STATE__.search.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
})()
"""


def _find_internal_option(group_title: str, text: str) -> tuple[str, str]:
    """查找内部筛选选项。

    Returns:
        (group_title, option_text)

    Raises:
        ValueError: 未找到匹配的选项。
    """
    options = _FILTER_OPTIONS.get(group_title)
    if not options:
        raise ValueError(f"筛选组 {group_title} 不存在")

    for option_text in options:
        if option_text == text:
            return group_title, option_text

    raise ValueError(f"在筛选组 {group_title} 中未找到 '{text}'，有效值: {options}")


def _convert_filters(filter_opt: FilterOption) -> list[tuple[str, str]]:
    """将 FilterOption 转换为内部 (group_title, option_text) 列表。"""
    result: list[tuple[str, str]] = []

    if filter_opt.sort_by:
        result.append(_find_internal_option("排序依据", filter_opt.sort_by))
    if filter_opt.note_type:
        result.append(_find_internal_option("笔记类型", filter_opt.note_type))
    if filter_opt.publish_time:
        result.append(_find_internal_option("发布时间", filter_opt.publish_time))
    if filter_opt.search_scope:
        result.append(_find_internal_option("搜索范围", filter_opt.search_scope))
    if filter_opt.location:
        result.append(_find_internal_option("位置距离", filter_opt.location))

    return result


def search_feeds(
    page: Page,
    keyword: str,
    filter_option: FilterOption | None = None,
) -> list[Feed]:
    """搜索 Feeds。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        filter_option: 可选筛选条件。

    Raises:
        NoFeedsError: 没有捕获到搜索结果。
        ValueError: 筛选选项无效。
    """
    search_url = make_search_url(keyword)
    page.navigate(search_url)
    page.wait_for_load()
    page.wait_dom_stable()

    # 等待 __INITIAL_STATE__ 初始化
    _wait_for_initial_state(page)

    # 应用筛选条件
    if filter_option:
        internal_filters = _convert_filters(filter_option)
        if internal_filters:
            _apply_filters(page, internal_filters)

    # 提取搜索结果
    result = page.evaluate(_EXTRACT_SEARCH_JS)
    if not result:
        raise NoFeedsError()

    feeds_data = json.loads(result)
    return [Feed.from_dict(f) for f in feeds_data]


def _wait_for_initial_state(page: Page, timeout: float = 10.0) -> None:
    """等待 __INITIAL_STATE__ 就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = page.evaluate("window.__INITIAL_STATE__ !== undefined")
        if ready:
            return
        time.sleep(0.5)
    logger.warning("等待 __INITIAL_STATE__ 超时")


def _apply_filters(page: Page, filters: list[tuple[str, str]]) -> None:
    """应用筛选条件。"""
    # 点击筛选按钮（当前小红书页面结构下 click 比 hover 更稳定）
    page.click_element(FILTER_BUTTON)

    # 等待筛选分组出现
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if page.has_element(FILTER_PANEL):
            break
        sleep_random(300, 600)

    for group_title, option_text in filters:
        clicked = page.evaluate(
            f"""
            (() => {{
                const groups = Array.from(document.querySelectorAll('div.filters'));
                for (const group of groups) {{
                    const titleEl = group.querySelector(':scope > span');
                    const title = titleEl ? (titleEl.textContent || '').trim() : '';
                    if (title !== {json.dumps(group_title)}) continue;
                    const tags = Array.from(group.querySelectorAll('div.tags, button, span'));
                    for (const el of tags) {{
                        const text = (el.textContent || '').trim();
                        if (text === {json.dumps(option_text)}) {{
                            el.click();
                            return true;
                        }}
                    }}
                }}
                return false;
            }})()
            """
        )
        if not clicked:
            raise ValueError(f"未找到筛选项: {{group_title}} -> {{option_text}}")
        sleep_random(300, 600)

    page.wait_dom_stable()
    _wait_for_initial_state(page)
