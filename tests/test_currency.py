"""Currency conversion: CBR parsing, caching, fallbacks and enrichment."""

from __future__ import annotations

import httpx
import respx

from app.core.currency import (
    CBR_URL,
    OPEN_ER_URL,
    FALLBACK_RATES_TO_RUB,
    CurrencyConverter,
    convert_with_rates,
    parse_cbr_xml,
    parse_open_er,
)
from app.models import utcnow

CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?>'
    "<ValCurs><Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>92,5000</Value></Valute>"
    "<Valute><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>100,1000</Value></Valute>"
    "<Valute><CharCode>KZT</CharCode><Nominal>100</Nominal><Value>19,5000</Value></Valute></ValCurs>"
)


def test_parse_cbr_xml_with_declaration() -> None:
    rates = parse_cbr_xml(CBR_XML)
    assert round(rates["USD"], 2) == 92.5
    assert round(rates["KZT"], 4) == 0.195
    assert rates["RUB"] == 1.0


def test_parse_open_er() -> None:
    rates = parse_open_er({"rates": {"RUB": 90.0, "USD": 1.0, "EUR": 0.9}})
    assert round(rates["USD"], 2) == 90.0
    assert round(rates["EUR"], 2) == 100.0


def test_convert_with_rates() -> None:
    rates = {"RUB": 1.0, "USD": 90.0, "EUR": 100.0}
    assert convert_with_rates(100, "USD", "RUB", rates) == 9000
    assert convert_with_rates(9000, "RUB", "USD", rates) == 100
    assert convert_with_rates(50, "USD", "USD", rates) == 50
    assert convert_with_rates(10, "XXX", "RUB", rates) is None


async def test_get_rates_from_cbr_and_cache() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CBR_URL).mock(return_value=httpx.Response(200, content=CBR_XML.encode("cp1251")))
        converter = CurrencyConverter()
        rates = await converter.get_rates()
        assert round(rates["USD"], 2) == 92.5
        assert converter.source == "cbr.ru"

        rub = await converter.to_rub(100, "USD")
        assert rub == 9250
        usd = await converter.to_usd(9250, "RUB")
        assert round(usd, 2) == 100

        await converter.get_rates()  # cached, no new request
        assert route.call_count == 1
        await converter.aclose()


async def test_fallback_when_apis_unavailable() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(CBR_URL).mock(side_effect=httpx.ConnectError("down"))
        mock.get(OPEN_ER_URL).mock(side_effect=httpx.ConnectError("down"))
        converter = CurrencyConverter()
        rates = await converter.get_rates()
        assert converter.source == "fallback"
        assert rates["USD"] == FALLBACK_RATES_TO_RUB["USD"]
        await converter.aclose()


async def test_open_er_used_when_cbr_fails() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(CBR_URL).mock(return_value=httpx.Response(500, text="error"))
        mock.get(OPEN_ER_URL).mock(
            return_value=httpx.Response(200, json={"rates": {"RUB": 91.0, "USD": 1.0, "EUR": 0.9}})
        )
        converter = CurrencyConverter()
        rates = await converter.get_rates()
        assert converter.source == "open.er-api.com"
        assert round(rates["EUR"], 2) == round(91.0 / 0.9, 2)
        await converter.aclose()


async def test_db_cache_is_used() -> None:
    async def loader() -> dict:
        return {
            "rates": {"RUB": 1.0, "USD": 50.0},
            "source": "db",
            "updated_at": utcnow().isoformat(),
        }

    async with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CBR_URL).mock(return_value=httpx.Response(200, content=CBR_XML.encode()))
        converter = CurrencyConverter(cache_loader=loader)
        rates = await converter.get_rates()
        assert rates["USD"] == 50.0
        assert converter.source == "db"
        assert route.call_count == 0
        await converter.aclose()


async def test_enrich_fills_both_currencies(converter, make_job) -> None:
    job = make_job(currency="USD", budget_min=100, budget_max=100, budget_usd=None, budget_rub=None)
    await converter.enrich(job)
    assert job.budget_rub == 9000
    assert job.budget_usd == 100


async def test_enrich_negotiable_stays_empty(converter, make_job) -> None:
    job = make_job(budget_min=None, budget_max=None, budget_type="negotiable")
    await converter.enrich(job)
    assert job.budget_rub is None
