"""PROMO_SPEC §3.1: the Yandex Direct v501 client against a scripted HTTP server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date

import pytest

from app.core.clock import FakeClock
from app.promo.direct import SANDBOX_URL, DirectClient
from app.promo.platforms import (
    NOT_APPLIED,
    UNSUPPORTED_BUDGET,
    AuthError,
    Budget,
    BudgetKind,
    CampaignInfo,
    CampaignState,
    HttpClient,
    HttpReply,
    PlatformError,
    RateLimited,
    SpendLimit,
    SpendRow,
    Transient,
)
from tests.ads_server import Reply, ScriptedServer, scripted_server

TOKEN = "y0_direct-secret-token"
CAMPAIGNS = "POST /json/v501/campaigns"
REPORTS = "POST /json/v501/reports"


@pytest.fixture
async def server() -> AsyncIterator[ScriptedServer]:
    async with scripted_server() as scripted:
        yield scripted


@pytest.fixture
async def direct(server: ScriptedServer, clock: FakeClock) -> AsyncIterator[DirectClient]:
    client = DirectClient(TOKEN, vat_pct=22, clock=clock, base_url=server.url("/json/v501/"))
    yield client
    await client.close()


def unified(campaign_id: int, name: str, state: str, strategy: dict[str, object]) -> dict[str, object]:
    return {"Id": campaign_id, "Name": name, "Type": "UNIFIED_CAMPAIGN", "State": state, "Status": "ACCEPTED",
            "UnifiedCampaign": {"BiddingStrategy": strategy}}


SEARCH_ONLY = {
    "Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
               "WbMaximumClicks": {"WeeklySpendLimit": 300_000_000, "BidCeiling": None}},
    "Network": {"BiddingStrategyType": "SERVING_OFF"},
}


async def test_campaigns_get_request_and_budget_parsing(server: ScriptedServer, direct: DirectClient) -> None:
    server.reply(CAMPAIGNS, Reply(body={"result": {"Campaigns": [
        unified(701, "Поиск", "ON", SEARCH_ONLY),
        unified(702, "Сети", "SUSPENDED", {"Search": {"BiddingStrategyType": "SERVING_OFF"},
                                           "Network": {"BiddingStrategyType": "AVERAGE_CPC",
                                                       "AverageCpc": {"AverageCpc": 5_000_000,
                                                                      "WeeklySpendLimit": 1_000_000_000}}}),
        unified(703, "Период", "OFF", {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {
            "WeeklySpendLimit": None, "CustomPeriodBudget": {"SpendLimit": 5_000_000_000}}}}),
        {"Id": 704, "Name": "Старая", "Type": "TEXT_CAMPAIGN", "State": "ARCHIVED"},
        unified(705, "Период и неделя", "ON", {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                                                          "WbMaximumClicks": {
            "WeeklySpendLimit": 300_000_000, "CustomPeriodBudget": {"SpendLimit": 5_000_000_000}}}}),
        unified(706, "Клик за период", "ON", {"Search": {"BiddingStrategyType": "AVERAGE_CPC", "AverageCpc": {
            "AverageCpc": 5_000_000, "WeeklySpendLimit": 1_000_000_000, "BudgetType": "CUSTOM_PERIOD_BUDGET"}}}),
        unified(707, "Сезон", "ON", {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {
            "WeeklySpendLimit": None, "CustomPeriodBudget": {"SpendLimit": 5_000_000_000, "StartDate": "2026-11-24",
                                                             "EndDate": "2026-12-12", "AutoContinue": "NO"}}},
            "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}),
        unified(708, "За конверсии", "ON", {"Search": {"BiddingStrategyType": "PAY_FOR_CONVERSION",
                                                       "PayForConversion": {"Cpa": 50_000_000, "GoalId": 1,
                                                                            "WeeklySpendLimit": 2_000_000_000}}}),
        unified(709, "Две части", "ON", {"Search": SEARCH_ONLY["Search"],
                                         "Network": {"BiddingStrategyType": "AVERAGE_CPC", "AverageCpc": {
                                             "AverageCpc": 5_000_000, "WeeklySpendLimit": 1_000_000_000}}}),
        unified(710, "Ручная", "ON", {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"}}),
    ]}}, headers={"Units": "11/20828/64000"}))

    ids = ["701", "702", "703", "704", "705", "706", "707", "708", "709", "710"]
    campaigns = await direct.list_campaigns(ids)

    (seen,) = server.requests
    assert seen.path == "/json/v501/campaigns"
    assert seen.headers["Authorization"] == f"Bearer {TOKEN}" and seen.headers["Accept-Language"] == "ru"
    assert seen.json == {"method": "get", "params": {
        "SelectionCriteria": {"Ids": [int(campaign_id) for campaign_id in ids]},
        "FieldNames": ["Id", "Name", "Type", "State", "Status"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy"],
    }}
    week = BudgetKind.WEEK
    assert campaigns == [
        CampaignInfo("701", "Поиск", CampaignState.ACTIVE, Budget(366_00, week)),  # 300 ₽ net + 22%
        CampaignInfo("702", "Сети", CampaignState.PAUSED, Budget(1220_00, week)),
        # budgets that cannot be read (a period without dates, a period flagged without its numbers,
        # budgets in both parts, a manual strategy) leave the budget unknown
        CampaignInfo("703", "Период", CampaignState.OTHER, None),
        CampaignInfo("704", "Старая", CampaignState.OTHER, None),
        CampaignInfo("705", "Период и неделя", CampaignState.ACTIVE, None),
        CampaignInfo("706", "Клик за период", CampaignState.ACTIVE, None),
        CampaignInfo("707", "Сезон", CampaignState.ACTIVE, None,
                     limit=SpendLimit(6100_00, date(2026, 11, 24), date(2026, 12, 12))),
        CampaignInfo("708", "За конверсии", CampaignState.ACTIVE, Budget(2440_00, week), pays_per_conversion=True),
        CampaignInfo("709", "Две части", CampaignState.ACTIVE, None),
        CampaignInfo("710", "Ручная", CampaignState.ACTIVE, None),
    ]
    assert await direct.list_campaigns([]) == [] and len(server.requests) == 1


@pytest.mark.parametrize(("method", "results", "warning"), [
    ("suspend", "SuspendResults", 10020), ("resume", "ResumeResults", 10021),
])
async def test_suspend_and_resume_are_idempotent(server: ScriptedServer, direct: DirectClient, method: str,
                                                 results: str, warning: int) -> None:
    server.reply(CAMPAIGNS,
                 Reply(body={"result": {results: [{"Id": 701}]}}),
                 Reply(body={"result": {results: [{"Id": 701, "Warnings": [
                     {"Code": warning, "Message": "Объект уже в этом состоянии"}]}]}}),
                 Reply(body={"result": {results: [{"Errors": [
                     {"Code": 8800, "Message": "Объект не найден", "Details": "Кампания не найдена"}]}]}}),
                 Reply(body={"result": {results: [{"Id": 701, "Warnings": [
                     {"Code": 10165, "Message": "Параметр не будет применен"}]}]}}))
    call = getattr(direct, method)
    await call("701")
    await call("701")
    with pytest.raises(PlatformError) as error:
        await call("999")
    assert (error.value.code, error.value.message) == (8800, "Объект не найден Кампания не найдена")
    with pytest.raises(PlatformError) as error:
        await call("701")
    assert error.value.code == 10165, "any other warning may mean nothing happened"
    assert server.requests[0].json == {"method": method, "params": {"SelectionCriteria": {"Ids": [701]}}}


def with_weekly(micros: int) -> Reply:
    return Reply(body={"result": {"Campaigns": [unified(701, "Поиск", "ON", {
        "Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {"WeeklySpendLimit": micros}},
        "Network": {"BiddingStrategyType": "SERVING_OFF"}})]}})


async def test_weekly_budget_update_sends_the_strategy_type_and_net_micros_and_reads_it_back(
    server: ScriptedServer, direct: DirectClient
) -> None:
    server.reply(CAMPAIGNS, with_weekly(300_000_000), Reply(body={"result": {"UpdateResults": [{"Id": 701}]}}),
                 with_weekly(385_240_000))

    await direct.set_budget("701", 470_00, BudgetKind.WEEK)  # 470 ₽ gross = 385.24 ₽ net

    assert server.requests[1].json == {"method": "update", "params": {"Campaigns": [{
        "Id": 701,
        "UnifiedCampaign": {"BiddingStrategy": {"Search": {
            "BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {"WeeklySpendLimit": 385_240_000}}}},
    }]}}
    assert server.requests[2].json["method"] == "get", "the budget is read back"


@pytest.mark.parametrize(("update", "read_back", "code"), [
    # «Настройка не будет изменена»: a warning, and the budget stays as it was
    (Reply(body={"result": {"UpdateResults": [{"Id": 701, "Warnings": [
        {"Code": 10163, "Message": "Настройка не будет изменена"}]}]}}), None, 10163),
    (Reply(body={"result": {"UpdateResults": [{"Id": 701}]}}), with_weekly(300_000_000), NOT_APPLIED),
])
async def test_a_budget_change_that_did_not_happen_fails(server: ScriptedServer, direct: DirectClient,
                                                         update: Reply, read_back: Reply | None, code: object) -> None:
    server.reply(CAMPAIGNS, with_weekly(300_000_000), update, *([read_back] if read_back else []))
    with pytest.raises(PlatformError) as error:
        await direct.set_budget("701", 470_00, BudgetKind.WEEK)
    assert error.value.code == code


async def test_budget_changes_that_cannot_work_fail_clearly(server: ScriptedServer, direct: DirectClient) -> None:
    with pytest.raises(PlatformError):
        await direct.set_budget("701", 470_00, BudgetKind.DAY)
    assert server.requests == [], "nothing is sent for a daily budget"
    manual = unified(703, "Ручная", "ON", {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"}})
    period = unified(704, "Сезон", "ON", {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {
        "CustomPeriodBudget": {"SpendLimit": 10**9, "StartDate": "2026-11-24", "EndDate": "2026-12-12"}}}})
    server.reply(CAMPAIGNS, Reply(body={"result": {"Campaigns": [manual]}}),
                 Reply(body={"result": {"Campaigns": [period]}}))
    for campaign_id in ("703", "704"):
        with pytest.raises(PlatformError) as error:
            await direct.set_budget(campaign_id, 470_00, BudgetKind.WEEK)
        assert error.value.code == UNSUPPORTED_BUDGET
    assert server.routes() == [CAMPAIGNS, CAMPAIGNS], "no update is attempted"


async def test_spend_report_waits_for_the_offline_queue(server: ScriptedServer, direct: DirectClient,
                                                        clock: FakeClock) -> None:
    tsv = "Date\tCampaignId\tClicks\tCost\n2026-11-28\t701\t6\t90.20\n2026-11-29\t701\t3\t--\n2026-11-29\t702\t1\t4.5\n"
    server.reply(REPORTS, Reply(201, "", {"retryIn": "3"}), Reply(202, "", {"retryIn": "5"}), Reply(200, tsv))
    started = clock.monotonic()

    spend = await direct.daily_spend(["701", "702"], date(2026, 11, 24), date(2026, 11, 30))

    assert spend == {("701", date(2026, 11, 28)): SpendRow(90_20, 6), ("701", date(2026, 11, 29)): SpendRow(0, 3),
                     ("702", date(2026, 11, 29)): SpendRow(4_50, 1)}
    assert clock.monotonic() - started == 8
    first, *polls = server.requests
    assert first.json == {"params": {
        "SelectionCriteria": {"DateFrom": "2026-11-24", "DateTo": "2026-11-30",
                              "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": ["701", "702"]}]},
        "FieldNames": ["Date", "CampaignId", "Clicks", "Cost"],
        "ReportName": first.json["params"]["ReportName"],
        "ReportType": "CAMPAIGN_PERFORMANCE_REPORT", "DateRangeType": "CUSTOM_DATE", "Format": "TSV",
        "IncludeVAT": "YES",
    }}
    assert "2026-11-24..2026-11-30" in first.json["params"]["ReportName"]
    assert all(poll.json == first.json for poll in polls), "the same report is polled under the same name"
    headers = first.headers
    assert (headers["processingMode"], headers["returnMoneyInMicros"], headers["skipReportHeader"],
            headers["skipReportSummary"]) == ("auto", "false", "true", "true")


async def test_every_fetch_gets_its_own_report_name(server: ScriptedServer, direct: DirectClient,
                                                    clock: FakeClock) -> None:
    server.reply(REPORTS, Reply(200, "Date\tCampaignId\tClicks\tCost\n"), Reply(200, ""))
    assert await direct.daily_spend(["701"], date(2026, 11, 30), date(2026, 11, 30)) == {}
    clock.advance(7200)
    assert await direct.daily_spend(["701"], date(2026, 11, 30), date(2026, 11, 30)) == {}
    names = [seen.json["params"]["ReportName"] for seen in server.requests]
    assert names[0] != names[1], "an offline report is kept for 5 hours: a reused name would return old numbers"


async def test_report_wait_is_bounded(server: ScriptedServer, direct: DirectClient) -> None:
    server.reply(REPORTS, *[Reply(202, "", {"retryIn": "120"})] * 5)
    with pytest.raises(Transient):
        await direct.daily_spend(["701"], date(2026, 11, 30), date(2026, 11, 30))
    assert len(server.requests) == 3, "two waits of 120 s fit into 5 minutes, a third does not"


class SlowHttp(HttpClient):
    """Every request takes ``seconds`` of the fake clock and answers 202 «retry in 10 s»."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        super().__init__()
        self.clock, self.seconds = clock, seconds
        self.timeouts: list[float | None] = []

    async def request(self, method: str, url: str, **kwargs: object) -> HttpReply:
        timeout = kwargs.get("timeout")
        self.timeouts.append(timeout if isinstance(timeout, float | int) else None)
        self.clock.advance(self.seconds)
        return HttpReply(202, {"retryIn": "10"}, "")


