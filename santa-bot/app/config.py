"""Configuration from the environment (.env), validated at startup (§12).

``load_config`` collects every problem and raises ``ConfigError`` with all of
them in Russian, so the owner can fix the .env file in one go.

Feature flags:
- ``bot_enabled``: MAX_BOT_TOKEN is set. Without it the website still runs.
- ``payments_enabled``: ROBOKASSA_MERCHANT_LOGIN is set (and then the passwords
  of the current mode are required). Without it upgrades are unavailable.

Generated secrets: MAX_WEBHOOK_SECRET, WEBHOOK_PATH_SECRET and ADMIN_EXPORT_TOKEN
may be left empty. On first start the app generates the missing ones, stores them
in ``<DATA_DIR>/secrets.env`` (mode 0600, on the Docker volume) and prints them
once. Later starts read them from that file, so the webhook URL stays stable. A
value set in .env always wins over the file.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.models import Settings
from app.core.pricing import PriceList, validate_price_list

SECRETS_FILE = "secrets.env"
DEFAULT_API_BASE = "https://platform-api2.max.ru"
HASH_ALGORITHMS = ("md5", "sha256", "sha512")
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
_PATH_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MONTH_DAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет", ""}


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class GeneratedSecret:
    name: str
    alphabet: str
    length: int


GENERATED_SECRETS = (
    GeneratedSecret("MAX_WEBHOOK_SECRET", string.ascii_letters + string.digits + "-", 32),
    GeneratedSecret("WEBHOOK_PATH_SECRET", string.ascii_letters + string.digits, 24),
    GeneratedSecret("ADMIN_EXPORT_TOKEN", string.ascii_letters + string.digits, 32),
)


@dataclass(frozen=True, slots=True)
class Config:
    public_base_url: str
    data_dir: Path
    port: int
    max_bot_token: str
    max_bot_username: str
    max_api_base: str
    mode: str
    max_webhook_secret: str
    webhook_path_secret: str
    robokassa_merchant_login: str
    robokassa_password1: str
    robokassa_password2: str
    robokassa_test_password1: str
    robokassa_test_password2: str
    robokassa_test: bool
    robokassa_hash: str
    robokassa_send_receipt: bool
    admin_user_ids: tuple[int, ...]
    admin_export_token: str
    owner_full_name: str
    owner_inn: str
    support_email: str
    default_settings: Settings
    consent_version: str
    tz: ZoneInfo
    digest_from: str
    digest_to: str
    metrica_id: str
    s3_endpoint: str
    s3_bucket: str
    s3_key: str
    s3_secret: str
    warnings: tuple[str, ...] = field(default=())

    @property
    def bot_enabled(self) -> bool:
        return bool(self.max_bot_token)

    @property
    def payments_enabled(self) -> bool:
        return bool(self.robokassa_merchant_login)

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_endpoint)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "santa.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def webhook_path(self) -> str:
        return f"/max/webhook/{self.webhook_path_secret}"

    @property
    def webhook_url(self) -> str:
        return self.public_base_url + self.webhook_path

    @property
    def robokassa_passwords(self) -> tuple[str, str]:
        """(password1, password2) for the current mode: test passwords when ROBOKASSA_TEST=1."""
        if self.robokassa_test:
            return self.robokassa_test_password1, self.robokassa_test_password2
        return self.robokassa_password1, self.robokassa_password2

    @property
    def default_prices(self) -> PriceList:
        return PriceList.from_settings(self.default_settings)


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    announce: Callable[[str], None] | None = None,
) -> Config:
    """Read, validate and complete the configuration. Raises ``ConfigError``.

    ``announce`` receives the one-time message about generated secrets (stdout by default).
    """
    reader = _Reader(os.environ if env is None else env)
    data_dir = Path(reader.text("DATA_DIR", "/data"))
    generated = _load_or_generate_secrets(reader, data_dir, announce or _print)
    config = _build(reader, data_dir, generated)
    if reader.problems:
        raise ConfigError(reader.problems)
    return config


class _Reader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def text(self, name: str, default: str = "") -> str:
        return self._env.get(name, "").strip() or default

    def flag(self, name: str, default: bool) -> bool:
        raw = self._env.get(name)
        if raw is None or not raw.strip():
            return default
        value = raw.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        self.problems.append(f"{name} должен быть 0 или 1 (сейчас: {raw!r}).")
        return default

    def integer(self, name: str, default: int, *, minimum: int = 1) -> int:
        raw = self.text(name)
        if not raw:
            return default
        if not raw.isdigit() or int(raw) < minimum:
            self.problems.append(f"{name} должен быть целым числом не меньше {minimum} (сейчас: {raw!r}).")
            return default
        return int(raw)


def _print(message: str) -> None:
    print(message, file=sys.stdout, flush=True)


def _load_or_generate_secrets(reader: _Reader, data_dir: Path, announce: Callable[[str], None]) -> dict[str, str]:
    """Values for GENERATED_SECRETS missing from the env: from secrets.env, or newly generated."""
    missing = [s for s in GENERATED_SECRETS if not reader.text(s.name)]
    if not missing:
        return {}
    path = data_dir / SECRETS_FILE
    stored = _read_env_file(path)
    new = {
        s.name: "".join(secrets.choice(s.alphabet) for _ in range(s.length)) for s in missing if s.name not in stored
    }
    if new:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            _write_env_file(path, {**stored, **new})
        except OSError as error:
            reader.problems.append(
                f"Не удалось сохранить сгенерированные секреты в {path}: {error}. "
                "Проверьте, что папка данных (DATA_DIR) доступна для записи."
            )
            return {}
        lines = "\n".join(f"  {name}={value}" for name, value in new.items())
        announce(
            "Сгенерированы недостающие секреты (показываются один раз, сохранены в "
            f"{path}; при желании перенесите их в .env):\n{lines}"
        )
    return {s.name: stored.get(s.name) or new[s.name] for s in missing}


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.startswith("#"):
            values[name.strip()] = value.strip()
    return values


def _write_env_file(path: Path, values: Mapping[str, str]) -> None:
    content = "# Сгенерировано ботом при первом запуске. Не публикуйте этот файл.\n"
    content += "".join(f"{name}={value}\n" for name, value in values.items())
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _build(reader: _Reader, data_dir: Path, generated: Mapping[str, str]) -> Config:
    problems = reader.problems

    def secret(name: str) -> str:
        return reader.text(name) or generated.get(name, "")

    base_url = _public_base_url(reader)
    token = reader.text("MAX_BOT_TOKEN")
    username = reader.text("MAX_BOT_USERNAME").removeprefix("https://max.ru/").lstrip("@")
    if token and not username:
        problems.append("Укажите MAX_BOT_USERNAME — ник бота с его карточки в MAX, например se1234567_bot.")
    elif username and not _USERNAME_RE.match(username):
        problems.append(f"MAX_BOT_USERNAME выглядит неправильно: {username!r}. Пример: se1234567_bot.")
    if not token:
        reader.warnings.append("MAX_BOT_TOKEN не задан: бот выключен, работает только сайт.")

    api_base = reader.text("MAX_API_BASE", DEFAULT_API_BASE).rstrip("/")
    if urlsplit(api_base).hostname == "platform-api.max.ru":
        problems.append(f"MAX_API_BASE: старый адрес больше не работает, используйте {DEFAULT_API_BASE}.")
    elif not api_base.startswith("https://"):
        problems.append(f"MAX_API_BASE должен начинаться с https:// (сейчас: {api_base!r}).")

    mode = reader.text("MODE", "webhook").lower()
    if mode not in ("webhook", "polling"):
        problems.append(f"MODE должен быть webhook или polling (сейчас: {mode!r}).")
    elif mode == "webhook" and token and not base_url.startswith("https://"):
        problems.append("Для MODE=webhook адрес сайта должен быть https:// (MAX не принимает http).")

    webhook_secret = secret("MAX_WEBHOOK_SECRET")
    if not _WEBHOOK_SECRET_RE.match(webhook_secret):
        problems.append("MAX_WEBHOOK_SECRET: 5–256 символов из латинских букв, цифр, '-' и '_'.")
    path_secret = secret("WEBHOOK_PATH_SECRET")
    if not _PATH_SECRET_RE.match(path_secret):
        problems.append("WEBHOOK_PATH_SECRET: 16–64 символа из латинских букв, цифр, '-' и '_'.")

    robokassa_test = reader.flag("ROBOKASSA_TEST", True)
    login = reader.text("ROBOKASSA_MERCHANT_LOGIN")
    passwords = {name: reader.text(name) for name in (
        "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2", "ROBOKASSA_TEST_PASSWORD1", "ROBOKASSA_TEST_PASSWORD2")}
    if login:
        needed = ("ROBOKASSA_TEST_PASSWORD1", "ROBOKASSA_TEST_PASSWORD2") if robokassa_test else (
            "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2")
        for name in needed:
            if not passwords[name]:
                problems.append(f"Задан ROBOKASSA_MERCHANT_LOGIN, но не задан {name} (кабинет Robokassa → "
                                "Технические настройки).")
    else:
        reader.warnings.append("ROBOKASSA_MERCHANT_LOGIN не задан: оплата выключена.")
    hash_name = reader.text("ROBOKASSA_HASH", "md5").lower()
    if hash_name not in HASH_ALGORITHMS:
        problems.append(f"ROBOKASSA_HASH должен быть одним из: {', '.join(HASH_ALGORITHMS)} (сейчас: {hash_name!r}).")

    owner_full_name = reader.text("OWNER_FULL_NAME")
    owner_inn = reader.text("OWNER_INN")
    support_email = reader.text("SUPPORT_EMAIL")
    _check_owner(reader, owner_full_name, owner_inn, support_email, required=bool(token or login))

    admin_ids = _admin_ids(reader)
    if token and not admin_ids:
        reader.warnings.append("ADMIN_USER_IDS пуст: отправьте боту /whoami и впишите свой id.")

    defaults = Settings(
        free_limit=reader.integer("FREE_LIMIT", 10, minimum=3),
        price_S=reader.integer("PRICE_S", 490),
        price_M=reader.integer("PRICE_M", 990),
        price_L=reader.integer("PRICE_L", 2490),
        limit_S=reader.integer("LIMIT_S", 30),
        limit_M=reader.integer("LIMIT_M", 100),
        limit_L=reader.integer("LIMIT_L", 300),
        maintenance=False,
    )
    problems.extend(validate_price_list(PriceList.from_settings(defaults)))

    tz_name = reader.text("TZ", "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TZ: неизвестный часовой пояс {tz_name!r}. Обычно Europe/Moscow.")
        tz = ZoneInfo("UTC")

    digest_from, digest_to = reader.text("DIGEST_FROM", "11-01"), reader.text("DIGEST_TO", "01-10")
    for name, value in (("DIGEST_FROM", digest_from), ("DIGEST_TO", digest_to)):
        if not _MONTH_DAY_RE.match(value):
            problems.append(f"{name} должен быть в формате ММ-ДД, например 11-01 (сейчас: {value!r}).")

    metrica_id = reader.text("METRICA_ID")
    if metrica_id and not metrica_id.isdigit():
        problems.append(f"METRICA_ID должен состоять из цифр (сейчас: {metrica_id!r}).")
    s3 = {name: reader.text(name) for name in ("S3_ENDPOINT", "S3_BUCKET", "S3_KEY", "S3_SECRET")}
    if any(s3.values()) and not all(s3.values()):
        missing = ", ".join(name for name, value in s3.items() if not value)
        problems.append(f"Для резервных копий в S3 заполните все переменные S3_*: не хватает {missing}.")

    port = reader.integer("PORT", 8080)
    return Config(
        public_base_url=base_url,
        data_dir=data_dir,
        port=port,
        max_bot_token=token,
        max_bot_username=username,
        max_api_base=api_base,
        mode=mode,
        max_webhook_secret=webhook_secret,
        webhook_path_secret=path_secret,
        robokassa_merchant_login=login,
        robokassa_password1=passwords["ROBOKASSA_PASSWORD1"],
        robokassa_password2=passwords["ROBOKASSA_PASSWORD2"],
        robokassa_test_password1=passwords["ROBOKASSA_TEST_PASSWORD1"],
        robokassa_test_password2=passwords["ROBOKASSA_TEST_PASSWORD2"],
        robokassa_test=robokassa_test,
        robokassa_hash=hash_name,
        robokassa_send_receipt=reader.flag("ROBOKASSA_SEND_RECEIPT", False),
        admin_user_ids=admin_ids,
        admin_export_token=secret("ADMIN_EXPORT_TOKEN"),
        owner_full_name=owner_full_name,
        owner_inn=owner_inn,
        support_email=support_email,
        default_settings=defaults,
        consent_version=reader.text("CONSENT_VERSION", "2026-10"),
        tz=tz,
        digest_from=digest_from,
        digest_to=digest_to,
        metrica_id=metrica_id,
        s3_endpoint=s3["S3_ENDPOINT"],
        s3_bucket=s3["S3_BUCKET"],
        s3_key=s3["S3_KEY"],
        s3_secret=s3["S3_SECRET"],
        warnings=tuple(reader.warnings),
    )


def _public_base_url(reader: _Reader) -> str:
    domain = reader.text("DOMAIN")
    base_url = reader.text("PUBLIC_BASE_URL") or (f"https://{domain}" if domain else "")
    base_url = base_url.rstrip("/")
    if not base_url:
        reader.problems.append("Укажите DOMAIN (например santa-v-chate.ru) или PUBLIC_BASE_URL.")
        return ""
    parts = urlsplit(base_url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        reader.problems.append(f"PUBLIC_BASE_URL должен начинаться с https:// (сейчас: {base_url!r}).")
    return base_url


def _check_owner(reader: _Reader, full_name: str, inn: str, email: str, *, required: bool) -> None:
    missing = [name for name, value in (
        ("OWNER_FULL_NAME", full_name), ("OWNER_INN", inn), ("SUPPORT_EMAIL", email)) if not value]
    if missing:
        message = f"Не заполнены {', '.join(missing)} — они нужны для юридических страниц, Robokassa и MAX."
        (reader.problems if required else reader.warnings).append(message)
    if inn and not (inn.isdigit() and len(inn) in (10, 12)):
        reader.problems.append(f"OWNER_INN должен состоять из 12 цифр (сейчас: {inn!r}).")
    if email and not _EMAIL_RE.match(email):
        reader.problems.append(f"SUPPORT_EMAIL выглядит неправильно: {email!r}.")


def _admin_ids(reader: _Reader) -> tuple[int, ...]:
    raw = reader.text("ADMIN_USER_IDS")
    ids = []
    for part in filter(None, (piece.strip() for piece in raw.split(","))):
        if part.lstrip("-").isdigit():
            ids.append(int(part))
        else:
            reader.problems.append(f"ADMIN_USER_IDS: {part!r} — не число. Пример: 12345678,87654321.")
    return tuple(ids)
