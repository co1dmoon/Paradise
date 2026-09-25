MVP BUILD SPEC: «Санта в чате», a Secret Santa bot for the MAX messenger (Russia)

0. SCOPE, PRIORITIES, DEFINITION OF DONE
- What to build: a MAX chat bot with a Russian-language UI. It runs Secret Santa gift exchanges for groups (families, offices, school classes, friends) entirely inside MAX:
  1. The organizer creates a game in a private chat with the bot.
  2. The bot gives the organizer a forwardable invite. The organizer pastes it into any group chat.
  3. Participants tap the link, join inside the bot, and write gift wishes.
  4. The organizer runs the draw. Each participant privately receives whom to gift and that person's wishes.
  5. Santa and recipient can exchange anonymous messages.
- Payments: free up to 10 participants. Bigger games need a one-time per-game upgrade, paid through Robokassa. The owner is a Russian self-employed person (НПД).
- Also build:
  - a tiny website: landing, legal pages, payment callbacks;
  - admin tools inside the bot;
  - analytics that measure the viral loop;
  - a Docker deployment to one Russian VPS.
- The owner is a non-engineer. The owner must be able to:
  - configure everything through one .env file;
  - start it with `docker compose up -d --build`;
  - operate it through admin commands in the bot.
  Write README_RU.md in plain Russian covering setup, deploy, daily operation and troubleshooting.
- Timeline: today is 2026-09-24. Public launch must happen by 2026-11-01. Usage peaks 15 Nov – 25 Dec. The real MAX token may not exist while you build, so everything must be testable offline against a fake MAX API.
- P0 = must ship. P1 = build only after all P0 work is done and tests are green.
- DONE means all of the following:
  1. All P0 features work.
  2. `pytest -q` is green, fully offline.
  3. `python -m tools.simulate` prints a readable Russian transcript of a complete 12-person office game. The game must cover: joining, hitting the free limit and the waiting list, a test-mode payment, exclusions, the draw, and anonymous Q&A.
  4. `docker compose build` succeeds.
  5. README_RU.md is complete.
  6. .env.example documents every variable.

