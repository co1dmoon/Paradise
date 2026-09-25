"""HTTP routes (§8): the website, Robokassa callbacks, the MAX webhook, /healthz and the CSV export.

Seam: ``app.main.create_app`` calls ``register(app)`` once while building the
application. Handlers reach the ``AppContext`` at request time through
``request.app[app.context.CTX_KEY]``.

Every response gets the security headers (CSP ``default-src 'self'``, plus
mc.yandex.ru only when METRICA_ID is set). Secrets in URLs and headers are
compared in constant time, and a mismatch looks exactly like a missing page (404).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import jinja2
from aiohttp import web

from app.context import CTX_KEY, AppContext
from app.core import texts
from app.core.clock import to_iso
from app.payments import robokassa
from app.updates import process_update
from app.web import export, pages

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

JINJA_KEY = web.AppKey("jinja", jinja2.Environment)
COUNTER_KEY = web.AppKey("draw_counter", pages.DrawCounter)
WEBHOOK_SECRET_HEADER = "X-Max-Bot-Api-Secret"
METRICA_ORIGIN = "https://mc.yandex.ru"
ROBOTS_TXT = "User-agent: *\nDisallow: /pay/\nDisallow: /max/\nDisallow: /admin/\n"
LEGAL_PAGES = {
    "/offer": "offer.html",
    "/privacy": "privacy.html",
    "/consent": "consent.html",
    "/terms": "terms.html",
    "/contacts": "contacts.html",
}


def register(app: web.Application) -> None:
    """Add the site's routes, static files and security headers to ``app``."""
    app[JINJA_KEY] = pages.environment()
    app[COUNTER_KEY] = pages.DrawCounter()
    app.middlewares.append(_report_errors)
    app.on_response_prepare.append(_security_headers)
    router = app.router
    router.add_get("/", landing)
    for path, template in LEGAL_PAGES.items():
        router.add_get(path, _legal_page(template))
    router.add_get("/robots.txt", robots)
    for path, handler in (("/pay/success", pay_success), ("/pay/fail", pay_fail),
                          ("/pay/robokassa/result", robokassa_result)):
        router.add_get(path, handler)
        router.add_post(path, handler)
    router.add_post("/max/webhook/{secret}", max_webhook)
    router.add_get("/healthz", healthz)
    router.add_get("/admin/export/{table}.csv", admin_export)
    router.add_static("/static", pages.STATIC_DIR, append_version=False)


def _ctx(request: web.Request) -> AppContext:
    return request.app[CTX_KEY]


def secrets_match(expected: str, received: str) -> bool:
    """Constant-time equality; an empty expected secret never matches."""
    return bool(expected) and hmac.compare_digest(expected.encode(), received.encode())


# --- pages ---------------------------------------------------------------------------------------


async def _render(request: web.Request, template: str, **values: Any) -> web.Response:
    common = await pages.common_values(_ctx(request))
    return pages.html(request.app[JINJA_KEY], template, {**common, **values})


async def landing(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    src = request.query.get("utm_source") or request.query.get("src")
    draws = await request.app[COUNTER_KEY].visible(ctx)
    return await _render(
        request, "index.html",
        open_bot_url=pages.landing_bot_link(ctx.config, src),
        draws=draws,
        draws_word=texts.plural(draws or 0, "жеребьёвка", "жеребьёвки", "жеребьёвок"),
        example_text=pages.example_result(),
        example_buttons=pages.example_buttons(),
    )


def _legal_page(template: str) -> Handler:
    async def page(request: web.Request) -> web.Response:
        return await _render(request, template)

    return page


async def robots(request: web.Request) -> web.Response:
    return web.Response(text=ROBOTS_TXT, content_type="text/plain")


async def pay_success(request: web.Request) -> web.Response:
    """SuccessURL: a plain page with a way back to the bot. NEVER changes state (§7)."""
    return await _payment_page(request, success=True)


async def pay_fail(request: web.Request) -> web.Response:
    return await _payment_page(request, success=False)


async def _payment_page(request: web.Request, *, success: bool) -> web.Response:
    params = await _params(request)
    inv_id = pages.parse_inv_id(params.get("InvId", ""))
    link = pages.payment_return_link(_ctx(request).config, inv_id)
    return await _render(request, "pay_result.html", success=success, inv_id=inv_id, return_url=link)


# --- Robokassa -----------------------------------------------------------------------------------


async def robokassa_result(request: web.Request) -> web.Response:
    """ResultURL, GET or POST (as set in the Robokassa cabinet): 'OK{InvId}' as text/plain."""
    reply = await robokassa.process_result(_ctx(request), await _params(request))
    return web.Response(text=reply.text, status=reply.status, content_type="text/plain")


async def _params(request: web.Request) -> Mapping[str, str]:
    """Query parameters, overridden by form fields for POST."""
    params = dict(request.query)
    if request.method == "POST":
        form = await request.post()
        params.update({key: value for key, value in form.items() if isinstance(value, str)})
    return params


# --- MAX webhook -------------------------------------------------------------------------------


async def max_webhook(request: web.Request) -> web.Response:
    """Answer 200 at once and process in the background; wrong path or header secret → 404 (§8)."""
    ctx = _ctx(request)
    config = ctx.config
    if not (
        config.bot_enabled
        and secrets_match(config.webhook_path_secret, request.match_info["secret"])
        and secrets_match(config.max_webhook_secret, request.headers.get(WEBHOOK_SECRET_HEADER, ""))
    ):
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="invalid JSON") from None
    updates = _updates_of(body)
    if updates:
        ctx.spawn(_process_in_order(ctx, updates), name="webhook")
    return web.json_response({"ok": True})


