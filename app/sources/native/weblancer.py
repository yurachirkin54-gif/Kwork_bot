"""Weblancer adapter.

The catalogue is a Next.js application and the task list is rendered client-side,
so the plain HTTP HTML has no cards. The adapter therefore supports
`use_browser: true` (Playwright, public pages only) and reports a clear
parse_error otherwise instead of pretending to work.
"""

from __future__ import annotations

from app.sources.base import SourceParseError
from app.sources.generic_html import GenericHTMLAdapter


class WeblancerAdapter(GenericHTMLAdapter):
    kind = "weblancer"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if not self.listing_url:
            self.listing_url = "https://www.weblancer.net/freelance/"
        selectors = self.config.selectors
        if not selectors.item:
            selectors.item = "article, div[class*='job-card'], div[class*='job-item'], li[class*='job']"
        if not selectors.title:
            selectors.title = "a[class*='title'], h2 a, h3 a"
        if not selectors.url:
            selectors.url = "a[class*='title']@href, h2 a@href, h3 a@href"
        if not selectors.description:
            selectors.description = "[class*='desc'], p"
        if not selectors.price:
            selectors.price = "[class*='price'], [class*='budget'], [class*='amount']"
        if not selectors.proposals:
            selectors.proposals = "[class*='response'], [class*='proposal'], [class*='bid']"
        if not selectors.published_at:
            selectors.published_at = "time@datetime, [class*='date'], [class*='time']"
        if not selectors.status:
            selectors.status = "[class*='status'], [class*='badge']"
        self.selectors = selectors

    async def fetch_listing(self):
        try:
            return await super().fetch_listing()
        except SourceParseError as exc:
            hint = (
                " Лента Weblancer рендерится через JavaScript: включите `use_browser: true` "
                "для этого источника (нужен playwright) или укажите актуальные CSS-селекторы."
            )
            raise SourceParseError(f"{exc}.{hint}", source=self.name) from exc
