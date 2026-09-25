"""The ad autopilot (PROMO_SPEC §6 and its Implementation notes): fetch → store → decide → act → report.

Jobs (Moscow time; they run in a scheduler task of their own, so a slow platform never holds up the
bot's jobs, and they keep their last-run guards):
- promo_daily at PROMO_REPORT_TIME: fetch the campaigns and their spend (the last 7 days, since
  registration for a campaign never fetched), run rules 1–6, carry out or record the decisions
  and send the report to every admin;
- promo_guard every 2 hours in autopilot mode: fetch the campaigns and today's spend and apply
  rules 1–3 (season, blind budgets, cap). It writes when it acted, and — at most every 6 hours
  per platform — when it could not fetch fresh numbers.

Fetching happens outside the promo lock. Storing what was fetched, deciding and acting happen
under it, and a campaign the bot changed after a fetch began keeps the newer values. Admin
changes (/ads resume, /ads budget, the buttons) fetch their campaign again first and refuse
when that fails: they never act on old numbers. Neither do the rules raise budgets or start
campaigns on numbers older than 4 hours.

Test mode, the default until /ads auto on, never calls a mutating API method: decisions are
stored as proposals of mode 'dry' and the report says «Сделал бы». In autopilot mode pauses and
the season start happen at once; budget raises always wait for an admin's tap (a separate
message with [Поднять до X ₽] [Не надо], valid for 24 hours and voided when the autopilot is
switched off); other campaigns start again only by /ads resume. The admins' own commands
(/ads stop, resume, budget) act in both modes and respect the season, the cap and blind
budgets; a large /ads budget change waits for a tap too. A platform without credentials is
skipped with a note; one that refuses our keys alerts the admins at most once per 6 hours
while the other keeps working. One campaign's failure never stops the others.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from app.config import Config
from app.context import AppContext
from app.core import texts
from app.core.clock import Clock
from app.core.dates import format_moment
from app.db import Db
from app.handlers import views
from app.max_api import OutMessage
from app.promo import attribution, content, links, platforms, rules, store
from app.promo.attribution import SourceMetrics, cohort_cutoff
from app.promo.direct import LIVE_URL as DIRECT_LIVE_URL
from app.promo.direct import SANDBOX_URL as DIRECT_SANDBOX_URL
from app.promo.direct import DirectClient
from app.promo.platforms import (
    AdApiError,
    AdPlatform,
    AuthError,
    Budget,
    BudgetKind,
    CampaignInfo,
    CampaignState,
    HttpClient,
    Platform,
    PlatformError,
    RateLimited,
    SpendRow,
    Transient,
    gross_kop,
    minimum_gross_kop,
    net_kop,
)
from app.promo.rules import ActionKind, CampaignFacts, Decision, PausedBy, Situation, Thresholds
from app.promo.store import ActionMode, ActionStatus, PromoAction, PromoCampaign, PromoSettings, SpendTotals
from app.promo.vkads import VkAdsClient

log = logging.getLogger(__name__)

GUARD_INTERVAL = 2 * 3600.0
AUTH_ALERT_INTERVAL = 6 * 3600.0
BLIND_ALERT_INTERVAL = 6 * 3600.0
FRESH_FOR = timedelta(hours=4)  # older numbers are not good enough to raise a budget or start a campaign
PROPOSAL_LIFETIME = timedelta(hours=24)
FETCH_DAYS = 7
CONFIRM_RAISE_PCT = 30  # /ads budget asks for a tap above this raise…
CONFIRM_DAY_KOP = 500_00  # …or when a day of the new budget may cost this much more
_DEFAULT_KIND = {Platform.DIRECT: BudgetKind.WEEK, Platform.VK: BudgetKind.DAY}


class NotConfigured(AdApiError):
    """The platform has no credentials in .env."""


class CampaignNotFound(Exception):
    """/ads add: the platform does not list a campaign with this id."""


class TryAgain(Exception):
    """A tap that could not be checked right now: its buttons stay for another try."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def build_platforms(config: Config, db: Db, clock: Clock, ssl_context: ssl.SSLContext) -> dict[Platform, AdPlatform]:
    """The clients of the platforms that have credentials (none while PROMO_ENABLED=0)."""
    promo = config.promo
    clients: dict[Platform, AdPlatform] = {}
    if not promo.enabled:
        return clients
    if promo.direct_configured:
        base_url = DIRECT_SANDBOX_URL if promo.direct_sandbox else DIRECT_LIVE_URL
        clients[Platform.DIRECT] = DirectClient(promo.direct_token, vat_pct=promo.vat_pct, clock=clock,
                                                base_url=base_url, http=HttpClient(ssl_context))
    if promo.vk_configured:
        clients[Platform.VK] = VkAdsClient(promo.vk_client_id, promo.vk_client_secret,
                                           store.DbTokenStore(db, Platform.VK), vat_pct=promo.vat_pct,
                                           clock=clock, http=HttpClient(ssl_context))
    return clients


# --- fetching from the platforms (no lock) and storing it (under the lock) ------------------------------------


