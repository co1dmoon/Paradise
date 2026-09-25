# PROMO SPEC: «Автопилот рекламы» for «Санта в чате»

Status: build spec for the coding agent. Read together with docs/SPEC.md (the bot) and docs/ADS_RESEARCH.md
(verified facts about the Yandex Direct API, the VK Ads API, the MAX channel API and Russian ad law, with sources).
When this spec and the official docs disagree, follow the docs and write the deviation into
"## Implementation notes" at the end of this file.

## 0. What and why

The owner asked for "an engine that advertises the bot by itself everywhere". Spam (posting into other people's
chats, communities or comments, mass messages to bot users, unmarked paid posts) is illegal in Russia or gets the
MAX bot banned, so it is OUT. This spec builds the legal version, inside the existing app process:

1. **Paid-ads autopilot** for Yandex Direct and VK Ads through their official APIs. The owner creates campaigns once
   in the web interfaces (with the ready kit from §9), registers them in the bot with one command, and from then on the
   autopilot every day pulls spend, joins it with the bot's own per-source conversions, pauses expensive campaigns,
   proposes budget raises for cheap ones, enforces a hard total cap, stops everything when the season window ends, and
   reports to the owner. Both platforms mark ads (ОРД/erid) automatically.
2. **Tracking links** for manual promotion (posts, community admins, partners): one command creates a link, the
   report shows what each link brought.
3. **Autoposting to the owner's OWN public MAX channel** from a ready season content calendar.
4. **A daily report** to the admins in MAX, with approve/decline buttons for budget raises.

Priorities: P0 must ship with tests green. P1 only after P0 is done.

Non-negotiable safety rules:
- **Dry-run by default.** Until an admin runs `/ads auto on`, the autopilot never calls a mutating API method; it
  reports what it *would* do. Show the mode in every report.
- **Pauses are automatic** (in auto mode). **Budget raises always need an admin tap** (§5.3). Resumes happen only by
  admin command.
- **Hard cap**: the autopilot suspends every registered campaign as soon as gross spend to date plus one more day of
  current budgets could cross the cap.
- **Season window**: outside PROMO_START..PROMO_END (Moscow dates) every registered active campaign is suspended.
- Never send promotional messages to bot users. The only new outbound messages are to admins and to the owner's own
  channel (MAX rules §1.5 of the developer requirements, see research).
- Never log tokens or secrets.

## 1. Where the code goes

- `app/promo/` package:
  - `direct.py` — Yandex Direct API v501 client.
  - `vkads.py` — VK Ads API client (token lifecycle included).
  - `platforms.py` — a small `AdPlatform` protocol both clients implement, plus the gross/net money conversion.
  - `attribution.py` — per-source metrics from the bot's DB.
  - `rules.py` — the pure decision engine (no I/O).
  - `autopilot.py` — orchestration: fetch → compute → decide → apply/propose → report.
  - `channel.py` + `content.py` — own-channel posting and the season content calendar.
  - `links.py` — manual tracking links.
- Admin commands go into the existing admin handler module (follow its structure); Russian copy into `app/core/texts.py`
  (or a `promo` section of it) like the rest of the bot.
- Scheduler jobs go into the existing scheduler with the same last-run-guard pattern.
- Migration: `app/migrations/NNN_promo.sql`.
- Tests: `tests/test_promo_*.py`. Fakes for both ad APIs live in `tools/` next to `fake_max.py` or in the tests.
- No new runtime dependencies.

## 2. Attribution (P0)

The bot already records `users.first_source` ('s:<src>', 'j:CODE', 'n:CODE', 'direct') and games' sources. The
landing page passes `?src=` / `utm_source=` into the `s_<src>` deep link, sanitized to `[a-z0-9]{1,16}`.

Source naming (all fit the sanitizer; do not change the sanitizer):
- Yandex Direct campaign: `yd<campaignId>` (e.g. `yd701234567`).
- VK Ads campaign (ad plan): `vk<adPlanId>`.
- Own MAX channel posts: `ch<NN>` (e.g. `ch07`).
- Manual links: `p<slug>` where slug is `[a-z0-9]{1,15}` chosen by the admin (e.g. `phabr`, `phrmsk`).

Metrics for a source S (implement in `attribution.py`, one query set, unit-tested on a seeded DB):
- `users`: users whose first_source is 's:S'.
- `organizers`: distinct users with first_source 's:S' who organized at least one game.
- `games`: games organized by those users (any source value on the game).
- `games3`: those games that reached 3+ active participants at any point (a drawn game with 3+ participants counts;
  check how the bot stores participant status and use the most robust definition; document it).
- `paid_rub`: sum of paid + granted payments on those games.
- `downstream_games`: games whose `source_game_id` is one of those games (one level; informational).
- Cohort maturity: `games3_matured(cutoff)` counts only games organized by users first seen before `cutoff`
  (= today 00:00 MSK minus PROMO_LAG_DAYS). Spend for the matured CPA uses days before the same cutoff.

