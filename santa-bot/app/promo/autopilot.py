"""The ad autopilot (PROMO_SPEC §6): fetch → compute → decide → apply or propose → report.

Jobs (Moscow time; the scheduler keeps their last-run guards):
- promo_daily at PROMO_REPORT_TIME: refresh the campaigns and their spend (the last 7 days,
  since registration for a campaign never fetched), run rules 1–4, carry out or record the
  decisions and send the report to every admin;
- promo_guard every 2 hours in autopilot mode: refresh the campaigns and today's spend and
  apply rules 1–2 (season and cap). It writes only when it acted.

Test mode, the default until /ads auto on, never calls a mutating API method: decisions are
stored as proposals of mode 'dry' and the report says «Сделал бы». In autopilot mode pauses
happen at once; budget raises always wait for an admin's tap (a separate message with
[Поднять до X ₽] [Не надо], valid for 24 hours); campaigns start again only by /ads resume.
The admins' own commands (/ads stop, resume, budget) act in both modes and still respect
the season and the cap. A platform without credentials is skipped with a note; one that
refuses our keys alerts the admins at most once per 6 hours while the other keeps working.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from app.config import Config
from app.context import AppContext
from app.core import texts
from app.core.clock import Clock
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
    Transient,
    minimum_gross_kop,
    net_kop,
)
from app.promo.rules import ActionKind, CampaignFacts, Decision, PausedBy, Situation, Thresholds
from app.promo.store import ActionMode, ActionStatus, PromoAction, PromoCampaign, PromoSettings, SpendTotals
from app.promo.vkads import VkAdsClient

log = logging.getLogger(__name__)

GUARD_INTERVAL = 2 * 3600.0
AUTH_ALERT_INTERVAL = 6 * 3600.0
PROPOSAL_LIFETIME = timedelta(hours=24)
FETCH_DAYS = 7
_DEFAULT_KIND = {Platform.DIRECT: BudgetKind.WEEK, Platform.VK: BudgetKind.DAY}


class NotConfigured(AdApiError):
    """The platform has no credentials in .env."""


class CampaignNotFound(Exception):
    """/ads add: the platform does not list a campaign with this id."""


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


async def snapshot(ctx: AppContext) -> Snapshot:
    """The stored facts and settings as the rules see them."""
    db, promo, today = ctx.db, ctx.config.promo, ctx.today()
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
    facts = [_facts(c, spend.get(c.src, SpendTotals()), metrics[c.src]) for c in campaigns]
    return Snapshot(settings, campaigns, facts, metrics, situation)


def _facts(campaign: PromoCampaign, spend: SpendTotals, metrics: SourceMetrics) -> CampaignFacts:
    return CampaignFacts(
        src=campaign.src, platform=campaign.platform, name=campaign.name, state=campaign.state,
        budget=campaign.budget, plan_budget=campaign.plan_budget, spend_total_kop=spend.total_kop,
        spend_matured_kop=spend.matured_kop, spend_yesterday_kop=spend.yesterday_kop, games3=metrics.games3,
        games3_matured=metrics.games3_matured, paid_rub=metrics.paid_rub,
        last_budget_change_day=campaign.last_budget_change_day, paused_by=campaign.paused_by,
    )


async def has_work(ctx: AppContext) -> bool:
    """Whether there is anything to report: a platform, a campaign, a link or the channel."""
    return bool(ctx.ad_platforms or await store.enabled_campaigns(ctx.db) or await store.all_links(ctx.db)
                or ctx.config.promo.channel_id is not None)


# --- refreshing from the platforms ----------------------------------------------------------------------------


async def refresh(ctx: AppContext, campaigns: Sequence[PromoCampaign], *, today_only: bool) -> list[str]:
    """Store what each platform says about its campaigns and their spend; returns report notes."""
    notes: list[str] = []
    for platform in Platform:
        mine = [c for c in campaigns if c.platform == platform]
        if not mine:
            continue
        client = ctx.ad_platforms.get(platform)
        if client is None:
            notes.append(texts.promo_no_credentials(platform))
            continue
        try:
            notes += await _refresh_platform(ctx, client, mine, today_only=today_only)
        except AuthError as error:
            await _auth_alert(ctx, platform, error)
            notes.append(texts.promo_auth_note(platform))
        except AdApiError as error:
            log.warning("ad platform refresh failed", extra={"platform": platform, "error": str(error)})
            sandbox = platform == Platform.DIRECT and ctx.config.promo.direct_sandbox
            notes.append(texts.promo_fetch_failed(platform, describe(error), sandbox=sandbox))
    return notes


async def _refresh_platform(
    ctx: AppContext, client: AdPlatform, campaigns: Sequence[PromoCampaign], *, today_only: bool
) -> list[str]:
    notes = []
    ids = [c.external_id for c in campaigns]
    infos = {info.id: info for info in await client.list_campaigns(ids)}
    for campaign in campaigns:
        info = infos.get(campaign.external_id)
        if info is None:
            await store.mark_missing(ctx.db, campaign.src)
            notes.append(texts.promo_campaign_missing(name=campaign.name, src=campaign.src))
        else:
            await store.save_campaign_info(ctx.db, campaign.src, info)
    today = ctx.today()
    date_from = today if today_only else await _fetch_start(ctx, campaigns, today)
    spend = await client.daily_spend(ids, date_from, today)
    src_of = {c.external_id: c.src for c in campaigns}
    rows = {(src_of[external_id], day): row for (external_id, day), row in spend.items() if external_id in src_of}
    await store.replace_spend(ctx.db, list(src_of.values()), date_from, today, rows, ctx.clock.now())
    return notes


async def _fetch_start(ctx: AppContext, campaigns: Sequence[PromoCampaign], today: date) -> date:
    """The last 7 days; since registration for a campaign whose spend was never fetched."""
    start = today - timedelta(days=FETCH_DAYS - 1)
    for campaign in campaigns:
        if not await store.has_spend(ctx.db, campaign.src):
            start = min(start, campaign.registered_at.astimezone(ctx.config.tz).date())
    return start


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
    """promo_daily: refresh, decide, carry out or record, report (PROMO_SPEC §6)."""
    if not await has_work(ctx):
        return
    now = ctx.clock.now()
    await store.expire_proposals(ctx.db, now - PROPOSAL_LIFETIME)
    notes = await refresh(ctx, await store.enabled_campaigns(ctx.db), today_only=False)
    async with ctx.locks.promo:
        before = await snapshot(ctx)
        outcomes = await _carry_out(ctx, before, rules.decide(before.facts, before.situation), now)
    await _send_report(ctx, await snapshot(ctx), notes, outcomes)


async def guard_job(ctx: AppContext) -> None:
    """promo_guard: season and cap between the daily runs, in autopilot mode only."""
    campaigns = await store.enabled_campaigns(ctx.db)
    if not campaigns or not (await store.get_settings(ctx.db)).auto:
        return
    await refresh(ctx, campaigns, today_only=True)
    async with ctx.locks.promo:
        current = await snapshot(ctx)
        decisions = rules.protect(current.facts, current.situation)
        if not decisions:
            return
        outcomes = await _carry_out(ctx, current, decisions, ctx.clock.now())
    await ctx.alerts.notify_admins(texts.promo_guard_report([_outcome_line(outcome) for outcome in outcomes]))


async def _carry_out(ctx: AppContext, current: Snapshot, decisions: Sequence[Decision], now: datetime) -> list[Outcome]:
    """Pauses happen in autopilot mode; raises and everything in test mode become proposals."""
    mode = ActionMode.AUTO if current.settings.auto else ActionMode.DRY
    outcomes = []
    for decision in decisions:
        campaign = current.campaign(decision.src)
        assert campaign is not None
        status, error = ActionStatus.PROPOSED, None
        if mode == ActionMode.AUTO and not decision.needs_approval:
            error = await _pause_or_describe(ctx, campaign, PausedBy(decision.params["paused_by"]))
            status = ActionStatus.APPLIED if error is None else ActionStatus.FAILED
        action_id = await store.insert_action(
            ctx.db, now=now, src=decision.src, action=decision.action, params=decision.params,
            reason=decision.reason, mode=mode, status=status, error=error,
        )
        outcomes.append(Outcome(decision, campaign, status, mode, action_id, error))
    return outcomes


async def _pause_or_describe(ctx: AppContext, campaign: PromoCampaign, paused_by: PausedBy) -> str | None:
    try:
        await _pause(ctx, campaign, paused_by)
    except AdApiError as error:
        await _on_failure(ctx, campaign.platform, error)
        return describe(error)
    return None


# --- the report --------------------------------------------------------------------------------------------------


async def _send_report(ctx: AppContext, current: Snapshot, notes: Sequence[str], outcomes: Sequence[Outcome]) -> None:
    situation = current.situation
    header = texts.promo_report_header(day=situation.today, dry=not current.settings.auto,
                                       spent_kop=situation.spent_total_kop, cap_kop=situation.cap_kop)
    lines = [_campaign_line(facts) for facts in current.facts] or [texts.PROMO_NO_CAMPAIGNS]
    lines += [*notes, *(_outcome_line(outcome) for outcome in outcomes)]
    lines += await _links_and_channel_lines(ctx, current.settings)
    await ctx.alerts.notify_admins(texts.fit_lines(lines, header=header))
    for outcome in outcomes:
        if outcome.decision.needs_approval and outcome.mode == ActionMode.AUTO:
            await ctx.alerts.notify_admins(_proposal_message(ctx, outcome))


def _campaign_line(facts: CampaignFacts) -> str:
    return texts.promo_campaign_line(
        platform=facts.platform, name=facts.name, state=facts.state, paused_by=facts.paused_by,
        yesterday_kop=facts.spend_yesterday_kop, total_kop=facts.spend_total_kop, games3=facts.games3,
        cpa_kop=facts.spend_total_kop // facts.games3 if facts.games3 else None, paid_rub=facts.paid_rub,
    )


def _outcome_line(outcome: Outcome) -> str:
    decision, name = outcome.decision, outcome.campaign.name
    dry = outcome.mode == ActionMode.DRY
    if decision.action == ActionKind.SET_BUDGET:
        params = decision.params
        return texts.promo_proposal_line(name=name, from_kop=params["from_kop"], to_kop=params["to_kop"],
                                         kind=params["kind"], reason=decision.reason, dry=dry)
    if outcome.status == ActionStatus.FAILED:
        return texts.promo_pause_failed_line(name=name, reason=decision.reason, error=outcome.error or "")
    return texts.promo_paused_line(name=name, reason=decision.reason, dry=dry)


def _proposal_message(ctx: AppContext, outcome: Outcome) -> OutMessage:
    params = outcome.decision.params
    text = texts.promo_proposal(name=outcome.campaign.name, from_kop=params["from_kop"], to_kop=params["to_kop"],
                                kind=params["kind"], net_kop=net_kop(params["to_kop"], ctx.config.promo.vat_pct),
                                reason=outcome.decision.reason)
    return views.promo_proposal(outcome.action_id, text, params["to_kop"])


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
    lines += [_status_line(facts, current.metrics[facts.src]) for facts in current.facts] or [texts.PROMO_NO_CAMPAIGNS]
    for proposal in await store.open_proposals(ctx.db):
        campaign = current.campaign(proposal.src)
        if campaign is not None:
            lines.append(texts.promo_waiting_proposal(name=campaign.name, to_kop=proposal.params["to_kop"]))
    return texts.fit_lines(lines, header=header)


def _status_line(facts: CampaignFacts, metrics: SourceMetrics) -> str:
    budget = facts.budget
    return texts.promo_status_line(
        platform=facts.platform, name=facts.name, src=facts.src, state=facts.state, paused_by=facts.paused_by,
        yesterday_kop=facts.spend_yesterday_kop, total_kop=facts.spend_total_kop, games3=facts.games3,
        cpa_kop=facts.spend_total_kop // facts.games3 if facts.games3 else None, paid_rub=facts.paid_rub,
        downstream_games=metrics.downstream_games, budget_kop=budget.kop if budget else None,
        kind=budget.kind if budget else None,
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


async def unregister(ctx: AppContext, src: str) -> str:
    async with ctx.locks.promo:
        campaign = await store.get_campaign(ctx.db, src)
        if campaign is None or not campaign.enabled:
            return texts.promo_unknown_src(src)
        await store.disable_campaign(ctx.db, src)
    return texts.promo_removed(name=campaign.name, src=src)


async def stop_all(ctx: AppContext, admin_id: int) -> str:
    """/ads stop: suspend every registered campaign now, in any mode; only /ads resume starts them again."""
    async with ctx.locks.promo:
        campaigns = await store.enabled_campaigns(ctx.db)
        if not campaigns:
            return texts.PROMO_NO_CAMPAIGNS
        results = [(campaign, await _pause_or_describe(ctx, campaign, PausedBy.ADMIN)) for campaign in campaigns]
        failed = [campaign.src for campaign, error in results if error is not None]
        await store.insert_action(
            ctx.db, now=ctx.clock.now(), src=store.SUSPEND_ALL_SRC, action=ActionKind.SUSPEND_ALL,
            params={"failed": failed}, reason="", mode=ActionMode.ADMIN,
            status=ActionStatus.FAILED if failed else ActionStatus.APPLIED, decided_by=admin_id,
        )
    return texts.promo_stopped([texts.promo_stop_line(name=campaign.name, error=error) for campaign, error in results])


async def resume(ctx: AppContext, src: str, admin_id: int) -> str:
    """/ads resume: start one campaign again, if the season and the cap allow it."""
    async with ctx.locks.promo:
        current = await snapshot(ctx)
        campaign = current.campaign(src)
        if campaign is None:
            return texts.promo_unknown_src(src)
        situation = current.situation
        if not rules.in_season(situation):
            return texts.promo_resume_out_of_season(ended=rules.season_ended(situation))
        resumed = rules.with_change(current.facts, src, state=CampaignState.ACTIVE)
        if not rules.cap_holds(resumed, situation):
            return _over_cap(resumed, situation)
        try:
            await _client(ctx, campaign.platform).resume(campaign.external_id)
        except AdApiError as error:
            await _record_admin(ctx, campaign, ActionKind.RESUME, {}, admin_id, error=error)
            return texts.promo_action_failed(name=campaign.name, error=describe(error))
        await store.set_state(ctx.db, src, CampaignState.ACTIVE, None)
        await _record_admin(ctx, campaign, ActionKind.RESUME, {}, admin_id)
    return texts.promo_resumed(campaign.name)


async def set_budget(ctx: AppContext, src: str, gross_kop: int, admin_id: int) -> str:
    """/ads budget: set a budget now (gross), keeping the platform minimum and, for a running campaign, the cap."""
    async with ctx.locks.promo:
        current = await snapshot(ctx)
        campaign = current.campaign(src)
        if campaign is None:
            return texts.promo_unknown_src(src)
        situation = current.situation
        if gross_kop < situation.min_budget_kop:
            return texts.promo_budget_too_small(situation.min_budget_kop)
        budget = Budget(gross_kop, campaign.budget.kind if campaign.budget else _DEFAULT_KIND[campaign.platform])
        changed = rules.with_change(current.facts, src, budget=budget)
        if campaign.state == CampaignState.ACTIVE and not rules.cap_holds(changed, situation):
            return _over_cap(changed, situation)
        vat = ctx.config.promo.vat_pct
        params = {"from_kop": campaign.budget.kop if campaign.budget else None, "to_kop": gross_kop,
                  "kind": budget.kind}
        try:
            await _apply_budget(ctx, campaign, budget)
        except AdApiError as error:
            await _record_admin(ctx, campaign, ActionKind.SET_BUDGET, params, admin_id, error=error)
            return texts.promo_budget_failed(platform=campaign.platform, name=campaign.name,
                                             net_kop=net_kop(gross_kop, vat), error=describe(error))
        await _record_admin(ctx, campaign, ActionKind.SET_BUDGET, params, admin_id)
    return texts.promo_budget_set(name=campaign.name, gross_kop=gross_kop, kind=budget.kind,
                                  net_kop=net_kop(gross_kop, vat))


async def approve(ctx: AppContext, action_id: int, admin_id: int) -> str:
    """[Поднять до X ₽]: re-check the proposal against today's facts, then raise the budget."""
    async with ctx.locks.promo:
        action = await store.get_action(ctx.db, action_id)
        if action is None or action.action != ActionKind.SET_BUDGET:
            return texts.BUTTON_OUTDATED
        if action.status != ActionStatus.PROPOSED:
            return texts.PROMO_PROPOSAL_DECIDED[action.status]
        if action.mode != ActionMode.AUTO:
            return texts.PROMO_PROPOSAL_DRY
        current = await snapshot(ctx)
        campaign = current.campaign(action.src)
        if campaign is None:
            return await _expire(ctx, action_id, texts.PROMO_EXPIRED_REMOVED, admin_id)
        budget = Budget(action.params["to_kop"], BudgetKind(action.params["kind"]))
        problem = _approval_problem(ctx, action, campaign, current, budget)
        if problem is not None:
            return await _expire(ctx, action_id, problem, admin_id)
        net = net_kop(budget.kop, ctx.config.promo.vat_pct)
        try:
            await _apply_budget(ctx, campaign, budget)
        except AdApiError as error:
            await _on_failure(ctx, campaign.platform, error)
            await store.finish_action(ctx.db, action_id, ActionStatus.FAILED, error=describe(error),
                                      decided_by=admin_id)
            return texts.promo_budget_failed(platform=campaign.platform, name=campaign.name, net_kop=net,
                                             error=describe(error))
        await store.finish_action(ctx.db, action_id, ActionStatus.APPLIED, decided_by=admin_id)
    return texts.promo_raise_applied(name=campaign.name, to_kop=budget.kop, kind=budget.kind, net_kop=net)