@dataclass(frozen=True, slots=True)
class Fetched:
    """What one platform said about some campaigns, not stored yet."""

    platform: Platform
    started_at: datetime
    date_from: date
    date_to: date
    infos: Mapping[str, CampaignInfo] = field(default_factory=dict)  # by src
    missing: tuple[str, ...] = ()  # srcs the platform no longer lists
    spend: Mapping[tuple[str, date], SpendRow] = field(default_factory=dict)  # by (src, day)
    covered: tuple[str, ...] = ()  # srcs whose spend was fetched
    notes: tuple[str, ...] = ()  # for the report
    problem: str | None = None  # why some numbers are not fresh (auth problems alert on their own)

    def complete_for(self, src: str) -> bool:
        return src in self.infos and src in self.covered


async def fetch(ctx: AppContext, campaigns: Sequence[PromoCampaign], *, today_only: bool) -> list[Fetched]:
    """Ask each platform about its campaigns; failures become notes, never exceptions."""
    today, results = ctx.today(), []
    for platform in Platform:
        mine = [c for c in campaigns if c.platform == platform]
        if not mine:
            continue
        date_from = today if today_only else await _fetch_start(ctx, mine, today)
        client = ctx.ad_platforms.get(platform)
        if client is None:
            results.append(Fetched(platform, ctx.clock.now(), date_from, today,
                                   notes=(texts.promo_no_credentials(platform),)))
        else:
            results.append(await _fetch_platform(ctx, client, mine, date_from, today))
    return results


async def _fetch_start(ctx: AppContext, campaigns: Sequence[PromoCampaign], today: date) -> date:
    """The last 7 days; since registration for a campaign whose spend was never fetched."""
    start = today - timedelta(days=FETCH_DAYS - 1)
    for campaign in campaigns:
        if not await store.has_spend(ctx.db, campaign.src):
            start = min(start, campaign.registered_at.astimezone(ctx.config.tz).date())
    return start


async def _fetch_platform(
    ctx: AppContext, client: AdPlatform, campaigns: Sequence[PromoCampaign], date_from: date, date_to: date
) -> Fetched:
    platform, started = client.platform, ctx.clock.now()
    fetched = Fetched(platform, started, date_from, date_to)
    by_id = {c.external_id: c for c in campaigns}
    try:
        listed = {info.id: info for info in await client.list_campaigns(list(by_id)) if info.id in by_id}
    except Exception as error:  # anything at all: a failing platform must not stop the other one
        return await _failed(ctx, fetched, error)
    found = [c for c in campaigns if c.external_id in listed]
    fetched = replace(
        fetched,
        infos={c.src: listed[c.external_id] for c in found},
        missing=tuple(c.src for c in campaigns if c.external_id not in listed),
        notes=tuple(texts.promo_campaign_missing(name=c.name, src=c.src) for c in campaigns
                    if c.external_id not in listed),
    )
    try:
        spend, refused = await _fetch_spend(client, [c.external_id for c in found], date_from, date_to)
    except Exception as error:  # as above
        return await _failed(ctx, fetched, error)
    covered = [c for c in found if c.external_id not in refused]
    refusals = tuple(texts.promo_spend_refused(platform=platform, name=by_id[campaign_id].name,
                                               detail=describe(error)) for campaign_id, error in refused.items())
    return replace(
        fetched,
        spend={(by_id[campaign_id].src, day): row for (campaign_id, day), row in spend.items()
               if campaign_id in by_id},
        covered=tuple(c.src for c in covered),
        notes=fetched.notes + refusals,
        problem=refusals[0] if refusals else None,
    )


async def _fetch_spend(
    client: AdPlatform, ids: Sequence[str], date_from: date, date_to: date
) -> tuple[dict[tuple[str, date], SpendRow], dict[str, PlatformError]]:
    """Spend of all ids at once; when the platform refuses the request (VK refuses all of it for one
    unknown campaign), one id at a time. Returns the spend and the ids refused on their own."""
    if not ids:
        return {}, {}
    try:
        return dict(await client.daily_spend(ids, date_from, date_to)), {}
    except PlatformError as error:
        if len(ids) == 1:
            return {}, {ids[0]: error}
    spend: dict[tuple[str, date], SpendRow] = {}
    refused: dict[str, PlatformError] = {}
    for campaign_id in ids:
        try:
            spend.update(await client.daily_spend([campaign_id], date_from, date_to))
        except PlatformError as error:
            refused[campaign_id] = error
    return spend, refused


async def _failed(ctx: AppContext, fetched: Fetched, error: Exception) -> Fetched:
    platform = fetched.platform
    if isinstance(error, AuthError):
        await _auth_alert(ctx, platform, error)
        return replace(fetched, notes=(*fetched.notes, texts.promo_auth_note(platform)))
    if isinstance(error, AdApiError):
        log.warning("ad platform fetch failed", extra={"platform": platform, "error": str(error)})
    else:
        log.exception("ad platform fetch failed unexpectedly", extra={"platform": platform}, exc_info=error)
    sandbox = platform == Platform.DIRECT and ctx.config.promo.direct_sandbox
    note = texts.promo_fetch_failed(platform, describe(error), sandbox=sandbox)
    return replace(fetched, notes=(*fetched.notes, note), problem=note)


async def save(ctx: AppContext, results: Sequence[Fetched]) -> None:
    """Store what was fetched; call it under the promo lock."""
    today = ctx.today()
    for fetched in results:
        for src, info in fetched.infos.items():
            await store.save_campaign_info(ctx.db, src, info, today=today, fetched_at=fetched.started_at)
        for src in fetched.missing:
            await store.mark_missing(ctx.db, src, fetched_at=fetched.started_at)
        if fetched.covered:
            await store.replace_spend(ctx.db, fetched.covered, fetched.date_from, fetched.date_to, fetched.spend,
                                      fetched.started_at)