def _updates_of(body: Any) -> list[dict[str, Any]]:
    """A single Update object or {updates: [...]}; anything that is not an object is skipped."""
    if isinstance(body, dict) and isinstance(body.get("updates"), list):
        items = body["updates"]
    else:
        items = [body]
    return [item for item in items if isinstance(item, dict)]


async def _process_in_order(ctx: AppContext, updates: list[dict[str, Any]]) -> None:
    for raw in updates:
        await process_update(ctx, raw)


# --- health and export ----------------------------------------------------------------------------


async def healthz(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    db_ok = await ctx.db.ping()
    last_update = ctx.runtime.last_update_at
    report = {
        "ok": db_ok,
        "db_ok": db_ok,
        "outbox_pending": await ctx.outbox.pending_count() if db_ok else None,
        "last_update_at": to_iso(last_update) if last_update else None,
        "webhook_registered": ctx.runtime.webhook_registered,
        "bot_enabled": ctx.config.bot_enabled,
        "payments_enabled": ctx.config.payments_enabled,
        "payments_test_mode": ctx.config.payments_enabled and ctx.config.robokassa_test,
    }
    return web.json_response(report, status=200 if db_ok else 503, headers={"Cache-Control": "no-store"})


async def admin_export(request: web.Request) -> web.StreamResponse:
    """P1: /admin/export/{payments|games|events}.csv?token=ADMIN_EXPORT_TOKEN. Never wishes or relays."""
    ctx = _ctx(request)
    table = request.match_info["table"]
    if table not in export.TABLES or not secrets_match(ctx.config.admin_export_token,
                                                        request.query.get("token", "")):
        raise web.HTTPNotFound()
    body = await export.to_csv(ctx.db, table)
    return web.Response(
        body=body.encode("utf-8-sig"),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{table}.csv"',
            "Cache-Control": "no-store",
        },
    )


# --- errors and security headers --------------------------------------------------------------------


@web.middleware
async def _report_errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    """An unhandled exception becomes a plain 500 and an admin alert (at most once per 5 minutes).

    Only the route pattern is logged: the webhook path itself contains a secret.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as error:
        resource = request.match_info.route.resource
        route = resource.canonical if resource is not None else "?"
        log.exception("request failed", extra={"method": request.method, "route": route})
        await _ctx(request).alerts.error(f"{type(error).__name__} на сайте ({request.method} {route})")
        raise web.HTTPInternalServerError() from None




async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    config = request.app[CTX_KEY].config
    response.headers["Content-Security-Policy"] = content_security_policy(metrica=bool(config.metrica_id))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if config.public_base_url.startswith("https://"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"


def content_security_policy(*, metrica: bool) -> str:
    """default-src 'self'; Yandex Metrica's origin is allowed only when METRICA_ID is set."""
    extra = f" {METRICA_ORIGIN}" if metrica else ""
    return "; ".join((
        "default-src 'self'",
        f"script-src 'self'{extra}",
        f"img-src 'self' data:{extra}",
        f"connect-src 'self'{extra}",
        "style-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))