async def decline(ctx: AppContext, action_id: int, admin_id: int) -> str:
    async with ctx.locks.promo:
        action = await store.get_action(ctx.db, action_id)
        if action is None or action.action != ActionKind.SET_BUDGET:
            return texts.BUTTON_OUTDATED
        if not await store.finish_action(ctx.db, action_id, ActionStatus.DECLINED, decided_by=admin_id):
            return texts.PROMO_PROPOSAL_DECIDED[action.status]
        campaign = await store.get_campaign(ctx.db, action.src)
    name = campaign.name if campaign else action.src
    return texts.promo_raise_declined(name=name, from_kop=action.params["from_kop"])


async def _expire(ctx: AppContext, action_id: int, reason: str, admin_id: int) -> str:
    await store.finish_action(ctx.db, action_id, ActionStatus.EXPIRED, error=reason, decided_by=admin_id)
    return texts.promo_proposal_expired(reason)


def _over_cap(campaigns: Sequence[CampaignFacts], situation: Situation) -> str:
    return texts.promo_over_cap(cap_kop=situation.cap_kop, projected_kop=rules.projected_kop(campaigns, situation))


def _approval_problem(
    ctx: AppContext, action: PromoAction, campaign: PromoCampaign, current: Snapshot, budget: Budget
) -> str | None:
    """Why a proposal no longer holds (then it expires), or None."""
    situation = current.situation
    if ctx.clock.now() - action.ts > PROPOSAL_LIFETIME:
        return texts.PROMO_EXPIRED_OLD
    if campaign.state != CampaignState.ACTIVE:
        return texts.PROMO_EXPIRED_NOT_RUNNING
    current_kop = campaign.budget.kop if campaign.budget else None
    if current_kop != action.params["from_kop"] or campaign.last_budget_change_day == situation.today:
        return texts.PROMO_EXPIRED_CHANGED
    if not rules.in_season(situation):
        return texts.promo_reason_season(ended=rules.season_ended(situation))
    if not rules.cap_holds(rules.with_change(current.facts, action.src, budget=budget), situation):
        return texts.promo_expired_cap(situation.cap_kop)
    return None