def _checked_now(results: Sequence[Fetched], src: str) -> bool:
    return any(fetched.complete_for(src) for fetched in results)


def _notes(results: Sequence[Fetched]) -> list[str]:
    return [note for fetched in results for note in fetched.notes]


# --- the numbers -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    settings: PromoSettings
    campaigns: list[PromoCampaign]
    facts: list[CampaignFacts]
    metrics: dict[str, SourceMetrics]
    situation: Situation

    def campaign(self, src: str) -> PromoCampaign | None:
        return next((c for c in self.campaigns if c.src == src), None)

    def facts_of(self, src: str) -> CampaignFacts | None:
        return next((f for f in self.facts if f.src == src), None)


async def snapshot(ctx: AppContext) -> Snapshot:
    """The stored facts and settings as the rules see them."""
    db, promo, today, now = ctx.db, ctx.config.promo, ctx.today(), ctx.clock.now()
    settings = await store.get_settings(db)
    campaigns = await store.enabled_campaigns(db)
    cutoff = today - timedelta(days=settings.lag_days)
    spend = await store.spend_totals(db, today, cutoff)
    metrics = await attribution.source_metrics(db, [c.src for c in campaigns],
                                               cohort_cutoff(today, settings.lag_days, ctx.config.tz))
    situation = Situation(
        today=today, season_start=promo.season_start, season_end=promo.season_end,
        cap_kop=settings.cap_rub * 100, spent_total_kop=await store.total_spend(db), cutoff=cutoff,
        thresholds=Thresholds(pause_cpa_kop=settings.pause_cpa_rub * 100, scale_cpa_kop=settings.scale_cpa_rub * 100,
                              min_spend_kop=settings.min_spend_rub * 100, max_raise_pct=settings.max_raise_pct),
        min_budget_kop=minimum_gross_kop(promo.vat_pct),
    )
    facts = [await _facts(db, c, spend.get(c.src, SpendTotals()), metrics[c.src], now) for c in campaigns]
    return Snapshot(settings, campaigns, facts, metrics, situation)


async def _facts(db: Db, campaign: PromoCampaign, spend: SpendTotals, metrics: SourceMetrics,
                 now: datetime) -> CampaignFacts:
    limit = campaign.limit
    in_limit = 0 if limit is None else await store.spend_within(db, campaign.src, limit.start, limit.end)
    return CampaignFacts(
        src=campaign.src, platform=campaign.platform, name=campaign.name, state=campaign.state,
        budget=campaign.budget, limit=limit, pays_per_conversion=campaign.pays_per_conversion,
        previous_budget_kop=campaign.previous_budget_kop, last_budget_change_day=campaign.last_budget_change_day,
        spend_total_kop=spend.total_kop, spend_matured_kop=spend.matured_kop,
        spend_yesterday_kop=spend.yesterday_kop, spend_in_limit_kop=in_limit, games3=metrics.games3,
        games3_matured=metrics.games3_matured, paid_rub=metrics.paid_rub, paused_by=campaign.paused_by,
        fresh=_fresh(campaign, now),
    )


def _fresh(campaign: PromoCampaign, now: datetime) -> bool:
    return campaign.spend_checked_at is not None and now - campaign.spend_checked_at <= FRESH_FOR


async def has_work(ctx: AppContext) -> bool:
    """Whether there is anything to report: a platform, a campaign, a link or the channel."""
    return bool(ctx.ad_platforms or await store.enabled_campaigns(ctx.db) or await store.all_links(ctx.db)
                or ctx.config.promo.channel_id is not None)


# --- jobs ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    decision: Decision
    campaign: PromoCampaign
    status: ActionStatus
    mode: ActionMode
    action_id: int
    error: str | None = None


async def daily_job(ctx: AppContext) -> None:
    """promo_daily: fetch, store, decide, carry out or record, report (PROMO_SPEC §6)."""
    if not await has_work(ctx):
        return
    now = ctx.clock.now()
    await store.expire_proposals(ctx.db, now - PROPOSAL_LIFETIME)
    fetched = await fetch(ctx, await store.enabled_campaigns(ctx.db), today_only=False)
    async with ctx.locks.promo:
        await save(ctx, fetched)
        before = await snapshot(ctx)
        outcomes = await _carry_out(ctx, before, rules.decide(before.facts, before.situation), now)
    await _send_report(ctx, await snapshot(ctx), _notes(fetched), outcomes)


async def guard_job(ctx: AppContext) -> None:
    """promo_guard: season, blind budgets and the cap between the daily runs, in autopilot mode only."""
    campaigns = await store.enabled_campaigns(ctx.db)
    if not campaigns or not (await store.get_settings(ctx.db)).auto:
        return
    fetched = await fetch(ctx, campaigns, today_only=True)
    for result in fetched:
        if result.problem is not None:
            await ctx.alerts.alert(f"promo_blind:{result.platform}", texts.promo_guard_blind(result.problem),
                                   min_interval=BLIND_ALERT_INTERVAL)
    async with ctx.locks.promo:
        await save(ctx, fetched)
        current = await snapshot(ctx)
        decisions = rules.protect(current.facts, current.situation)
        if not decisions:
            return
        outcomes = await _carry_out(ctx, current, decisions, ctx.clock.now())
    await ctx.alerts.notify_admins(texts.promo_guard_report([_outcome_line(outcome) for outcome in outcomes]))


