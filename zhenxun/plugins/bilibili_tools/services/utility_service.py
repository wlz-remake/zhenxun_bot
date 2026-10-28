import asyncio

# 新版 nonebot_plugin_htmlrender 顶层不再导出 get_browser，
# 需从 browser 子模块（向后兼容入口）导入
from nonebot_plugin_htmlrender.browser import get_browser

from zhenxun.services.log import logger

from ..config import (
    PLUGIN_TEMP_DIR,
    SCREENSHOT_ELEMENT_OPUS_ALTERNATIVES,
    SCREENSHOT_TIMEOUT,
)
from ..utils.exceptions import ScreenshotError


class ScreenshotService:
    """截图服务"""

    @staticmethod
    async def _cleanup_page_for_screenshot(
        page,
        target_selector: str,
        login_popup_selectors: list[str],
        hide_selectors: list[str],
    ) -> None:
        """执行页面清理JS：移除登录弹层、隐藏顶栏及页面装饰元素

        页面顶栏/登录浮窗等可能在 domcontentloaded 之后由脚本延迟挂载，
        因此本方法会在选择器匹配阶段和元素截图前各调用一次。
        """
        try:
            js_code = """
            (function() {
                const loginSelectors = %s;
                loginSelectors.forEach(selector => {
                    const elements = document.querySelectorAll(selector);
                    elements.forEach(el => el && el.remove());
                });
                const headerSelectors = %s;
                headerSelectors.forEach(selector => {
                    const elements = document.querySelectorAll(selector);
                    elements.forEach(el => el && (el.style.display = 'none'));
                });
                const floatingElements = document.querySelectorAll('.fixed-element, .floating, .popup, .modal, [style*="position: fixed"]');
                floatingElements.forEach(el => {
                    if (el && !el.matches('%s')) {
                        el.style.display = 'none';
                    }
                });
                return 'Attempted to clean up page for screenshot';
            })();
            """ % (
                str(login_popup_selectors),
                str(hide_selectors),
                target_selector,
            )

            result = await page.evaluate(js_code)
            logger.debug(f"执行页面清理 JS 结果: {result}", "B站截图")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"移除/隐藏元素 JS 执行失败: {e}", "B站截图")

    @staticmethod
    async def _wait_element_images(page, selector: str) -> None:
        """滚动页面触发懒加载，并等待元素内图片加载完成（有界等待，失败不影响截图）"""
        try:
            await page.evaluate(
                """
                async () => {
                    const step = Math.max((window.innerHeight || 800) - 100, 300);
                    const total = document.documentElement.scrollHeight;
                    let y = 0;
                    for (let i = 0; i < 20 && y < total; i++) {
                        y += step;
                        window.scrollTo(0, y);
                        await new Promise((resolve) => setTimeout(resolve, 100));
                    }
                    window.scrollTo(0, 0);
                }
                """
            )
            await page.wait_for_function(
                """
                (selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return true;
                    return Array.from(el.querySelectorAll("img")).every(
                        (img) => img.complete && (img.naturalWidth > 0 || !img.currentSrc)
                    );
                }
                """,
                arg=selector,
                timeout=8000,
            )
        except Exception as e:
            logger.warning(
                f"等待元素内图片加载完成超时或失败，继续截图: {e}", "B站截图"
            )

    @staticmethod
    async def take_screenshot(url: str, element_selectors: str | list[str]) -> bytes:
        """获取网页元素的截图（已注入Cookie），支持多选择器"""

        # 确保element_selectors是列表
        if isinstance(element_selectors, str):
            element_selectors = [element_selectors]

        browser = await get_browser()
        if not browser:
            raise ScreenshotError("Browser is not available.")

        context = None
        page = None
        screenshot_bytes = None
        used_selector = None
        all_selectors_failed = False

        try:
            from ..sub_utils import build_bilibili_playwright_cookies

            playwright_cookies = build_bilibili_playwright_cookies()
            if playwright_cookies:
                logger.debug(
                    f"已加载 {len(playwright_cookies)} 个B站Cookies用于截图", "B站截图"
                )
            else:
                logger.warning("未找到有效的B站登录凭证，截图可能会失败", "B站截图")

            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                java_script_enabled=True,
            )

            if playwright_cookies:
                await context.add_cookies(playwright_cookies)  # type: ignore

            page = await context.new_page()

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=SCREENSHOT_TIMEOUT * 1000,
            )

            login_popup_selectors = [
                ".bili-mini-login-container",
                ".login-panel",
                ".unlogin-popover",
                ".login-tip",
            ]
            # 需要隐藏的页面元素：顶栏/登录弹层，以及新版 /opus/ 页面侧边的
            # 目录面板和右侧互动工具栏（它们不属于动态正文）
            hide_selectors = [
                "#bili-header-m",
                ".fixed-header",
                ".bili-header__bar",
                ".opus-toc",
                ".right-sidebar-wrap",
            ]

            # 尝试多个选择器
            element = None
            for selector in element_selectors:
                try:
                    logger.debug(f"尝试使用选择器: '{selector}'", "B站截图")

                    # 执行页面清理JS
                    await ScreenshotService._cleanup_page_for_screenshot(
                        page, selector, login_popup_selectors, hide_selectors
                    )

                    # 尝试query_selector
                    element = await page.query_selector(selector)
                    if not element:
                        logger.debug(
                            f"初始 query_selector 未找到 '{selector}'，尝试 wait_for_selector"
                        )
                        try:
                            wait_timeout = 10000
                            logger.debug(
                                f"使用 wait_for_selector 等待: '{selector}', 超时: {wait_timeout}ms"
                            )
                            element = await page.wait_for_selector(
                                selector, timeout=wait_timeout, state="visible"
                            )
                            logger.debug(f"wait_for_selector 成功找到 '{selector}'")
                        except Exception as e:
                            logger.warning(f"等待选择器 '{selector}' 超时或失败: {e}")

                    if element:
                        used_selector = selector
                        logger.info(
                            f"成功找到元素，使用选择器: '{used_selector}'", "B站截图"
                        )
                        break
                except Exception as e:
                    logger.warning(f"使用选择器 '{selector}' 时出错: {e}", "B站截图")
                    continue

            if element:
                # 触发懒加载并等待元素内图片加载完成，避免截图出现裂图或空白
                await ScreenshotService._wait_element_images(page, used_selector)

                # 图片等待期间页面可能延迟挂载顶栏/登录浮窗，截图前再清理一次
                await ScreenshotService._cleanup_page_for_screenshot(
                    page, used_selector, login_popup_selectors, hide_selectors
                )
                await asyncio.sleep(0.3)
                screenshot_bytes = await element.screenshot(
                    type="png",
                    timeout=SCREENSHOT_TIMEOUT * 500,
                    animations="disabled",
                )
                logger.info(f"截图成功: {url}, element: {used_selector}")
            else:
                all_selectors_failed = True
                logger.error(f"所有选择器都失败: {element_selectors}")
                try:
                    debug_path = PLUGIN_TEMP_DIR / "debug_screenshot.png"
                    await page.screenshot(path=str(debug_path), full_page=True)
                    logger.error(f"已保存当前页面截图到 {debug_path} 用于调试。")
                except Exception as ss_err:
                    logger.error(f"保存调试截图失败: {ss_err}")

                # 兜底：降级为整页截图，尽量仍返回内容而不是直接报错
                try:
                    await asyncio.sleep(0.5)
                    screenshot_bytes = await page.screenshot(
                        type="png",
                        full_page=True,
                        timeout=SCREENSHOT_TIMEOUT * 1000,
                        animations="disabled",
                    )
                    logger.warning(f"所有元素选择器失败，已降级为整页截图: {url}")
                except Exception as fallback_err:
                    logger.error(f"整页截图兜底失败: {fallback_err}")

        except Exception as e:
            logger.error(f"截图失败 for {url}: {e}")
            raise ScreenshotError(
                f"截图失败: {e}",
                cause=e,
                context={"url": url, "selectors": element_selectors},
            )
        finally:
            if page:
                await page.close()
            if context:
                await context.close()

        if all_selectors_failed and not screenshot_bytes:
            raise ScreenshotError(f"所有选择器都失败: {element_selectors}")
        if not screenshot_bytes:
            raise ScreenshotError(f"未能获取截图字节: {url}")
        return screenshot_bytes

    @staticmethod
    async def get_opus_screenshot(opus_id: str, url: str) -> bytes:
        """获取动态截图"""
        logger.debug(f"获取动态截图: {opus_id}, URL: {url}", "B站解析")

        selectors = SCREENSHOT_ELEMENT_OPUS_ALTERNATIVES
        logger.debug(f"使用动态截图选择器列表: {selectors}")

        if "t.bilibili.com" in url or "m.bilibili.com" in url:
            url = f"https://www.bilibili.com/opus/{opus_id}"
        return await ScreenshotService.take_screenshot(url, selectors)