async def test_slow_answers_count_against_the_report_wait(clock: FakeClock) -> None:
    http = SlowHttp(clock, seconds=100)
    direct = DirectClient(TOKEN, vat_pct=22, clock=clock, http=http)
    with pytest.raises(Transient):
        await direct.daily_spend(["701"], date(2026, 11, 30), date(2026, 11, 30))
    assert http.timeouts == [120, 120, 80], "the last request may only take what is left of the 5 minutes"


@pytest.mark.parametrize(("reply", "error", "code"), [
    (Reply(body={"error": {"error_code": 53, "error_string": "Ошибка авторизации",
                           "error_detail": "Неверный OAuth-токен"}}), AuthError, "53"),
    (Reply(body={"error": {"error_code": 58, "error_string": "Незавершенная регистрация"}}), AuthError, "58"),
    (Reply(body={"error": {"error_code": "3000", "error_string": "Нет доступа к API"}}), AuthError, "3000"),
    (Reply(body={"error": {"error_code": 152, "error_string": "Недостаточно баллов"}}), RateLimited, None),
    (Reply(body={"error": {"error_code": 506, "error_string": "Превышен лимит соединений"}}), RateLimited, None),
    (Reply(body={"error": {"error_code": 1000, "error_string": "Сервер временно недоступен"}}), Transient, None),
    (Reply(body={"error": {"error_code": 1002, "error_string": "Ошибка операции"}}), Transient, None),
    (Reply(body={"error": {"error_code": 1002, "error_string": "Ошибка операции",
                           "error_detail": "Неверный OAuth-токен"}}), AuthError, "1002"),
    (Reply(500, "<html>oops</html>"), Transient, None),
    (Reply(429, ""), RateLimited, None),
    (Reply(body={"error": {"error_code": 8000, "error_string": "Неверный запрос"}}), PlatformError, None),
    (Reply(404, "<html>no such version</html>"), PlatformError, None),
])
async def test_errors_map_to_the_promo_classes(server: ScriptedServer, direct: DirectClient, reply: Reply,
                                               error: type[Exception], code: str | None) -> None:
    server.reply(CAMPAIGNS, reply)
    with pytest.raises(error) as raised:
        await direct.list_campaigns(["701"])
    if code is not None:
        assert raised.value.code == code  # type: ignore[attr-defined]