async def _carry_out(ctx: AppContext, current: Snapshot, decisions: Sequence[Decision], now: datetime) -> list[Outcome]:
    """Pauses and season starts happen in autopilot mode; raises and everything in test mode become proposals."""
    mode = ActionMode.AUTO if current.settings.auto else ActionMode.DRY
    outcomes = []
    for decision in decisions:
        campaign = current.campaign(decision.src)
        assert campaign is not None
        status, error = ActionStatus.PROPOSED, None
        if mode == ActionMode.AUTO and not decision.needs_approval:
            paused_by = decision.params.get("paused_by")
            error = await _act(ctx, campaign, decision.action, PausedBy(paused_by) if paused_by else None)
            status = ActionStatus.APPLIED if error is None else ActionStatus.FAILED
        action_id = await store.insert_action(
            ctx.db, now=now, src=decision.src, action=decision.action, params=decision.params,
            reason=decision.reason, mode=mode, status=status, error=error,
        )
        outcomes.append(Outcome(decision, campaign, status, mode, action_id, error))
    return outcomes


# --- the report --------------------------------------------------------------------------------------------------


async def _send_report(ctx: AppContext, current: Snapshot, notes: Sequence[str], outcomes: Sequence[Outcome]) -> None:
    situation = current.situation
    header = texts.promo_report_header(day=situation.today, dry=not current.settings.auto,
                                       spent_kop=situation.spent_total_kop, cap_kop=situation.cap_kop)
    lines = [_campaign_line(facts) for facts in current.facts] or [texts.PROMO_NO_CAMPAIGNS]
    if any(_cpa(facts) != _decision_cpa(facts) for facts in current.facts):
        lines.append(texts.promo_decision_note(lag_days=current.settings.lag_days))
    lines += [*notes, *(_outcome_line(outcome) for outcome in outcomes)]
    lines += await _links_and_channel_lines(ctx, current.settings)
    await ctx.alerts.notify_admins(texts.fit_lines(lines, header=header))
    for outcome in outcomes:
        if outcome.decision.needs_approval and outcome.mode == ActionMode.AUTO:
            await ctx.alerts.notify_admins(_proposal_message(ctx, current, outcome))


def _cpa(facts: CampaignFacts) -> int | None:
    return facts.spend_total_kop // facts.games3 if facts.games3 else None


def _decision_cpa(facts: CampaignFacts) -> int | None:
    """The cost per game the rules decide on: matured spend and games only (PROMO_LAG_DAYS)."""
    return facts.spend_matured_kop // facts.games3_matured if facts.games3_matured else None


def _campaign_line(facts: CampaignFacts) -> str:
    return texts.promo_campaign_line(
        platform=facts.platform, name=facts.name, state=facts.state, paused_by=facts.paused_by,
        yesterday_kop=facts.spend_yesterday_kop, total_kop=facts.spend_total_kop, games3=facts.games3,
        cpa_kop=_cpa(facts), decision_cpa_kop=_decision_cpa(facts), paid_rub=facts.paid_rub,
    )


def _outcome_line(outcome: Outcome) -> str:
    decision, campaign = outcome.decision, outcome.campaign
    dry = outcome.mode == ActionMode.DRY
    if decision.action == ActionKind.SET_BUDGET:
        params = decision.params
        return texts.promo_proposal_line(name=campaign.name, from_kop=params["from_kop"], to_kop=params["to_kop"],
                                         kind=params["kind"], reason=decision.reason, dry=dry)
    if decision.action == ActionKind.RESUME:
        if outcome.status == ActionStatus.FAILED:
            return texts.promo_start_failed_line(name=campaign.name, src=campaign.src, error=outcome.error or "")
        return texts.promo_started_line(name=campaign.name, reason=decision.reason, src=campaign.src, dry=dry)
    if outcome.status == ActionStatus.FAILED:
        return texts.promo_pause_failed_line(name=campaign.name, reason=decision.reason, error=outcome.error or "")
    return texts.promo_paused_line(name=campaign.name, reason=decision.reason, dry=dry)


def _proposal_message(ctx: AppContext, current: Snapshot, outcome: Outcome) -> OutMessage:
    params = outcome.decision.params
    budget = Budget(params["to_kop"], BudgetKind(params["kind"]))
    text = texts.promo_proposal(
        name=outcome.campaign.name, from_kop=params["from_kop"], to_kop=budget.kop, kind=budget.kind,
        net_kop=net_kop(budget.kop, ctx.config.promo.vat_pct), reason=outcome.decision.reason,
        week_day_kop=_week_day_after(current, outcome.campaign.src, budget),
    )
    return views.promo_proposal(outcome.action_id, text, budget.kop)


def _week_day_after(current: Snapshot, src: str, budget: Budget) -> int | None:
    """For a Direct weekly budget: what the day of the change may cost (the old budget still counts)."""
    facts = current.facts_of(src)
    if facts is None or budget.kind != BudgetKind.WEEK:
        return None
    (changed,) = rules.with_budget([facts], src, budget, current.situation.today)
    return rules.day_exposure(changed, current.situation.today)