Definitions used everywhere (report + rules):
- `cpa_raw = spend_gross_total / games3` (∞ when games3 = 0).
- `cpa_matured = spend_gross_before_cutoff / games3_matured`.
- `roas = paid_rub / spend_gross_total`.

## 3. Platforms (P0)

Common protocol (`platforms.py`):
- `list_campaigns(ids) -> list[CampaignInfo]` — id, name, state ('active'|'paused'|'other'), budget (gross RUB,
  kind 'week'|'day' or None if unknown).
- `daily_spend(ids, date_from, date_to) -> dict[(id, date), SpendRow]` — gross cost in kopecks (int), clicks.
- `suspend(id)`, `resume(id)`, `set_budget(id, gross_rub, kind)`.
- Errors map to: `AuthError` (token invalid/expired → admin alert with the exact fix), `RateLimited`,
  `Transient`, `PlatformError(code, message)`.
- All money inside the app is **gross (VAT included), integer kopecks**. Conversion uses PROMO_VAT_PCT (22).

### 3.1 Yandex Direct (`direct.py`)
Facts (see research §1; re-check the linked official pages):
- Base `https://api.direct.yandex.com/json/v501/` (sandbox `https://api-sandbox.direct.yandex.com/json/v5/` — v501 in
  the sandbox is unverified: if a v501 sandbox call fails, report it clearly rather than silently switching versions).
- Header `Authorization: Bearer <YANDEX_DIRECT_TOKEN>`, `Accept-Language: ru`. Log the `Units` header at debug level.
- `campaigns.get` with `SelectionCriteria.Ids` and `FieldNames` [Id, Name, State, Status] plus the unified-campaign
  field names needed to read the budget (`UnifiedCampaignFieldNames` with `BiddingStrategy`) — verify names in docs.
- `campaigns.suspend` / `campaigns.resume` with `SelectionCriteria.Ids`. Treat "already suspended/active" warnings as
  success.
