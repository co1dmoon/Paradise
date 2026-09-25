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

Written with the implementation on 2026-09-25. The code is in `app/promo/`, the owner's kit in
`docs/PROMO_KIT_RU.md`, the tests in `tests/test_promo_*.py` (offline: aiohttp test servers stand in for both APIs,
`tools/fake_ads.py` for the orchestration tests).

### Checked on the official pages

Yandex Direct (yandex.com/dev/direct, yandex.ru/support/direct, yandex.ru/dev/id):
- JSON endpoints `https://api.direct.yandex.com/json/v501/{service}`; headers `Authorization: Bearer <token>` and
  `Accept-Language: ru`; the `Units` response header reads spent/left/limit (logged at debug level).
- `campaigns.get`: `FieldNames` [Id, Name, Type, State, Status] plus `UnifiedCampaignFieldNames` [BiddingStrategy].
  State ON → active, SUSPENDED → paused, anything else → other.
- The budget of a unified campaign: `UnifiedCampaign.BiddingStrategy.{Search|Network}.<Structure>.WeeklySpendLimit`
  (micros, net of VAT) in WbMaximumClicks, WbMaximumConversionRate, AverageCpc, AverageCpa and PayForConversion.
  `campaigns.update` requires `BiddingStrategyType` in the strategy part it sends, and "values of omitted parameters
  don't change" (best-practice/part-update), so an update sends the current type and the new `WeeklySpendLimit` only.
- `campaigns.suspend` / `resume`: the per-object warnings 10020 "already suspended" and 10021 "not suspended" count
  as success.
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
  items}`; status active/blocked/deleted; budgets are decimals net of VAT.
- `POST v2/ad_plans/mass_action.json`, a JSON array of up to 200 changes → 204.
- `GET v2/statistics/ad_plans/day.json?id=…&date_from=…&date_to=…&metrics=base` → `items[{id, rows[{date, base{clicks,
  spent}}]}]`, up to 200 ids; `spent` is net of VAT.
- `X-RateLimit-RPS-Remaining`, `-Hourly-Remaining`, `-Daily-Remaining`; 429 means too many requests.
- URL macros (help/features/utm): `{{ad_plan_id}}` is the campaign and `{{campaign_id}}` the ad group; the group's
  «Параметры URL» take priority over the ad's link and by default add VK's own UTM tags (`utm_source=vk_ads`).
- Universal ads (help/features/formats): title ≤ 40, short text ≤ 90 characters.

### Not verified, and what the code does about it

No failure below crashes a job or moves money: a failed read leaves the stored numbers and says so in the report,
a failed change is stored as `failed` and the admins get the manual steps. Mutating calls are never retried.

1. **Direct sandbox at v501.** The documented sandbox is `…/json/v5/`. With YANDEX_DIRECT_SANDBOX=1 the client calls
   `https://api-sandbox.direct.yandex.com/json/v501/`, never switches versions, and a failure shows in the report
   with a note that the sandbox may not serve v501.
2. **The `campaigns.update` body** follows the docs but was never sent to the real API; nor is it stated whether
   `WeeklySpendLimit` may carry kopecks (the client sends the net amount floored to the kopeck, in micros). If Direct
   refuses, the change is `failed` and the admin gets «Директ → кампания → Редактировать → Стратегия → Недельный
   бюджет N ₽» with the net amount.
3. **Weekly vs period budget.** The docs do not say which limit applies when a strategy returns both
   `WeeklySpendLimit` and a `CustomPeriodBudget` (or `BudgetType: CUSTOM_PERIOD_BUDGET`). Such a campaign counts as
   having no weekly budget: no raises, `/ads budget` refuses with an explanation, and the cap uses the rule for an
   unknown budget. The kit tells the owner to use a weekly budget.
4. **VK `budget_limit_day` as a string with kopecks** (`"385.24"`): if VK refuses it, same as 2 with «VK Реклама →
   кампания → Бюджет».
5. **Error bodies of the VK token endpoint** are not documented (only those of API calls are). The client reads the
   OAuth form `{"error": "invalid_client"}` and the VK forms `{"error": {"code"}}` / `{"code"}`; anything else
   becomes an `AuthError` with a generic "check the keys" alert.
6. **Whether `token/delete.json` frees the limit.** On a 403 the client deletes the tokens once and asks once more;
   if VK still refuses, the admins get the `token_limit` alert (with the support address). There is no loop.
7. **Direct's invalid-token code.** The error list says 53, the token page says 1002. A 1002 whose text mentions the
   token is an `AuthError` (the "get a new token" alert); any other 1002 is a server error.