1. STACK AND REPO LAYOUT
- Python 3.12, asyncio.
- Runtime dependencies: aiohttp (web server and HTTP client), aiosqlite, jinja2, certifi.
- Dev dependencies: pytest, pytest-asyncio.
- Pin exact versions. Add nothing else: no ORM, Redis, Celery or frontend framework.
- SQLite in WAL mode at /data/santa.db, on a Docker volume, with foreign keys ON.
- Layout:
  - app/main.py: app factory, routes, startup and shutdown.
  - app/config.py: env parsing and validation. Fail fast with clear messages.
  - app/db.py and app/migrations/NNN_*.sql: migrations are applied at startup.
  - app/max_api.py: the `MaxApi` interface and its `HttpMaxApi` implementation.
  - app/outbox.py: durable, rate-limited sending.
  - app/core/: platform-agnostic logic. Files: games.py, draw.py, pricing.py, payloads.py, analytics.py, kb.py (neutral button specs), texts.py. ALL Russian copy lives in texts.py so the owner can edit wording in one file.
  - app/handlers/: router.py, private.py, callbacks.py, admin.py, and group.py (P1).
  - app/payments/robokassa.py.
  - app/scheduler.py.
  - app/web/templates/*.html and app/web/static/site.css.
  - tools/fake_max.py: FakeMaxApi. It records every send, edit and answer per target.
  - tools/simulate.py.
  - tools/make_avatar.py (P1): draws a 500x500 PNG avatar without external assets.
  - certs/: the two Russian CA files (see §2).
  - tests/, Dockerfile, docker-compose.yml, Caddyfile, .env.example, README_RU.md.
- core/ must not import aiohttp or anything MAX-specific, so that a Telegram or web adapter can be added later.

2. MAX BOT API: VERIFIED FACTS
Source: dev.max.ru, read 2026-09-24. Re-read the docs if a call misbehaves.
- Base URL: https://platform-api2.max.ru. Do NOT use platform-api.max.ru.
- Authentication: send the header `Authorization: <token>`. Tokens in the query string are no longer accepted.
- TLS trust: the MAX docs say to add the Минцифры certificate to the trusted list.
  - Bundle two certificates in certs/:
    - Russian Trusted Root CA, from https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt. SHA-256 fingerprint D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31. Valid to 2032-02-27.
    - Russian Trusted Sub CA, from https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt. SHA-256 fingerprint BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B. Valid to 2027-03-06.
  - A test must verify both fingerprints.
  - Build one SSLContext from the certifi bundle plus these two files. Use it for all outbound HTTPS.
  - A daily job alerts the admin 30 days before any bundled certificate expires.
- Limits:
  - At most 30 requests per second in total. Use a token bucket of 25/s.
  - At most 2 sends, edits or answers per second per dialog or chat. Use 1/s per target.
  - Message text is at most 4000 characters.
- Webhook delivery:
  - Register with POST /subscriptions {url, update_types, secret}.
  - The URL must be HTTPS on port 443 with a certificate from a trusted CA. Self-signed certificates are not allowed.
  - The secret arrives in the X-Max-Bot-Api-Secret header.
  - Respond 200 within 30 s. MAX retries up to 10 times with backoff.
  - After 8 hours of failures MAX DELETES the subscription, so run a watchdog.
- Long polling: GET /updates?limit=&timeout=(0-90)&marker=. The docs say it is not for production. Support it only as MODE=polling for local manual tests.
  - In polling mode, do not register a webhook.
  - If a webhook subscription exists, log a warning. MAX may not deliver updates by polling while a webhook is active; this is unverified.
- Update types to handle. Every update has update_type and timestamp (in ms):
  - bot_started {chat_id, user, payload, user_locale}
  - message_created {message}
  - message_callback {callback {callback_id, payload, user}, message}
  - bot_added {chat_id, user, is_channel}
  - bot_removed
  - bot_stopped
  Ignore all other types.
- Message fields:
  - sender {user_id, first_name, last_name, username, name, is_bot}
  - recipient {chat_id, chat_type: dialog|chat|channel, user_id}
  - body {mid, text, attachments}
- Sending: POST /messages?user_id=... for a private message, or ?chat_id=... for a group.
  - Body: {text, attachments:[{type:"inline_keyboard", payload:{buttons:[[btn,...],...]}}], notify}.
  - Add the query param disable_link_preview=true to every message that contains user-written text.
  - In P0, send plain text only: no `format` field, to avoid markup-escaping bugs.
- Buttons: {type:"callback", text, payload} and {type:"link", text, url}. The docs do not state limits, so stay conservative:
  - at most 3 buttons per row;
  - at most 8 rows;
  - button text at most 40 characters;
  - callback payload at most 64 ASCII characters.
- Editing: PUT /messages?message_id=.... A bot can edit only its own messages. Messages with buttons can be edited regardless of age.
- Answering a callback: POST /answers?callback_id=....
  - The body field `message` is documented; it replaces the message the button was on.
  - `notification` is TamTam-compatible but not documented. If the API rejects it, answer with an empty body or {message}, and send a normal message instead.
  - ALWAYS answer every callback within about 1 s.
- Deep links: https://max.ru/<BOT_USERNAME>?start=<payload>. The payload is at most 128 characters and arrives as bot_started.payload.
  - It is UNVERIFIED whether a user who already started the bot receives bot_started again. Support both cases, including the code fallback in §5.3.
- Other calls: GET /me, GET /chats/{chatId}.
  - Do not use GET /chats: it is deprecated.
  - Do not use POST /chats/{chatId}/members: it is removed from 2026-09-30.
- Platform facts that shape the product:
  - Only Russia-resident legal entities, ИП or self-employed people can own bots. Self-employed owners get at most 2 bots.
  - A self-employed owner's bot gets an automatic nickname like se1234567_bot. It cannot be changed, and users cannot find the bot by its name. So build every link from the config value MAX_BOT_USERNAME, and never rely on in-app search.
  - Adding the bot to group chats is OFF by default. The owner enables it in the bot settings, which triggers re-moderation.
  - The rules forbid advertising, marketing and mass messages through the API.
  - Contests and promotions need separate approval from MAX.
  - Apps that host user-generated content must accept complaints and remove content.
- HttpMaxApi methods:
  - get_me
  - send(target, body, disable_link_preview=False)
  - edit(message_id, body)
  - answer(callback_id, notification=None, message=None)
  - get_chat(chat_id)
  - list_subscriptions()
  - subscribe(url, types, secret)
  - get_updates(marker, timeout)
- Parse responses leniently: ignore unknown fields and never crash on unexpected shapes.
- Map errors to these classes:
  - RateLimited: HTTP 429.
  - Transient: 5xx and timeouts.
  - Unauthorized: 401.
  - Forbidden: any other 4xx whose body mentions denied, blocked, forbidden or not found.
  - BadRequest: any other 400.

3. DATA MODEL
SQLite. Timestamps are stored as UTC ISO text. Dates are shown to users in Europe/Moscow time.
- users:
  - user_id INTEGER PK (the MAX id)
  - max_name TEXT
  - username TEXT NULL
  - first_seen_at
  - first_source TEXT (see §6)
  - consent_at NULL
  - consent_version NULL
  - dm_ok INT DEFAULT 1
  - blocked INT DEFAULT 0
  - games_created_today INT
  - games_created_day TEXT
- user_state (pending text input):
  - user_id PK
  - kind TEXT: one of resume, title, budget_custom, date_custom, wishes, display_name, code, relay_to_receiver, relay_to_santa, reply_relay, admin_input
  - game_id NULL
  - data JSON
  - expires_at: now + 30 min. An 'Отмена' button and /cancel clear the state.
- games:
  - id PK
  - code TEXT UNIQUE: 6 characters from ABCDEFGHJKLMNPQRSTUVWXYZ23456789
  - title
  - organizer_id
  - organizer_participates INT
  - budget_text
  - exchange_date TEXT NULL
  - status: collecting | drawn | finished | cancelled
  - tier: free | S | M | L
  - participant_limit INT
  - anon_chat INT DEFAULT 1
  - reminder_on INT DEFAULT 1
  - group_chat_id NULL
  - group_card_mid NULL
  - source_game_id NULL
  - source TEXT
  - created_at, drawn_at, finished_at, cancelled_at
  - reveal_done INT DEFAULT 0
  - last_join_notice_at, last_waiting_notice_at, last_wish_reminder_at
  - org_nudge_sent INT
  - pre_exchange_sent INT
  - redraw_count INT DEFAULT 0
- participants:
  - id PK
  - game_id, user_id
  - display_name
  - wishes TEXT NULL
  - status: active | waiting | left | removed
  - joined_at
  - via: link | code | group | ref
  - gift_ready INT DEFAULT 0
  - result_dm_ok INT NULL
  - UNIQUE(game_id, user_id)
- exclusions: game_id, user_a, user_b. Always store user_a < user_b. PK(game_id, user_a, user_b).
- assignments: game_id, giver_id, receiver_id. PK(game_id, giver_id). UNIQUE(game_id, receiver_id).
- payments:
  - inv_id INTEGER PK AUTOINCREMENT (this is the Robokassa InvId)
  - game_id
  - payer_id NULL
  - tier
  - amount_rub INT
  - status: created | paid | refunded | granted
  - provider: robokassa | manual
  - created_at, paid_at
  - raw TEXT
- relay_messages: id PK, game_id, from_id, to_id, direction (to_receiver | to_santa), text, created_at. Purged after 30 days.
- reports: id PK, relay_id NULL, game_id, reporter_id, reported_id, text, created_at, resolved INT.
- events: id PK, ts, type, user_id NULL, game_id NULL, props JSON.
- settings: key PK, value. Keys: free_limit, price_S, price_M, price_L, limit_S, limit_M, limit_L, maintenance. Seeded from env on first start; after that the DB value wins.
- outbox:
  - id PK
  - kind: send | edit
  - target_type: user | chat
  - target_id
  - message_id NULL
  - body JSON
  - disable_preview INT
  - not_before
  - attempts INT
  - status: pending | done | dead
  - last_error
  - created_at
  - dedupe_key UNIQUE NULL
- processed_updates: key PK, ts. Used to deduplicate webhook retries. The key is the callback_id, the message mid, or 'bs:'+user+':'+timestamp. Prune after 3 days.

4. PRICING AND LIMITS
Defaults below. The admin can change them at runtime with /price.
- Free: up to 10 active participants. The organizer counts if they participate.
- Paid tiers, one-time per game:
  - S: up to 30 participants, 490 ₽
  - M: up to 100 participants, 990 ₽
  - L: up to 300 participants, 2490 ₽
- More than 300: show 'напишите нам' with SUPPORT_EMAIL. The admin later uses /grant after a bank transfer.
- Upgrade price = target tier price minus the sum already paid or granted for this game. If the result is 0 or less, no payment is needed.
- ANY consented participant or the organizer may pay for a game.
- Upgrades are allowed only while the game status is collecting.

5. FLOWS AND COPY
Ship this Russian copy in texts.py. Keep the meaning; small wording fixes are OK.
Global rules:
- Answer every callback.
- Re-check permissions from the DB on every action. Never trust the payload for authorization.
  - Organizer-only actions require organizer_id == user.
  - Participant actions require an active participant row.
- Ignore messages sent by bots.
- In group chats, ignore everything except bot_added (P1).
- If a list does not fit into 4000 characters, truncate it and end with 'и ещё N'.
- Show button lists 8 per page, with 'Дальше' / 'Назад'.

5.1 Consent and menu (P0)
- A user without consent_at gets the consent screen, whatever update they send.
- Save any start payload in user_state (kind=resume) and resume it after consent.
- Consent screen text:
  'Привет! Я помогу провести «Тайного Санту» — обмен подарками в семье, на работе или в классе.
  Чтобы участвовать, нужно согласие на обработку данных: имени из MAX и ваших пожеланий к подарку. Подробно: {BASE}/consent · Политика: {BASE}/privacy · Правила: {BASE}/terms'
  Button: [Согласен(на)] (callback c:yes).
- On consent, store consent_at and consent_version, then route the resumed payload.
- Before consent, store only user_id, first_seen_at and first_source.
- Menu text: 'Что сделаем?' Buttons: [Создать игру] [Мои игры] / [Вступить по коду] [Как это работает].
- Commands:
  - /start with no payload shows the menu.
  - /help shows the help text.
  - /whoami works for anyone and replies 'Ваш id в MAX: {id}'.
  - /cancel clears the pending input.

5.2 Organizer creates a game (P0)
Four steps. Every step can be skipped.
1. 'Как назовём игру? Например: «Отдел продаж», «Семья Ивановых», «5Б класс».' Text, at most 60 characters. [Пропустить] sets the title to 'Тайный Санта'.
2. 'Бюджет подарка?' Buttons: [до 500 ₽] [до 1000 ₽] [до 1500 ₽] / [до 3000 ₽] [Без ограничений] [Свой вариант]. A custom budget is at most 30 characters.
3. 'Когда обмен подарками?'
   - Between Oct 1 and Dec 25, offer the future dates among 20.12, 25.12, 26.12, 27.12 and 28.12.
   - Otherwise offer today +7, +14 and +21 days.
   - Also offer [Своя дата] (parse ДД.ММ or ДД.ММ.ГГГГ; the date must be between tomorrow and 180 days ahead) and [Пока не знаю].
4. 'Вы тоже участвуете в обмене?' Buttons: [Да, участвую] [Нет, только организую].
After step 4:
- Create the game with status collecting, tier free, participant_limit = free_limit and source set as in §6. If the organizer participates, add them as an active participant.
- Send the INVITE as a separate message, ready to forward:
  'Тайный Санта «{title}»
  Бюджет: {budget}. Обмен подарками: {date | дату сообщит организатор}.
  Участвовать: нажмите ссылку, затем «Начать»
  https://max.ru/{BOT}?start=j_{CODE}
  Если бот у вас уже открыт — просто отправьте ему код: {CODE}'
- Then send:
  'Готово! Перешлите приглашение выше в общий чат (нажмите на сообщение → «Переслать») или скопируйте ссылку. Когда кто-то вступит — я напишу. Если вы участвуете — напишите свои пожелания.'
  Buttons: [Мои пожелания] [Пульт игры].
- Anti-abuse: at most 20 games per user per day.

5.3 Joining (P0)
Entry points:
- the j_{CODE} payload;
- any private text matching ^\s*(код\s*)?([A-Za-z2-9]{6})\s*$ where the code exists (case-insensitive);
- the [Вступить по коду] button, which asks for the code;
- the group card link (P1).
Checks:
- Game missing or cancelled: say so.
- Game already drawn: 'Жеребьёвка в этой игре уже прошла — вступить нельзя. Можно устроить свою игру.' with [Создать игру].
- User already in the game: open the game view.
- Active participants already at participant_limit: set the status to waiting and go to §5.6.
On success, send:
  'Вы в игре «{title}»! Организатор: {org}. Бюджет: {budget}. Обмен: {date}.
  Как вас подписать в игре? Сейчас: «{max_name}». (Если играет ребёнок без MAX — вступите сами и укажите, например: «Маша, 5Б».)'
  Buttons: [Оставить так] [Изменить имя]
Then ask for wishes:
  'Напишите, что хотели бы получить: 2–5 идей, можно ссылки на товары. Или нажмите «Удивите меня».'
  Button: [Удивите меня]
- Wishes are at most 1000 characters.
- [Удивите меня] stores the text 'Удивите меня!'.
After the wishes are saved, send:
  'Записал! Когда организатор проведёт жеребьёвку, я пришлю, кому вы дарите.'
  Buttons: [Изменить пожелания] [Выйти из игры] / [Устроить игру в другом чате]
Leaving: allowed before the draw. Leaving after the draw is P1 (§5.5).

5.4 Organizer panel (P0)
Panel text:
  'Игра «{title}» · код {CODE}
  Участников: {n} из {limit}{; в очереди: w}
  Без пожеланий: {k} · Исключений: {x}
  Бюджет: {budget} · Обмен: {date}'
Buttons:
- [Участники] [Исключения]
- [Напомнить о пожеланиях]
- [Провести жеребьёвку]
- [Приглашение] [Настройки]
- [Расширить игру]: shown only when n >= limit - 2 or w > 0, and a higher tier exists.
- After the draw, the buttons become [Кто не получил пару], plus P1 [Раскрыть, кто чей Санта] and P1 [Перезапустить жеребьёвку].
What each button does:
- Участники: a numbered list of names, each marked 'пожелания есть/нет'. Includes a [Убрать участника] picker with confirmation. The removed person gets: 'Организатор убрал вас из игры «{title}».'
- Исключения: prompt 'Кто не должен дарить друг другу (например, супруги)? Выберите первого человека.' The organizer picks the first person, then the second, and the pair is saved. Existing pairs are listed with [Убрать пару N]. At most 50 pairs.
- Напомнить о пожеланиях: at most once per 24 h per game. Goes only to active participants without wishes:
  'Организатор игры «{title}» просит написать пожелания к подарку — так вашему Санте будет проще.'
  Buttons: [Написать пожелания] [Удивите меня]
  Then tell the organizer how many people were reminded.
- Приглашение: re-sends the invite with the line 'Уже участвуют: {n} — {first 15 names}…'.
- Настройки: change the title, the budget, the date, 'участвую/не участвую', 'анонимные вопросы: вкл/выкл' and 'напоминание за день до обмена: вкл/выкл'. Also [Отменить игру] with confirmation. Cancelling notifies every participant: 'Игра «{title}» отменена организатором.'
- Join notices to the organizer (sent by the scheduler, at most once per 10 min per game):
  'В игру «{title}» вступили: {names}. Всего: {n} из {limit}.'

5.5 Draw (P0)
- Requirements: at least 3 active participants, and the user is the organizer.
- Confirmation text:
  'Провести жеребьёвку для {n} участников? После этого вступить будет нельзя.{ У k человек нет пожеланий.}{ В очереди w человек — они не попадут в игру.}'
  Buttons: [Да, провести] [Отмена]
- Algorithm (core/draw.py): draw_cycle(ids, excluded: set[frozenset], rng) -> dict[giver, receiver] | None.
  - Produce ONE random cycle through all participants. This guarantees nobody gifts themselves and no two people gift each other.
  - No two adjacent people in the cycle may form an excluded pair.
  - Search: shuffle and test, up to 5000 attempts. If that fails, run a randomized backtracking DFS with a 2 s budget.
  - Use random.SystemRandom.
  - If nothing is found: 'Не получается учесть все исключения — уберите часть пар.'
- In one transaction: write the assignments and set status=drawn and drawn_at. Then enqueue a result message for every participant:
  'Жеребьёвка в игре «{title}» проведена!
  Вы — Тайный Санта для: {receiver_name}.
  Пожелания: {wishes | «не написаны — можно спросить анонимно»}
  Бюджет: {budget}. Обмен: {date}.
  Никому не говорите, кто вам выпал.'
  Buttons:
  - [Спросить получателя анонимно] and [Написать своему Санте], only if anon_chat is on
  - [Подарок готов] (P1)
  - [Устроить игру в другом чате]
- When all result messages are sent or dead, tell the organizer:
  'Готово! Пары отправлены {sent} из {n}.{ Не дошло: names — попросите их открыть бота и нажать «Мои игры», там будет их пара.}'
- [Кому я дарю?] is always available in the game view. It shows the receiver and their current wishes. Wishes stay editable after the draw.
- P1, redraw: at most 2 times. Void the old assignments and resend all pairs with the prefix 'Жеребьёвка проведена заново — старую пару не учитывайте.'
- P1, leaving or removal after the draw: splice the cycle so that giver(L) now gifts receiver(L). Tell that giver: 'Ваш получатель выбыл. Теперь вы дарите: {name}…'. If the new pair is excluded, or fewer than 3 people remain, alert the organizer to redraw.

5.6 Free limit, waiting list, payment (P0)
- When someone lands on the waiting list, they get:
  'Мест нет: в бесплатной игре до {free_limit} участников. Я поставил вас в очередь и сообщил организатору. Расширить игру до {limit_S} человек может любой участник — {price} ₽ один раз.'
  Button: [Расширить за {price} ₽] (callback pay:{CODE}:{tier})
- The organizer gets at most one notice per hour:
  'В игру «{title}» хотят вступить ещё {w} чел., но мест нет. Расширить до {limit} — {price} ₽.'
  Button: [Расширить]
- Callback pay creates a payment row. Reuse an existing row if it has the same game, tier and payer, status created, and is younger than 24 h. Then send:
  'Оплата {amount} ₽ — расширение игры «{title}» до {limit} участников. Можно через СБП или картой на странице Robokassa. После оплаты игра расширится сама, а люди из очереди попадут в игру. Если страница не открывается — включите Wi-Fi.'
  Button: [Оплатить {amount} ₽], a link button to the Robokassa URL (§7).
- When a payment is confirmed:
  1. Raise tier and participant_limit to the maximum of the current and the paid values.
  2. Activate waiting participants in join order, up to the new limit. Each one gets: 'Место появилось — вы в игре «{title}»! Напишите, что хотели бы получить.' with [Удивите меня].
  3. Tell the payer: 'Оплата получена, спасибо! Игра «{title}» теперь до {limit} участников.'
  4. If the payer is not the organizer, tell the organizer: '{payer} оплатил(а) расширение игры «{title}» до {limit} участников.'
  5. P1: update the group card.
- If two payments arrive for the same game and tier, alert the admin: 'Двойная оплата {CODE}: InvId a и b — верните одну в кабинете Robokassa.'

5.7 Anonymous messages (P0, only when anon_chat=1)
- Santa asks the receiver:
  - Prompt: 'Напишите сообщение для {receiver}. Я передам его без вашего имени (до 500 символов).' with [Отмена].
  - The receiver gets: 'Сообщение от вашего Тайного Санты (игра «{title}»): «{text}»' with [Ответить Санте] [Пожаловаться].
- The receiver writes to their Santa:
  - The Santa gets: 'Сообщение от {receiver_name} — человека, которому вы дарите: «{text}»' with [Ответить анонимно] [Пожаловаться].
- Reply buttons carry the relay id. A reply always goes back to the counterpart. The Santa's identity is NEVER revealed.
- The sender gets a confirmation: 'Передал. Ваше имя не видно.' or 'Передал вашему Санте.'
- Limits:
  - text only, at most 500 characters;
  - at most 20 relays per user per game per day;
  - send with disable_link_preview;
  - keep relays for 30 days.
- [Пожаловаться] creates a report. Every admin gets a message with the text, the game code and the user ids, plus buttons [Заблокировать отправителя] [Закрыть жалобу].
- Blocked users cannot send relays or create games. They see: 'Доступ ограничен из-за жалоб. Вопросы: {SUPPORT_EMAIL}.'

5.8 Мои игры and help (P0)
- 'Мои игры' lists the user's games, as organizer or participant, with their status. Tapping a game opens:
  - For a participant: game info, their name and wishes, and the buttons:
    - [Изменить пожелания] [Изменить имя] [Выйти из игры], before the draw;
    - [Кому я дарю?] plus the relay buttons, after the draw;
    - [Устроить игру в другом чате].
  - For the organizer: the organizer panel.
- Help text:
  'Как это работает:
  1) Организатор нажимает «Создать игру» и пересылает приглашение в общий чат.
  2) Участники нажимают ссылку, затем «Начать», и пишут, что хотят получить.
  3) Организатор нажимает «Провести жеребьёвку» — каждому приходит, кому он дарит, и пожелания этого человека.
  4) Своему получателю можно задать вопрос анонимно.
  До {free_limit} участников — бесплатно. Больше — один раз за игру: {price_S} ₽ (до {limit_S}), {price_M} ₽ (до {limit_M}), {price_L} ₽ (до {limit_L}).
  Ребёнок без MAX? Родитель вступает сам и меняет имя, например «Маша, 5Б».
  Вопросы: {SUPPORT_EMAIL}. Правила: {BASE}/terms · Политика: {BASE}/privacy'

5.9 Unsolicited messages (P0)
These are the ONLY messages the bot sends without a user action, because MAX forbids mass and marketing messages:
- (a) Join and waiting notices to the organizer, throttled as described above.
- (b) One nudge to the organizer when the game is not drawn and the exchange is 3 days away or less:
  'До обмена в «{title}» {d} дн., а жеребьёвки ещё не было. Участников: {n}.'
  Button: [Провести жеребьёвку]
- (c) If reminder_on is on and the game is drawn: one reminder to each participant at 12:00 MSK on the day before exchange_date:
  'Завтра обмен подарками в игре «{title}». Вы дарите: {receiver}.'
  Button: [Кому я дарю?]
- (d) Payment confirmations and draw results.
Nothing else: no broadcasts, no promotions, no 'come back' messages.

5.10 Group mode (P1)
This requires the owner to enable group chats in the bot settings.
- On bot_added (is_channel = false), post:
  'Привет! Я помогу провести Тайного Санту в этом чате. Кто организует — нажмите кнопку, настройка займёт минуту.'
  Button: [Создать игру в этом чате], a link to https://max.ru/{BOT}?start=gc_{chat_id}. Encode a negative chat id as gcm{abs}.
- The gc payload runs the §5.2 wizard with group_chat_id set. Call GET /chats/{chatId} first to make sure the chat exists.
- Then post the GROUP CARD in the chat:
  'Тайный Санта «{title}»
  Бюджет: {budget} · Обмен: {date}
  Участвуют: {n} из {limit}: {first 15 names}…
  Нажмите «Участвую», затем «Начать» в боте — туда придёт ваша пара.'
  Button: [Участвую], a LINK to j_{CODE}. The card has no callbacks, so strangers never trigger bot actions.
- Edit the card:
  - on count changes, at most once per 10 s per game;
  - after payments;
  - after the draw, with the text 'Жеребьёвка проведена! Каждому участнику бот прислал пару в личные сообщения. Не пришло? Откройте бота → «Мои игры».'
- If an edit fails, post a new card and store its mid.
- On bot_removed, clear group_chat_id. The game continues in link mode.

5.11 Reveal (P1)
- After exchange_date, the organizer can press [Раскрыть, кто чей Санта]. After confirmation, the chain ('Ольга → Иван, Иван → Мария…') goes out once per game:
  - Group mode: posted in the group, followed by 'Провести такую же игру в другом чате: https://max.ru/{BOT}?start=n_{CODE}'.
  - Link mode: sent to every participant with a [Устроить игру в другом чате] button.

6. VIRAL MECHANICS THAT MUST EXIST IN CODE
1. The forwardable invite carries both the deep link and the plain code (§5.2), and can be re-sent with live progress (§5.4).
2. Joining happens only inside the bot. Every participant sees the product and can be messaged later.
3. [Устроить игру в другом чате] button:
   - inside the bot it is callback ref:{CODE}, which starts the §5.2 wizard with source_game_id set;
   - outside the bot it is the deep link n_{CODE}.
   It appears in: the join confirmation, the draw result, the game view, the 'already drawn' message and the reveal.
4. Anyone can pay for an upgrade (§5.6).
5. P1: the live group card with names, and the group reveal.
6. Landing attribution:
   - The landing button 'Открыть бота' links to s_{src}. src comes from utm_source or src, sanitized to [a-z0-9]{1,16}; the default is 'site'.
   - Ads can link directly to https://max.ru/{BOT}?start=s_yd or s_vk.
7. Payload grammar (core/payloads.py, unit-tested):
   - j_CODE: join a game
   - n_CODE: new game, referred by that game
   - s_src: source tag
   - p_INVID: return from payment; show 'Проверяю оплату…' and then the panel
   - gc_ID / gcmID: create a game for a group (P1)
   - o_CODE: organizer panel
   Anything else is treated as an empty payload.
8. users.first_source is set once, to one of 'j:CODE', 'n:CODE', 's:src' or 'direct'.
   games.source is one of:
   - 'ref': created via the ref button or an n_ payload;
   - 'participant': the organizer's first touch was as a participant of another game;
   - 's:src';
   - 'direct'.
9. Analytics events to record: user_first_seen, consent, game_created, invite_open, join, join_waiting, wishes_saved, draw_done{n}, result_dm_failed, relay_sent, report, pay_click{tier,amount}, pay_success{tier,amount}, ref_click, group_added, reveal.

7. ROBOKASSA
The shop is registered as self-employed. Start in test mode.
- Payment URL (GET):
  https://auth.robokassa.ru/Merchant/Index.aspx?MerchantLogin={login}&OutSum={amount}.00&InvId={inv_id}&Description={urlencoded}&SignatureValue={sig}&Culture=ru
  Add &IsTest=1 when ROBOKASSA_TEST=1.
- Description: at most 100 characters, no quotes or special symbols. Example: 'Расширение игры Тайный Санта до 30 участников'.
- Request signature: hash('{login}:{OutSum}:{InvId}:{password1}') with ROBOKASSA_HASH, as lowercase hex.
  - The hash is md5 by default; also support sha256 and sha512.
  - In test mode, use the test passwords.
  - No Shp_ parameters: InvId alone identifies our payment row.
- Receipt: send the Receipt parameter only if ROBOKASSA_SEND_RECEIPT=1.
  - Items: [{name, quantity:1, sum, tax:"none"}].
  - It then joins the signature as '{login}:{OutSum}:{InvId}:{Receipt}:{password1}'. Follow docs.robokassa.ru/ru/fiscalization exactly.
  - Off by default: the Робочеки СМЗ service issues НПД receipts automatically.
- ResultURL: /pay/robokassa/result, accepting both POST and GET. Processing order:
  1. Read OutSum, InvId and SignatureValue.
  2. Verify hash('{OutSum as received}:{InvId}:{password2}'). Use the EXACT received OutSum string, which may look like '490.000000'. Compare case-insensitively, in constant time.
  3. Check that Decimal(OutSum) == amount_rub.
  4. If the payment is already paid, return 'OK{InvId}' and do nothing else (idempotent).
  5. Otherwise, in one transaction: mark it paid, apply the tier (§5.6) and write an event.
  6. Enqueue the notifications.
  7. Respond text/plain 'OK{InvId}'.
  A bad signature returns 400 and alerts the admin, at most once per 10 min.
- SuccessURL /pay/success and FailURL /pay/fail are plain pages: 'Спасибо! Оплата получена — вернитесь в MAX' or 'Оплата не прошла — попробуйте ещё раз'. Each has a big link to https://max.ru/{BOT}?start=p_{InvId}. The SuccessURL must NEVER change state.
- Refunds are done manually in the Robokassa cabinet. /refund INVID only marks the payment as refunded.
- Tests: the signature against a vector computed inside the test, a valid result, a bad signature, an amount mismatch, a replay, the '490.000000' format, test mode, and two concurrent result calls.

8. WEBSITE
aiohttp + jinja2, Russian, mobile-first.
- No external resources: no Google Fonts, CDN or captcha.
- No logins and no user accounts (this also avoids the 199-FZ login rules).
- Each page is under 60 KB.
- CSP: default-src 'self', plus mc.yandex.ru only when METRICA_ID is set.
Routes:
- GET /: the landing page. Contents:
  - H1 'Тайный Санта прямо в чате MAX — без сайтов, почты и регистрации';
  - the three steps, an example result message, and the prices read from settings;
  - FAQ: a child without MAX, exclusions, 'не пришла пара', payment, refunds, data;
  - a big button 'Открыть бота в MAX' linking to s_{src};
  - the counter 'Уже проведено {N} жеребьёвок', hidden while N < 50 and cached for 10 min;
  - a footer with the seller's full name, ИНН and email, and links to the legal pages.
- GET /offer, /privacy, /consent, /terms, /contacts: Russian TEMPLATES with placeholders filled from env. README_RU must tell the owner to review them.
  - Offer: the seller is a self-employed person. The service is 'предоставление доступа к расширенным функциям программы (чат-бота) для организации обмена подарками'. It includes:
    - the prices;
    - that the service is rendered when the limit increases, immediately after payment;
    - refunds: on request before the draw (email, within 14 days); none after the draw except on technical failure;
    - liability limits and contacts.
  - Privacy policy:
    - Data processed: MAX id, name, display name, wishes, anonymous messages (kept 30 days), payment metadata.
    - Purposes: running games and taking payments.
    - The data is stored on a server in Russia.
    - Game data is deleted 180 days after the game ends.
    - Users' rights, and how to request deletion by email.
  - Consent: a separate document, as 152-FZ art. 9 requires.
  - Terms:
    - users must be 14+; parents act for children;
    - forbidden content;
    - how complaints and blocking work;
    - a statement that this is not a contest or prize draw.
- GET /pay/success, GET /pay/fail, POST|GET /pay/robokassa/result.
- POST /max/webhook/{WEBHOOK_PATH_SECRET}:
  - check the X-Max-Bot-Api-Secret header in constant time; on mismatch return 404;
  - accept either a single Update object or {updates:[...]};
  - deduplicate, run processing in a background task, and return 200 immediately.
- GET /healthz returns JSON: {ok, db_ok, outbox_pending, last_update_at, webhook_registered, bot_enabled, payments_enabled}.
- P1: GET /admin/export/{payments|games|events}.csv?token=ADMIN_EXPORT_TOKEN. Never include wishes or relay texts.
- The app must start and serve the website even if MAX_BOT_TOKEN or the Robokassa settings are empty. The bot and payments are then disabled, with a warning. The owner can deploy the site first, because Robokassa reviews the site before connecting the shop.

9. OWNER AND ADMIN FEATURES
Bot commands for ADMIN_USER_IDS only (except /whoami, which anyone can use).
- /stats: numbers for today, the last 7 days and the whole season:
  - new users and consents;
  - games created, and games with 3 or more participants;
  - draws, and participants in draws (average and median per game);
  - games that hit the free limit;
  - paid games and revenue in ₽;
  - conversion = paid games / games that hit the limit;
  - participant→organizer rate = distinct users who organized a game after first joining another game / distinct participants;
  - games created via ref;
  - result-DM failure rate;
  - open reports;
  - where organizers came from over the last 7 days.
- /game CODE: a summary of the game (without wishes or relays), its payments and status. Buttons: [Выдать S] [Выдать M] [Выдать L] [Отменить игру].
- /grant CODE S|M|L [amount]: records a manual payment with status 'granted' (for example, a company's bank transfer) and applies the tier.
- /refund INVID: marks the payment as refunded.
- /price S 490 and /price free 10 (likewise M, L, limit_S and so on): change settings and reply with the new price list. The landing picks up changes within 10 min.
- /block USERID, /unblock USERID. Report messages include one-tap buttons for these.
- /maintenance on|off: while on, new games and joins are refused with 'Идут технические работы. Попробуйте через 15 минут.'
- Daily digest to the admins at 21:00 MSK, between DIGEST_FROM and DIGEST_TO: yesterday's and today's key numbers.
- Error alerts: unhandled exceptions are sent to the admins at most once per 5 min, with a count of suppressed repeats. A 401 from MAX produces 'Токен бота не принят — проверьте MAX_BOT_TOKEN'.
- If a command-menu API exists (unverified), register the menu. Otherwise skip it; everything is reachable through buttons.

10. SCHEDULER
One asyncio loop with a 30 s tick. Each job keeps its own last-run guard.
- Outbox worker, running continuously:
  - rate limits: 25/s overall and 1/s per target;
  - on 429 or a Transient error, retry with backoff of 2, 5, 15, 60, 180 and 600 s, then mark dead;
  - on Forbidden to a user: set dm_ok=0, mark the message dead, and set result_dm_ok=0 if it was a draw result;
  - on BadRequest: mark dead and alert the admin.
  Interactive replies are sent directly but go through the same limiter. Bulk and scheduled messages go through the outbox.
- Every minute: join and waiting notices, and the organizer nudges.
- 12:00 MSK: pre-exchange reminders.
- 00:10 MSK: mark games finished once exchange_date is at least 3 days past.
- 03:30 MSK, data retention:
  - delete finished and cancelled games with all their rows 180 days after exchange_date, or 180 days after creation if there is no date;
  - delete relays older than 30 days;
  - delete users who never consented after 7 days, and users with no games for 365 days;
  - set payer_id to NULL on payments older than 180 days, but keep the amounts;
  - strip user_id from events older than 180 days.
- 04:00 MSK: SQLite online backup to /data/backups/santa-YYYYMMDD.db, keeping 14 files. P1: also upload to an S3-compatible bucket in Russia if S3_* is set.
- Every 10 min, webhook watchdog: list subscriptions. If ours is missing, re-subscribe and alert the admin 'Подписка вебхука была потеряна и восстановлена'.
- Daily: certificate expiry check.
- 21:00 MSK: the digest.

11. RELIABILITY, SECURITY, COMPLIANCE
- Locking: one process. A per-user asyncio.Lock serializes each user's updates. A per-game lock covers join, limit, payment and draw. A webhook request must never block for more than 1 s.
- Startup sequence:
  1. Run migrations.
  2. Validate the config.
  3. Call GET /me and log the username; warn the admin if it differs from MAX_BOT_USERNAME.
  4. Ensure the webhook subscription exists, with update_types [bot_started, bot_added, bot_removed, bot_stopped, message_created, message_callback] and the secret.
  5. Start the outbox and the scheduler.
- Logs are JSON lines on stdout. NEVER log wishes, relay texts or tokens.
- Input validation:
  - title at most 60 characters, name 40, wishes 1000, relay 500, budget 30;
  - strip control characters;
  - no markup.
- Personal data stays only on the Russian VPS and is never sent to foreign services. Yandex Metrica on the landing is optional.
- Keep a consent gate with a stored consent_version.
- User-generated content duties: a report button, blocking, and deletion.
- Copy rules:
  - Russian only. Avoid English loanwords (168-FZ): write 'пожелания', never 'вишлист'.
  - Never use 'розыгрыш', 'конкурс' or 'приз' in the bot or in its description. Use 'обмен подарками' and 'жеребьёвка пар'.
  - No MAX logos. Plain-text mentions of MAX are fine.
- Secrets live only in .env, which is git-ignored. The webhook requires both the random path segment and the header secret.

12. CONFIG (.env.example, with comments)
The owner provides:
- DOMAIN, e.g. santa-v-chate.ru.
- PUBLIC_BASE_URL=https://DOMAIN
- MAX_BOT_TOKEN: from the MAX partner platform, Боты → бот → Расширенные настройки → Токен. It becomes available after the bot passes moderation.
- MAX_BOT_USERNAME: shown on the bot card, e.g. se1234567_bot.
- MAX_API_BASE=https://platform-api2.max.ru
- MODE=webhook|polling
- MAX_WEBHOOK_SECRET: 32 characters from [A-Za-z0-9-].
- WEBHOOK_PATH_SECRET: 24 random characters.
- ROBOKASSA_MERCHANT_LOGIN, ROBOKASSA_PASSWORD1, ROBOKASSA_PASSWORD2, ROBOKASSA_TEST_PASSWORD1, ROBOKASSA_TEST_PASSWORD2: from the Robokassa cabinet, Технические настройки.
- ROBOKASSA_TEST=1: set to 0 after a successful test payment.
- ROBOKASSA_HASH=md5
- ROBOKASSA_SEND_RECEIPT=0
- ADMIN_USER_IDS: comma-separated ids, obtained with /whoami.
- ADMIN_EXPORT_TOKEN
- OWNER_FULL_NAME, OWNER_INN, SUPPORT_EMAIL: required on the legal pages and by Robokassa and MAX.
- FREE_LIMIT=10, PRICE_S=490, LIMIT_S=30, PRICE_M=990, LIMIT_M=100, PRICE_L=2490, LIMIT_L=300
- CONSENT_VERSION=2026-10
- TZ=Europe/Moscow
- DIGEST_FROM=11-01, DIGEST_TO=01-10
- Optional: METRICA_ID; S3_ENDPOINT, S3_BUCKET, S3_KEY, S3_SECRET.
On first start, the app generates any missing random secrets and prints them once.

13. DEPLOYMENT
Target: a Russian VPS, with no Cloudflare in front.
- Dockerfile:
  - python:3.12-slim;
  - apt-get install ca-certificates and tzdata;
  - copy certs/*.pem to /usr/local/share/ca-certificates/*.crt and run update-ca-certificates;
  - pip install -r requirements.txt;
  - run as a non-root user;
  - EXPOSE 8080; CMD python -m app.main.
- docker-compose.yml:
  - app: restart unless-stopped, env_file .env, volume ./data:/data, healthcheck on /healthz;
  - caddy:2: ports 80 and 443, volumes caddy_data and caddy_config, and the Caddyfile.
- Caddyfile: `{$DOMAIN} { encode gzip  reverse_proxy app:8080 }`. Let's Encrypt satisfies MAX's trusted-certificate rule.
Owner steps (README_RU must repeat these in Russian):
1. Rent a VPS: Ubuntu 24.04, 1-2 vCPU, 1-2 GB RAM, Moscow region (for example Timeweb Cloud, about 500-900 ₽/month).
2. Point the DNS A record at the VPS IP.
3. Install Docker and the compose plugin. If pulls from Docker Hub or PyPI fail from the VPS, configure the provider's registry mirror in /etc/docker/daemon.json.
4. Copy the code to the server with git clone or scp.
5. cp .env.example .env and fill it in.
6. docker compose up -d --build
7. Open https://DOMAIN/healthz.
8. In MAX, send /whoami to the bot, put the id into ADMIN_USER_IDS, and run docker compose up -d again.
9. In the Robokassa cabinet set:
   - Result URL https://DOMAIN/pay/robokassa/result (POST);
   - Success URL https://DOMAIN/pay/success (GET);
   - Fail URL https://DOMAIN/pay/fail (GET);
   - hash algorithm MD5.
10. Make a test payment. Then set ROBOKASSA_TEST=0, make one real 490 ₽ payment to yourself, and refund it in the cabinet.
Maintenance:
- Update: git pull && docker compose up -d --build.
- Logs: docker compose logs -f app.
- Restore: stop the app, copy a backup over /data/santa.db, start the app.
README_RU must also include:
- the bot card for moderation: name 'Санта в чате — Тайный Санта', and a description of at most 200 characters, e.g. 'Проведу «Тайного Санту» для семьи, коллег или класса: приглашение по ссылке, пожелания, жеребьёвка пар, анонимные вопросы. До 10 человек бесплатно. Правила: DOMAIN/terms';
- the data categories and purposes to enter in the Roskomnadzor notification;
- a season checklist.

14. TESTS
All tests run offline. FakeMaxApi records every send, edit and answer per target.
Unit tests:
- Draw:
  - n from 3 to 60 with random exclusions (up to n/3 pairs): the result is a single cycle of length n with no self-gifts and no excluded adjacency;
  - n=3 with any exclusion returns None;
  - a draw for 300 people finishes in under 1 s.
- Payload parsing and sanitizing.
- Pricing and upgrade price differences.
- Date parsing.
- Robokassa signatures (§7).
- Notice throttling.
- Data retention.
- Certificate fingerprints.
End-to-end tests (webhook → handlers → FakeMaxApi):
1. Consent gate: nothing but the id and source is stored before consent; the start payload resumes after consent.
2. Family game in link mode:
   - 8 users join via j_, set names and wishes, and the organizer adds one exclusion;
   - the draw gives each user exactly one correct result with the receiver's wishes;
   - the organizer panel shows correct counts.
3. Limit and payment:
   - the 11th user lands on the waiting list;
   - the pay callback creates a payment;
   - a ResultURL with a valid test signature upgrades the game to S, activates the waiting user and notifies them;
   - a replayed ResultURL changes nothing and still returns OK{InvId}.
4. Code fallback: a returning user sends 'код abc123' and joins.
5. Relay:
   - the receiver never sees the giver's name or id;
   - reply routing works;
   - a report reaches the admin;
   - a blocked user cannot send.
6. A non-organizer's draw or remove callback is refused.
7. A webhook with a wrong secret gets 404 and nothing is processed.
8. Outbox:
   - 300 queued sends respect 25/s overall and 1/s per target (use a fake clock);
   - Forbidden sets dm_ok=0;
   - pending messages survive a restart.
9. The ref button creates a game with source_game_id set, and /stats counts it.
10. P1: bot_added → gc_ flow → the group card is posted and then edited on join and after the draw.
Simulator: tools/simulate.py runs scenario 3 with 12 named Russian users on FakeMaxApi and prints a chat-like transcript. It serves as the owner's demo and as a smoke test.

15. OUT OF SCOPE (do not build)
- Telegram, VK, WhatsApp or web versions of the game.
- Browser-based participation.
- MAX mini-apps.
- Gift ideas, marketplace or affiliate links, and any ads.
- Recurring subscriptions.
- Automatic invoices or closing documents for companies.
- A refund API and OpState polling.
- Email or SMS.
- Website accounts or logins of any kind (including Google, Apple or VK ID).
- Phone-number collection.
- Any AI/LLM features.
- Paid reveal, Santa calls, physical delivery.
- Broadcasts, newsletters, re-engagement messages, referral payouts.
- A multi-language UI.
- An admin web dashboard.
- Postgres, Redis, multiple instances, Kubernetes.

16. PHASE 2 BACKLOG (after the season, only if the numbers justify it)
- Team mode for the 23 Feb / 8 Mar class exchanges, where one group gifts the other (bipartite matching).
- Avoiding last year's pairs.
- Year-round birthday wish lists with gift reservation.
- 'Тайный друг' for teams.
- A Telegram adapter for CIS users (Stars payments, hosted abroad).
- A web fallback for participants without MAX.
- A MAX mini-app UI for wishes.
## Implementation notes
Deliberate deviations and decisions made while building (foundation stage):
- Data model: `participants.via` also allows `organizer` (the organizer's own row). `payments.game_id` is nullable with ON DELETE SET NULL, so amounts survive the 180-day game purge. `outbox` has two extra columns, `purpose` and `game_id`, so delivery hooks can track draw results (`result_dm_ok`) and send the 'Пары отправлены' summary exactly once.
- MAX API (checked on dev.max.ru 2026-09-24): PUT /messages, POST /answers and POST /subscriptions answer HTTP 200 with `{success:false, message}` on failure; this is treated as an error and classified by the message text. HTTP 403 is always Forbidden. `notification` in /answers is undocumented: on BadRequest the bot answers without it, stops sending it, and the outbox sends the text as a normal message.
- Rate limits: the global bucket is 25/s with a burst of 1 (evenly spaced). Callback answers use their own 1/s lane per dialog, so a dialog gets at most one send/edit plus one answer per second (MAX allows 2/s).
- Retries: HTTP 401 is retried with the same backoff as 429/5xx (a token being fixed should not kill queued messages) and logged as an error.
- Config: extra optional variables `DATA_DIR` (default /data) and `PORT` (default 8080). Generated secrets are stored in `DATA_DIR/secrets.env` (mode 0600) and printed once; a value in .env wins. Owner fields (OWNER_*, SUPPORT_EMAIL) are required only when the bot or payments are enabled; otherwise they are warnings.
- Leaving or being removed before the draw deletes that person's exclusions and moves the first waiting person into the game. A removed participant cannot rejoin the same game.
- /stats periods are Moscow calendar days: today, the last 7 days including today, and the season since September 1.
Bot flows stage (handlers):
- Handlers live in `app/handlers/`: `router.py` (dispatch), `private.py` (bot_started, typed text, commands; registries `@command` and `@state_handler`), `callbacks.py` (buttons; registry `@on(prefix, admin_only=)`), plus `session.py` (reply/answer bookkeeping, refusal texts), `flows.py` (shared flows), `views.py` (message builders and the callback vocabulary `Action`) and `notices.py` (queued messages to other people, including `payment_applied` for the ResultURL and `draw_results`).
- Callback payloads carry the game id (`pn:17`, `rmy:17:12345`), except `pay:{CODE}:{tier}` and `ref:{CODE}` as specified. Navigation screens (panel, lists, pickers, settings) replace the message whose button was pressed via the callback answer's `message`; results and prompts are new messages.
- The creation wizard keeps its draft in `user_state.data`; step 1–3 use the kinds title, budget_custom and date_custom (typed text at the budget or date step is taken as a custom value), step 4 keeps date_custom and re-asks on typed text. Settings edits reuse these kinds with `game_id` set.
- An explicit 'код XXXXXX' always joins, even during a pending input; a bare code joins only when no input is pending and the game exists. Before consent, a typed code of an existing game is kept as the resume payload `j_CODE`.
- The organizer opening their own invite gets the panel instead of joining; the organizer leaves or rejoins only through the participation toggle in the settings. Leaving and removal after the draw are not offered (P1).
- The 'already drawn' message's [Создать игру] button is the ref button, so such games count as referred (§6.3).
- When nobody can pay for more places (top tier reached or payments disabled) the waiting message omits the price and the pay button.
- A person can report the same anonymous message once; a repeated tap only repeats 'жалоба отправлена'.
- Blocked users are refused relays and game creation only, as §5.7 says; they can still join games.
- Unverified (Robokassa): a Receipt in a GET link. It is signed URL-encoded, as the fiscalization docs say, and URL-encoded once more as a query value.
Payments and website stage:
- Robokassa docs (notifications-and-redirects, fiscalization; read 2026-09-25) agree with §7: ResultURL gets OutSum, InvId, SignatureValue (plus Fee, EMail, PaymentMethod, IncCurrLabel, IsTest) by GET or POST as set in the cabinet; OutSum has two decimals in test mode and six in live mode; the answer is 'OK{InvId}'. If Shp_ parameters ever arrive they are appended to the signature string sorted by name, as the docs say.
- ResultURL answers: 503 while payments are disabled (Robokassa retries later); 400 for a malformed notice (no alert), a bad signature (alert throttled to once per 10 min), an unknown InvId or an amount mismatch (both alert the admins, since the signature was valid). A notice for a payment that is already paid, granted or refunded returns 'OK{InvId}' and changes nothing.
- Marking paid, applying the tier, the pay_success event and all notifications (payer, organizer, activated waiting people, double-payment and after-draw alerts) are written in ONE transaction under the game lock, so a crash cannot apply a payment without announcing it. `payments.raw` keeps only OutSum, InvId, Fee, PaymentMethod, IncCurrLabel and IsTest — never the payer's e-mail or the signature.
- /pay/success and /pay/fail accept GET and POST (in case the cabinet is set to POST) and never touch the database; the return link carries p_{InvId} only when InvId looks like ours.
- Webhook: while the bot is disabled the endpoint is 404 as well; a body that is not JSON gets 400. The updates of one request are processed in order in one background task; deduplication happens there, in `process_update`.
- The landing counter counts draw_done events without redraws. Yandex Metrica is loaded from /static/metrica.js (no inline script, so the CSP needs no 'unsafe-inline'); with METRICA_ID set the CSP allows https://mc.yandex.ru for script, img and connect, and the privacy policy gains a paragraph about it.
- Extra: /robots.txt (hides /pay/, /max/, /admin/), HSTS when PUBLIC_BASE_URL is https, /healthz answers 503 when the database check fails. The CSV export starts with a BOM for Excel and neutralizes cells that start with =, +, -, @.
Operations, admin and integration stage:
- Scheduler (`app/scheduler.py`, jobs in `app/jobs.py`): every job's last run is stored in a new table `job_runs` (migration 002), so a restart neither repeats nor skips a daily job. A first start before a daily slot waits for it; a missed slot is made up when the bot is back, except the digest (only until 23:00) and the pre-exchange reminder (only until 21:00). Extra daily job: the certificate check runs at 09:00. The tick paces itself in real time; due-ness uses the injected clock.
- Organizer nudge (§5.9 b): sent between 10:00 and 21:00 Moscow time only, so it does not arrive at midnight when the 3-day window opens. It is sent for exchange dates 1–3 days ahead.
- Waiting notice when nobody can pay (top tier or payments off): 'В игру … хотят вступить ещё w чел., но мест нет.' with the panel button. Join notices carry a [Пульт игры] button.
- Ending games (00:10): besides finishing drawn games 3 days after the exchange, abandoned games end so retention can delete them: a never-drawn game is cancelled (silently) 30 days after its exchange date, and a game without a date ends 180 days after creation (finished if drawn, else cancelled).
- Retention (03:30), in addition to §10: deleting a game also deletes its complaints and queued messages; closed complaints (they quote relays) go after 30 days, open ones wait for an admin; expired pending inputs are deleted; delivered/dead outbox rows (they quote wishes and relays) go after 7 days; processed update keys after 3 days. 'No games for 365 days' is counted from the user's last game date, kept in the new column `users.last_game_at` when retention purges that game. Deleted users' ids are removed from events and payments.
- Backups are written under a temporary name and renamed, with mode 0600, and converted to a single self-contained file (journal_mode=DELETE). P1 S3 upload is not built.
- Errors: unhandled exceptions in handlers, jobs and web routes alert the admins through one throttle (once per 5 min with the suppressed count); the web returns a plain 500 and logs only the route pattern (the webhook path holds a secret). The user whose update failed gets 'Что-то пошло не так…'. A 401 from MAX (sends, GET /me, the watchdog, polling) alerts 'Токен бота не принят…' at most once per hour; the alert is queued and arrives once the token works.
- Admin (§9): `/admin` lists the commands. `/game CODE` shows [Выдать S/M/L] only while the game is collecting and [Отменить игру] while it is collecting or drawn; cancelling asks for confirmation and tells every participant and the organizer 'отменена администрацией сервиса'. [Выдать …] grants with amount 0; `/grant` refuses games that are no longer collecting and tells the organizer the new limit. `/refund` accepts only paid or granted payments. `/price` without arguments shows the prices; changes are validated like the env prices. `/block` and the report's block button reply with an [Разблокировать] button.
- People moved from the queue into the game (payment, grant, someone leaving) can type their wishes right away: their pending input becomes 'wishes' unless they are typing something else. Returning from the payment page (p_) asks for wishes when they are missing.
- Polling mode (local tests only) processes the updates of one page in order, like a webhook batch.