async def _links_and_channel_lines(ctx: AppContext, settings: PromoSettings) -> list[str]:
    cutoff = cohort_cutoff(ctx.today(), settings.lag_days, ctx.config.tz)
    lines = []
    link_entries = [
        texts.promo_link_entry(slug=link.src[1:], users=m.users, games=m.games, games3=m.games3, paid_rub=m.paid_rub)
        for link, m in await links.links_with_metrics(ctx.db, cutoff)
    ]
    if link_entries:
        lines.append(texts.promo_links_line(link_entries))
    posts = sum(status == store.PostStatus.SENT for status in (await store.post_statuses(ctx.db)).values())
    if ctx.config.promo.channel_id is not None or posts:
        metrics = attribution.total((await attribution.source_metrics(
            ctx.db, [post.id for post in content.CALENDAR], cutoff)).values())
        lines.append(texts.promo_channel_line(posts=posts, users=metrics.users, games=metrics.games))
    return lines


async def status(ctx: AppContext) -> str:
    """/ads: the mode, the money, every campaign and the proposals that wait."""
    current = await snapshot(ctx)
    settings, situation = current.settings, current.situation
    header = texts.promo_status_header(dry=not settings.auto, spent_kop=situation.spent_total_kop,
                                       cap_kop=situation.cap_kop)
    lines = [_rules_summary(ctx, settings)]
    lines += [_status_line(ctx, current, campaign) for campaign in current.campaigns] or [texts.PROMO_NO_CAMPAIGNS]
    for proposal in await store.open_proposals(ctx.db):
        campaign = current.campaign(proposal.src)
        if campaign is not None:
            lines.append(texts.promo_waiting_proposal(name=campaign.name, to_kop=proposal.params["to_kop"]))
    return texts.fit_lines(lines, header=header)


def _status_line(ctx: AppContext, current: Snapshot, campaign: PromoCampaign) -> str:
    facts = current.facts_of(campaign.src)
    assert facts is not None
    budget, checked = facts.budget, campaign.spend_checked_at
    numbers_from = None if facts.fresh else (format_moment(checked, ctx.config.tz) if checked else "")
    return texts.promo_status_line(
        platform=facts.platform, name=facts.name, src=facts.src, state=facts.state, paused_by=facts.paused_by,
        yesterday_kop=facts.spend_yesterday_kop, total_kop=facts.spend_total_kop, games3=facts.games3,
        cpa_kop=_cpa(facts), decision_cpa_kop=_decision_cpa(facts), paid_rub=facts.paid_rub,
        downstream_games=current.metrics[facts.src].downstream_games, budget_kop=budget.kop if budget else None,
        kind=budget.kind if budget else None, limit_kop=facts.limit.kop if facts.limit else None,
        numbers_from=numbers_from,
    )


def _rules_summary(ctx: AppContext, settings: PromoSettings) -> str:
    promo = ctx.config.promo
    return texts.promo_rules_summary(
        pause_cpa_rub=settings.pause_cpa_rub, scale_cpa_rub=settings.scale_cpa_rub,
        min_spend_rub=settings.min_spend_rub, max_raise_pct=settings.max_raise_pct, lag_days=settings.lag_days,
        season=f"{_day_month(promo.season_start)}–{_day_month(promo.season_end)}",
    )


def _day_month(month_day: str) -> str:
    month, day = month_day.split("-")
    return f"{day}.{month}"


# --- admin actions (PROMO_SPEC §7) ------------------------------------------------------------------------------


async def register(ctx: AppContext, platform: Platform, external_id: str, name: str) -> tuple[PromoCampaign, bool]:
    """/ads add: check the campaign through the API when possible; returns it and whether it was checked.

    Raises ``CampaignNotFound`` or the platform's ``AdApiError``.
    """
    fallback = store.campaign_src(platform, external_id)
    client = ctx.ad_platforms.get(platform)
    if client is None:
        info = CampaignInfo(external_id, name or fallback, CampaignState.OTHER, None)
    else:
        found = await client.list_campaigns([external_id])
        if not found:
            raise CampaignNotFound(external_id)
        info = replace(found[0], id=external_id, name=name or found[0].name or fallback)
    async with ctx.locks.promo:
        campaign = await store.register_campaign(ctx.db, platform, info, ctx.clock.now())
    return campaign, client is not None


async def unregister(ctx: AppContext, src: str) -> OutMessage | str:
    """/ads remove: the campaign keeps running on the platform unless the admin taps [Остановить на площадке]."""
    async with ctx.locks.promo:
        campaign = await store.get_campaign(ctx.db, src)
        if campaign is None or not campaign.enabled:
            return texts.promo_unknown_src(src)
        await store.disable_campaign(ctx.db, src)
    running = campaign.state != CampaignState.PAUSED
    text = texts.promo_removed(name=campaign.name, src=src, running=running)
    return views.promo_removed(src, text) if running else text


async def stop_removed(ctx: AppContext, src: str, admin_id: int) -> str:
    """[Остановить на площадке] under /ads remove."""
    async with ctx.locks.promo:
        campaign = await store.get_campaign(ctx.db, src)
        if campaign is None:
            return texts.promo_unknown_src(src)
        error = await _act(ctx, campaign, ActionKind.PAUSE, PausedBy.ADMIN)
        await _record_admin(ctx, campaign, ActionKind.PAUSE, {"paused_by": PausedBy.ADMIN}, admin_id, error)
    return texts.promo_stopped_one(campaign.name) if error is None else texts.promo_action_failed(
        name=campaign.name, error=error)