8. **Day boundaries of the reports.** Direct reports by the account's time zone, VK presumably by Moscow days. Spend
   is stored by the platforms' days; another zone could move spend between neighbouring days, never change totals.
9. **Whether Reports calls cost API points** is not stated; irrelevant at one small report per run.
10. **The MAX channel id** is accepted as any signed integer; the bot also sends the admins the id when it is added
    to a channel (bot_added, the P1 item of §9.5 is done). A post MAX refuses reaches the admins through the outbox
    delivery hook (at most every 5 minutes).
11. Not needed by this implementation: VK objective and package codes (campaigns are created in the web interfaces,
    §12), a MAX bot link as an ad destination (ads lead to the landing page, which passes `src` into `s_<src>`), the
    real lead time of VK API access (the kit asks the owner to apply by 10 November).

### Deviations from this spec, and additions

1. Money in the protocol is integer kopecks: `set_budget(id, gross_kop, kind)`. Rubles appear only in commands and
   texts; `/ads budget` and `/ads cap` take whole gross rubles.
2. Schema additions: `promo_campaigns.plan_budget_kind` (to read the plan budget), `promo_posts_sent.status`
   ('sent' | 'skipped'), the table `promo_channels` (channels the bot was added to, P1) and an index on
   `users(first_source)`. `promo_actions.src` is `'*'` for suspend_all. `promo_posts_sent.mid` stays empty: the outbox
   does not hand back message ids.
3. Attribution: `users` counts the `user_first_seen` events of a source, so people removed by the 7-day retention of
   unconsented users still count. `games3` = drawn games (`drawn_at` survives later departures) or games with 3+
   active participants now; the participants table keeps no history, so a game that had 3 and lost people before a
   draw is not counted (the error is on the cautious side).
4. Rule 2 uses `ceil(week / 7)` for a weekly budget as written, although Direct may spend up to 35% of it in one day;
   the 35% share is used only for a Direct campaign whose current budget is unknown and whose plan budget was weekly.
   With nothing known it assumes `max(cap − spent, 1 kopeck)`.
5. Rule 4 floors the target to 10 ₽ (never above +max_raise_pct or the cap room), lifts it to the platform minimum
   (366 ₽ gross at 22% VAT) only when the cap room allows, skips raises under 50 ₽, and several cheap campaigns
   share the cap room in turn.
6. An admin's pause is sticky: after `/ads stop` a campaign keeps `paused_by='admin'` even if the owner starts it in
   the cabinet, and rules 3–4 leave it alone until `/ads resume` (rules 1–2 still apply).
7. Proposals: in autopilot mode each raise comes as its own message right after the report, with [Поднять до X ₽]
   [Не надо]; the answer replaces that message. In test mode the report line reads «Предложил бы: …» (pauses:
   «Сделал бы: …») without buttons. Open proposals expire after 24 hours, also swept by the daily job. `/ads` adds
   the payback (ROAS) and the games that participants of the source's games organized later (`downstream_games`).
8. promo_guard acts only in autopilot mode (in test mode the daily report says what it would do) and also refreshes
   the campaigns' states and budgets, which rules 1–2 need.
9. A budget set by `/ads budget` is checked against the cap only for a running campaign; a paused one is checked by
   `/ads resume`.
10. The Direct report name also carries the fetch time: offline reports are kept for 5 hours under their name, and
    a repeated name would return stale numbers.
11. The promo commands, buttons and the channel-id notice need PROMO_ENABLED=1 (otherwise one line explains how to
    switch it on). Settings are seeded on the first start with PROMO_ENABLED=1; `promo_auto` and `promo_channel_on`
    always start at 0.
12. The landing page now prefers `src` over `utm_source`, because VK adds `utm_source=vk_ads` to every link by
    default.
13. One lock (`locks.promo`) serializes the autopilot's decisions with the admins' ad commands and taps; the network
    refresh runs outside it.
14. Channel posts use this year's dates, and `promo_posts_sent` is keyed by post id: v1 covers one season (next
    season needs new ids or a cleared table). The admins' credential alerts are throttled in memory, so a restart may
    repeat one.
15. The scheduler runs jobs one after another: a Direct offline report can hold the daily job for up to 5 minutes,
    delaying the minute jobs that one time.
16. The kit has 10 titles and 5 texts for Direct, split into two combinatorial ads (one takes at most 7 and 3).
17. `/ads rules` takes an optional fifth value, the delay in days (0–14): `promo_lag_days` is seeded once like the
    thresholds, so without it the delay could never change after the first start.