async def set_auto(ctx: AppContext, on: bool) -> str:
    await store.set_settings(ctx.db, auto=on)
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


async def _pause(ctx: AppContext, campaign: PromoCampaign, paused_by: PausedBy) -> None:
    await _client(ctx, campaign.platform).suspend(campaign.external_id)
    await store.set_state(ctx.db, campaign.src, CampaignState.PAUSED, paused_by)


async def _apply_budget(ctx: AppContext, campaign: PromoCampaign, budget: Budget) -> None:
    await _client(ctx, campaign.platform).set_budget(campaign.external_id, budget.kop, budget.kind)
    await store.set_budget(ctx.db, campaign.src, budget, ctx.today())


async def _record_admin(
    ctx: AppContext, campaign: PromoCampaign, action: ActionKind, params: Mapping[str, object], admin_id: int,
    *, error: AdApiError | None = None,
) -> None:
    if error is not None:
        await _on_failure(ctx, campaign.platform, error)
    await store.insert_action(
        ctx.db, now=ctx.clock.now(), src=campaign.src, action=action, params=params, reason="",
        mode=ActionMode.ADMIN, status=ActionStatus.APPLIED if error is None else ActionStatus.FAILED,
        error=None if error is None else describe(error), decided_by=admin_id,
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


def describe(error: AdApiError) -> str:
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
        case PlatformError():
            return texts.promo_error_platform(error.message)
    return str(error)