- Budget: unified campaigns keep the budget inside the strategy (`WeeklySpendLimit`, micros, NET of VAT, min 300 ₽).
  Implement `set_budget(kind='week')` via `campaigns.update` exactly as the docs specify for UnifiedCampaign (read
  https://yandex.com/dev/direct/doc/en/campaigns/update-unified-campaign). If the docs do not make the required
  fields clear, implement it anyway per the best reading AND make failure non-fatal: the action is marked failed and the
  report tells the admin how to change the weekly budget by hand in the Direct UI.
- Spend: `reports` service, `CAMPAIGN_PERFORMANCE_REPORT`, FieldNames [Date, CampaignId, Clicks, Cost],
  `DateRangeType: CUSTOM_DATE`, `IncludeVAT: YES`, `Format: TSV`, headers `processingMode: auto`,
  `returnMoneyInMicros: false`, `skipReportHeader: true`, `skipReportSummary: true`. Handle 201/202 by waiting
  `retryIn` seconds (bounded total wait, e.g. 5 min, via the injectable clock/sleeper), then retry. ReportName must be
  unique per request parameters (include the date range and ids hash).
- Errors: JSON `error.error_code`; 53/54-style auth errors → AuthError (verify codes in docs), 152/506/9000-series
  limits → RateLimited/Transient per docs. Never retry non-idempotent calls blindly.

### 3.2 VK Ads (`vkads.py`)
Facts (research §2):
- Base `https://ads.vk.ru/api/`. OAuth2 `client_credentials` at `POST /api/v2/oauth2/token.json`
  (form-encoded), token lives ~24 h, refresh with `grant_type=refresh_token`.
- **At most 5 tokens per client+user; the 6th request fails.** Persist the token pair in the DB (table
  `promo_tokens`), reuse it, refresh before expiry, and only request a new one when there is none or refresh fails.
  If token creation fails because of the limit, call the documented token-deletion endpoint once
  (verify its path and parameters in the VK Ads API docs) and retry; alert the admin if that fails.
- `GET /api/v2/ad_plans.json` (fields incl. id, name, status, budget_limit_day, budget_limit) for registered ids.
- Pause/resume/budget: `POST /api/v2/ad_plans/mass_action.json` with `[{"id":…, "status":"blocked"|"active"}]` or
  `budget_limit_day` (NET of VAT, string or number per docs, min 300 ₽).
- Spend: `GET /api/v2/statistics/ad_plans/day.json?id=…&date_from=…&date_to=…&metrics=base`; `spent` is NET →
  convert to gross.
- Respect `X-RateLimit-*` headers; 429 → RateLimited.

## 4. Data model (P0) — migration NNN_promo.sql

- `promo_campaigns`: `src TEXT PK`, `platform TEXT` ('yd'|'vk'), `external_id TEXT`, `name TEXT`,
  `state TEXT` (last known), `budget_kind TEXT NULL`, `budget_kop INT NULL` (last known gross),
  `plan_budget_kop INT NULL` (budget when registered), `registered_at`, `enabled INT DEFAULT 1`,
  `last_budget_change_day TEXT NULL`, `paused_by TEXT NULL` ('autopilot'|'admin'|'cap'|'season'|NULL).
- `promo_spend`: `src`, `day` (YYYY-MM-DD MSK), `cost_kop INT`, `clicks INT`, `fetched_at`; PK(src, day). Upsert —
  platforms revise recent days.
- `promo_actions`: `id PK`, `ts`, `src`, `action` ('pause'|'resume'|'set_budget'|'suspend_all'),
  `params JSON`, `reason TEXT`, `mode` ('dry'|'auto'|'admin'), `status` ('proposed'|'applied'|'failed'|'declined'|
  'expired'), `error TEXT NULL`, `decided_by INT NULL`.
- `promo_links`: `src TEXT PK`, `title TEXT`, `created_at`, `created_by INT`.
- `promo_posts_sent`: `post_id TEXT PK`, `sent_at`, `mid TEXT NULL`.
- `promo_tokens`: `platform TEXT PK`, `access_token`, `refresh_token`, `expires_at`.
- `settings` keys (seeded from env, DB wins, like the bot's prices): `promo_auto` (0/1), `promo_cap_rub`,
  `promo_pause_cpa_rub`, `promo_scale_cpa_rub`, `promo_min_spend_rub`, `promo_max_raise_pct`, `promo_lag_days`,
  `promo_channel_on` (0/1).

Retention: promo tables hold no personal data except admin ids; keep them for the season, no purge needed.

## 5. Rules engine (P0, `rules.py`, pure and exhaustively unit-tested)

Input per campaign: src, platform, state, budget (kop, kind), plan budget, spend totals (all / before cutoff /
yesterday), games3 raw and matured, paid, last change day, paused_by. Global: cap, spent total (gross, all
campaigns), today (MSK), season window, thresholds. Output: ordered list of `Decision(src, action, params,
reason_ru, needs_approval: bool)`.

Order of evaluation:
1. **Season window**: if today is outside PROMO_START..PROMO_END → `pause` every active campaign
   (reason 'сезон закончился' / 'сезон ещё не начался'), no approval. Nothing else is evaluated.
2. **Hard cap**: let `day_equiv(budget) = budget if kind=='day' else ceil(budget/7)`; if
   `spent_total + Σ day_equiv(active budgets) > cap` → `pause` all active campaigns, reason 'достигнут лимит
   {cap} ₽', no approval. If budget is unknown for an active campaign, assume 35% of the weekly limit per day for
   Direct (research: a day may spend up to 35% of the weekly budget) or the day budget for VK; if nothing is known,
   assume `cap - spent_total` (i.e. be conservative).
3. **Expensive**: for each active campaign with `spend_before_cutoff >= min_spend` and
   (`games3_matured == 0` or `cpa_matured > pause_cpa`) → `pause`, reason with the numbers, no approval.
4. **Cheap**: for each active campaign with `games3_matured >= 2` and `cpa_matured < scale_cpa` and no budget change
   today and a known budget → `set_budget` to `min(budget * (1 + max_raise_pct/100), budget_that_keeps_cap)`, rounded
   to 10 ₽, never below the platform minimum 300 ₽ net, `needs_approval=True`. Skip if the raise would be < 50 ₽.
5. Otherwise no decision (the report still shows the numbers).

A campaign paused by an admin (`paused_by='admin'`) is never touched by rules 3–4.

## 6. Autopilot orchestration (P0, `autopilot.py`)

Jobs (Europe/Moscow, last-run guards, injectable clock):
- **promo_daily** at PROMO_REPORT_TIME (default 10:00): refresh campaign info, fetch spend for the last 7 days (and
  since registration on first run), recompute metrics, run rules, then:
  - dry mode: store decisions as `proposed` with mode 'dry', nothing applied;
  - auto mode: apply decisions without approval immediately (store `applied`/`failed`); store approval-needed ones as
    `proposed` and put buttons in the report.
  - send the report (§7) to every admin.
- **promo_guard** every 2 hours: refresh today's spend only and apply rule 1–2 (season + cap) in auto mode. Send a
  message only when it acts.
- Only runs when PROMO_ENABLED=1 and at least one platform is configured or at least one link/channel exists.
- A platform with missing credentials is skipped with a one-line note in the report.
- AuthError → admin alert with the exact fix (Direct: get a new OAuth token; VK: check client_id/secret), at most once
  per 6 hours, and the other platform keeps working.

Approval flow: report buttons `[Поднять до X ₽]` `[Не надо]` → callback payloads within the bot's 64-char limit, e.g.
`pa:<actionId>` / `pd:<actionId>`. On approve: re-check that the action is still `proposed`, not older than 24 h,
the campaign is still active and the cap still holds; then apply and edit the message with the result. Otherwise
mark `expired` and say why. Admin-only, re-checked from the DB.

## 7. Admin commands and report (P0)

Admin-only (reuse the bot's admin check):
- `/ads` — status: mode (тест/автопилот), cap and spent, per campaign one line (name, state, spend yesterday / total,
  games3, CPA, ROAS), pending proposals.
- `/ads add yd <campaignId> [имя]` and `/ads add vk <adPlanId> [имя]` — validate through the API (skip validation
  with a warning if credentials are missing), store, and reply with the exact tracking URL to put into the ad:
  `https://DOMAIN/?src=yd<id>` (or vk). Also give the platform macro alternative if verified in docs
  (e.g. Direct `{campaign_id}`), so one URL template works for all ads without re-moderation.
- `/ads remove <src>` — stop managing (does not pause on the platform).
- `/ads auto on|off` — switch real actions on/off. `on` replies with a clear summary of what the autopilot will do.
- `/ads stop` — suspend every registered campaign now (auto or not; admin action), `paused_by='admin'`.
- `/ads resume <src>` — resume one campaign (checks season and cap first).
- `/ads budget <src> <руб>` — set a budget now (admin action, still checks cap).
- `/ads cap <руб>` — change the total cap.
- `/ads rules <pause_cpa> <scale_cpa> [min_spend] [max_raise_pct]` — change thresholds.
- `/link <slug> [название]` — create a manual tracking link; reply with the landing URL `https://DOMAIN/?src=p<slug>`
  and the direct bot link `https://max.ru/BOT?start=s_p<slug>`.
- `/links` — per-link metrics (users, games, games3, paid).
- `/channel` — channel status and the next 3 scheduled posts; `/channel test` sends the next post to the admin
  privately as a preview; `/channel on|off`; `/channel send <postId>` sends one post now.

Daily report text (Russian, plain text, ≤ 4000 chars, truncate lists with 'и ещё N'):
```
Реклама — {date}. Режим: {тест — ничего не меняю | автопилот}.
Потрачено всего {spent} ₽ из {cap} ₽.
Яндекс «{name}»: вчера {y} ₽, всего {t} ₽ · игр на 3+: {g} · {cpa} ₽ за игру · оплат {paid} ₽ · {state}
VK «{name}»: …
{Сделал: «…» — пауза: {reason}.}
{Предлагаю: поднять бюджет «…» с {a} до {b} ₽ в {неделю|день}: {reason}} [Поднять до {b} ₽] [Не надо]
Ссылки: {slug}: {users} чел., {games} игр, {games3} на 3+, оплат {paid} ₽ · …
Канал MAX: постов {n}, пришло {users} чел., игр {games}.
```
In dry mode every action line starts with 'Сделал бы:' instead of 'Сделал:'.

## 8. Own MAX channel (P0)

- Config: `PROMO_MAX_CHANNEL_ID` (the channel chat_id; the bot must be a channel admin), `PROMO_CHANNEL_AD_LABEL`
  (optional text appended to each post, e.g. 'Реклама. Иванов И. И., ИНН …, erid: …' — empty by default).
- Sending: `POST /messages?chat_id=<channel>` through the existing MaxApi/outbox (limit ≤ 2/s per chat is already
  stricter in the bot's limiter). Link button `[Провести Тайного Санту]` → `https://max.ru/BOT?start=s_chNN`.
- `content.py`: a season calendar of 12–16 posts from 5 Nov to 24 Dec (MSK dates and times, e.g. 12:00), each
  `{id: 'ch01'…, date: 'MM-DD', time: 'HH:MM', text: …}`. Tone: useful and human, not salesy. Topics: how to run a
  Secret Santa at work / in a family / in a school class; exclusions for couples; budget etiquette; 10 gift ideas up
  to 500 / 1000 / 1500 ₽; anonymous questions to your recipient; what to do if someone didn't get a pair; reminder
  a week and a day before typical exchange dates; after-party reveal. Follow the bot's copy rules (no 'розыгрыш',
  'конкурс', 'приз', no English loanwords like 'вишлист'). No discounts or promo codes (keeps posts informational).
- Job **promo_channel** every 10 minutes: post every due, not-yet-sent post (idempotent via `promo_posts_sent`),
  skip posts more than 24 h overdue (mark skipped), only when `promo_channel_on=1` and the channel id is set.

## 9. Owner kit (P0, docs/PROMO_KIT_RU.md, plain Russian)

1. What the autopilot does and does not do, and why spam is out (bans, fines — cite the research numbers briefly).
2. Money plan: season window 24.11–12.12, test 6 000 ₽ then up to 15 000 ₽ total; VAT note (cabinets show net
   figures, the autopilot counts gross); platform minimums (300 ₽); stagger platforms if the budget is small.
   Be honest: at ~10% paid games and ~600 ₽ average check, one game brings ~60 ₽ directly, so paid ads mostly pay
   back through the viral loop and next season — the test is to learn the cost, not to profit immediately.
3. Yandex Direct step by step: account + «Данные рекламодателя»; creating the unified campaign in the UI (search
   only, Russia, dates, weekly budget, strategy), keywords and negative keywords lists (write them: 25–40 keywords
   around «тайный санта онлайн», «жеребьёвка тайный санта», «тайный санта для офиса/класса/семьи», «тайный санта в
   макс»; negatives like «скачать», «фильм», «песня», «костюм», «купить костюм», «игра на пк»), 8–10 titles and 4–5
   texts for the combinatorial ad (≤56/81 chars as the platform requires — verify limits), the tracking URL, then
   OAuth app + API application (timings), getting the token, `/ads add yd <id>`.
4. VK Ads step by step: cabinet «Физлицо» + details, «Сайт» campaign to the landing page, audiences (HR, teachers,
   parents of schoolchildren, office workers 25–45), 5–6 texts, image guidance (1080×1080, what to show), 18+ if
   required, API access (save client_secret within 10 minutes), `/ads add vk <id>`.
5. Own MAX channel: create a public channel via the partner platform, add the bot as admin, get the chat id (explain
   how the bot shows it: implement `/channel` printing the id of any channel where the bot was added — P1 if it needs
   the bot_added update), put it into .env.
6. Manual promotion that is allowed, with ready texts and a `/link` per place: family/work/friends chats; personal
   messages to HR, parent committees, teachers, coaches; a Habr «Я пиарюсь» post (once); asking community admins to
   post (template message) — and the rules: Pikabu, vc.ru and Habr forbid self-promo outside their ad products; paid
   posts need marking (Yandex ORD / VK AdBlogger).
7. Daily routine in season: read the report, tap approve/decline, check /ads, when to press /ads stop.
8. Legal checklist.

## 10. Config (.env.example additions, documented in Russian like the rest)

PROMO_ENABLED=0, YANDEX_DIRECT_TOKEN=, YANDEX_DIRECT_SANDBOX=0, VK_ADS_CLIENT_ID=, VK_ADS_CLIENT_SECRET=,
PROMO_CAP_RUB=15000, PROMO_PAUSE_CPA_RUB=250, PROMO_SCALE_CPA_RUB=150, PROMO_MIN_SPEND_RUB=500,
PROMO_MAX_RAISE_PCT=30, PROMO_LAG_DAYS=2, PROMO_START=11-24, PROMO_END=12-12, PROMO_VAT_PCT=22,
PROMO_REPORT_TIME=10:00, PROMO_MAX_CHANNEL_ID=, PROMO_CHANNEL_AD_LABEL=.
Config validation: fail fast on malformed values; missing credentials only disable that platform.
README_RU.md: add a short section pointing to docs/PROMO_KIT_RU.md and listing the new commands.

## 11. Tests (P0, offline)

- rules.py: every rule and their order; season window edges; cap with unknown budgets; admin-paused campaigns
  untouched; no double budget change per day; raise rounding/minimums; ∞ CPA; matured vs raw.
- attribution.py on a seeded DB: users/organizers/games/games3/paid/downstream per source; cohort cutoff.
- direct.py against a fake HTTP server (aiohttp test server): request shapes (URL v501, headers, bodies),
  reports TSV parsing incl. 201/202 + retryIn, money conversion, error mapping, suspend/resume idempotency.
- vkads.py fake server: token issue/reuse/refresh/limit→delete→retry, mass_action bodies, stats parsing, net→gross.
- autopilot: dry vs auto; guard job acting only on season/cap; approval flow (approve, decline, expired, stale cap);
  AuthError alert throttling; missing credentials.
- channel: schedule, idempotency, overdue skip, label appending, on/off.
- commands: /ads add/remove/auto/stop/resume/budget/cap/rules, /link, /links, /channel test; non-admins refused.
- The whole existing suite stays green; `python -m tools.simulate` still works.

## 12. Out of scope (v1)

- Creating campaigns, groups, ads or keywords through the APIs (fields partly unverified; the UI takes ~15 minutes
  with the kit).
- Offline conversions to Yandex Metrica or the VK pixel.
- Posting to VK communities (the `wall` permission is granted only on request), Dzen, Pikabu, vc.ru, Habr.
- Telegram in any form. Messenger placements inside Direct (manual test only, see kit).
- Any message to bot users that is not a direct response to their action.

## Implementation notes

Written with the implementation on 2026-09-25 and revised the same day after an independent review (14 findings,
all fixed; see the last list). The code is in `app/promo/`, the owner's kit in `docs/PROMO_KIT_RU.md`, the tests in
`tests/test_promo_*.py` (offline: aiohttp test servers stand in for both APIs, `tools/fake_ads.py` for the
orchestration tests).

### Checked on the official pages

Yandex Direct (yandex.com/dev/direct, yandex.ru/support/direct, yandex.ru/dev/id):
- JSON endpoints `https://api.direct.yandex.com/json/v501/{service}`; headers `Authorization: Bearer <token>` and
  `Accept-Language: ru`; the `Units` response header reads spent/left/limit (logged at debug level).
- `campaigns.get`: `FieldNames` [Id, Name, Type, State, Status] plus `UnifiedCampaignFieldNames` [BiddingStrategy].
  State ON → active, SUSPENDED → paused, anything else → other.
- A unified campaign's strategy has a `Search` and a `Network` part; the budget sits in the part's structure
  (WbMaximumClicks, WbMaximumConversionRate, AverageCpc, AverageCpa, AverageCrr, PayForConversion,
  PayForConversionCrr): `WeeklySpendLimit` (micros, net of VAT) or `CustomPeriodBudget {SpendLimit, StartDate,
  EndDate, AutoContinue}` — "When creating a campaign, you can't specify both". `campaigns.update` requires
  `BiddingStrategyType` in the part it sends, and "values of omitted parameters don't change"
  (best-practice/part-update).
- `campaigns.suspend` / `resume`: warnings 10020 "already suspended" and 10021 "not suspended"; other warnings
  include 10163 «Настройка не будет изменена» and 10165 «Параметр не будет применен» (errors-list).
- The weekly budget (support/direct/ru/strategies/week-budget): with 3+ days of impressions a day may take up to
  35% of the week plus what was carried over from last week; pay-per-click strategies carry over at most 30% of
  the budget; a change of the weekly budget restarts the week, and on that day «35% от старого бюджета + 35% от
  нового» (pay per click) or «100% старого + 100% нового бюджета, если все конверсии будут получены в один день»
  (pay per conversion) may be spent.
- Reports: CAMPAIGN_PERFORMANCE_REPORT with Date/CampaignId/Clicks/Cost, `IncludeVAT: YES`, TSV and the headers of
  §3.1; HTTP 201/202 with `retryIn`; offline reports are kept for 5 hours under their name.
- Error codes (errors-list): 53, 54, 58, 513, 3000 → `AuthError`; 152, 506 → `RateLimited`; 52, 1000, 1001, 1002
  → `Transient`.
- `{campaign_id}` is replaced in ad links (support/direct/statistics/url-tags).
- ResponsiveAd: up to 7 titles of ≤ 56 characters and up to 3 texts of ≤ 81 (the kit's copy was counted by script).
- A token for one's own account: an app with the redirect URI `https://oauth.yandex.ru/verification_code`, then
  `https://oauth.yandex.ru/authorize?response_type=token&client_id=…`; the token lives "не менее года".

VK Ads (ads.vk.ru/doc/api, ads.vk.ru/help):
- `POST v2/oauth2/token.json` (form) with `client_credentials` or `refresh_token`; `expires_in` is 86400 (a string in
  some examples); a refresh replaces the access token and the old one stops working.
- At most 5 tokens per client and user, the next request gets HTTP 403; `POST v2/oauth2/token/delete.json` with
  `client_id` and `client_secret` deletes the tokens of the account the API access belongs to.
- API errors 401 `{"code", "message"}`: `invalid_token` (issue a new token), `expired_token` (refresh),
  `invalid_client`, `invalid_user`, `revoked_token`.
- `GET v2/ad_plans.json?_id__in=…&fields=id,name,status,budget_limit_day,budget_limit&limit=50` → `{count, offset,
  items}`; status active/blocked/deleted; `budget_limit_day` (daily) and `budget_limit` (the whole campaign) are
  decimals net of VAT.
- `POST v2/ad_plans/mass_action.json`, a JSON array of up to 200 changes → 204.
- `GET v2/statistics/ad_plans/day.json?id=…&date_from=…&date_to=…&metrics=base` → `items[{id, rows[{date, base{clicks,
  spent}}]}]`, up to 200 ids; `spent` is net of VAT; an unknown or unavailable id fails the request with 400
  `ERR_WRONG_ADPLANS`.
- Limits are counted per method («для различных методов будут различные значения») and per calendar second, hour
  and day, in `X-RateLimit-RPS-Remaining`, `-Hourly-Remaining`, `-Daily-Remaining`; 429 means too many requests.
- URL macros (help/features/utm): `{{ad_plan_id}}` is the campaign and `{{campaign_id}}` the ad group; the group's
  «Параметры URL» take priority over the ad's link and by default add VK's own UTM tags (`utm_source=vk_ads`).
- Universal ads (help/features/formats): title ≤ 40, short text ≤ 90 characters, no emoji in the text, images
  1:1/4:5/16:9 up to 5 MB, text on an image ≤ 20%.

### Not verified, and what the code does about it

No failure below crashes a job or moves money: a failed read leaves the stored numbers, says so in the report and,
from the guard, alerts the admins (at most every 6 hours per platform); a failed change is stored as `failed` and
the admins get the manual steps. Mutating calls are never retried, except a VK change refused with 429 (see
deviations). **Before `/ads auto on` the kit (section 4.6) makes the owner run both budget writes once on
stopped campaigns with the minimum budget and compare the result in each web interface.**

1. **Direct sandbox at v501.** The documented sandbox is `…/json/v5/`. With YANDEX_DIRECT_SANDBOX=1 the client calls
   `https://api-sandbox.direct.yandex.com/json/v501/`, never switches versions, and a failure shows in the report
   with a note that the sandbox may not serve v501.
2. **The `campaigns.update` body** follows the docs but was never sent to the real API; nor is it stated whether
   `WeeklySpendLimit` may carry kopecks (the client sends the net amount floored to the kopeck, in micros). The
   client fails on any warning of the update and reads the budget back: if it did not change, the change is
   `failed` (`not_applied`) and the admin gets «Директ → кампания → Редактировать → Стратегия → Недельный бюджет
   N ₽». That the read-back sees the new value at once (the update being synchronous) is assumed.
3. **VK `budget_limit_day` as a string with kopecks** (`"385.24"`): if VK refuses it, same as 2 with «VK Реклама →
   кампания → Бюджет».
4. **Which Direct parts carry a budget.** `SERVING_OFF` and `NETWORK_DEFAULT` parts are taken to carry none (the
   latter as "the search settings"); budgets in both parts, a part with a strategy the client does not know
   (e.g. HIGHEST_POSITION) or without a budget it can read, and a budget for a period without readable dates leave
   the budget blind — the campaign is then paused on its own (deviation 3).
5. **Error bodies of the VK token endpoint** are not documented (only those of API calls are). The client reads the
   OAuth form `{"error": "invalid_client"}` and the VK forms `{"error": {"code"}}` / `{"code"}`; anything else
   becomes an `AuthError` with a generic "check the keys" alert. A 403 on a refresh is treated like the token
   limit (a new token is requested).
6. **Whether `token/delete.json` frees the limit.** On a 403 the client deletes the tokens once and asks once more;
   if VK still refuses, the admins get the `token_limit` alert (with the support address). There is no loop.
7. **Direct's invalid-token code.** The error list says 53, the token page says 1002. A 1002 whose text mentions the
   token is an `AuthError` (the "get a new token" alert); any other 1002 is a server error.
8. **Day boundaries.** Direct reports by the account's time zone, VK presumably by Moscow days, and VK's daily
   request limits are taken to reset at Moscow midnight. Spend is stored by the platforms' days; another zone could
   move spend between neighbouring days, never change totals.
9. **Whether Reports calls cost API points** is not stated; irrelevant at a few small reports a day.
10. **The MAX channel id** is accepted as any signed integer; the bot also sends the admins the id when it is added
    to a channel (bot_added, the P1 item of §9.5 is done).
11. Not needed by this implementation: VK objective and package codes (campaigns are created in the web interfaces,
    §12), a MAX bot link as an ad destination (ads lead to the landing page, which passes `src` into `s_<src>`), the
    real lead time of VK API access (the kit asks the owner to apply by 10 November).

### Deviations from this spec, and additions

1. Money in the protocol is integer kopecks: `set_budget(id, gross_kop, kind)`. Rubles appear only in commands and
   texts; `/ads budget` and `/ads cap` take whole gross rubles. A budget the bot sets is stored as the platform will
   report it (gross of the net amount floored to the kopeck), so a later read does not look like a cabinet change.
2. Schema (`003_promo.sql`, edited in place because it was never released): `plan_budget_*` was dropped (nothing
   reads it once blind budgets are paused, deviation 3); added `previous_budget_kop`, `limit_kop/_start/_end`,
   `pays_per_conversion`, `changed_at`, `spend_checked_at`, `paused_by = 'preseason'`, `promo_posts_sent.status`
   ('sent' | 'skipped' | 'failed') and `.outbox_id`, the table `promo_channels` (P1) and an index on
   `users(first_source)`. `promo_actions.src` is `'*'` for suspend_all; `promo_posts_sent.mid` stays empty (the
   outbox does not hand back message ids).
3. **Budgets (rule 2 of §5 replaced).** One more day of a campaign is the smallest visible bound: VK's daily budget;
   Direct's weekly budget at 45.5% of the week when paying per click (35% of the week plus up to 30% carried
   over) and 100% when paying per conversion, with the replaced budget(s) added on the day of a change (also a
   change noticed in the cabinet); what is left of VK's budget for the whole campaign or of Direct's budget for a
   period (the whole period budget when today is outside its dates). A campaign with none of these is **blind**:
   it is paused on its own (reason «не вижу бюджета…», `paused_by='autopilot'`) and left out of the others'
   projection, `/ads add` warns about it loudly and `/ads resume` refuses it. The cap check itself is unchanged.
4. Rule 4 (raises) floors the target to 10 ₽ (never above +max_raise_pct or the room under the cap — for a
   weekly budget counting the old and the new week on the change day), lifts it to the platform minimum (366 ₽
   gross at 22% VAT) only when the room allows, skips raises under 50 ₽ and needs numbers fetched within 4
   hours; several cheap campaigns share the room in turn. The raise message for Direct says that the week
   restarts and what the change day may cost.
5. **The season window** pauses before the season as `preseason` and after it as `season`. Campaigns paused
   `preseason` are started again by the first daily run inside the season (autopilot mode, a report line
   «Сделал: … — запуск: сезон начался»), each only with fresh numbers, a visible budget and room under the cap —
   otherwise they stay paused for the cap or the blind budget. In test mode the line says which `/ads resume` to
   run. This is the one start the autopilot makes without an admin.
6. An admin's pause is sticky: after `/ads stop` a campaign keeps `paused_by='admin'` even if the owner starts it in
   the cabinet, and rules 5–6 leave it alone until `/ads resume` (season, blind budgets and the cap still apply).
7. Proposals: in autopilot mode each raise comes as its own message after the report, with [Поднять до X ₽]
   [Не надо]; the answer replaces that message. In test mode the report line reads «Предложил бы: …» (pauses:
   «Сделал бы: …») without buttons. Open proposals expire after 24 hours and when the autopilot is switched off;
   a tap in test mode or after `/ads auto off` is refused. `/ads` adds the payback (ROAS), the games that
   participants of the source's games organized later (`downstream_games`), a budget for the whole campaign and
   how old the numbers are when they are older than 4 hours.
8. **Fresh numbers.** Fetches run outside the promo lock; what they bring is stored under it, together with the
   decisions, and a campaign the bot changed after a fetch began keeps the newer values (`changed_at`). A tap,
   `/ads resume` and `/ads budget` fetch their campaign and today's spend again first and refuse (keeping the
   buttons) when that fails. Spend requests ask only for campaigns the platform listed, and a refused request
   (VK's ERR_WRONG_ADPLANS) is repeated one campaign at a time, so one bad id hides nobody else's spend.
9. promo_guard acts only in autopilot mode and applies rules 1–3 (season, blind budgets, cap). It also refreshes
   the campaigns' states and budgets, and alerts the admins (at most every 6 hours per platform) when it cannot
   fetch fresh numbers. A failed automatic pause is reported as «СРОЧНО: не удалось остановить …».
10. `/ads budget` is gross; its usage text says «Директ — за неделю, VK — за день»; a new budget more than 30% above
    the current one, one whose day may cost over 500 ₽ more, or one set where none was visible waits for a tap
    ([Да, X ₽ в неделю/день] [Не надо]) and is re-checked like a raise when tapped. The cap is checked only for a
    running campaign (a paused one is checked by `/ads resume`).
11. `/ads remove` says that further spend is neither counted nor stopped at the season's end and, while the
    campaign may be running, offers [Остановить на площадке]. `/ads stop` stops «все подключённые кампании».
12. The Direct report name also carries the fetch time: offline reports are kept for 5 hours under their name, and
    a repeated name would return stale numbers. The whole wait for a report, requests included, is bounded by
    5 minutes.
13. The promo jobs run in a scheduler task of their own, so a slow platform never delays the bot's jobs.
14. VK limits are tracked per method: a read waits for its own method's hour or day; changes (pauses above all) are
    never held back, and one refused with 429 is sent again after 2, 5 and 10 s. Direct changes fail on any
    warning other than "already in that state" for suspend/resume.
15. The promo commands, buttons and the channel-id notice need PROMO_ENABLED=1 (otherwise one line explains how to
    switch it on). Settings are seeded on the first start with PROMO_ENABLED=1; `promo_auto` and `promo_channel_on`
    always start at 0. `/ads rules` takes an optional fifth value, the delay in days (0–14), because
    `promo_lag_days` is seeded once like the thresholds.
16. The landing page prefers `src` over `utm_source`, because VK adds `utm_source=vk_ads` to every link by default.
17. Channel posts use this year's dates, and `promo_posts_sent` is keyed by post id: v1 covers one season. A post MAX
    refuses is marked failed and `/channel send` sends it again. `/channel` and `/channel on` warn while the ad
    label is empty: the button under every post leads to a paid service, so the kit now recommends an erid.
18. One refusal or unexpected error of one campaign never stops the pauses of the others (errors are caught per
    platform and per campaign); VK's token-limit error is an `AdApiError`. The admins' alerts are throttled in
    memory, so a restart may repeat one.
19. The kit has 10 titles and 5 texts for Direct, split into two combinatorial ads (one takes at most 7 and 3).

### The review's findings (2026-09-25) and their fixes

1. Unknown budgets broke the cap → blind campaigns are paused alone; limits for the whole campaign or a period
   are used as bounds (deviation 3).
2. Direct weekly budgets were projected as 1/7 a day → 45.5% / 100% and both budgets on the change day; the raise
   message and the kit explain the restart (deviations 3–4).
3. The guard ran blind on stale spend → alerts, per-campaign fallback for refused spend requests, fresh numbers
   for raises, starts and admin changes (deviations 8–9).
4. One VK method's daily limit blocked every pause → limits per method, changes never held back and retried on 429
   (deviation 14).
5. Campaigns paused before the season stayed paused → `preseason` pauses start again at the season's start
   (deviation 5).
6. Overstated texts → `/ads remove`, `/ads stop` and the kit's VK «Каналы» campaign (end date, a budget for the
   whole campaign, not registered, the cap lowered by its budget) (deviation 11).
7. Unmarked channel posts → the kit recommends an erid from Yandex's ORD; `/channel` warns (deviation 17).
8. A 403 on the VK refresh escaped as a plain exception → an `AdApiError`, errors caught per platform and campaign
   (deviation 18).
9. Refreshes could overwrite admin changes → stored under the lock, newer changes kept (deviation 8).
10. A raise could be approved after `/ads auto off` → proposals voided, taps refused (deviation 7).
11. Unknown Direct update warnings passed as success → they fail, and the budget is read back (item 2 above).
12. `/ads budget` weekly vs daily confusion → usage text and confirmation (deviation 10).
13. A refused channel post could never be re-sent → marked failed, `/channel send` retries (deviation 17).
14. The report wait counted only sleeps and the promo jobs blocked the bot's jobs → a bounded total wait and a
    scheduler task of their own (deviations 12–13).