async def stop_all(ctx: AppContext, admin_id: int) -> str:
    """/ads stop: suspend every registered campaign now, in any mode; only /ads resume starts them again."""
    async with ctx.locks.promo:
        campaigns = await store.enabled_campaigns(ctx.db)
        if not campaigns:
            return texts.PROMO_NO_CAMPAIGNS
        results = [(c, await _act(ctx, c, ActionKind.PAUSE, PausedBy.ADMIN)) for c in campaigns]
        failed = [campaign.src for campaign, error in results if error is not None]
        await store.insert_action(
            ctx.db, now=ctx.clock.now(), src=store.SUSPEND_ALL_SRC, action=ActionKind.SUSPEND_ALL,
            params={"failed": failed}, reason="", mode=ActionMode.ADMIN,
            status=ActionStatus.FAILED if failed else ActionStatus.APPLIED, decided_by=admin_id,
        )
    return texts.promo_stopped([texts.promo_stop_line(name=campaign.name, error=error) for campaign, error in results])


async def resume(ctx: AppContext, src: str, admin_id: int) -> str:
    """/ads resume: start one campaign again, on fresh numbers, if the season, its budget and the cap allow it."""
    campaign = await store.get_campaign(ctx.db, src)
    if campaign is None or not campaign.enabled:
        return texts.promo_unknown_src(src)
    if campaign.platform not in ctx.ad_platforms:
        return texts.promo_action_failed(name=campaign.name, error=texts.PROMO_ERROR_NO_KEYS)
    fetched = await fetch(ctx, [campaign], today_only=True)
    async with ctx.locks.promo:
        await save(ctx, fetched)
        current = await snapshot(ctx)
        campaign, facts = current.campaign(src), current.facts_of(src)
        if campaign is None or facts is None:
            return texts.promo_unknown_src(src)
        problem = _resume_problem(current, facts, fetched)
        if problem is not None:
            return problem
        error = await _act(ctx, campaign, ActionKind.RESUME, None)
        await _record_admin(ctx, campaign, ActionKind.RESUME, {}, admin_id, error)
    return texts.promo_resumed(campaign.name) if error is None else texts.promo_action_failed(
        name=campaign.name, error=error)


def _resume_problem(current: Snapshot, facts: CampaignFacts, fetched: Sequence[Fetched]) -> str | None:
    situation = current.situation
    if not rules.in_season(situation):
        return texts.promo_resume_out_of_season(ended=rules.season_ended(situation))
    if not _checked_now(fetched, facts.src):
        return texts.PROMO_NOT_CHECKED
    if not rules.budget_visible(facts, situation.today):
        return texts.promo_resume_blind(facts.platform)
    resumed = rules.with_state(current.facts, facts.src, CampaignState.ACTIVE)
    if not rules.cap_holds(resumed, situation):
        return _over_cap(resumed, situation)
    return None


async def set_budget(ctx: AppContext, src: str, gross_kop: int, admin_id: int) -> OutMessage | str:
    """/ads budget: set a budget (gross) on fresh numbers, keeping the platform minimum and, for a running
    campaign, the cap; a large change waits for a tap (then ``approve`` applies it)."""
    campaign = await store.get_campaign(ctx.db, src)
    if campaign is None or not campaign.enabled:
        return texts.promo_unknown_src(src)
    minimum = minimum_gross_kop(ctx.config.promo.vat_pct)
    if gross_kop < minimum:
        return texts.promo_budget_too_small(minimum)
    if campaign.platform not in ctx.ad_platforms:
        return texts.promo_action_failed(name=campaign.name, error=texts.PROMO_ERROR_NO_KEYS)
    fetched = await fetch(ctx, [campaign], today_only=True)
    async with ctx.locks.promo:
        await save(ctx, fetched)
        current = await snapshot(ctx)
        campaign, facts = current.campaign(src), current.facts_of(src)
        if campaign is None or facts is None:
            return texts.promo_unknown_src(src)
        if not _checked_now(fetched, src):
            return texts.PROMO_NOT_CHECKED
        budget = Budget(gross_kop, campaign.budget.kind if campaign.budget else _DEFAULT_KIND[campaign.platform])
        changed = rules.with_budget(current.facts, src, budget, current.situation.today)
        if facts.active and not rules.cap_holds(changed, current.situation):
            return _over_cap(changed, current.situation)
        from_kop = campaign.budget.kop if campaign.budget else None
        params = {"from_kop": from_kop, "to_kop": gross_kop, "kind": budget.kind}
        if _needs_confirmation(facts, budget, current.situation.today):
            action_id = await store.insert_action(
                ctx.db, now=ctx.clock.now(), src=src, action=ActionKind.SET_BUDGET, params=params, reason="",
                mode=ActionMode.ADMIN, status=ActionStatus.PROPOSED,
            )
            text = texts.promo_budget_confirm(
                name=campaign.name, from_kop=from_kop, to_kop=gross_kop, kind=budget.kind,
                net_kop=net_kop(gross_kop, ctx.config.promo.vat_pct),
                week_day_kop=_week_day_after(current, src, budget))
            return views.promo_budget_confirm(action_id, text, gross_kop, budget.kind)
        error = await _change_budget(ctx, campaign, budget)
        await _record_admin(ctx, campaign, ActionKind.SET_BUDGET, params, admin_id, error)
    return _budget_result(ctx, campaign, budget, error)


