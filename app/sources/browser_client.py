"""Optional Playwright client for public JS-rendered pages (never for captcha bypass)."""

from __future__ import annotations

from loguru import logger


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed or the browser binary is missing."""


class BrowserFetchError(RuntimeError):
    """Navigation or rendering failed."""


class BrowserClient:
    """Lazy, lazily-installed headless Chromium used only by explicitly configured sources."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30000, proxy: str | None = None) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._context = None

    async def _ensure(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise BrowserUnavailable(
                "Playwright не установлен. Выполните: pip install playwright && playwright install chromium"
            ) from exc
        try:
            self._playwright = await async_playwright().start()
            launch_args: dict[str, object] = {"headless": self.headless}
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}
            self._browser = await self._playwright.chromium.launch(**launch_args)
            self._context = await self._browser.new_context(
                locale="ru-RU",
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        except BrowserUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - environment specific
            raise BrowserUnavailable(f"Не удалось запустить Chromium: {exc}") from exc

    async def fetch_html(self, url: str, wait_selector: str | None = None) -> str:
        await self._ensure()
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=self.timeout_ms)
            else:
                await page.wait_for_timeout(1500)
            return await page.content()
        except Exception as exc:
            logger.debug(f"Браузерный запрос не удался для {url}: {exc}")
            raise BrowserFetchError(str(exc)) from exc
        finally:
            await page.close()

    async def aclose(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # pragma: no cover - best effort cleanup
            pass
        finally:
            self._context = None
            self._browser = None
            self._playwright = None
