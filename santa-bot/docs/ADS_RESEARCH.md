# Research brief: a legal "ad autopilot" for «Санта в чате» (MAX bot), as of 2026-09-25

**How to read the tags.** ✅ means I checked it on an official page. ◐ means it comes from an official Yandex or VK blog or education page. ⚠ means only a third-party source says it. ❓ means I could not verify it, and I give the reason.

**Access problems I hit.**
- fas.gov.ru and rkn.gov.ru reset the connection or returned 503 from this environment. FAS statements below are cited through the outlets that quoted them.
- ads.vk.com now redirects to **ads.vk.ru**. WebFetch refused vk.* domains, so I read ads.vk.ru and dev.vk.com with curl.

---

## 1. Yandex Direct API (v5 / v501)

**Getting access as an individual or self-employed person**
- **Steps** ✅:
  1. Get a Yandex ID.
  2. Create an OAuth app at https://oauth.yandex.ru/?dialog=create-client-entry. Choose «Для доступа к API или отладки» and grant the `direct:api` permission. The form also lists `passport:business`. You receive a ClientID and a Client secret.
  3. In Direct, open the API settings page (https://direct.yandex.ru/registered/main.pl?cmd=apiSettings), then «Мои заявки» → «Новая заявка». Pick the ClientID, describe the app and accept the terms. Source: https://yandex.ru/dev/direct/doc/ru/concepts/register
- **Precondition** ✅: «Чтобы получить доступ к странице настроек API, необходимо создать хотя бы одну кампанию в веб-интерфейсе Директ Про» (same page).
- **Review time** ✅: "from one hour to three business days (up to seven at peak)". Reviews happen on Russian business days, 10:00–19:00. Only the developer may apply, and it is one application per ClientID. Sources: https://yandex.ru/dev/direct/doc/ru/access-request and https://yandex.ru/dev/direct/doc/ru/troubleshooting/register
- **Requests need** ✅: an approved application, a user who owns a Direct account (a business, agency or agency-client representative) and has accepted the API terms, and an OAuth token in `Authorization`. Source: https://yandex.ru/dev/direct/doc/ru/concepts/access. The API is free: https://yandex.ru/support/direct/alternative-interfaces/api.html
- **Individual or self-employed eligibility** ❓: no official page I read restricts API access by legal form. Individuals and self-employed people can advertise and pay in Direct ✅ (https://yandex.ru/support/direct/ru/payments/faq).

**"Test" vs "full" access** ❓
- The current official pages I read describe a single application and do not mention two tiers.
- The test/full split (test = sandbox only) appears in a 2018 third-party guide (https://convertmonster.ru/blog/kontekstnaya-reklama-blog/kak-poluchit-polnyj-dostup-k-api-yandex-direct/) and in a search snippet. I could not find it on a current official page.

**Sandbox** ✅
- Endpoint: `https://api-sandbox.direct.yandex.com/json/v5/{service}`. It is fully isolated from real data and keeps data for one month after last use.
- In the sandbox, Reports can only cover one campaign per report.
- Turn it on at API settings → «Песочница» → Launch. You can choose the Client role and have test campaigns created. Sources: https://yandex.com/dev/direct/doc/en/concepts/sandbox and https://yandex.com/dev/direct/doc/en/concepts/sandbox-init
- ❓ Whether the sandbox serves v501 (the version the unified campaign needs) is unverified.

**Campaign types that matter now** ✅
- UNIFIED_CAMPAIGN (Единая перфоманс-кампания) supports add, update, delete, suspend, resume, archive, unarchive and get. It must use `https://api.direct.yandex.com/v501/`. Source: https://yandex.ru/dev/direct/doc/ru/objects/campaign
- TEXT_CAMPAIGN is still listed. However, since «22 мая» (the page gives no year), apps that create text campaigns «продолжат работать в режиме совместимости, но создаваться будет ЕПК». Source: https://yandex.ru/dev/direct/doc/ru/unified-campaign-update
- Since 30 June 2026, text-and-image ads inside unified campaigns are edit-only. From 14 July 2026 they were converted to combinatorial ads (RESPONSIVE_AD). `ads.add` with `TextAd` now creates a combinatorial ad. Sources: https://yandex.com/dev/direct/doc/ru/update-tga and https://yandex.ru/dev/direct/doc/ru/objects/ad; the 2026 date comes from https://www.seonews.ru/events/s-14-iyulya-yandeks-direkt-obnovit-tekstovo-graficheskie-obyavleniya-do-kombinatornykh/
- **What to build on: UNIFIED_CAMPAIGN + UNIFIED_AD_GROUP + ResponsiveAd.**

**Endpoints (JSON)** ✅
All of these are at `https://api.direct.yandex.com/json/v501/…`:
- `campaigns`: add, update, delete, suspend, resume, archive, unarchive, get. At most 10 campaigns per `add`. https://yandex.ru/dev/direct/doc/ru/campaigns/campaigns, https://yandex.com/dev/direct/doc/en/campaigns/add
- `adgroups`: add, update, delete, get. The unified group requires `"UnifiedAdGroup":{"OfferRetargeting":"YES|NO"}`, plus `Name`, `CampaignId` and `RegionIds`. https://yandex.ru/dev/direct/doc/ru/adgroups/adgroups, https://yandex.ru/dev/direct/doc/en/adgroups/add
- `keywords`: add, update, delete, suspend, resume, get. At most 1000 objects per call. https://yandex.ru/dev/direct/doc/ru/keywords/keywords, https://yandex.ru/dev/direct/doc/en/keywords/add
- `ads`: add, update, delete, suspend, resume, archive, unarchive, moderate, get. https://yandex.ru/dev/direct/doc/ru/ads/ads
  - `ResponsiveAd` fields: `Titles`[] and `Texts`[] are required; `Href`, `AdImageHashes`, `SitelinkSetId`, `ErirAdDescription` and others are optional. https://yandex.ru/dev/direct/doc/ru/ads/add
  - `ads.moderate` only accepts ads in DRAFT status, and the group must already have targeting such as keywords. https://yandex.ru/dev/direct/doc/ru/ads/moderate
- `campaigns.suspend` / `resume` take `{"method":"suspend","params":{"SelectionCriteria":{"Ids":[…]}}}`, up to 1000 IDs. https://yandex.com/dev/direct/doc/en/campaigns/suspend

**Budgets**
- **Structure** ✅: in a unified campaign the budget sits inside the strategy: `UnifiedCampaign.BiddingStrategy.Search.WbMaximumClicks{WeeklySpendLimit (required), BidCeiling, CustomPeriodBudget{SpendLimit, StartDate, EndDate, AutoContinue}}`.
  - Money values are integers equal to the amount × 1,000,000.
  - To show on search only: set `Search.PlacementTypes{SearchResults…}` and `Network.BiddingStrategyType:"SERVING_OFF"`.
  - Sources: https://yandex.ru/dev/direct/doc/ru/campaigns/add-unified-campaign, https://yandex.com/dev/direct/doc/en/campaigns/update-unified-campaign
- **Daily budget** ✅: the top-level `DailyBudget` works only with a manual strategy; otherwise the API returns an error. https://yandex.com/dev/direct/doc/en/campaigns/add
- **Minimums**:
  - Weekly budget is at least 300 ₽ ✅. At 3+ show-days a day may spend up to 35% of the weekly budget ✅. Changing it mid-week can cause overspend ✅. https://yandex.ru/support/direct/ru/strategies/week-budget
  - Daily budget is at least 300 ₽, and a period budget at least 50 ₽ per day ◐. Budgets can change up to 3 times a day; decreases apply the next day, increases immediately ◐. https://b2b.yandex.ru/adv/edu/materials/kakoj-byvaet-bjudzhet-v-direct
- **VAT basis** ✅: «Все денежные показатели в аккаунте рекламодателя указаны без учета НДС». All budgets and bids are net of VAT. https://yandex.ru/support/direct/ru/payments/faq

**Reports (daily spend and clicks per campaign)** ✅
- Endpoint: `POST https://api.direct.yandex.com/json/v501/reports`. https://yandex.ru/dev/direct/doc/ru/reports
- Example body: `ReportType:"CAMPAIGN_PERFORMANCE_REPORT"`, `FieldNames:["Date","CampaignId","Clicks","Cost"]`, `IncludeVAT:"YES"`, `Format:"TSV"`. https://yandex.ru/dev/direct/doc/en/example
- `DateRangeType:"CUSTOM_DATE"` needs `DateFrom`/`DateTo`. https://yandex.ru/dev/direct/doc/ru/reports
- Headers: `processingMode` (online / offline / auto), `returnMoneyInMicros:false`, `skipReportHeader`, `skipColumnHeader`, `skipReportSummary`. https://yandex.ru/dev/direct/doc/en/headers
- Response codes: 200 = ready, 201/202 = queued; use the `retryIn` header. https://yandex.ru/dev/direct/doc/en/mode
- Limits: at most 20 Reports requests per 10 seconds per user, at most 5 offline reports queued, and reports are kept 5 hours. https://yandex.ru/dev/direct/doc/ru/restrictions
- ❓ Whether Reports calls consume points is not stated.

**Points (units)** ✅
- Each advertiser gets an individual daily limit that depends on their spend and activity. It is released as 1/24 per hour on a sliding window.
- The `Units` response header reads spent / remaining / limit (the docs' example is `10/20828/64000`).
- Costs: `campaigns.get` = 10 + 1 per object; `campaigns.add`, `suspend` and `resume` = 10 + 5 per object; `ads.add` = 20 + 20; `keywords.add` = 20 + 2; a failed call = 20.
- At most 5 concurrent requests per advertiser.
- Sources: https://yandex.ru/dev/direct/doc/ru/concepts/units, https://yandex.ru/dev/direct/doc/en/concepts/units

**Money**
- Minimum payment is 300 ₽ excluding VAT ✅ (https://yandex.ru/support/direct/ru/payments/min-payment). That is about 366 ₽ gross at 22% (my calculation).
- VAT is 22% from 1 January 2026, and every advertiser pays it, including individuals and self-employed people ✅. https://yandex.ru/support/direct/ru/payments/faq

**Ad marking (ORD / erid)** ✅
- Marking is automatic: the token «формируется и присваивается креативам автоматически».
- A direct advertiser only needs to fill in «Данные рекламодателя» (an individual gives full name, phone and INN). Nothing extra is needed in the ORD cabinet. The same data can be passed through the API.
- Source: https://yandex.ru/support/direct/ru/technologies-and-services/ad-labelingl

**Linking an ad straight to max.ru**
- Direct's ad rules say «если объявление ведет в мессенджер, рекомендуем заполнять название организации и описание в мессенджере» ✅. That implies messenger destinations are accepted. https://yandex.ru/support/direct/ru/moderation/ad-rules
- A MAX **channel** link as the destination is confirmed only by eLama, 18 May 2026 ⚠: https://elama.ru/blog/reklama-v-max-cherez-yandex-direct/
- A MAX **bot** deep link as the destination is ❓ unverified.
- The landing page must match the ad text and must not be a «сайт в разработке» stub ✅ (same ad-rules page).
- **Recommendation: send ads to the landing page.** Tracking parameters are lost on a direct max.ru link.

**Moderation risks for a Secret Santa product**
- If an ad announces a contest or draw that requires a purchase, 38-FZ art. 9 requires the dates and a source for the rules, organizer and prizes ✅. https://www.consultant.ru/document/cons_doc_LAW_58968/7b11ca039c27a1a3999a4f93bed2bcdd9149f906/
- Direct treats such ads as the «Стимулирующие мероприятия» category: no documents are needed in Russia, but the event «не должно подразумевать риск». If it is presented as a lottery, the lottery rules apply ✅. https://yandex.com/support/direct/ru/moderation/categories/lottery-buy-to-win.html
- Words fully in capitals and unproven superlatives are prohibited ✅ (ad-rules page).
- **My advice (not a platform rule):** avoid «розыгрыш», «приз» and «выиграй». Use «жеребьёвка» or «обмен подарками» instead.
- Moderation usually takes a few hours ✅. https://yandex.ru/support/direct/ru/troubleshooting/moderation

---

## 2. VK Ads API (VK Реклама, ads.vk.ru)

**Who can get access and how** ✅
- «Доступ к API VK Рекламы могут получить агентства и прямые рекламодатели — юридические и физические лица.»
- Steps: an individual fills in their details in «Настройки», then goes to «Настройки» → «Доступ к API» → «Запросить доступ к API» and enters a contact person.
- `client_id` and `client_secret` then appear. **The secret can be retrieved only within 10 minutes.**
- Support contact: ads_api@vk.team.
- Source: https://ads.vk.ru/help/articles/help_api
- ❓ The API documentation still says client registration is «в ручном режиме по заявке в Службу поддержки», which contradicts the self-service flow. The real lead time is unverified. https://ads.vk.ru/doc/api/info/Устройство%20API

**Authentication** ✅
- OAuth2 `client_credentials` is for your own account: `POST /api/v2/oauth2/token.json` with `grant_type=client_credentials&client_id=…&client_secret=…`.
- The access token lives 24 hours. Refresh it with `grant_type=refresh_token`.
- At most 5 tokens per client+user; the 6th request fails with 403. Tokens unused for a month are deleted.
- The authorization-code flow for other people's accounts is only granted on conditions.
- Source: https://ads.vk.ru/doc/api/info/Авторизация%20в%20API

**What the API can do** ✅
- **Create a campaign**: `POST /api/v2/ad_plans.json` with nested `ad_groups` and `banners`.
  - Plan fields: `autobidding_mode:"max_goals"`, `budget_limit_day`, `budget_limit`, `date_start`, `date_end`, `objective`.
  - Group field: `package_id`. Banner fields: `urls.primary.id` and `textblocks`.
  - Source: https://ads.vk.ru/doc/api/info/Быстрый%20старт
- **Supporting calls**:
  - URL checks: `GET /api/v1/urls/?url=` or `POST /api/v2/urls.json`. https://ads.vk.ru/doc/api/resource/CreateUrl
  - Images: `POST /api/v2/content/static.json`.
  - Packages: `GET /api/v2/packages.json`. https://ads.vk.ru/doc/api/resource/Packages
- **Edit a campaign**: `POST /api/v2/ad_plans/{id}.json` returns 204. https://ads.vk.ru/doc/api/resource/AdPlan
- **Pause, resume or change budget**: `POST /api/v2/ad_plans/mass_action.json` with `[{"id":…,"status":"blocked"|"active","budget_limit_day":…}]`, up to 200 campaigns. https://ads.vk.ru/doc/api/resource/AdPlanMassAction
  - Status values are `active`, `blocked` and `deleted`. https://ads.vk.ru/doc/api/object/AdPlan
- **Statistics**: `GET /api/v2/statistics/{banners|ad_groups|ad_plans|users}/{day|summary}.json?id=…&date_from=…&date_to=…&metrics=base`.
  - The base metrics include shows, clicks, spent, cpc and vk.goals.
  - At most 366 days back and 200 objects per request.
  - https://ads.vk.ru/doc/api/info/Statistics
- **Rate limits**: per second, per hour and per day, returned in `X-RateLimit-*` headers and by `GET /api/v2/throttling.json`. https://ads.vk.ru/doc/api/info/Устройство%20API
- ❓ The quick start only shows `objective:"socialengagement"`. The objective codes for the "Website" campaign type are unverified, so create the first campaign in the web interface and then manage it through the API.

**Minimum budget, VAT and payments** ✅
- The daily or total limit is at least 300 ₽ (10,000 ₽ for Dzen). The budget is mandatory. https://ads.vk.ru/help/general/start/budget_limits
- VAT is 22%. Paying 6,000 ₽ puts 4,918.03 ₽ on the balance.
- «В кабинете все финансов�ые показатели указываются без учёта НДС» — all cabinet figures are net of VAT.
- Autopay is 610–97,600 ₽ gross. There is no invoice for individual accounts.
- ❓ The minimum manual top-up for residents is not stated.
- Source: https://ads.vk.ru/help/statistics/finance/finance_individuals

**Ad marking** ✅
- «Объявления, запущенные через VK Рекламу, маркируются автоматически», and reports go to ЕРИР automatically.
- An individual must fill in full name, a 12-digit INN and phone in «Настройки». https://ads.vk.ru/help/ord/labeling, https://ads.vk.ru/help/ord/filling/ord_advertiser

**MAX as a destination**
- ✅ The official format is the «ВКонтакте, ОК, Mini Apps, Дзен, MAX» → «Каналы» campaign type.
  - The MAX channel must be **public**, and the only goal available is «Клики по рекламе».
  - A public MAX channel can be created by legal entities, ИП and **self-employed people** verified on the MAX partner platform.
  - Source: https://ads.vk.ru/help/general/network/promo_channel
- ❓ A MAX **bot** link as the URL is not covered in the official docs. A third-party blog says VK support allows traffic to MAX ⚠ (https://kudelnikov.com/blog-and-cases/target-vk-max).
- **Recommendation:** use the "Website" type pointed at your landing page.
- **Rules that apply** ✅ (updated 11.03.2026, https://ads.vk.ru/help/documents/moderation):
  - For remote sales, a self-employed advertiser must show «ФИО, ИНН для самозанятых» in the ad (rule 1.1).
  - A contest or draw ad must give its terms and rules (1.2).
  - «Doorway» pages that exist only to redirect are banned (2.3). **So the landing must have real content.**
  - At most 5 emoji, none in the headline, no words fully in capitals.
  - Promotional events require 18+ targeting (4.1).
- Moderation time: «может занять некоторое время», with no number given ✅. https://ads.vk.ru/help/general/start/campaign_status

**Sending conversions back to VK Ads** ✅
- A server-to-server GET to `https://top-fwz1.mail.ru/tracker?id=PIXELID;e=RG%3AVALUE/GOAL;rb_clickid=…`. The `rb_clickid` parameter is added to every ad link.
- Source: https://ads.vk.ru/help/general/sites/offline_events

---

## 3. Organic channels the owner controls

**(a) The owner's own VK community**
- **The API route is effectively closed** ✅. `wall.post` works only with a *user* token from a Standalone app via Implicit Flow. The `wall` permission is «выдаётся в исключительных случаях через запрос в поддержку по электронной почте devsupport@corp.vk.com». Community tokens are not listed.
  - Useful parameters: `from_group`, `publish_date` (scheduling), `mark_as_ads`.
  - Relevant errors: 219 "Advertisement post was recently added" and 224 "Too many ads posts".
  - Source: https://dev.vk.com/ru/method/wall.post
- User tokens are limited to 3 requests per second. https://dev.vk.com/ru/api/api-requests
- **Marking** ◐ (VK's business blog, 11 Sep 2025, https://vk.ru/@business-markirovka-postov-v-soobschestve-vkontakte):
  - «Публикации, в которых автор рассказывает о своей деятельности, демонстрирует товары и услуги или анонсирует ближайшие поступления, не подлежат маркировке».
  - But the same article lists «саморекламу в блогах или сообществах» among the formats that must be marked.
  - A FAS letter of 29.05.2023 No. 08/41716/23 says information on one's *own website* is not advertising ⚠ (copy at https://www.v2b.ru/documents/pismo-fas-rossii-ot-29-05-2023-08-41716-23-ob-informatsii-v-seti/).
  - ❓ I found no official FAS text about own social communities. **Treat it as a grey zone:** informational posts go unmarked; posts with calls to action, discounts or promo codes get an erid from an ORD. Yandex ORD is currently free ◐: https://b2b.yandex.ru/adv/edu/materials/ord

**(b) The owner's own MAX channel through the Bot API — this works** ✅
- `POST https://platform-api2.max.ru/messages?chat_id=<channel>` with the header `Authorization: <token>`. It «Отправляет сообщение в диалог, групповой чат или канал».
  - Text up to 4,000 characters; `inline_keyboard` link buttons are supported.
  - **At most 2 messages per second per chat or channel.**
  - Query-parameter tokens no longer work, and the old platform-api.max.ru domain has been replaced.
  - Sources: https://dev.max.ru/docs-api/methods/POST/messages, https://dev.max.ru/docs-api
- «Администратором канала может быть назначен как пользователь, так и бот». https://dev.max.ru/docs/channels/manage
- A self-employed person may have **1** public channel created through the platform. https://dev.max.ru/docs
- The user agreement allows channels for commercial information (clause 3.11.1). https://legal.max.ru/ps
- **Main rule to respect:** apps must not use the API «для рассылки рекламных и маркетинговых или иных массовых сообщений пользователям» (clause 1.5). https://dev.max.ru/docs/legal/requirements
  - So: posting in your own channel is fine; broadcasting promos to bot users is not.
  - The same marking logic as (a) applies to channel posts.

**(c) Dzen**
- ❓ I found no public posting API.
- The official automated route is an RSS feed from a site linked to the channel ✅. Conditions:
  - The channel needs 10 subscribers and a verified site.
  - The site must have original content: at least 10 items in the first feed and at least 3 publications in the last month.
  - Feeds get at most 3 reviews a year; Cyrillic-script domains are not allowed.
  - Sources: https://dzen.ru/help/ru/website/site-to-channel.html, https://dzen.ru/help/ru/website/rss-modify.html, https://dzen.ru/help/ru/website/website-requirements.html
- Not worth it for version 1.

**(d) Pikabu, vc.ru, Habr — all manual, and promotion is mostly forbidden** ✅
- **Pikabu:** «спам, реклама, ссылки на собственные ресурсы» are banned. Advertising is allowed only through Пикабу+, company blogs or the ad cabinet. Bots and scripts are banned. https://pikabu.ru/information/rules
- **vc.ru:** «Посты, продвигающие коммерческие сервисы… запрещены». Restrictions on ad-only blogs are lifted with a Pro subscription. https://vc.ru/rules
- **Habr:** advertising is allowed only in company blogs or once in «Я пиарюсь». https://habr.com/ru/docs/help/rules/
- ❓ I verified no posting API for any of the three.

---

## 4. Hard NOs and their consequences

| What | Rule and consequence |
|---|---|
| Automated posting in other people's chats or communities, comment spam, bot farms | MAX user agreement: 4.3.4 (mass messaging without consent), 4.3.3.10 (unwanted ads), 4.3.7 (automated scripts without permission), 4.3.13 (mass identical actions). Clause 9.2 allows blocking or removal. https://legal.max.ru/ps. The bot token «может быть отозван», and the app can be blocked. https://dev.max.ru/docs-api, https://dev.max.ru/docs/legal/rules. Pikabu, Habr and vc.ru rules are above. |
| Ads sent as messages | 38-FZ art. 18 requires the recipient's prior consent and bans automatic sending. https://www.consultant.ru/document/cons_doc_LAW_58968/f892dec1383709792452f18d36e7043306e2be0a/ |
| Invite spam | The MAX method `POST /chats/{chatId}/members` has been restricted since 9 Sep 2026 and is removed on 30 Sep 2026 (https://dev.max.ru/docs-api). VK developer rule 4.3 bans pushing users to send invitations (https://dev.vk.com/ru/rules). |
| Buying Telegram ads for a Russian audience | 38-FZ art. 5 part 10.7 (72-FZ, in force 1 Sep 2025) bans ads on resources whose access is restricted. https://www.consultant.ru/document/cons_doc_LAW_58968/f67f81c57fdcdacc2643d19d59369f7e185e1156/. On 25 Mar 2026 FAS announced «переходный период до конца 2026 года, в течение которого меры ответственности… применяться не будут». This is quoted by https://www.fontanka.ru/2026/03/25/76329519/ and https://rb.ru/news/fas-dala-oficialnoe-razyasnenie-za-reklamu-v-telegram-i-youtube-ne-budut-shtrafovat-do-2027-goda/ (FAS page fas.gov.ru/news/34584 was unreachable). **It is still unlawful; only enforcement is paused.** This also covers the Telegram placements inside Direct's messenger ads. |
| Paid posts without marking | Fines for a post without an erid (KoAP 14.3 part 16): **citizens 30–100k ₽; officials 100–200k ₽; legal entities 200–500k ₽**. Failing to report to ЕРИР (part 15): citizens 10–30k ₽. Missing «реклама» label or other ad-law breaches (part 1): citizens 2–2.5k ₽, legal entities 100–500k ₽. https://www.consultant.ru/document/cons_doc_LAW_34661/2d50fc1c4013ea9ab20b8b2666c1650b1dc4c982/. An ИП is fined as an official (note to KoAP art. 2.4, search result https://www.consultant.ru/document/cons_doc_LAW_34661/de33c73dc4e364406642dc44f280f59154201a2e/). ❓ Whether a self-employed person without ИП status is fined as a citizen or as an official is unsettled; assume the worst case. |
| Enforcement examples | «Лиса рулит» (an ИП), three posts without erid, 300k ₽ (https://www.rbc.ru/technology_and_media/30/11/2023/656733669a794740fd8a3188, https://adpass.ru/lisa-vyrulila-na-shtraf-za-otsutstvie-markirovki-reklamy-bloger-mozhet-zaplatit-do-500-tys-rublej/). Oct 2025: a citizen blogger fined 30k ₽ for an Instagram ad (https://www.rbc.ru/society/21/10/2025/68f789a09a79473e0ff50fa5). 2024 court cases: https://www.garant.ru/ia/opinion/author/tyupa_vsevolod/1760873/. ❓ I found no verified 2026 case against an individual. |

---

## 5. Other legal, cheap channels

1. **Yandex Direct "Реклама в мессенджерах"** — ads shown *inside* Telegram and MAX channels.
   - It is auto-marked. Auto-selected channels are pay-per-click with a bid from 30 ₽; catalog channels are pay-per-view ✅. https://yandex.ru/support/direct/ru/efficiency/messengers-ads
   - MAX placements have been in beta since 18 May 2026 ✅. https://b2b.yandex.ru/adv/news/reklama-max-v-direct
   - ❓ Whether you can exclude Telegram is unverified, which carries the legal risk from section 4. API support is also ❓. Treat it as a manual test only.
2. **VK Ads «Каналы»** — promote the owner's public MAX channel, pay-per-click ✅ (section 2). A good pairing with automated posts in that channel.
3. **Yandex Metrica offline conversions** — feed «game reached 3+ participants» back to Direct.
   - `POST https://api-metrika.yandex.net/management/v1/counter/{id}/offline_conversions/upload`, a multipart CSV with `ClientId` or `Yclid`, plus `Target` and `DateTime`. It needs a JavaScript-event goal ✅. https://yandex.com/dev/metrika/en/management/offline-conv, https://yandex.com/dev/metrika/doc/api2/management/offline_conversion/upload.html
   - ❓ Whether offline goals work with pay-per-conversion is stated only in a search snippet of Yandex materials, not verified on a page.
4. **VK AdBlogger** — posts by VK authors with automated paperwork, marking and payment ◐ (VK press release, 18 Sep 2024, https://vk.company/ru/press/releases/11857/). Deals are manual. ❓ No API found; the minimum is unverified.
5. **In-product virality** — organizers sharing invite deep links themselves. This is free and legal as long as the bot never mass-messages (MAX requirement 1.5 above).
6. **Not suitable**:
   - **Yandex Business** — from 3,000 ₽/month with a 90-day minimum ⚠ (eLama, 2 May 2026, https://elama.ru/blog/kak-reklamirovat-kompaniyu-s-pomoschyu-yandeks-biznesa/); no API found ❓.
   - **Avito** — links and messenger redirects are forbidden in listings according to third-party sources ⚠; I did not fetch the official rules.
   - **2GIS** — not researched.
   - **Third-party MAX bot catalogs** (e.g. https://maxdash.ru/bots) exist; one-time manual listing, value unverified.

---

## RECOMMENDED AUTOPILOT DESIGN

### Version 1 automates three things through APIs
1. Yandex Direct: a search-only unified campaign pointing to the landing page.
2. VK Ads: a "Website" campaign pointing to the landing page, and optionally a «Каналы» campaign for the MAX channel.
3. Posting to the owner's own MAX channel, plus daily reports to the owner through the bot.

VK community posting cannot be automated through the API.

### Attribution plumbing
1. Each campaign's ad URL is `https://<domain>/?src=yd_<campaignId>` or `?src=vk_<adPlanId>`.
2. The landing page stores the Metrica ClientID or yclid, and VK's `rb_clickid`, under a short token.
3. The button opens `https://max.ru/<bot>?start=s_yd_<cid>_<token>`. The payload must be ≤128 characters or MAX drops it ✅ (https://dev.max.ru/docs/chatbots/bots-coding/prepare). The bot reads it from the `bot_started` update.
4. When a game reaches 3+ participants, send it to the SQLite DB, to the Metrica offline-conversion upload, and to the VK S2S tracker.

### API calls per channel

| | Create | Pause / resume | Budget | Report |
|---|---|---|---|---|
| **Direct** (`json/v501/`, `Authorization: Bearer`) | `campaigns.add` (UnifiedCampaign, WB_MAXIMUM_CLICKS, `CustomPeriodBudget` 24.11–12.12, Network SERVING_OFF) → `adgroups.add` → `keywords.add` → `ads.add` (ResponsiveAd) → `ads.moderate` | `campaigns.suspend` / `campaigns.resume` | `campaigns.update` changing `WeeklySpendLimit` or `CustomPeriodBudget.SpendLimit` (×10⁶, net of VAT); send BiddingStrategyType too (❓ test in sandbox) | `reports`: CAMPAIGN_PERFORMANCE_REPORT with Date, CampaignId, Clicks, Cost, IncludeVAT=YES |
| **VK Ads** (`https://ads.vk.ru/api/v2/…`) | `oauth2/token.json` (client_credentials) → `urls.json` → `content/static.json` → `ad_plans.json` | `ad_plans/mass_action.json` with status `blocked` / `active` | same call with `budget_limit_day`, or `budget_limit` + `date_end` (net of VAT) | `statistics/ad_plans/day.json?metrics=base` (spent is net; multiply by 1.22) |
| **MAX channel and owner** | — | — | — | `POST /messages?chat_id=<channel>` (≤2 per second per chat); `POST /messages?user_id=<owner>` for reports and approve/stop buttons |

### Rule engine and safeguards
- **Money basis.** Count all money gross: VAT-inclusive cost = net × 1.22.
  - The 15,000 ₽ cap is 12,295 ₽ net; the 6,000 ₽ test is 4,918 ₽ net.
  - Note that 6,000 ₽ cannot fund both platforms at a 300 ₽ daily minimum for all 19 days. **Stagger the platforms, or use period or total budgets.**
- **Evaluate once a day (Moscow time).**
  - Pause a campaign if cost per 3+ game exceeds 250 ₽ **and** it has spent at least 500 ₽ gross.
  - Raise its budget by at most 30% a day if cost per 3+ game is below 150 ₽.
  - Allow at most one budget change per campaign per day (Direct's limit is 3 a day).
  - Count 3+ games by cohort, with a 48–72 hour lag.
- **Hard cap, three layers:**
  1. Platform caps: Direct `CustomPeriodBudget` and VK `budget_limit` with `date_end`.
  2. The autopilot suspends everything when projected spend would cross 15,000 ₽.
  3. **Prepaid balances**: only top up what may be spent. Do not use VK's «обещанный платёж» (spend now, pay later).
- **Owner controls.** A `/stop` command in the bot suspends everything. Budget increases beyond the plan need the owner's tap to approve.

### What the owner must obtain
Aim to start by about **1 November** to leave buffer.

**Yandex**
- A Yandex ID and a Direct account with «Данные рекламодателя» filled in (full name, INN, phone).
- One campaign created in the Директ Про web interface — this is required before the API settings page opens.
- An OAuth app with `direct:api`, and an API access application (1 hour to 3 business days; up to 7 at peak).
- Prepayment of at least 300 ₽ excluding VAT.
- A Metrica counter with a JavaScript goal and a Metrica token.

**VK Ads**
- A cabinet of type "Физлицо" with full name, INN and phone.
- API access request, then save the `client_secret` within 10 minutes. The real lead time is ❓ given the conflicting docs.
- A VK pixel with a JavaScript event, and a prepaid balance.

**MAX**
- The verified partner profile (the bot already passed moderation).
- One public channel created through «Каналы в MAX для бизнеса», with the bot added as channel admin.
- The channel's `chat_id`, via a `bot_added` update or `POST /subscriptions` as the docs state.

**Landing page**
- Real content: how it works, prices, FAQ, **owner's full name and INN**, privacy policy and terms. No automatic redirect (VK's doorway rule).
- An ORD cabinet (Yandex ORD or ORD VK) only if any posts in the owner's own channels are promotional.

**Ad moderation times:** Direct usually a few hours; VK unspecified.

### What stays manual
- Writing ad texts and creatives, and fixing moderation rejections.
- All payments and top-ups.
- Creating the first campaigns in the web interfaces (I recommend creating them in the UI and letting the API govern them).
- VK community posts, scheduled by hand in VK's interface (or apply for the `wall` permission).
- Dzen, Pikabu, vc.ru and Habr posts; VK AdBlogger deals.
- Direct messenger-ads tests (MAX-only if Telegram can be excluded).
- ORD registration and reporting for any promotional own-channel posts.
- A lawyer's view on the self-promotion marking grey zone.

### Test in the sandbox before launch (❓ items)
- v501 support in the sandbox.
- Required fields in `campaigns.update` for unified campaigns.
- VK objective and package codes for the "Website" type.
- Whether Direct and VK accept a max.ru bot link as the ad URL.