def _needs_confirmation(facts: CampaignFacts, budget: Budget, today: date) -> bool:
    """A budget not seen before, one raised by more than 30%, or a day that may cost 500 ₽ more."""
    if facts.budget is None:
        return True
    if budget.kop * 100 > facts.budget.kop * (100 + CONFIRM_RAISE_PCT):
        return True
    steady = replace(facts, previous_budget_kop=None, last_budget_change_day=None)
    before = rules.day_exposure(steady, today) or 0
    after = rules.day_exposure(replace(steady, budget=budget), today) or 0
    return after - before > CONFIRM_DAY_KOP


def _budget_result(ctx: AppContext, campaign: PromoCampaign, budget: Budget, error: str | None) -> str:
    net = net_kop(budget.kop, ctx.config.promo.vat_pct)
    if error is not None:
        return texts.promo_budget_failed(platform=campaign.platform, name=campaign.name, net_kop=net, error=error)
    return texts.promo_budget_set(name=campaign.name, gross_kop=budget.kop, kind=budget.kind, net_kop=net)


async def approve(ctx: AppContext, action_id: int, admin_id: int) -> str:
    """[Поднять до X ₽] or [Да, X ₽ …]: re-check the change against freshly fetched numbers, then make it.

    Raises ``TryAgain`` when the numbers could not be fetched: the proposal stays open.
    """
    action = await store.get_action(ctx.db, action_id)
    if action is None or action.action != ActionKind.SET_BUDGET:
        return texts.BUTTON_OUTDATED
    if action.status != ActionStatus.PROPOSED:
        return texts.PROMO_PROPOSAL_DECIDED[action.status]
    if action.mode == ActionMode.DRY:
        return texts.PROMO_PROPOSAL_DRY
    campaign = await store.get_campaign(ctx.db, action.src)
    fetched = [] if campaign is None or not campaign.enabled else await fetch(ctx, [campaign], today_only=True)
    async with ctx.locks.promo:
        await save(ctx, fetched)
        action = await store.get_action(ctx.db, action_id)
        assert action is not None
        if action.status != ActionStatus.PROPOSED:  # decided while the numbers were fetched
            return texts.PROMO_PROPOSAL_DECIDED[action.status]
        current = await snapshot(ctx)
        campaign, facts = current.campaign(action.src), current.facts_of(action.src)
        if campaign is None or facts is None:
            return await _expire(ctx, action_id, texts.PROMO_EXPIRED_REMOVED, admin_id)
        if not _checked_now(fetched, action.src):
            raise TryAgain(texts.PROMO_NOT_CHECKED)
        budget = Budget(action.params["to_kop"], BudgetKind(action.params["kind"]))
        problem = _approval_problem(ctx, current, action, facts, budget)
        if problem is not None:
            return await _expire(ctx, action_id, problem, admin_id)
        error = await _change_budget(ctx, campaign, budget)
        await store.finish_action(ctx.db, action_id, ActionStatus.APPLIED if error is None else ActionStatus.FAILED,
                                  error=error, decided_by=admin_id)
    if error is None and action.mode == ActionMode.AUTO:
        net = net_kop(budget.kop, ctx.config.promo.vat_pct)
        return texts.promo_raise_applied(name=campaign.name, to_kop=budget.kop, kind=budget.kind, net_kop=net)
    return _budget_result(ctx, campaign, budget, error)


def _approval_problem(
    ctx: AppContext, current: Snapshot, action: PromoAction, facts: CampaignFacts, budget: Budget
) -> str | None:
    """Why a proposal (or an admin's large change) no longer holds — then it expires — or None."""
    situation = current.situation
    if ctx.clock.now() - action.ts > PROPOSAL_LIFETIME:
        return texts.PROMO_EXPIRED_OLD
    if action.mode == ActionMode.AUTO:
        if not current.settings.auto:
            return texts.PROMO_EXPIRED_AUTO_OFF
        if not facts.active:
            return texts.PROMO_EXPIRED_NOT_RUNNING
        if facts.last_budget_change_day == situation.today:
            return texts.PROMO_EXPIRED_CHANGED
        if not rules.in_season(situation):
            return texts.promo_reason_season(ended=rules.season_ended(situation))
    if (facts.budget.kop if facts.budget else None) != action.params["from_kop"]:
        return texts.PROMO_EXPIRED_CHANGED
    changed = rules.with_budget(current.facts, facts.src, budget, situation.today)
    if facts.active and not rules.cap_holds(changed, situation):
        return texts.promo_expired_cap(situation.cap_kop)
    return None


async def decline(ctx: AppContext, action_id: int, admin_id: int) -> str:
    async with ctx.locks.promo:
        action = await store.get_action(ctx.db, action_id)
        if action is None or action.action != ActionKind.SET_BUDGET:
            return texts.BUTTON_OUTDATED
        if not await store.finish_action(ctx.db, action_id, ActionStatus.DECLINED, decided_by=admin_id):
            return texts.PROMO_PROPOSAL_DECIDED[action.status]
        campaign = await store.get_campaign(ctx.db, action.src)
    return texts.promo_raise_declined(name=campaign.name if campaign else action.src,
                                      from_kop=action.params["from_kop"])


async def _expire(ctx: AppContext, action_id: int, reason: str, admin_id: int) -> str:
    await store.finish_action(ctx.db, action_id, ActionStatus.EXPIRED, error=reason, decided_by=admin_id)
    return texts.promo_proposal_expired(reason)


def _over_cap(campaigns: Sequence[CampaignFacts], situation: Situation) -> str:
    return texts.promo_over_cap(cap_kop=situation.cap_kop, projected_kop=rules.projected_kop(campaigns, situation))