async def test_report_errors(server: ScriptedServer, direct: DirectClient) -> None:
    server.reply(REPORTS, Reply(400, {"error": {"error_code": 4000, "error_string": "Неверные параметры"}}),
                 Reply(500, {"error": {"error_code": 1002, "error_string": "Ошибка операции"}}),
                 Reply(200, "some garbage without a header"))
    for expected in (PlatformError, Transient, PlatformError):
        with pytest.raises(expected):
            await direct.daily_spend(["701"], date(2026, 11, 30), date(2026, 11, 30))


async def test_units_are_logged_at_debug_and_the_token_never(server: ScriptedServer, direct: DirectClient,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    server.reply(CAMPAIGNS, Reply(body={"result": {"Campaigns": []}}, headers={"Units": "10/20828/64000"}),
                 Reply(body={"error": {"error_code": 53, "error_string": "Ошибка авторизации"}}))
    with caplog.at_level(logging.DEBUG):
        await direct.list_campaigns(["701"])
        with pytest.raises(AuthError):
            await direct.list_campaigns(["701"])
    assert any(getattr(record, "units", None) == "10/20828/64000" for record in caplog.records)
    ours = [record for record in caplog.records if record.name.startswith("app.")]
    assert ours and all(TOKEN not in f"{record.getMessage()} {record.__dict__}" for record in ours)


def test_the_sandbox_is_asked_at_v501_too() -> None:
    assert SANDBOX_URL == "https://api-sandbox.direct.yandex.com/json/v501/"