async def set_auto(ctx: AppContext, on: bool) -> str:
    async with ctx.locks.promo:
        await store.set_settings(ctx.db, auto=on)
        if not on:
            await store.expire_open_proposals(ctx.db, texts.PROMO_EXPIRED_AUTO_OFF)
    if not on:
        return texts.PROMO_AUTO_OFF
    settings = await store.get_settings(ctx.db)
    return texts.promo_auto_on(cap_kop=settings.cap_rub * 100, spent_kop=await store.total_spend(ctx.db),
                               rules=_rules_summary(ctx, settings))


async def set_cap(ctx: AppContext, cap_rub: int) -> str:
    await store.set_settings(ctx.db, cap_rub=cap_rub)
    return texts.promo_cap_set(cap_kop=cap_rub * 100, spent_kop=await store.total_spend(ctx.db))


async def set_rules(ctx: AppContext, **values: int) -> str:
    await store.set_settings(ctx.db, **values)
    return _rules_summary(ctx, await store.get_settings(ctx.db))


# --- carrying out ---------------------------------------------------------------------------------------------------


def _client(ctx: AppContext, platform: Platform) -> AdPlatform:
    client = ctx.ad_platforms.get(platform)
    if client is None:
        raise NotConfigured(platform)
    return client


async def _act(ctx: AppContext, campaign: PromoCampaign, action: ActionKind, paused_by: PausedBy | None) -> str | None:
    """Pause or start a campaign; what went wrong, or None. One campaign's failure never stops the others."""
    starting = action == ActionKind.RESUME
    try:
        client = _client(ctx, campaign.platform)
        if starting:
            await client.resume(campaign.external_id)
        else:
            await client.suspend(campaign.external_id)
    except AdApiError as error:
        await _on_failure(ctx, campaign.platform, error)
        return describe(error)
    except Exception:  # anything at all: the pauses of the other campaigns must still happen
        log.exception("ad platform call failed unexpectedly", extra={"src": campaign.src, "action": action})
        return texts.PROMO_ERROR_INTERNAL
    state = CampaignState.ACTIVE if starting else CampaignState.PAUSED
    await store.set_state(ctx.db, campaign.src, state, paused_by, ctx.clock.now())
    return None


async def _change_budget(ctx: AppContext, campaign: PromoCampaign, budget: Budget) -> str | None:
    """Set a budget on the platform and store it as the platform will report it (net, back to gross)."""
    try:
        await _client(ctx, campaign.platform).set_budget(campaign.external_id, budget.kop, budget.kind)
    except AdApiError as error:
        await _on_failure(ctx, campaign.platform, error)
        return describe(error)
    except Exception:  # as in _act
        log.exception("ad platform budget change failed unexpectedly", extra={"src": campaign.src})
        return texts.PROMO_ERROR_INTERNAL
    vat = ctx.config.promo.vat_pct
    reported = Budget(gross_kop(net_kop(budget.kop, vat), vat), budget.kind)
    await store.set_budget(ctx.db, campaign.src, reported, today=ctx.today(), now=ctx.clock.now())
    return None


async def _record_admin(
    ctx: AppContext, campaign: PromoCampaign, action: ActionKind, params: Mapping[str, object], admin_id: int,
    error: str | None,
) -> None:
    await store.insert_action(
        ctx.db, now=ctx.clock.now(), src=campaign.src, action=action, params=params, reason="",
        mode=ActionMode.ADMIN, status=ActionStatus.APPLIED if error is None else ActionStatus.FAILED,
        error=error, decided_by=admin_id,
    )


async def _on_failure(ctx: AppContext, platform: Platform, error: AdApiError) -> None:
    log.warning("ad platform call failed", extra={"platform": platform, "error": str(error)})
    if isinstance(error, AuthError):
        await _auth_alert(ctx, platform, error)


async def _auth_alert(ctx: AppContext, platform: Platform, error: AuthError) -> None:
    """The fix for the owner, at most once per 6 hours per platform."""
    log.error("ad platform refused our credentials", extra={"platform": platform, "code": error.code})
    await ctx.alerts.alert(f"promo_auth:{platform}",
                           texts.promo_auth_alert(platform=platform, code=error.code, detail=error.detail),
                           min_interval=AUTH_ALERT_INTERVAL)


def describe(error: Exception) -> str:
    """A short Russian description of a failure for the admins."""
    match error:
        case NotConfigured():
            return texts.PROMO_ERROR_NO_KEYS
        case AuthError():
            return texts.PROMO_ERROR_AUTH
        case RateLimited():
            return texts.PROMO_ERROR_BUSY
        case Transient():
            return texts.PROMO_ERROR_DOWN
        case PlatformError(code=platforms.UNSUPPORTED_BUDGET):
            return texts.PROMO_ERROR_BUDGET_KIND
        case PlatformError(code=platforms.NOT_FOUND):
            return texts.PROMO_ERROR_NOT_FOUND
        case PlatformError(code=platforms.BAD_RESPONSE):
            return texts.PROMO_ERROR_UNREADABLE
        case PlatformError(code=platforms.NOT_APPLIED):
            return texts.PROMO_ERROR_NOT_APPLIED
        case PlatformError():
            return texts.promo_error_platform(error.message)
        case AdApiError():
            return str(error)
    return texts.PROMO_ERROR_INTERNAL
