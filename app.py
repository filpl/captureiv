import os
import re
import json
import secrets
import hashlib
import mimetypes
import math
import time
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError, URLError
import hmac
import smtplib
from email.message import EmailMessage

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_from_directory, abort, session, jsonify, make_response, g, has_request_context
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required,
    current_user
)
from sqlalchemy import inspect, text, or_, func, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import with_loader_criteria, Session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from storage import R2Storage, R2_FILENAME_PREFIX, cloud_storage_enabled, is_r2_filename, r2_object_key

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def load_secret_key():
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    if os.environ.get("APP_ENV", "development").strip().lower() == "production":
        raise RuntimeError("Production requires a strong SECRET_KEY supplied through the environment.")
    key_file = INSTANCE_DIR / ".session-key"
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        key_file.write_text(key, encoding="utf-8")
        key_file.chmod(0o600)
        return key


def normalize_database_url(value):
    """Normalize common managed-PostgreSQL URLs to the declared psycopg 3 driver."""
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def validate_production_configuration(app_env, database_url, secret_key, *, storage_backend=None,
                                     r2_configured=None, smtp_configured=None, stripe_key=None,
                                     stripe_webhook_configured=None, public_app_url=None,
                                     billing_terms_approved=None, data_retention_policy_approved=None):
    if (app_env or "development").strip().lower() != "production":
        return
    if (not secret_key or len(secret_key) < 32
            or secret_key.strip().lower() in {"replace-with-a-long-random-value", "changeme", "secret"}):
        raise RuntimeError("Production requires a strong SECRET_KEY of at least 32 characters.")
    public_app_url = os.environ.get("PUBLIC_APP_URL", "") if public_app_url is None else public_app_url
    public_url = urlsplit(public_app_url)
    if (public_url.scheme != "https" or not public_url.hostname or public_url.username
            or public_url.password or public_url.path not in {"", "/"}
            or public_url.query or public_url.fragment):
        raise RuntimeError("Production requires PUBLIC_APP_URL set to the platform's HTTPS origin.")
    if database_url.strip().lower().startswith("sqlite:"):
        raise RuntimeError("Production requires a managed database URL; SQLite is for local development only.")
    storage_backend = storage_backend or os.environ.get("STORAGE_BACKEND", "local")
    if storage_backend.strip().lower() != "r2":
        raise RuntimeError("Production requires private R2 storage; local disk is for development only.")
    if r2_configured is None:
        r2_configured = bool(
            os.environ.get("R2_BUCKET_NAME", "").strip()
            and os.environ.get("R2_ACCESS_KEY_ID", "").strip()
            and os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
            and (os.environ.get("R2_ENDPOINT_URL", "").strip()
                 or os.environ.get("R2_ACCOUNT_ID", "").strip())
        )
    if not r2_configured:
        raise RuntimeError("Production requires private R2 bucket credentials and endpoint configuration.")
    if smtp_configured is None:
        smtp_configured = bool(os.environ.get("SMTP_HOST", "").strip())
    if not smtp_configured:
        raise RuntimeError("Production requires SMTP configuration for verification and account recovery email.")
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "") if stripe_key is None else stripe_key
    if not stripe_key.startswith("sk_live_"):
        raise RuntimeError("Production requires live-mode Stripe credentials; test keys are not accepted.")
    if stripe_webhook_configured is None:
        stripe_webhook_configured = bool(os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip())
    if not stripe_webhook_configured:
        raise RuntimeError("Production requires the Stripe live webhook signing secret.")
    billing_terms_approved = (os.environ.get("BILLING_TERMS_APPROVED", "0") == "1"
                              if billing_terms_approved is None else billing_terms_approved)
    data_retention_policy_approved = (
        os.environ.get("DATA_RETENTION_POLICY_APPROVED", "0") == "1"
        if data_retention_policy_approved is None else data_retention_policy_approved)
    if not billing_terms_approved:
        raise RuntimeError("Production requires owner approval of customer billing and refund terms.")
    if not data_retention_policy_approved:
        raise RuntimeError("Production requires owner approval of the data-retention policy.")


app = Flask(__name__)
app_env = os.environ.get("APP_ENV", "development").strip().lower()
database_url = os.environ.get("DATABASE_URL", "").strip() or ("sqlite:///" + str(INSTANCE_DIR / "drone_portal.db"))
database_url = normalize_database_url(database_url)
secret_key = load_secret_key()
validate_production_configuration(app_env, database_url, secret_key)
secure_cookies = app_env == "production" or os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
try:
    trusted_proxy_count = int(os.environ.get("TRUSTED_PROXY_COUNT", "1"))
except ValueError as exc:
    if app_env == "production":
        raise RuntimeError("TRUSTED_PROXY_COUNT must be an integer from 1 to 5.") from exc
    trusted_proxy_count = 1
if app_env == "production" and not 1 <= trusted_proxy_count <= 5:
    raise RuntimeError("Production TRUSTED_PROXY_COUNT must be an integer from 1 to 5.")
app.config.update(
    APP_ENV=app_env,
    TRUSTED_PROXY_COUNT=trusted_proxy_count,
    PUBLIC_APP_URL=os.environ.get("PUBLIC_APP_URL", "").strip().rstrip("/"),
    SECRET_KEY=secret_key,
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", 8 * 1024 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=secure_cookies,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=secure_cookies,
    MARKETING_SITE_URL=os.environ.get("MARKETING_SITE_URL", "http://127.0.0.1:5173/"),
)
if app_env == "production":
    # Production must be reached through the configured reverse proxy. This
    # restores the client address for rate limiting and HTTPS for request URLs.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_count, x_proto=1)
ENTERPRISE_INQUIRY_EMAIL = os.environ.get("ENTERPRISE_INQUIRY_EMAIL", "").strip()


def stripe_api_key_is_valid(key=None, environment=None):
    """Allow Stripe test keys outside production and live keys only in production."""
    key = os.environ.get("STRIPE_SECRET_KEY", "") if key is None else key
    environment = app.config.get("APP_ENV", "development") if environment is None else environment
    expected_prefix = "sk_live_" if str(environment).strip().lower() == "production" else "sk_test_"
    return bool(key and key.startswith(expected_prefix))


def absolute_app_url(endpoint, **values):
    path = url_for(endpoint, **values)
    if app.config.get("APP_ENV") == "production":
        return app.config["PUBLIC_APP_URL"] + path
    return request.url_root.rstrip("/") + path

# Change these in production through environment variables.
app.config["MAX_TAGS"] = 30
app.config["MAX_TAG_LENGTH"] = 40
app.config["MAX_SEARCH_LENGTH"] = 120

DB = SQLAlchemy(app)
db = DB  # backwards-compatible alias
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."

ALLOWED_VIDEO = {"mp4", "mov", "m4v", "webm", "avi"}
ALLOWED_DOCS = {"pdf", "jpg", "jpeg", "png", "csv", "xlsx", "docx", "txt", "zip"}
DIRECTIONS = ["W-E", "S-N", "SW-N", "SW-E", "E-SW"]
MAP_DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def now_utc():
    return datetime.utcnow()


class SchemaMigration(db.Model):
    """Database revision ledger; revisions are appended and never rewritten."""
    __tablename__ = "schema_migration"
    revision = db.Column(db.String(100), primary_key=True)
    applied_at = db.Column(db.DateTime, default=now_utc, nullable=False)


CURRENT_SCHEMA_REVISION = "0001_saas_foundation"
ENTERPRISE_SCHEMA_REVISION = "0002_enterprise_provisioning"
VIEWER_ACCESS_SCHEMA_REVISION = "0003_viewer_access_lifecycle"
RATE_LIMIT_INDEX_SCHEMA_REVISION = "0004_rate_limit_lookup_index"
R2_UPLOAD_SCHEMA_REVISION = "0005_r2_upload_sessions"
SHARE_TOKEN_SCHEMA_REVISION = "0006_opaque_viewer_share_links"
PAST_DUE_SCHEMA_REVISION = "0007_billing_grace_period_start"
STORAGE_RESERVATION_SCHEMA_REVISION = "0008_local_upload_quota_reservations"
COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION = "0009_company_limit_overrides"
SCHEDULED_CANCELLATION_SCHEMA_REVISION = "0010_scheduled_subscription_cancellation"
PLAN_FEATURE_SCHEMA_REVISION = "0011_subscription_plan_features"
PINNED_PHOTO_SCHEMA_REVISION = "0012_pinned_photo_assets"
SAAS_BILLING_SCHEMA_REVISION = "0013_refund_review_requests"
BASIC_PLAN_SCHEMA_REVISION = "0014_basic_plan_entitlements"
BILLING_PERIOD_START_SCHEMA_REVISION = "0015_subscription_period_start"
BASIC_MONTHLY_PRICE_CENTS = 39900
BASIC_PROJECT_LIMIT = 1
BASIC_ACCOUNT_LIMIT = 2
BASIC_STORAGE_LIMIT_BYTES = 1_000_000_000_000  # 1 TB in decimal bytes.
OPEN_REFUND_STATUSES = ("Submitted", "Under Review", "Processing")
REFUND_STATUSES = ("Submitted", "Under Review", "Approved", "Partially Approved",
                   "Denied", "Processing", "Completed", "Failed")
PLAN_FEATURES = {
    "flight_scans": "Flight scans and video uploads",
    "site_maps": "Site maps and capture tools",
    "photos": "Mapped project photos",
    "documents": "Project documents",
    "employee_accounts": "Employee accounts",
    "viewer_links": "Public viewer links",
}
DEFAULT_PLAN_FEATURES = tuple(PLAN_FEATURES)


def migration_timestamp_type(dialect_name=None):
    """Return a timestamp type accepted by the current SQL dialect."""
    dialect_name = dialect_name or db.engine.dialect.name
    return "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"


def migration_boolean_column(default=False, dialect_name=None):
    """Build a portable additive boolean column definition."""
    dialect_name = dialect_name or db.engine.dialect.name
    default_sql = ("TRUE" if default else "FALSE") if dialect_name == "postgresql" else ("1" if default else "0")
    return f"BOOLEAN NOT NULL DEFAULT {default_sql}"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)
    # SaaS identity fields are nullable/defaulted so existing local accounts remain valid.
    email = db.Column(db.String(254), unique=True, nullable=True, index=True)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    platform_owner = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ProjectAccess(db.Model):
    """Private project permissions plus separate link and code credentials."""
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    code = db.Column(db.String(4), unique=True, nullable=False, index=True)
    share_token = db.Column(db.String(64), unique=True, nullable=True, index=True,
                            default=lambda: secrets.token_urlsafe(32))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    allow_overview = db.Column(db.Boolean, nullable=False, default=False)
    allow_media = db.Column(db.Boolean, nullable=False, default=False)
    allow_documents = db.Column(db.Boolean, nullable=False, default=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    __table_args__ = (db.UniqueConstraint("company_id", "project", name="uq_project_access_company_project"),)


class ProjectSiteMap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    filename = db.Column(db.String(500), unique=True, nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False, default=0)
    uploaded_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "project", name="uq_project_site_map_company_project"),)


class ProjectPhotoReference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    filename = db.Column(db.String(500), unique=True, nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False, default=0)
    uploaded_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "project", name="uq_project_photo_reference_company_project"),)


class ProjectPhotoAnnotation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    data = db.Column(db.Text, nullable=False, default='{"areas":[],"pins":[]}')
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "project", name="uq_project_photo_annotation_company_project"),)


class ProjectPinnedPhoto(db.Model):
    """Private still images attached to a mapped photo pin."""
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    pin_id = db.Column(db.String(64), nullable=False, index=True)
    filename = db.Column(db.String(500), unique=True, nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False, default=0)
    uploaded_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (
        db.Index("ix_project_pinned_photo_company_project_pin", "company_id", "project", "pin_id"),
    )


class ProjectSiteSetup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    module = db.Column(db.String(20), nullable=False, default="map")
    address = db.Column(db.String(300), default="")
    center_lat = db.Column(db.Float, nullable=True)
    center_lng = db.Column(db.Float, nullable=True)
    zoom = db.Column(db.Integer, default=16)
    locked = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "project", name="uq_project_site_setup_company_project"),)


class ProjectFlightPath(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    label = db.Column(db.String(64), default="")
    direction = db.Column(db.String(40), default="Custom path")
    points = db.Column(db.Text, nullable=False, default="[]")
    flight_id = db.Column(db.Integer, db.ForeignKey("flight.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    flight = db.relationship("Flight", lazy="joined")
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)


class ProjectMapPin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    label = db.Column(db.String(120), nullable=False)
    direction = db.Column(db.String(8), nullable=False)
    x_percent = db.Column(db.Float, nullable=False)
    y_percent = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)


class Flight(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    flight_id = db.Column(db.String(80), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    direction = db.Column(db.String(30), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    filename = db.Column(db.String(500), nullable=False, unique=True)
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, default=0)
    duration = db.Column(db.String(30), default="")
    uploaded_at = db.Column(db.DateTime, default=now_utc, nullable=False, index=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    # Construction-specific metadata.
    project = db.Column(db.String(160), default="Main Project", index=True)
    area = db.Column(db.String(160), default="")
    tags = db.Column(db.Text, default="[]")  # JSON list; keeps SQLite setup simple.
    notes = db.Column(db.Text, default="")
    drone = db.Column(db.String(120), default="")
    pilot = db.Column(db.String(120), default="")
    client = db.Column(db.String(160), default="")
    captured_at = db.Column(db.DateTime, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    checksum_sha256 = db.Column(db.String(64), default="", index=True)
    status = db.Column(db.String(30), default="ready", nullable=False)
    view_count = db.Column(db.Integer, default=0, nullable=False)
    download_count = db.Column(db.Integer, default=0, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "flight_id", name="uq_flight_company_flight_id"),)

    @property
    def tag_list(self):
        return parse_tags(self.tags)


class ProjectFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(500), nullable=False, unique=True)
    category = db.Column(db.String(50), default="Document")
    uploaded_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    project = db.Column(db.String(160), default="Main Project", index=True)
    tags = db.Column(db.Text, default="[]")
    notes = db.Column(db.Text, default="")
    original_filename = db.Column(db.String(500), default="")
    file_size = db.Column(db.BigInteger, default=0)
    checksum_sha256 = db.Column(db.String(64), default="", index=True)
    download_count = db.Column(db.Integer, default=0, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)

    @property
    def tag_list(self):
        return parse_tags(self.tags)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    resource_type = db.Column(db.String(50), default="")
    resource_id = db.Column(db.String(80), default="")
    detail = db.Column(db.Text, default="")
    ip_address = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False, index=True)


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending")
    storage_limit_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    storage_limit_override_bytes = db.Column(db.BigInteger, nullable=True)
    project_limit = db.Column(db.Integer, nullable=True)
    employee_limit = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class CompanyMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    role = db.Column(db.String(24), nullable=False, default="employee")
    status = db.Column(db.String(24), nullable=False, default="pending")
    company = db.relationship("Company", lazy="joined")
    user = db.relationship("User", lazy="joined")


class ProjectAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    project = db.Column(db.String(160), nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("company_id", "user_id", "project", name="uq_project_assignment"),)


class SubscriptionPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.String(500), default="")
    monthly_amount_cents = db.Column(db.Integer, nullable=True)
    annual_amount_cents = db.Column(db.Integer, nullable=True)
    project_limit = db.Column(db.Integer, nullable=True)
    employee_limit = db.Column(db.Integer, nullable=True)
    storage_limit_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    stripe_monthly_price_id = db.Column(db.String(160), default="")
    stripe_annual_price_id = db.Column(db.String(160), default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    custom_pricing = db.Column(db.Boolean, nullable=False, default=False)
    features_json = db.Column(db.Text, nullable=False,
                              default=lambda: json.dumps(list(DEFAULT_PLAN_FEATURES)))

    @property
    def feature_keys(self):
        try:
            keys = json.loads(self.features_json or "[]")
        except (TypeError, json.JSONDecodeError):
            keys = []
        return [key for key in keys if key in PLAN_FEATURES] if isinstance(keys, list) else []


class CompanySubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plan.id"), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="pending")
    billing_interval = db.Column(db.String(12), default="monthly")
    stripe_price_id_override = db.Column(db.String(160), default="")
    custom_amount_cents = db.Column(db.Integer, nullable=True)
    stripe_customer_id = db.Column(db.String(160), default="", index=True)
    stripe_subscription_id = db.Column(db.String(160), nullable=True, unique=True)
    current_period_start = db.Column(db.DateTime, nullable=True)
    current_period_end = db.Column(db.DateTime, nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    past_due_since = db.Column(db.DateTime, nullable=True)
    stripe_event_created = db.Column(db.BigInteger, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc, nullable=False)
    company = db.relationship("Company", lazy="joined")
    plan = db.relationship("SubscriptionPlan", lazy="joined")


class UploadTransfer(db.Model):
    """Tenant-scoped reservation and state for a resumable R2 multipart upload."""
    __tablename__ = "upload_transfer"
    id = db.Column(db.String(36), primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    fingerprint = db.Column(db.String(64), nullable=False, index=True)
    project = db.Column(db.String(160), nullable=False)
    date = db.Column(db.Date, nullable=False)
    direction = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    area = db.Column(db.String(160), nullable=False, default="")
    tags = db.Column(db.Text, nullable=False, default="[]")
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False)
    content_type = db.Column(db.String(160), nullable=False)
    object_key = db.Column(db.String(700), nullable=False, unique=True)
    multipart_upload_id = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="starting", index=True)
    flight_id = db.Column(db.Integer, db.ForeignKey("flight.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    __table_args__ = (
        db.Index("ix_upload_transfer_company_status_expiry", "company_id", "status", "expires_at"),
        db.Index("ix_upload_transfer_user_fingerprint", "user_id", "fingerprint"),
    )


class StorageReservation(db.Model):
    """Short-lived quota reservation while a regular upload is being stored."""
    id = db.Column(db.String(36), primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    file_size = db.Column(db.BigInteger, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc)
    __table_args__ = (db.Index("ix_storage_reservation_company_expiry", "company_id", "expires_at"),)


class EmailActionToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    purpose = db.Column(db.String(24), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)


class EnterpriseRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(180), nullable=False)
    contact_name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(254), nullable=False, index=True)
    phone = db.Column(db.String(40), default="")
    estimated_projects = db.Column(db.String(80), default="")
    estimated_users = db.Column(db.String(80), default="")
    estimated_storage = db.Column(db.String(80), default="")
    comments = db.Column(db.Text, default="")
    status = db.Column(db.String(24), nullable=False, default="new")
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class StripeEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(160), nullable=False, unique=True)
    event_type = db.Column(db.String(120), nullable=False)
    stripe_created = db.Column(db.BigInteger, nullable=False, default=0)
    received_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class AnnualRefundRequest(db.Model):
    """A customer request for owner-reviewed annual subscription refund."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("company_subscription.id"), nullable=False, index=True)
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="Submitted", index=True)
    contact_email = db.Column(db.String(254), nullable=False)
    stripe_subscription_reference = db.Column(db.String(160), default="")
    stripe_invoice_reference = db.Column(db.String(160), default="")
    stripe_payment_intent_id = db.Column(db.String(160), default="")
    original_amount_cents = db.Column(db.Integer, nullable=False, default=0)
    approved_amount_cents = db.Column(db.Integer, nullable=True)
    subscription_started_at = db.Column(db.DateTime, nullable=True)
    subscription_ends_at = db.Column(db.DateTime, nullable=True)
    requested_at = db.Column(db.DateTime, nullable=False, default=now_utc)
    reason = db.Column(db.String(80), nullable=False)
    comments = db.Column(db.Text, nullable=False, default="")
    decision_explanation = db.Column(db.Text, nullable=False, default="")
    agreed_termination_at = db.Column(db.DateTime, nullable=True)
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    stripe_refund_id = db.Column(db.String(160), default="", unique=True)
    idempotency_key = db.Column(db.String(160), nullable=False, unique=True)
    stripe_event_created = db.Column(db.BigInteger, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=now_utc, onupdate=now_utc)
    company = db.relationship("Company", lazy="joined")
    subscription = db.relationship("CompanySubscription", lazy="joined")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id], lazy="joined")
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id], lazy="joined")


class RateLimitEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route = db.Column(db.String(40), nullable=False, index=True)
    subject_hash = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False, index=True)
    __table_args__ = (db.Index("ix_rate_limit_route_subject_created", "route", "subject_hash", "created_at"),)


TENANT_MODELS = (ProjectAccess, ProjectSiteMap, ProjectPhotoReference, ProjectPhotoAnnotation,
                 ProjectPinnedPhoto, ProjectSiteSetup, ProjectFlightPath, ProjectMapPin, Flight, ProjectFile, UploadTransfer,
                 StorageReservation)


def current_membership():
    if not has_request_context() or not current_user.is_authenticated:
        return None
    return CompanyMembership.query.filter_by(user_id=current_user.id, status="active").first()


def current_company_id():
    membership = current_membership()
    return membership.company_id if membership else None


@event.listens_for(Session, "do_orm_execute")
def apply_company_scope(execute_state):
    """Apply a backend tenant filter to ORM reads and ORM bulk mutations."""
    if (not execute_state.is_orm_statement or not has_request_context()
            or getattr(g, "loading_login_user", False) or getattr(g, "resolving_company_scope", False)):
        return
    if not current_user.is_authenticated:
        return
    if getattr(g, "allow_public_viewer_scope", False):
        return
    g.resolving_company_scope = True
    try:
        company_id = current_company_id() or -1
        is_company_admin = bool(current_user.is_admin)
        assigned_projects = tuple(row[0] for row in db.session.query(ProjectAssignment.project).filter_by(
            company_id=company_id, user_id=current_user.id).all()) if not is_company_admin else ()
    finally:
        g.resolving_company_scope = False
    statement = execute_state.statement
    for model in TENANT_MODELS:
        if is_company_admin:
            criterion = lambda entity: entity.company_id == company_id
        else:
            criterion = lambda entity: (entity.company_id == company_id) & entity.project.in_(assigned_projects)
        statement = statement.options(with_loader_criteria(model, criterion, include_aliases=True))
    execute_state.statement = statement


@event.listens_for(Session, "before_flush")
def attach_company_to_new_records(_session, _flush_context, _instances):
    if not has_request_context() or not current_user.is_authenticated:
        return
    company_id = current_company_id()
    if not company_id:
        return
    for obj in _session.new:
        if isinstance(obj, TENANT_MODELS) and obj.company_id is None:
            obj.company_id = company_id


@login_manager.user_loader
def load_user(user_id):
    try:
        g.loading_login_user = True
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    finally:
        if has_request_context():
            g.loading_login_user = False


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        membership = current_membership()
        if (not current_user.is_admin or not current_user.active or not membership
                or not membership.company or membership.company.status != "active"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def company_feature_enabled(feature, company_id=None):
    if feature not in PLAN_FEATURES:
        return False
    membership = current_membership()
    target_company_id = company_id or (membership.company_id if membership else None)
    if not target_company_id:
        return False
    subscription = (CompanySubscription.query.filter_by(company_id=target_company_id)
                    .order_by(CompanySubscription.id.desc()).first())
    # Legacy accounts without an attached plan retain existing capabilities.
    return not subscription or not subscription.plan or feature in subscription.plan.feature_keys


def company_feature_required(feature):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            membership = current_membership()
            if not membership or not company_feature_enabled(feature, membership.company_id):
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def upload_user_required(fn):
    """Allow active company members to upload footage; project scope is checked per request."""
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        membership = current_membership()
        if (not current_user.active or not membership or not membership.company
                or membership.company.status != "active"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def user_can_upload_project(project, company_id=None):
    membership = current_membership()
    if not membership or not project:
        return False
    if company_id is not None and membership.company_id != company_id:
        return False
    if current_user.is_admin or membership.role == "company_admin":
        return True
    return ProjectAssignment.query.filter_by(company_id=membership.company_id,
        user_id=current_user.id, project=project).first() is not None


def platform_owner_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.platform_owner or not current_user.active:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_post():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.path == "/api/billing/stripe-webhook":
            return
        expected = session.get("_csrf")
        supplied = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, "Invalid CSRF token")


@app.before_request
def protect_private_upload_directory():
    # UPLOAD_DIR predates the protected-file route and resides under Flask's
    # static root. Keep existing files in place, but never serve them through
    # the framework's unauthenticated static-file handler.
    if request.endpoint == "static":
        filename = (request.view_args or {}).get("filename", "").replace("\\", "/")
        if filename.split("/", 1)[0].lower() == "uploads":
            abort(404)


@app.before_request
def rate_limit_sensitive_routes():
    if request.method != "POST":
        return
    route = {"/login": ("login", 15, timedelta(minutes=15)),
             "/register": ("registration", 8, timedelta(hours=1)),
             "/forgot-password": ("recovery", 8, timedelta(hours=1)),
             "/resend-verification": ("verification", 8, timedelta(hours=1)),
             "/": ("viewer-code", 20, timedelta(minutes=10))}.get(request.path)
    if route is None and request.path.startswith("/share/"):
        route = ("viewer-code", 20, timedelta(minutes=10))
    if not route:
        return
    prune_rate_limit_events_if_due()
    key, maximum, window = route
    subject = hashlib.sha256((app.config["SECRET_KEY"] + (request.remote_addr or "unknown")).encode()).hexdigest()
    cutoff = now_utc() - window
    count = RateLimitEvent.query.filter_by(route=key, subject_hash=subject).filter(RateLimitEvent.created_at >= cutoff).count()
    if count >= maximum:
        abort(429, "Too many attempts. Wait a while and try again.")
    db.session.add(RateLimitEvent(route=key, subject_hash=subject))
    db.session.commit()


_last_rate_limit_cleanup = 0.0


def prune_rate_limit_events_if_due():
    """Bound rate-limit table growth while retaining every active window."""
    global _last_rate_limit_cleanup
    now_monotonic = time.monotonic()
    if now_monotonic - _last_rate_limit_cleanup < 3600:
        return
    cutoff = now_utc() - timedelta(hours=1)
    try:
        RateLimitEvent.query.filter(RateLimitEvent.created_at < cutoff).delete(synchronize_session=False)
        db.session.commit()
        _last_rate_limit_cleanup = now_monotonic
    except Exception:
        db.session.rollback()
        app.logger.exception("Expired rate-limit event cleanup failed")


@app.before_request
def enforce_workspace_billing_window():
    if not has_request_context() or not current_user.is_authenticated or request.endpoint == "logout":
        return
    membership = current_membership()
    if not membership or not membership.company:
        pending_membership = CompanyMembership.query.filter_by(user_id=current_user.id, role="company_admin",
                                                               status="verified_pending_billing").first()
        pending_subscription = (CompanySubscription.query.filter_by(company_id=pending_membership.company_id)
                                .order_by(CompanySubscription.id.desc()).first() if pending_membership else None)
        if (request.endpoint in {"subscription_checkout", "checkout_pending"}
                and current_user.email_verified and pending_subscription
                and pending_subscription.status in {"pending", "checkout_started"}):
            return
        logout_user()
        return redirect(url_for("login"))
    company = membership.company
    subscription = (CompanySubscription.query.filter_by(company_id=company.id)
                    .order_by(CompanySubscription.id.desc()).first())
    expired = company.status != "active"
    billing_expired = False
    if subscription and subscription.status == "canceled":
        billing_expired = not subscription.current_period_end or subscription.current_period_end <= now_utc()
        expired = expired or billing_expired
    if subscription and subscription.cancel_at_period_end:
        billing_expired = (not subscription.current_period_end or
                           subscription.current_period_end <= now_utc())
        expired = expired or billing_expired
    if subscription and subscription.status == "past_due":
        try:
            grace_days = max(0, min(30, int(os.environ.get("BILLING_GRACE_DAYS", "7"))))
        except ValueError:
            grace_days = 7
        grace_started = subscription.past_due_since or subscription.updated_at
        billing_expired = grace_started + timedelta(days=grace_days) <= now_utc()
        expired = expired or billing_expired
    if expired:
        # Keep owner suspension separate so a later successful invoice cannot
        # silently undo an administrative access decision.
        if company.status != "suspended":
            company.status = "billing_suspended" if billing_expired else "suspended"
        if not billing_expired:
            for member in CompanyMembership.query.filter_by(company_id=company.id).all():
                member.status = "suspended"
                member.user.active = False
        db.session.commit()
        logout_user()
        flash("Workspace access is paused. Project data and uploads have been preserved.", "error")
        return redirect(url_for("login"))
    if membership.role == "employee" and not company_feature_enabled("employee_accounts", company.id):
        logout_user()
        flash("Employee access is not included in this workspace plan.", "error")
        return redirect(url_for("login"))


def ext_allowed(filename, allowed):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in allowed)


def extension(filename):
    return Path(filename).suffix.lower().lstrip(".")


def human_size(n):
    n = n or 0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return f"{x:.1f} {units[i]}"


app.jinja_env.filters["filesize"] = human_size
app.jinja_env.filters["tags"] = lambda value: ", ".join(parse_tags(value))


def parse_tags(value):
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        try:
            raw = json.loads(value)
            if not isinstance(raw, list):
                raw = str(value).split(",")
        except (json.JSONDecodeError, TypeError):
            raw = str(value).split(",")
    cleaned = []
    seen = set()
    for tag in raw:
        tag = re.sub(r"\s+", " ", str(tag).strip())
        if not tag:
            continue
        tag = tag[: app.config["MAX_TAG_LENGTH"]]
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(tag)
        if len(cleaned) >= app.config["MAX_TAGS"]:
            break
    return cleaned


def tags_from_request():
    values = request.form.getlist("tags")
    if len(values) == 1 and "," in values[0]:
        values = values[0].split(",")
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        values = payload.get("tags", values)
    return parse_tags(values)


def safe_name(filename):
    name = secure_filename(filename or "")
    if not name:
        name = "upload"
    return name[:240]


def unique_filename(original):
    return f"{secrets.token_hex(12)}_{safe_name(original)}"


def sha256_file(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


STORAGE_FILE_MODELS = (Flight, ProjectFile, ProjectSiteMap, ProjectPhotoReference, ProjectPinnedPhoto)


def company_storage_bytes(company_id):
    return sum(
        db.session.query(func.coalesce(func.sum(model.file_size), 0))
        .filter_by(company_id=company_id).scalar() or 0
        for model in STORAGE_FILE_MODELS
    )


def company_storage_limit_bytes(company, subscription=None):
    """Return the effective quota used for upload enforcement and reporting."""
    if company is not None and company.storage_limit_override_bytes is not None:
        return company.storage_limit_override_bytes
    if subscription is None and company is not None:
        subscription = (CompanySubscription.query.filter_by(company_id=company.id)
                        .order_by(CompanySubscription.id.desc()).first())
    if subscription and subscription.plan:
        return (company.storage_limit_bytes if subscription.plan.custom_pricing
                else subscription.plan.storage_limit_bytes)
    return company.storage_limit_bytes if company else 0


def lock_company_quota(company_id):
    """Serialize limit checks and reservations for one tenant."""
    result = db.session.execute(text("UPDATE company SET status = status WHERE id = :company_id"),
                                {"company_id": company_id})
    if not result.rowcount:
        return None
    company = Company.query.filter_by(id=company_id).first()
    if company:
        db.session.refresh(company)
    return company


def reserve_company_storage(company_id, file_size, *, replacing_size=0):
    """Atomically reserve quota before a regular local or R2-backed upload starts."""
    if file_size < 0:
        raise ValueError("The uploaded file size is invalid.")
    company = lock_company_quota(company_id)
    if not company or company.status != "active":
        db.session.rollback()
        raise ValueError("This workspace is not active for uploads.")
    now = now_utc()
    StorageReservation.query.filter(
        StorageReservation.company_id == company.id,
        StorageReservation.expires_at <= now,
    ).delete(synchronize_session=False)
    limit = company_storage_limit_bytes(company)
    used = company_storage_bytes(company.id)
    local_reserved = (db.session.query(func.coalesce(func.sum(StorageReservation.file_size), 0))
                      .filter(StorageReservation.company_id == company.id,
                              StorageReservation.expires_at > now).scalar() or 0)
    cloud_reserved = (db.session.query(func.coalesce(func.sum(UploadTransfer.file_size), 0))
                      .filter(UploadTransfer.company_id == company.id,
                              UploadTransfer.status.in_(("starting", "uploading")),
                              UploadTransfer.expires_at > now).scalar() or 0)
    reserved_size = max(0, file_size - max(0, replacing_size))
    if not limit or used + local_reserved + cloud_reserved + reserved_size > limit:
        db.session.rollback()
        raise ValueError("The upload would exceed the company storage allowance.")
    reservation = StorageReservation(
        id=str(uuid4()), company_id=company.id, user_id=current_user.id,
        file_size=reserved_size, expires_at=now + timedelta(hours=24),
    )
    db.session.add(reservation)
    db.session.commit()
    return reservation.id


def complete_storage_reservation(reservation_id):
    """Remove a reservation in the same transaction that writes file metadata."""
    if not reservation_id:
        return
    reservation = StorageReservation.query.filter_by(id=reservation_id).first()
    if reservation:
        company = lock_company_quota(reservation.company_id)
        if not company:
            abort(404)
        db.session.delete(reservation)


def release_storage_reservation(reservation_id):
    """Release quota after a duplicate or failed upload without affecting other tenants."""
    if not reservation_id:
        return
    reservation = StorageReservation.query.filter_by(id=reservation_id).first()
    if reservation:
        company = lock_company_quota(reservation.company_id)
        if company:
            db.session.delete(reservation)
            db.session.commit()


def aggregate_company_storage(company_ids):
    """Aggregate tenant and project usage in a fixed number of grouped queries."""
    company_ids = set(company_ids)
    totals = {company_id: 0 for company_id in company_ids}
    by_project = {company_id: {} for company_id in company_ids}
    if not company_ids:
        return totals, by_project
    for model in STORAGE_FILE_MODELS:
        company_rows = db.session.query(
            model.company_id, func.coalesce(func.sum(model.file_size), 0)
        ).filter(model.company_id.in_(company_ids)).group_by(model.company_id).all()
        for company_id, amount in company_rows:
            totals[company_id] += int(amount or 0)
        project_rows = db.session.query(
            model.company_id, model.project, func.coalesce(func.sum(model.file_size), 0)
        ).filter(model.company_id.in_(company_ids)).group_by(model.company_id, model.project).all()
        for company_id, project, amount in project_rows:
            project_name = project or "Unassigned"
            by_project[company_id][project_name] = (
                by_project[company_id].get(project_name, 0) + int(amount or 0))
    return totals, by_project


def save_upload(file_storage, *, replacing_size=0):
    """Save an uploaded file safely and return (filename, size, sha256)."""
    original = safe_name(file_storage.filename)
    filename = unique_filename(original)
    destination = UPLOAD_DIR / filename
    company_id = None
    reservation_id = None
    if current_user.is_authenticated:
        company_id = current_company_id()
        if not company_id:
            raise ValueError("This workspace is not active for uploads.")
        try:
            stream = file_storage.stream
            position = stream.tell()
            stream.seek(0, os.SEEK_END)
            expected_size = stream.tell()
            stream.seek(position)
        except (AttributeError, OSError, ValueError) as exc:
            raise ValueError("The upload size could not be verified.") from exc
        reservation_id = reserve_company_storage(company_id, expected_size, replacing_size=replacing_size)
    try:
        file_storage.save(destination)
        size = destination.stat().st_size
        if reservation_id and size != expected_size:
            raise ValueError("The uploaded file size changed while it was being saved.")
    except Exception:
        destination.unlink(missing_ok=True)
        if reservation_id:
            release_storage_reservation(reservation_id)
        raise
    try:
        checksum = sha256_file(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        release_storage_reservation(reservation_id)
        raise
    if cloud_storage_enabled():
        if company_id is None:
            destination.unlink(missing_ok=True)
            release_storage_reservation(reservation_id)
            raise ValueError("Sign in to upload files to private cloud storage.")
        object_key = f"companies/{company_id}/{filename}"
        try:
            content_type = mimetypes.guess_type(original)[0] or "application/octet-stream"
            R2Storage().upload_file(destination, object_key, content_type)
        except Exception:
            destination.unlink(missing_ok=True)
            release_storage_reservation(reservation_id)
            raise
        destination.unlink(missing_ok=True)
        filename = R2_FILENAME_PREFIX + object_key
    return filename, size, checksum, original, reservation_id


PROJECT_MODELS = (Flight, ProjectFile, ProjectAccess, ProjectSiteMap, ProjectPhotoReference,
                  ProjectPhotoAnnotation, ProjectPinnedPhoto, ProjectSiteSetup, ProjectFlightPath, ProjectMapPin)


def company_project_names(company_id):
    """Return every project known to a company, including setup-only/photo-only projects."""
    names = set()
    if not company_id:
        return names
    for model in PROJECT_MODELS:
        names.update(value for (value,) in model.query.with_entities(model.project)
                     .filter_by(company_id=company_id).all() if value)
    return names


def project_capacity_available(project):
    company_id = current_company_id()
    if not company_id or not project:
        return False
    company = lock_company_quota(company_id)
    if not company or company.status != "active":
        db.session.rollback()
        return False
    projects = company_project_names(company_id)
    if project in projects:
        return True
    subscription = CompanySubscription.query.filter_by(company_id=company_id).order_by(CompanySubscription.id.desc()).first()
    company = Company.query.filter_by(id=company_id).first()
    limit = company.project_limit if company and company.project_limit is not None else (
        subscription.plan.project_limit if subscription and subscription.plan else None)
    if limit is None:
        return True
    return len(projects) < limit


def delete_stored_file(filename):
    if not filename:
        return
    if is_r2_filename(filename):
        try:
            R2Storage().delete(r2_object_key(filename))
        except Exception:
            app.logger.exception("Could not delete R2 object %s", filename)
        return
    path = UPLOAD_DIR / Path(filename).name
    try:
        path.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        return
    if path.exists() and path.is_file():
        path.unlink()


def parse_date(value, fallback=None):
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback


def viewer_access_active(access):
    return bool(access and not access.revoked_at
                and (not access.expires_at or access.expires_at > now_utc()))


def parse_datetime(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def audit(action, resource_type="", resource_id="", detail=""):
    try:
        entry = AuditLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            detail=detail[:4000],
            ip_address=(request.headers.get("X-Forwarded-For", request.remote_addr) or "")[:80],
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def migrate_sqlite():
    """Apply the versioned SaaS foundation migration without deleting existing data."""
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        db.create_all()
        db.session.add_all([SchemaMigration(revision=CURRENT_SCHEMA_REVISION),
                            SchemaMigration(revision=ENTERPRISE_SCHEMA_REVISION),
                            SchemaMigration(revision=VIEWER_ACCESS_SCHEMA_REVISION),
                            SchemaMigration(revision=RATE_LIMIT_INDEX_SCHEMA_REVISION),
                            SchemaMigration(revision=R2_UPLOAD_SCHEMA_REVISION),
                            SchemaMigration(revision=SHARE_TOKEN_SCHEMA_REVISION),
                            SchemaMigration(revision=PAST_DUE_SCHEMA_REVISION),
                            SchemaMigration(revision=STORAGE_RESERVATION_SCHEMA_REVISION),
                            SchemaMigration(revision=COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION),
                            SchemaMigration(revision=SCHEDULED_CANCELLATION_SCHEMA_REVISION),
                            SchemaMigration(revision=PLAN_FEATURE_SCHEMA_REVISION),
                            SchemaMigration(revision=PINNED_PHOTO_SCHEMA_REVISION),
                            SchemaMigration(revision=SAAS_BILLING_SCHEMA_REVISION),
                            SchemaMigration(revision=BILLING_PERIOD_START_SCHEMA_REVISION)])
        db.session.commit()
        return
    if "schema_migration" in existing_tables and db.session.get(SchemaMigration, CURRENT_SCHEMA_REVISION):
        apply_enterprise_schema_migration()
        apply_viewer_access_schema_migration()
        apply_rate_limit_index_migration()
        apply_r2_upload_schema_migration()
        apply_share_token_schema_migration()
        apply_past_due_schema_migration()
        apply_storage_reservation_schema_migration()
        apply_company_limit_override_migration()
        apply_scheduled_cancellation_migration()
        apply_plan_feature_migration()
        apply_pinned_photo_migration()
        apply_saas_billing_migration()
        apply_billing_period_start_migration()
        return

    timestamp_type = migration_timestamp_type()
    bool_true = migration_boolean_column(default=True)
    bool_false = migration_boolean_column(default=False)
    expected = {
        "user": {
            "last_login_at": timestamp_type,
            "email": "VARCHAR(254)", "email_verified": bool_true,
            "platform_owner": bool_false,
        },
        "flight": {
            "company_id": "INTEGER", "project": "VARCHAR(160)", "area": "VARCHAR(160)", "tags": "TEXT",
            "notes": "TEXT", "drone": "VARCHAR(120)", "pilot": "VARCHAR(120)",
            "client": "VARCHAR(160)", "captured_at": timestamp_type, "latitude": "FLOAT",
            "longitude": "FLOAT", "checksum_sha256": "VARCHAR(64)",
            "status": "VARCHAR(30)", "view_count": "INTEGER", "download_count": "INTEGER",
        },
        "project_file": {
            "company_id": "INTEGER", "project": "VARCHAR(160)", "tags": "TEXT", "notes": "TEXT",
            "original_filename": "VARCHAR(500)", "file_size": "BIGINT",
            "checksum_sha256": "VARCHAR(64)", "download_count": "INTEGER",
        },
        "project_access": {"company_id": "INTEGER", "allow_overview": bool_false,
                           "allow_media": bool_false, "allow_documents": bool_false},
        "project_site_map": {"company_id": "INTEGER", "file_size": "BIGINT NOT NULL DEFAULT 0"},
        "project_photo_reference": {"company_id": "INTEGER", "file_size": "BIGINT NOT NULL DEFAULT 0"},
        "project_photo_annotation": {"company_id": "INTEGER"},
        "project_site_setup": {"company_id": "INTEGER"},
        "project_flight_path": {"company_id": "INTEGER"},
        "project_map_pin": {"company_id": "INTEGER"},
        "company_subscription": {"stripe_event_created": "BIGINT NOT NULL DEFAULT 0"},
    }
    for table, columns in expected.items():
        if table not in existing_tables:
            continue
        current = {c["name"] for c in inspect(db.engine).get_columns(table)}
        for column, sql_type in columns.items():
            if column not in current:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'))
    db.session.commit()
    db.create_all()
    migrate_tenant_unique_indexes()

    # Normalize NULLs introduced by migrations.
    db.session.execute(text("UPDATE flight SET tags='[]' WHERE tags IS NULL"))
    db.session.execute(text("UPDATE flight SET project='Main Project' WHERE project IS NULL"))
    db.session.execute(text("UPDATE flight SET status='ready' WHERE status IS NULL"))
    db.session.execute(text("UPDATE flight SET view_count=0 WHERE view_count IS NULL"))
    db.session.execute(text("UPDATE flight SET download_count=0 WHERE download_count IS NULL"))
    db.session.execute(text("UPDATE project_file SET tags='[]' WHERE tags IS NULL"))
    db.session.execute(text("UPDATE project_file SET project='Main Project' WHERE project IS NULL"))
    db.session.execute(text("UPDATE project_file SET download_count=0 WHERE download_count IS NULL"))
    db.session.commit()

    # Assign pre-SaaS records to a finite legacy workspace. New registrations are
    # already associated with their own pending companies and are left untouched.
    has_legacy_data = any(model.query.count() for model in (User, Flight, ProjectFile, ProjectAccess))
    if has_legacy_data:
        legacy = Company.query.filter_by(name="Legacy workspace").first()
        if not legacy:
            disk_usage = sum(path.stat().st_size for path in UPLOAD_DIR.rglob("*") if path.is_file())
            row_usage = sum(db.session.query(func.coalesce(func.sum(model.file_size), 0)).scalar() or 0
                            for model in STORAGE_FILE_MODELS)
            legacy = Company(name="Legacy workspace", status="active",
                             storage_limit_bytes=max(disk_usage, row_usage) + 1024**3)
            db.session.add(legacy)
            db.session.flush()
        for model in (Flight, ProjectFile, ProjectAccess, ProjectSiteMap, ProjectPhotoReference,
                      ProjectPhotoAnnotation, ProjectPinnedPhoto, ProjectSiteSetup, ProjectFlightPath, ProjectMapPin):
            model.query.filter(model.company_id.is_(None)).update({"company_id": legacy.id}, synchronize_session=False)
        for user in User.query.all():
            if not CompanyMembership.query.filter_by(user_id=user.id).first():
                db.session.add(CompanyMembership(company_id=legacy.id, user_id=user.id,
                                                 role="company_admin" if user.is_admin else "employee",
                                                 status="active" if user.active else "suspended"))
        legacy_projects = company_project_names(legacy.id)
        for membership in CompanyMembership.query.filter_by(company_id=legacy.id, role="employee").all():
            if not ProjectAssignment.query.filter_by(company_id=legacy.id, user_id=membership.user_id).first():
                db.session.add_all(ProjectAssignment(company_id=legacy.id, user_id=membership.user_id, project=name)
                                   for name in sorted(legacy_projects))
        db.session.commit()
    db.session.add(SchemaMigration(revision=CURRENT_SCHEMA_REVISION))
    db.session.commit()
    apply_enterprise_schema_migration()
    apply_viewer_access_schema_migration()
    apply_rate_limit_index_migration()
    apply_r2_upload_schema_migration()
    apply_share_token_schema_migration()
    apply_past_due_schema_migration()
    apply_storage_reservation_schema_migration()
    apply_company_limit_override_migration()
    apply_scheduled_cancellation_migration()
    apply_plan_feature_migration()
    apply_pinned_photo_migration()
    apply_saas_billing_migration()
    apply_billing_period_start_migration()


def apply_enterprise_schema_migration():
    """Add Enterprise provisioning fields as the second tracked schema revision."""
    if db.session.get(SchemaMigration, ENTERPRISE_SCHEMA_REVISION):
        return
    tables = set(inspect(db.engine).get_table_names())
    expected = {
        "company": {"project_limit": "INTEGER", "employee_limit": "INTEGER"},
        "company_subscription": {"stripe_price_id_override": "VARCHAR(160)", "custom_amount_cents": "INTEGER"},
        "enterprise_request": {"company_id": "INTEGER", "admin_user_id": "INTEGER"},
    }
    for table, columns in expected.items():
        if table not in tables:
            continue
        present = {column["name"] for column in inspect(db.engine).get_columns(table)}
        for name, sql_type in columns.items():
            if name not in present:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}'))
    db.session.commit()
    db.create_all()
    with db.engine.begin() as connection:
        connection.execute(text('CREATE INDEX IF NOT EXISTS "ix_enterprise_request_company_id" ON "enterprise_request" (company_id)'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS "ix_enterprise_request_admin_user_id" ON "enterprise_request" (admin_user_id)'))
    db.session.add(SchemaMigration(revision=ENTERPRISE_SCHEMA_REVISION))
    db.session.commit()


def apply_viewer_access_schema_migration():
    """Add expiry and revocation timestamps without invalidating existing viewer links."""
    if db.session.get(SchemaMigration, VIEWER_ACCESS_SCHEMA_REVISION):
        return
    tables = set(inspect(db.engine).get_table_names())
    if "project_access" in tables:
        columns = {column["name"] for column in inspect(db.engine).get_columns("project_access")}
        for name in ("expires_at", "revoked_at"):
            if name not in columns:
                db.session.execute(text(f'ALTER TABLE "project_access" ADD COLUMN "{name}" TIMESTAMP'))
        db.session.commit()
    db.create_all()
    db.session.add(SchemaMigration(revision=VIEWER_ACCESS_SCHEMA_REVISION))
    db.session.commit()


def apply_rate_limit_index_migration():
    """Index the full rate-limit lookup so it stays efficient as events accumulate."""
    if db.session.get(SchemaMigration, RATE_LIMIT_INDEX_SCHEMA_REVISION):
        return
    db.create_all()
    with db.engine.begin() as connection:
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS "ix_rate_limit_route_subject_created" '
            'ON "rate_limit_event" (route, subject_hash, created_at)'
        ))
    db.session.add(SchemaMigration(revision=RATE_LIMIT_INDEX_SCHEMA_REVISION))
    db.session.commit()


def apply_r2_upload_schema_migration():
    """Create the additive table used to reserve quota for resumable R2 uploads."""
    if db.session.get(SchemaMigration, R2_UPLOAD_SCHEMA_REVISION):
        return
    db.create_all()
    with db.engine.begin() as connection:
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS "ix_upload_transfer_company_status_expiry" '
            'ON "upload_transfer" (company_id, status, expires_at)'
        ))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS "ix_upload_transfer_user_fingerprint" '
            'ON "upload_transfer" (user_id, fingerprint)'
        ))
    db.session.add(SchemaMigration(revision=R2_UPLOAD_SCHEMA_REVISION))
    db.session.commit()


def apply_share_token_schema_migration():
    """Add independent opaque viewer URLs while preserving every legacy code."""
    if db.session.get(SchemaMigration, SHARE_TOKEN_SCHEMA_REVISION):
        return
    tables = set(inspect(db.engine).get_table_names())
    if "project_access" in tables:
        columns = {column["name"] for column in inspect(db.engine).get_columns("project_access")}
        if "share_token" not in columns:
            db.session.execute(text('ALTER TABLE "project_access" ADD COLUMN "share_token" VARCHAR(64)'))
            db.session.commit()
        for access in ProjectAccess.query.filter(ProjectAccess.share_token.is_(None)).all():
            access.share_token = secrets.token_urlsafe(32)
        db.session.commit()
    db.create_all()
    if "project_access" in set(inspect(db.engine).get_table_names()):
        with db.engine.begin() as connection:
            connection.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS "ix_project_access_share_token" '
                'ON "project_access" (share_token)'
            ))
    db.session.add(SchemaMigration(revision=SHARE_TOKEN_SCHEMA_REVISION))
    db.session.commit()


def apply_past_due_schema_migration():
    """Record the first failed-payment time so unrelated updates cannot reset grace."""
    if db.session.get(SchemaMigration, PAST_DUE_SCHEMA_REVISION):
        return
    tables = set(inspect(db.engine).get_table_names())
    if "company_subscription" in tables:
        columns = {column["name"] for column in inspect(db.engine).get_columns("company_subscription")}
        if "past_due_since" not in columns:
            db.session.execute(text('ALTER TABLE "company_subscription" ADD COLUMN "past_due_since" TIMESTAMP'))
        db.session.execute(text(
            "UPDATE company_subscription SET past_due_since = updated_at "
            "WHERE status = 'past_due' AND past_due_since IS NULL"
        ))
        db.session.commit()
    db.create_all()
    db.session.add(SchemaMigration(revision=PAST_DUE_SCHEMA_REVISION))
    db.session.commit()


def apply_storage_reservation_schema_migration():
    """Create the additive ledger for uploads that are not R2 multipart sessions."""
    if db.session.get(SchemaMigration, STORAGE_RESERVATION_SCHEMA_REVISION):
        return
    db.create_all()
    with db.engine.begin() as connection:
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS "ix_storage_reservation_company_expiry" '
            'ON "storage_reservation" (company_id, expires_at)'
        ))
    db.session.add(SchemaMigration(revision=STORAGE_RESERVATION_SCHEMA_REVISION))
    db.session.commit()


def apply_company_limit_override_migration():
    """Add an optional per-company storage allowance without changing existing limits."""
    if db.session.get(SchemaMigration, COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION):
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("company")}
    if "storage_limit_override_bytes" not in columns:
        db.session.execute(text('ALTER TABLE "company" ADD COLUMN "storage_limit_override_bytes" BIGINT'))
    db.session.add(SchemaMigration(revision=COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION))
    db.session.commit()


def apply_scheduled_cancellation_migration():
    """Persist Stripe's scheduled cancellation state for clear billing status."""
    if db.session.get(SchemaMigration, SCHEDULED_CANCELLATION_SCHEMA_REVISION):
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("company_subscription")}
    if "cancel_at_period_end" not in columns:
        db.session.execute(text(
            'ALTER TABLE "company_subscription" ADD COLUMN "cancel_at_period_end" '
            + migration_boolean_column(default=False)
        ))
    db.session.add(SchemaMigration(revision=SCHEDULED_CANCELLATION_SCHEMA_REVISION))
    db.session.commit()


def apply_plan_feature_migration():
    """Add explicit plan entitlements; existing plans retain every capability."""
    if db.session.get(SchemaMigration, PLAN_FEATURE_SCHEMA_REVISION):
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("subscription_plan")}
    if "features_json" not in columns:
        default_features = json.dumps(list(DEFAULT_PLAN_FEATURES), separators=(",", ":"))
        db.session.execute(text(
            'ALTER TABLE "subscription_plan" ADD COLUMN "features_json" TEXT NOT NULL '
            f"DEFAULT '{default_features}'"
        ))
    db.session.add(SchemaMigration(revision=PLAN_FEATURE_SCHEMA_REVISION))
    db.session.commit()


def apply_pinned_photo_migration():
    """Add private multi-image galleries for mapped photo pins."""
    if db.session.get(SchemaMigration, PINNED_PHOTO_SCHEMA_REVISION):
        return
    db.create_all()
    db.session.add(SchemaMigration(revision=PINNED_PHOTO_SCHEMA_REVISION))
    db.session.commit()


def apply_saas_billing_migration():
    """Create the additive annual refund-review table; retain billing history."""
    if db.session.get(SchemaMigration, SAAS_BILLING_SCHEMA_REVISION):
        return
    db.create_all()
    db.session.add(SchemaMigration(revision=SAAS_BILLING_SCHEMA_REVISION))
    db.session.commit()


def apply_billing_period_start_migration():
    """Store Stripe's actual current period start for annual refund reviews."""
    if db.session.get(SchemaMigration, BILLING_PERIOD_START_SCHEMA_REVISION):
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("company_subscription")}
    if "current_period_start" not in columns:
        db.session.execute(text(
            'ALTER TABLE "company_subscription" ADD COLUMN "current_period_start" '
            + migration_timestamp_type()
        ))
    db.session.add(SchemaMigration(revision=BILLING_PERIOD_START_SCHEMA_REVISION))
    db.session.commit()


def apply_basic_plan_defaults():
    """Apply the approved Basic entitlements once, without touching Enterprise."""
    if db.session.get(SchemaMigration, BASIC_PLAN_SCHEMA_REVISION):
        return
    plan = SubscriptionPlan.query.filter_by(name="Basic", custom_pricing=False).first()
    if plan:
        plan.description = ("One construction project, two company accounts, and 1 TB total storage. "
                            "Authorized public viewers are unlimited.")
        plan.monthly_amount_cents = BASIC_MONTHLY_PRICE_CENTS
        plan.project_limit = BASIC_PROJECT_LIMIT
        plan.employee_limit = BASIC_ACCOUNT_LIMIT
        plan.storage_limit_bytes = BASIC_STORAGE_LIMIT_BYTES
        # Stripe Price IDs are intentionally preserved and remain unset until configured.
    db.session.add(SchemaMigration(revision=BASIC_PLAN_SCHEMA_REVISION))
    db.session.commit()


def migrate_tenant_unique_indexes():
    """Replace global project/flight uniqueness with company-scoped constraints."""
    changes = {
        "flight": ("ix_flight_flight_id", "uq_flight_company_flight_id", "company_id, flight_id"),
        "project_access": ("ix_project_access_project", "uq_project_access_company_project", "company_id, project"),
        "project_site_map": ("ix_project_site_map_project", "uq_project_site_map_company_project", "company_id, project"),
        "project_photo_reference": ("ix_project_photo_reference_project", "uq_project_photo_reference_company_project", "company_id, project"),
        "project_photo_annotation": ("ix_project_photo_annotation_project", "uq_project_photo_annotation_company_project", "company_id, project"),
        "project_site_setup": ("ix_project_site_setup_project", "uq_project_site_setup_company_project", "company_id, project"),
    }
    if db.engine.dialect.name not in {"sqlite", "postgresql"}:
        return
    with db.engine.begin() as connection:
        for table, (old_index, new_index, columns) in changes.items():
            if table not in inspect(db.engine).get_table_names():
                continue
            connection.execute(text(f'DROP INDEX IF EXISTS "{old_index}"'))
            connection.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS "{new_index}" ON "{table}" ({columns})'))
@app.route("/", methods=["GET", "POST"])
def index():
    # Project-code entry is a public viewer flow, even when the same browser
    # happens to have an unrelated company account signed in.
    g.allow_public_viewer_scope = True
    if request.method == "POST":
        code = re.sub(r"[^A-Z]", "", request.form.get("code", "").upper())[:4]
        access = ProjectAccess.query.filter_by(code=code).first() if len(code) == 4 else None
        if viewer_access_active(access):
            session["viewer_code"] = access.code
            session["viewer_token"] = access.share_token
            return redirect(url_for("shared_project", token=access.share_token))
        flash("That project code was not recognized. Check the four letters and try again.", "error")
    return render_template("access.html")


@app.route("/share/<token>", methods=["GET", "POST"])
def shared_project(token):
    g.allow_public_viewer_scope = True
    access = ProjectAccess.query.filter_by(share_token=token).first()
    if not viewer_access_active(access):
        abort(404)
    if not company_feature_enabled("flight_scans", access.company_id):
        abort(404)
    if request.method == "POST":
        submitted = re.sub(r"[^A-Z]", "", request.form.get("code", "").upper())[:4]
        if len(submitted) != 4 or not secrets.compare_digest(submitted, access.code):
            flash("That project code was not recognized. Check the four letters and try again.", "error")
            return render_template("access.html", share_token=token,
                                   next_tab=request.form.get("next_tab", "map"), project_name=access.project)
        session["viewer_code"] = access.code
        session["viewer_token"] = access.share_token
        return redirect(url_for("shared_project", token=token,
                                tab=request.form.get("next_tab", "map")))
    if session.get("viewer_code") != access.code or session.get("viewer_token") != access.share_token:
        return render_template("access.html", share_token=token,
                               next_tab=request.args.get("tab", "map"), project_name=access.project)
    return render_project_view(access)


@app.get("/project/<code>")
def project_view(code):
    """Legacy URLs no longer grant access by themselves; send them through code entry."""
    g.allow_public_viewer_scope = True
    access = ProjectAccess.query.filter_by(code=code.upper()).first()
    if not viewer_access_active(access):
        abort(404)
    return redirect(url_for("shared_project", token=access.share_token, **request.args.to_dict()))


def render_project_view(access):
    if not company_feature_enabled("flight_scans", access.company_id):
        abort(404)
    token = access.share_token
    flights = Flight.query.filter_by(project=access.project, company_id=access.company_id).order_by(Flight.date.desc(), Flight.uploaded_at.desc()).all()
    docs = ProjectFile.query.filter_by(project=access.project, company_id=access.company_id).order_by(ProjectFile.date.desc(), ProjectFile.uploaded_at.desc()).all()
    dates = sorted({f.date for f in flights}, reverse=True)
    selected_date = parse_date(request.args.get("date"))
    if selected_date not in dates:
        selected_date = dates[0] if dates else now_utc().date()
    day_flights = [f for f in flights if f.date == selected_date]
    site_map = ProjectSiteMap.query.filter_by(project=access.project, company_id=access.company_id).first()
    photo_reference = ProjectPhotoReference.query.filter_by(project=access.project,
                                                              company_id=access.company_id).first()
    site_setup = ProjectSiteSetup.query.filter_by(project=access.project, company_id=access.company_id).first()
    if not site_setup:
        site_setup = ProjectSiteSetup(project=access.project,
                                      module="image" if site_map or photo_reference else "map")
    site_reference = site_map or photo_reference
    map_pins = ProjectMapPin.query.filter_by(project=access.project, company_id=access.company_id).order_by(ProjectMapPin.id).all()
    map_pin_data = [{"x": pin.x_percent, "y": pin.y_percent, "label": pin.label, "direction": pin.direction} for pin in map_pins]
    photo_pin_data = []
    if company_feature_enabled("photos", access.company_id):
        annotation_row = ProjectPhotoAnnotation.query.filter_by(project=access.project,
                                                                 company_id=access.company_id).first()
        try:
            annotations = json.loads(annotation_row.data) if annotation_row else {}
        except (TypeError, json.JSONDecodeError):
            annotations = {}
        assets = ProjectPinnedPhoto.query.filter_by(project=access.project,
                                                     company_id=access.company_id).order_by(ProjectPinnedPhoto.id).all()
        by_pin = {}
        for asset in assets:
            by_pin.setdefault(asset.pin_id, []).append({
                "url": url_for("protected_file", filename=asset.filename),
                "name": asset.original_filename,
            })
        for pin in annotations.get("pins", []) if isinstance(annotations, dict) else []:
            if not isinstance(pin, dict):
                continue
            try:
                x, y = float(pin.get("x")), float(pin.get("y"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1000 and 0 <= y <= 620):
                continue
            photo_pin_data.append({
                "id": str(pin.get("id") or ""), "x": x, "y": y,
                "label": str(pin.get("label") or "Site photo")[:80],
                "photos": by_pin.get(str(pin.get("id") or ""), []),
            })
    flight_paths = ProjectFlightPath.query.filter_by(project=access.project, date=selected_date, company_id=access.company_id).order_by(ProjectFlightPath.created_at).all()
    flight_path_data = []
    for path in flight_paths:
        try:
            points = json.loads(path.points or "[]")
        except (TypeError, json.JSONDecodeError):
            points = []
        flight_path_data.append({"id": path.id, "name": path.name, "label": path.label,
                                 "direction": path.direction, "points": points,
                                 "flight_id": path.flight_id, "flight_title": path.flight.title if path.flight else ""})
    selected_id = request.args.get("flight", type=int)
    selected_flight = next((f for f in day_flights if f.id == selected_id), None) or (day_flights[0] if day_flights else None)
    tab = request.args.get("tab", "map")
    permissions = {"map", "overview", "timeline", "media", "documents"}
    if not access.allow_overview:
        permissions.discard("overview")
        permissions.discard("timeline")
    if not access.allow_media:
        permissions.discard("media")
    if not access.allow_documents:
        permissions.discard("documents")
    if not company_feature_enabled("documents", access.company_id):
        permissions.discard("documents")
    if tab not in permissions:
        return redirect(url_for("shared_project", token=token, tab="map"))
    return render_template("project_view.html", project=access.project, code=access.code, share_token=token,
                           flights=flights, day_flights=day_flights, dates=dates, docs=docs,
                           selected_date=selected_date, selected_flight=selected_flight, tab=tab,
                           site_map=site_map, photo_reference=photo_reference, site_reference=site_reference,
                           map_pins=map_pins, map_pin_data=map_pin_data, photo_pin_data=photo_pin_data,
                           site_setup=site_setup, flight_paths=flight_path_data)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter(or_(User.username == username, User.email == username)).first()
        if user and user.check_password(password):
            membership = CompanyMembership.query.filter_by(user_id=user.id).first()
            pending_subscription = None
            if membership and membership.status == "verified_pending_billing" and user.email_verified:
                pending_subscription = (CompanySubscription.query.filter_by(company_id=membership.company_id)
                                        .order_by(CompanySubscription.id.desc()).first())
            if pending_subscription and pending_subscription.status in {"pending", "checkout_started"}:
                login_user(user, remember=False)
                session["pending_checkout_subscription_id"] = pending_subscription.id
                flash("Your company account is waiting for payment confirmation. Continue checkout to activate it.", "success")
                return redirect(url_for("subscription_checkout", subscription_id=pending_subscription.id))
            if not user.active:
                flash("Incorrect username or password.", "error")
                return render_template("login.html")
            login_user(user, remember=False)
            user.last_login_at = now_utc()
            db.session.commit()
            audit("login", "user", user.id, "Successful login")
            return redirect(url_for("dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("login.html")


def send_account_email(recipient, subject, body):
    """Send account mail when SMTP is configured; keep secrets in the environment."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("MAIL_FROM", "Construct IV <no-reply@constructiv.com>")
    message["To"] = recipient
    message.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls()
        username = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        if username:
            server.login(username, password)
        server.send_message(message)
    return True


@app.route("/register", methods=["GET", "POST"])
def register():
    plans = SubscriptionPlan.query.filter_by(active=True, custom_pricing=False).order_by(SubscriptionPlan.id).all()
    requested_plan = request.args.get("plan", "").strip().lower()
    pending_checkout = db.session.get(CompanySubscription, session.get("pending_checkout_subscription_id")) if session.get("pending_checkout_subscription_id") else None
    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()[:180]
        first = request.form.get("first_name", "").strip()[:80]
        last = request.form.get("last_name", "").strip()[:80]
        email = request.form.get("email", "").strip().lower()[:254]
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        plan = db.session.get(SubscriptionPlan, request.form.get("plan_id", type=int))
        if not company_name or not first or not last or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Complete the company and contact details with a valid work email.", "error")
        elif len(password) < 12:
            flash("Use a password with at least 12 characters.", "error")
        elif password != confirm:
            flash("The passwords do not match.", "error")
        elif User.query.filter(or_(User.username == email, User.email == email)).first():
            flash("An account already exists for that email. Sign in or recover the account.", "error")
        elif request.form.get("billing_interval", "monthly") not in {"monthly", "annual"}:
            flash("Choose monthly or annual billing.", "error")
        elif (request.form.get("billing_interval") == "annual" and
              (not plan or not plan.annual_amount_cents or not plan.stripe_annual_price_id)):
            flash("Annual billing is not available until its price is approved and configured.", "error")
        elif not plan or not plan.active or plan.custom_pricing:
            flash("Choose an available subscription plan.", "error")
        else:
            # Registration starts pending. Email verification alone never grants paid access.
            company = Company(name=company_name, status="pending", storage_limit_bytes=plan.storage_limit_bytes)
            user = User(username=email, email=email, email_verified=False, name=f"{first} {last}", active=False)
            user.set_password(password)
            db.session.add_all([company, user])
            try:
                db.session.flush()
            except IntegrityError:
                # A concurrent signup may have passed the earlier duplicate
                # lookup. Let the database uniqueness constraint arbitrate,
                # then return the normal duplicate-account message.
                db.session.rollback()
                flash("An account already exists for that email. Sign in or recover the account.", "error")
                return render_template("register.html", plans=plans, requested_plan=requested_plan,
                                       pending_checkout=pending_checkout), 400
            membership = CompanyMembership(company_id=company.id, user_id=user.id, role="company_admin", status="pending")
            subscription = CompanySubscription(company_id=company.id, plan_id=plan.id, status="pending", billing_interval=request.form.get("billing_interval", "monthly"))
            db.session.add_all([membership, subscription])
            raw_token = secrets.token_urlsafe(32)
            token = EmailActionToken(user_id=user.id, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                                     purpose="verify_email", expires_at=now_utc() + timedelta(hours=24))
            db.session.add(token)
            db.session.commit()
            verify_url = absolute_app_url("verify_email", token=raw_token)
            try:
                delivered = send_account_email(email, "Verify your Construct IV account",
                    f"Hello {first},\n\nVerify your email within 24 hours: {verify_url}\n\nYour account will remain pending until subscription billing is confirmed.")
            except Exception:
                app.logger.exception("Account verification email delivery failed")
                delivered = False
            audit("registration_pending", "company", company.id, f"New registration for {company_name}")
            return render_template("registration_pending.html", email=email, plan=plan,
                                   local_verify_url=verify_url if os.environ.get("APP_ENV", "development") != "production" and not delivered else "",
                                   email_sent=delivered), 201
        return render_template("register.html", plans=plans, requested_plan=requested_plan, pending_checkout=pending_checkout), 400
    return render_template("register.html", plans=plans, requested_plan=requested_plan, pending_checkout=pending_checkout)


@app.get("/verify-email/<token>")
def verify_email(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    action = EmailActionToken.query.filter_by(token_hash=digest, purpose="verify_email", used_at=None).first()
    if not action or action.expires_at < now_utc():
        flash("That verification link is invalid or expired. Start registration again or contact support.", "error")
        return redirect(url_for("register"))
    user = db.session.get(User, action.user_id)
    if not user:
        abort(404)
    user.email_verified = True
    action.used_at = now_utc()
    membership = CompanyMembership.query.filter_by(user_id=user.id).first()
    if membership:
        membership.status = "verified_pending_billing"
    db.session.commit()
    pending_subscription = (CompanySubscription.query.filter_by(company_id=membership.company_id)
                            .order_by(CompanySubscription.id.desc()).first() if membership else None)
    session["pending_checkout_subscription_id"] = pending_subscription.id if pending_subscription else None
    flash("Email verified. Continue to secure checkout; access starts only after billing confirmation.", "success")
    return redirect(url_for("subscription_checkout", subscription_id=session["pending_checkout_subscription_id"])) if session["pending_checkout_subscription_id"] else redirect(url_for("login"))


@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "GET":
        return render_template("verification_sent.html", local_verify_url="", sent=False)
    email = request.form.get("email", "").strip().lower()[:254]
    local_verify_url = ""
    user = User.query.filter(or_(User.email == email, User.username == email)).first() if email else None
    membership = (CompanyMembership.query.filter_by(user_id=user.id, role="company_admin").first()
                  if user else None)
    if user and user.email and not user.email_verified and membership and membership.status == "pending":
        EmailActionToken.query.filter_by(user_id=user.id, purpose="verify_email", used_at=None).update(
            {"used_at": now_utc()}, synchronize_session=False)
        raw_token = secrets.token_urlsafe(32)
        db.session.add(EmailActionToken(user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), purpose="verify_email",
            expires_at=now_utc() + timedelta(hours=24)))
        db.session.commit()
        verify_url = absolute_app_url("verify_email", token=raw_token)
        try:
            delivered = send_account_email(user.email, "Verify your Construct IV account",
                f"Verify your email within 24 hours: {verify_url}\n\nYour account will remain pending until subscription billing is confirmed.")
        except Exception:
            app.logger.exception("Account verification email resend failed")
            delivered = False
        if os.environ.get("APP_ENV", "development") != "production" and not delivered:
            local_verify_url = verify_url
    return render_template("verification_sent.html", local_verify_url=local_verify_url, sent=True)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    local_reset_url = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        user = User.query.filter(or_(User.email == email, User.username == email)).first()
        # Keep the response indistinguishable for known and unknown emails.
        if user and user.email:
            raw_token = secrets.token_urlsafe(32)
            db.session.add(EmailActionToken(user_id=user.id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), purpose="reset_password",
                expires_at=now_utc() + timedelta(hours=1)))
            db.session.commit()
            reset_url = absolute_app_url("reset_password_with_token", token=raw_token)
            try:
                delivered = send_account_email(email, "Reset your Construct IV password",
                    f"Use this secure link within one hour to set a new password: {reset_url}")
            except Exception:
                app.logger.exception("Password reset email delivery failed")
                delivered = False
            if os.environ.get("APP_ENV", "development") != "production" and not delivered:
                local_reset_url = reset_url
        return render_template("password_reset_sent.html", local_reset_url=local_reset_url)
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_with_token(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    action = EmailActionToken.query.filter_by(token_hash=digest, purpose="reset_password", used_at=None).first()
    if not action or action.expires_at < now_utc():
        flash("That password reset link is invalid or expired.", "error")
        return redirect(url_for("forgot_password"))
    user = db.session.get(User, action.user_id)
    if not user:
        abort(404)
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12 or password != confirm:
            flash("Use matching passwords with at least 12 characters.", "error")
        else:
            user.set_password(password)
            action.used_at = now_utc()
            db.session.commit()
            flash("Your password has been updated. Sign in to continue.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html")


@app.route("/account/checkout/<int:subscription_id>", methods=["GET", "POST"])
def subscription_checkout(subscription_id):
    subscription = db.get_or_404(CompanySubscription, subscription_id)
    membership = CompanyMembership.query.filter_by(company_id=subscription.company_id, role="company_admin").first()
    authenticated_owner = (current_user.is_authenticated and membership
                           and current_user.id == membership.user_id and membership.user.email_verified)
    verified_resume = (session.get("pending_checkout_subscription_id") == subscription.id
                       and membership and membership.user.email_verified)
    if not membership or (not authenticated_owner and not verified_resume):
        abort(403)
    if subscription.status not in {"pending", "checkout_started", "incomplete"}:
        flash("This subscription is no longer waiting for checkout. Review billing from your workspace.", "info")
        return redirect(url_for("dashboard") if current_user.is_authenticated else url_for("register"))
    plan = subscription.plan
    if (subscription.billing_interval == "annual" and
            (not plan or (not plan.custom_pricing
                          and (not plan.annual_amount_cents or not plan.stripe_annual_price_id)))):
        flash("Annual billing is not available until its price is approved and configured.", "error")
        return redirect(url_for("register"))
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    price_id = subscription.stripe_price_id_override or (
        (plan.stripe_annual_price_id if subscription.billing_interval == "annual" else plan.stripe_monthly_price_id)
        if plan else "")
    checkout_ready = stripe_api_key_is_valid(stripe_key) and bool(price_id)
    if request.method == "GET":
        if not stripe_api_key_is_valid(stripe_key):
            checkout_message = "Secure checkout is not connected yet. Your registration is saved; no payment can be taken."
        elif not price_id:
            stripe_mode = "live" if app.config.get("APP_ENV") == "production" else "test"
            checkout_message = f"This plan is not connected to a Stripe {stripe_mode} price yet. No payment can be taken."
        else:
            checkout_message = "You will continue to Stripe's secure checkout after reviewing the terms below."
        return render_template("checkout_review.html", subscription=subscription,
                               plan=plan, checkout_ready=checkout_ready,
                               checkout_message=checkout_message)
    if request.form.get("accept_billing_terms") != "yes":
        flash("Please review and accept the billing terms before continuing.", "error")
        return redirect(url_for("subscription_checkout", subscription_id=subscription.id))
    if not stripe_api_key_is_valid(stripe_key):
        flash("Secure subscription checkout is not configured yet. Your registration is saved as pending.", "error")
        return redirect(url_for("register"))
    if not price_id:
        stripe_mode = "live" if app.config.get("APP_ENV") == "production" else "test"
        flash(f"This plan is not connected to a Stripe {stripe_mode} price yet. No payment was taken.", "error")
        return redirect(url_for("register"))
    fields = {
        "mode": "subscription", "success_url": absolute_app_url("checkout_pending"),
        "cancel_url": absolute_app_url("register"), "client_reference_id": str(subscription.id),
        "line_items[0][price]": price_id, "line_items[0][quantity]": "1",
        "metadata[subscription_id]": str(subscription.id),
        "subscription_data[metadata][subscription_id]": str(subscription.id),
        "customer_email": membership.user.email or membership.user.username,
    }
    data = urlencode(fields).encode()
    req = URLRequest("https://api.stripe.com/v1/checkout/sessions", data=data,
                     headers={"Authorization": f"Bearer {stripe_key}",
                              "Content-Type": "application/x-www-form-urlencoded",
                              "Idempotency-Key": f"constructiv-checkout-subscription-{subscription.id}"})
    try:
        with urlopen(req, timeout=20) as response:
            checkout = json.loads(response.read())
        if checkout.get("url"):
            subscription.status = "checkout_started"
            db.session.commit()
            return redirect(checkout["url"], code=303)
    except (HTTPError, URLError, TimeoutError):
        app.logger.exception("Stripe checkout session creation failed")
    flash("Checkout could not be started. Your registration is still saved and no access was granted.", "error")
    return redirect(url_for("register"))


@app.get("/account/checkout-pending")
def checkout_pending():
    return render_template("checkout_pending.html")


@app.get("/account/billing-portal")
@admin_required
def billing_portal():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_api_key_is_valid(key):
        flash("Billing management is unavailable until Stripe is configured for this environment.", "error")
        return redirect(url_for("dashboard"))
    membership = current_membership()
    subscription = (CompanySubscription.query.filter_by(company_id=membership.company_id)
                    .order_by(CompanySubscription.id.desc()).first() if membership else None)
    if not subscription or not subscription.stripe_customer_id:
        flash("There is no Stripe billing profile on this workspace yet.", "error")
        return redirect(url_for("dashboard"))
    fields = {"customer": subscription.stripe_customer_id,
              "return_url": absolute_app_url("dashboard")}
    req = URLRequest("https://api.stripe.com/v1/billing_portal/sessions", data=urlencode(fields).encode(),
                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as response:
            portal = json.loads(response.read())
        if portal.get("url"):
            return redirect(portal["url"], code=303)
    except (HTTPError, URLError, TimeoutError):
        app.logger.exception("Stripe billing portal could not be opened")
    flash("Billing management could not be opened. Your workspace data is unchanged.", "error")
    return redirect(url_for("dashboard"))


def stripe_request(method, path, fields=None, *, idempotency_key=None):
    """Small server-only Stripe REST helper used for billing mutations and reads."""
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_api_key_is_valid(key):
        raise RuntimeError("Stripe is not configured for this environment.")
    fields = fields or {}
    url = "https://api.stripe.com/v1/" + path.lstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    if method.upper() == "GET":
        if fields:
            url += "?" + urlencode(fields)
        data = None
    else:
        data = urlencode(fields).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:255]
    req = URLRequest(url, data=data, headers=headers, method=method.upper())
    with urlopen(req, timeout=20) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("Stripe returned an invalid response.")
    return payload


def billing_subscription_for_current_company():
    membership = current_membership()
    if not membership:
        abort(403)
    return (CompanySubscription.query.filter_by(company_id=membership.company_id)
            .order_by(CompanySubscription.id.desc()).first())


def stripe_period_date(subscription_obj):
    timestamp = subscription_obj.get("current_period_end")
    return datetime.utcfromtimestamp(timestamp) if isinstance(timestamp, int) and timestamp > 0 else None


@app.route("/account/billing/cancel", methods=["GET", "POST"])
@admin_required
def cancel_subscription():
    subscription = billing_subscription_for_current_company()
    if (not subscription or not subscription.stripe_subscription_id
            or subscription.status not in {"active", "trialing", "past_due"}):
        flash("There is no active Stripe subscription to cancel.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("cancel_subscription.html", subscription=subscription,
                               plan=subscription.plan)
    if subscription.cancel_at_period_end:
        flash("Cancellation is already scheduled. Paid access continues through the date shown.", "info")
        return redirect(url_for("dashboard"))
    try:
        result = stripe_request("POST", f"subscriptions/{subscription.stripe_subscription_id}",
                                {"cancel_at_period_end": "true"},
                                idempotency_key=f"constructiv-schedule-cancel-{subscription.id}-{int((subscription.current_period_end or now_utc()).timestamp())}")
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError):
        app.logger.exception("Stripe cancellation scheduling failed for subscription record %s", subscription.id)
        flash("Stripe could not schedule cancellation. Your current subscription is unchanged.", "error")
        return redirect(url_for("dashboard"))
    period_end = stripe_period_date(result)
    if period_end:
        subscription.current_period_end = period_end
    subscription.cancel_at_period_end = bool(result.get("cancel_at_period_end"))
    db.session.commit()
    if not subscription.cancel_at_period_end:
        flash("Stripe did not confirm scheduled cancellation. Your subscription remains active.", "error")
        return redirect(url_for("dashboard"))
    audit("subscription_cancellation_scheduled", "subscription", subscription.id,
          f"Cancel at paid-through date {subscription.current_period_end.isoformat() if subscription.current_period_end else 'from Stripe'}")
    try:
        send_account_email(current_user.email or current_user.username, "Construct IV cancellation scheduled",
                           f"Your {subscription.plan.name if subscription.plan else 'subscription'} renewal is canceled. "
                           f"Paid access remains active through {subscription.current_period_end.strftime('%b %d, %Y') if subscription.current_period_end else 'the paid-through date shown in your billing dashboard'}. "
                           "Your project records and uploaded files have not been deleted. No automatic refund was issued.")
    except Exception:
        app.logger.exception("Cancellation confirmation email could not be delivered")
    flash("Cancellation scheduled. Your paid access continues through the Stripe billing period.", "success")
    return redirect(url_for("dashboard"))


@app.post("/account/billing/cancel/reverse")
@admin_required
def reverse_scheduled_cancellation():
    subscription = billing_subscription_for_current_company()
    if (not subscription or not subscription.stripe_subscription_id
            or not subscription.cancel_at_period_end or not subscription.current_period_end
            or subscription.current_period_end <= now_utc()):
        flash("There is no scheduled cancellation that can be reversed.", "error")
        return redirect(url_for("dashboard"))
    try:
        result = stripe_request("POST", f"subscriptions/{subscription.stripe_subscription_id}",
                                {"cancel_at_period_end": "false"},
                                idempotency_key=f"constructiv-reverse-cancel-{subscription.id}-{int(subscription.current_period_end.timestamp())}")
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError):
        app.logger.exception("Stripe scheduled cancellation reversal failed for subscription record %s", subscription.id)
        flash("Stripe could not reverse the scheduled cancellation. Your subscription state is unchanged.", "error")
        return redirect(url_for("dashboard"))
    subscription.cancel_at_period_end = bool(result.get("cancel_at_period_end"))
    subscription.current_period_end = stripe_period_date(result) or subscription.current_period_end
    db.session.commit()
    audit("subscription_cancellation_reversed", "subscription", subscription.id,
          "Scheduled cancellation reversed before the end of the paid period")
    flash("Your subscription renewal is active again.", "success")
    return redirect(url_for("dashboard"))


REFUND_REASONS = ("Project completed early", "Company no longer requires the service",
                  "Billing error", "Duplicate purchase", "Service issue", "Other")


@app.route("/account/billing/refund-request", methods=["GET", "POST"])
@admin_required
def annual_refund_request():
    subscription = billing_subscription_for_current_company()
    if (not subscription or subscription.billing_interval != "annual"
            or subscription.status not in {"active", "trialing", "past_due"}):
        abort(404)
    membership = current_membership()
    existing = AnnualRefundRequest.query.filter_by(
        company_id=membership.company_id, subscription_id=subscription.id
    ).filter(AnnualRefundRequest.status.in_(OPEN_REFUND_STATUSES)).order_by(
        AnnualRefundRequest.requested_at.desc()).first()
    if request.method == "GET":
        return render_template("annual_refund_request.html", subscription=subscription,
                               refund_request=existing, reasons=REFUND_REASONS,
                               company_name=membership.company.name)
    if existing:
        flash("Your refund request is already being reviewed.", "info")
        return redirect(url_for("dashboard"))
    reason = request.form.get("reason", "").strip()
    comments = request.form.get("comments", "").strip()[:4000]
    if reason not in REFUND_REASONS or not comments:
        flash("Choose a reason and add a short explanation for the review.", "error")
        return render_template("annual_refund_request.html", subscription=subscription,
                               refund_request=None, reasons=REFUND_REASONS,
                               company_name=membership.company.name), 400
    amount = subscription.custom_amount_cents
    if amount is None and subscription.plan:
        amount = subscription.plan.annual_amount_cents
    if amount is None:
        amount = 0
    invoice = {}
    if subscription.stripe_subscription_id and stripe_api_key_is_valid():
        try:
            stripe_subscription = stripe_request("GET", f"subscriptions/{subscription.stripe_subscription_id}",
                                                 {"expand[]": "latest_invoice.payment_intent"})
            invoice = stripe_subscription.get("latest_invoice") or {}
            if isinstance(invoice, str):
                invoice = stripe_request("GET", f"invoices/{invoice}")
            if not isinstance(invoice, dict):
                invoice = {}
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError):
            app.logger.exception("Annual refund review could not retrieve invoice details for subscription %s", subscription.id)
            invoice = {}
    payment_value = invoice.get("payment_intent") if isinstance(invoice, dict) else None
    payment_intent = payment_value.get("id") if isinstance(payment_value, dict) else payment_value
    invoice_lines = ((invoice.get("lines") or {}).get("data") or []) if isinstance(invoice, dict) else []
    invoice_period = (invoice_lines[0].get("period") or {}) if invoice_lines else {}
    item = AnnualRefundRequest(
        company_id=membership.company_id, subscription_id=subscription.id,
        requested_by_user_id=current_user.id, contact_email=(current_user.email or current_user.username),
        stripe_subscription_reference=subscription.stripe_subscription_id or "",
        stripe_invoice_reference=invoice.get("id", "") if isinstance(invoice, dict) else "",
        stripe_payment_intent_id=payment_intent or "",
        original_amount_cents=int(invoice.get("amount_paid") or amount) if isinstance(invoice, dict) else max(0, amount),
        subscription_started_at=(datetime.utcfromtimestamp(invoice_period["start"])
                                 if isinstance(invoice_period.get("start"), int)
                                 else subscription.current_period_start),
        subscription_ends_at=(datetime.utcfromtimestamp(invoice_period["end"])
                              if isinstance(invoice_period.get("end"), int)
                              else subscription.current_period_end), reason=reason, comments=comments,
        idempotency_key=f"constructiv-annual-refund-{uuid4().hex}")
    db.session.add(item)
    db.session.commit()
    audit("annual_refund_requested", "refund_request", item.id,
          "Annual early termination and refund review submitted; no refund or cancellation was performed")
    flash("Your annual refund request was submitted for Platform Owner review. Your subscription remains active.", "success")
    return redirect(url_for("dashboard"))


@app.get("/account/billing/invoices")
@admin_required
def billing_invoices():
    subscription = billing_subscription_for_current_company()
    invoices = []
    error = ""
    if subscription and subscription.stripe_customer_id and stripe_api_key_is_valid():
        try:
            result = stripe_request("GET", "invoices", {"customer": subscription.stripe_customer_id,
                                                          "limit": "20"})
            rows = (result.get("data") or [])[:20]
            invoices = [{
                "number": row.get("number") or row.get("id") or "Invoice",
                "created_at": datetime.utcfromtimestamp(row["created"]) if isinstance(row.get("created"), int) else None,
                "amount": f"{(row.get('amount_paid') or 0) / 100:,.2f} {str(row.get('currency') or '').upper()}",
                "status": row.get("status") or "unknown",
                "url": row.get("hosted_invoice_url") or "",
                "pdf_url": row.get("invoice_pdf") or "",
            } for row in rows]
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError):
            app.logger.exception("Stripe invoice history could not be loaded for subscription %s", subscription.id)
            error = "Invoice history could not be loaded right now. Use Manage Billing to view invoices in Stripe."
    elif not subscription or not subscription.stripe_customer_id:
        error = "Invoices will appear here after a Stripe customer is linked to this workspace."
    else:
        error = "Invoice history is not available until Stripe is configured."
    return render_template("billing_invoices.html", invoices=invoices, error=error,
                           can_manage=bool(subscription and subscription.stripe_customer_id))


@app.post("/owner/refunds/<int:refund_id>")
@platform_owner_required
def review_annual_refund(refund_id):
    item = db.get_or_404(AnnualRefundRequest, refund_id)
    if item.status in {"Denied", "Completed"}:
        flash("This refund review is closed.", "error")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    action = request.form.get("action", "").strip()
    explanation = request.form.get("decision_explanation", "").strip()[:4000]
    if action == "under_review":
        item.status = "Under Review"
        item.decision_explanation = explanation
        item.decided_by_user_id = current_user.id
        db.session.commit()
        audit("annual_refund_under_review", "refund_request", item.id, explanation or "Assigned for review")
        flash("Refund request marked under review.", "success")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    if action == "deny":
        if not explanation:
            flash("Add a decision explanation before denying a refund request.", "error")
            return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
        item.status = "Denied"
        item.decision_explanation = explanation
        item.decided_by_user_id = current_user.id
        db.session.commit()
        audit("annual_refund_denied", "refund_request", item.id, explanation)
        flash("Refund request denied. The subscription and company data were not changed.", "success")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    if action not in {"approve_full", "approve_partial"}:
        abort(400)
    if not explanation:
        flash("Add the reason for approving this refund.", "error")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    termination_value = request.form.get("agreed_termination_date", "").strip()
    agreed_termination = None
    if termination_value:
        try:
            agreed_termination = datetime.combine(date.fromisoformat(termination_value), datetime.min.time())
        except ValueError:
            flash("Enter a valid agreed termination date.", "error")
            return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    amount = item.original_amount_cents if action == "approve_full" else request.form.get("approved_amount_cents", type=int)
    if not amount or amount < 1 or amount > item.original_amount_cents:
        flash("Enter an approved refund amount within the original annual payment.", "error")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    if item.status == "Completed" or (item.stripe_refund_id and item.status != "Failed"):
        flash("A Stripe refund is already recorded for this request.", "error")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    if not item.stripe_subscription_reference:
        flash("The Stripe subscription reference is missing; no refund was sent.", "error")
        return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")
    if item.status != "Processing":
        item.status = "Approved" if amount == item.original_amount_cents else "Partially Approved"
        item.approved_amount_cents = amount
        item.decision_explanation = explanation
        item.agreed_termination_at = agreed_termination
        item.decided_by_user_id = current_user.id
        # Save the review decision before making the external request.
        db.session.commit()
        audit("annual_refund_approved", "refund_request", item.id,
              f"Approved {amount} cents; termination date={agreed_termination.date().isoformat() if agreed_termination else 'not specified'}")
        item.status = "Processing"
        db.session.commit()
    try:
        payment_intent = item.stripe_payment_intent_id
        if not payment_intent:
            subscription_data = stripe_request("GET", f"subscriptions/{item.stripe_subscription_reference}",
                                               {"expand[]": "latest_invoice.payment_intent"})
            invoice = subscription_data.get("latest_invoice") or {}
            if isinstance(invoice, str):
                invoice = stripe_request("GET", f"invoices/{invoice}")
            if not isinstance(invoice, dict):
                raise ValueError("Stripe did not return the annual invoice details.")
            payment_value = invoice.get("payment_intent")
            payment_intent = payment_value.get("id") if isinstance(payment_value, dict) else payment_value
            item.stripe_invoice_reference = invoice.get("id", "")
            item.original_amount_cents = int(invoice.get("amount_paid") or item.original_amount_cents or 0)
            if not payment_intent:
                raise ValueError("Stripe did not provide a payment to refund for this annual term.")
            if amount > item.original_amount_cents:
                raise ValueError("The approved amount exceeds the Stripe invoice payment amount.")
            item.stripe_payment_intent_id = payment_intent
            db.session.commit()
        refund_fields = {"payment_intent": payment_intent}
        if amount < item.original_amount_cents:
            refund_fields["amount"] = str(amount)
        refund = stripe_request("POST", "refunds", refund_fields,
                                idempotency_key=item.idempotency_key)
        refund_status = refund.get("status")
        item.stripe_refund_id = refund.get("id", "")
        item.status = "Completed" if refund_status == "succeeded" else (
            "Processing" if refund_status in {"pending", "requires_action"} else "Failed")
        item.approved_amount_cents = int(refund.get("amount") or amount)
        item.stripe_event_created = int(refund.get("created") or 0)
        db.session.commit()
        audit("annual_refund_processed", "refund_request", item.id,
              f"Stripe refund {item.status.lower()}; amount={item.approved_amount_cents} cents")
        flash(f"Stripe refund status: {item.status}.", "success" if item.status != "Failed" else "error")
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError):
        db.session.rollback()
        app.logger.exception("Stripe annual refund processing failed for request %s", item.id)
        item = db.get_or_404(AnnualRefundRequest, refund_id)
        item.status = "Failed"
        db.session.commit()
        audit("annual_refund_processing_failed", "refund_request", item.id,
              "Stripe refund could not be confirmed. Retry uses the same idempotency key.")
        flash("Stripe could not confirm the refund. The request is marked failed; retry will reuse its idempotency key.", "error")
    return redirect(url_for("platform_owner_dashboard") + "#annual-refunds")


def valid_stripe_signature(payload, signature_header, secret):
    try:
        parts = dict(item.split("=", 1) for item in signature_header.split(","))
        timestamp = int(parts["t"])
        if abs(int(time.time()) - timestamp) > 300:
            return False
        expected = hmac.new(secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts["v1"])
    except (ValueError, KeyError, TypeError):
        return False


@app.post("/api/billing/stripe-webhook")
def stripe_webhook():
    if not stripe_api_key_is_valid():
        abort(503, "Stripe is not configured for this environment")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    payload = request.get_data(cache=False)
    if not secret or not valid_stripe_signature(payload, request.headers.get("Stripe-Signature", ""), secret):
        abort(400, "Invalid Stripe signature")
    try:
        event = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        abort(400, "Invalid Stripe event payload")
    event_id, event_type = event.get("id"), event.get("type")
    event_created = event.get("created") if isinstance(event.get("created"), int) else 0
    if not event_id or not event_type:
        abort(400, "Invalid Stripe event")
    if StripeEvent.query.filter_by(event_id=event_id).first():
        return jsonify({"received": True, "duplicate": True})
    # Claim the unique event ID inside this transaction before applying state
    # changes. Concurrent Stripe retries block on the unique constraint and
    # return as duplicates after the winning transaction commits.
    db.session.add(StripeEvent(event_id=event_id, event_type=event_type, stripe_created=event_created))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        if StripeEvent.query.filter_by(event_id=event_id).first():
            return jsonify({"received": True, "duplicate": True})
        raise
    obj = (event.get("data") or {}).get("object") or {}
    if event_type in {"refund.created", "refund.updated"}:
        refund_item = AnnualRefundRequest.query.filter_by(stripe_refund_id=obj.get("id", "")).first()
        if refund_item and event_created >= refund_item.stripe_event_created:
            refund_item.stripe_event_created = event_created
            refund_item.status = ({"succeeded": "Completed", "pending": "Processing",
                                   "requires_action": "Processing", "failed": "Failed"}
                                  .get(obj.get("status"), refund_item.status))

    subscription_id = ((obj.get("metadata") or {}).get("subscription_id")
                       or obj.get("client_reference_id"))
    record = db.session.get(CompanySubscription, int(subscription_id)) if str(subscription_id or "").isdigit() else None
    stripe_subscription_id = obj.get("subscription") or (
        obj.get("id") if event_type.startswith("customer.subscription.") else None
    )
    parent = obj.get("parent") or {}
    subscription_details = parent.get("subscription_details") or {}
    stripe_subscription_id = stripe_subscription_id or subscription_details.get("subscription")
    if not record and stripe_subscription_id:
        record = CompanySubscription.query.filter_by(stripe_subscription_id=stripe_subscription_id).first()
    if record and event_created >= record.stripe_event_created and event_type in {
            "checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
            "customer.subscription.deleted", "invoice.payment_failed", "invoice.payment_succeeded", "invoice.paid"}:
        record.stripe_event_created = event_created
        if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
            # Stripe's customer portal owns subscription changes. Mirror a
            # recognized recurring Price into the local plan so limits and
            # storage allowances follow upgrades and downgrades.
            items = ((obj.get("items") or {}).get("data") or [])
            price = (items[0].get("price") or {}) if items and isinstance(items[0], dict) else {}
            price_id = price.get("id") if isinstance(price, dict) else price
            if price_id:
                matched_plan = SubscriptionPlan.query.filter(or_(
                    SubscriptionPlan.stripe_monthly_price_id == price_id,
                    SubscriptionPlan.stripe_annual_price_id == price_id,
                )).first()
                if matched_plan:
                    record.plan = matched_plan
                    if not matched_plan.custom_pricing:
                        record.company.storage_limit_bytes = matched_plan.storage_limit_bytes
                    recurring = price.get("recurring") if isinstance(price, dict) else None
                    interval = (recurring or {}).get("interval")
                    if interval in {"month", "year"}:
                        record.billing_interval = "annual" if interval == "year" else "monthly"
                else:
                    app.logger.warning("Stripe subscription %s uses an unmapped Price ID %s",
                                       obj.get("id", "unknown"), price_id)
            period_end = obj.get("current_period_end")
            if isinstance(period_end, int) and period_end > 0:
                record.current_period_end = datetime.utcfromtimestamp(period_end)
            period_start = obj.get("current_period_start")
            if isinstance(period_start, int) and period_start > 0:
                record.current_period_start = datetime.utcfromtimestamp(period_start)
            record.cancel_at_period_end = bool(obj.get("cancel_at_period_end", False))
        if event_type == "invoice.payment_failed":
            record.status = "past_due"
            if not record.past_due_since:
                record.past_due_since = datetime.utcfromtimestamp(event_created) if event_created else now_utc()
            enterprise_request = EnterpriseRequest.query.filter_by(company_id=record.company_id).first()
            if enterprise_request:
                enterprise_request.status = "payment_past_due"
        elif event_type in {"invoice.payment_succeeded", "invoice.paid"}:
            # A signed paid renewal is authoritative evidence that Stripe has
            # recovered the subscription after a failed payment.
            if obj.get("paid") is True or obj.get("status") == "paid":
                record.status = "active"
                record.past_due_since = None
                if record.company.status != "suspended":
                    record.company.status = "active"
                    if record.plan and not record.plan.custom_pricing:
                        record.company.storage_limit_bytes = record.plan.storage_limit_bytes
                    enterprise_request = EnterpriseRequest.query.filter_by(company_id=record.company_id).first()
                    if enterprise_request:
                        enterprise_request.status = "active"
        else:
            stripe_status = obj.get("status") or "pending"
            if event_type == "checkout.session.completed" and obj.get("status") == "complete" and obj.get("payment_status") in {"paid", "no_payment_required"} and obj.get("subscription"):
                stripe_status = "active"
            record.stripe_customer_id = obj.get("customer") or record.stripe_customer_id
            record.stripe_subscription_id = (obj.get("subscription") or record.stripe_subscription_id) if event_type == "checkout.session.completed" else (obj.get("id") or record.stripe_subscription_id)
            if stripe_status in {"active", "trialing"} and (event_type != "checkout.session.completed" or obj.get("payment_status") in {"paid", "no_payment_required"}):
                record.status = stripe_status
                record.past_due_since = None
                if record.company.status != "suspended":
                    record.company.status = "active"
                    if record.plan and not record.plan.custom_pricing:
                        record.company.storage_limit_bytes = record.plan.storage_limit_bytes
                    member = CompanyMembership.query.filter_by(company_id=record.company_id, role="company_admin").first()
                    if member and member.user.email_verified:
                        member.status = "active"
                        member.user.active = True
                        member.user.is_admin = True
                        enterprise_request = EnterpriseRequest.query.filter_by(admin_user_id=member.user_id).first()
                        if enterprise_request:
                            enterprise_request.status = "active"
            elif event_type == "customer.subscription.deleted" or stripe_status in {"canceled", "unpaid", "incomplete_expired"}:
                record.status = stripe_status
                record.past_due_since = None
                period_end = obj.get("current_period_end")
                paid_through = datetime.utcfromtimestamp(period_end) if isinstance(period_end, int) and period_end > datetime.utcnow().timestamp() else None
                record.current_period_end = paid_through
                # Keep billing expiry distinct from an explicit owner
                # suspension so a later verified resubscription can restore
                # access without undoing an owner's security decision.
                record.company.status = "active" if paid_through else "billing_suspended"
                enterprise_request = EnterpriseRequest.query.filter_by(company_id=record.company_id).first()
                if enterprise_request and not paid_through:
                    enterprise_request.status = "billing_ended"
            else:
                record.status = stripe_status
                if stripe_status == "past_due" and not record.past_due_since:
                    record.past_due_since = datetime.utcfromtimestamp(event_created) if event_created else now_utc()
                elif stripe_status not in {"past_due"}:
                    record.past_due_since = None
    db.session.commit()
    return jsonify({"received": True})


@app.route("/enterprise/request", methods=["GET", "POST"])
def enterprise_request():
    if request.method == "POST":
        if request.form.get("website", "").strip():
            abort(400)
        fields = {key: request.form.get(key, "").strip() for key in (
            "company_name", "contact_name", "email", "phone", "estimated_projects",
            "estimated_users", "estimated_storage", "comments")}
        if not fields["company_name"] or not fields["contact_name"] or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"]):
            flash("Enter your company, contact name and a valid email address.", "error")
        elif len(fields["comments"]) > 5000:
            flash("Comments are too long.", "error")
        else:
            inquiry = EnterpriseRequest(**fields)
            db.session.add(inquiry)
            db.session.commit()
            audit("enterprise_request_created", "company", fields["company_name"], "Custom pricing request")
            message = "\n".join((
                "A new Enterprise pricing request was submitted.",
                f"Company: {fields['company_name']}",
                f"Contact: {fields['contact_name']}",
                f"Email: {fields['email']}",
                f"Phone: {fields['phone'] or 'Not provided'}",
                f"Estimated projects: {fields['estimated_projects'] or 'Not provided'}",
                f"Estimated users: {fields['estimated_users'] or 'Not provided'}",
                f"Estimated storage: {fields['estimated_storage'] or 'Not provided'}",
                "Additional comments:",
                fields["comments"] or "None provided",
                f"Request reference: {inquiry.id}",
            ))
            notification_delivered = False
            if ENTERPRISE_INQUIRY_EMAIL:
                try:
                    notification_delivered = send_account_email(
                        ENTERPRISE_INQUIRY_EMAIL, "New Construct IV Enterprise pricing request", message)
                except Exception:
                    app.logger.exception("Enterprise request notification could not be sent")
            return render_template("enterprise_received.html",
                                   notification_delivered=notification_delivered), 201
    return render_template("enterprise_request.html")


@app.get("/owner")
@platform_owner_required
def platform_owner_dashboard():
    g.allow_public_viewer_scope = True
    companies = Company.query.order_by(Company.created_at.desc()).all()
    plans = SubscriptionPlan.query.order_by(SubscriptionPlan.id).all()
    requests = EnterpriseRequest.query.order_by(EnterpriseRequest.created_at.desc()).all()
    refund_requests = AnnualRefundRequest.query.order_by(AnnualRefundRequest.requested_at.desc()).all()
    subscriptions = {row.company_id: row for row in CompanySubscription.query.order_by(CompanySubscription.id.desc()).all()}
    pending_admins = {
        membership.company_id: membership.user
        for membership in CompanyMembership.query.filter_by(role="company_admin").all()
        if not membership.user.email_verified
        and (subscription := subscriptions.get(membership.company_id))
        and subscription.status == "pending" and subscription.plan and subscription.plan.custom_pricing
    }
    usage, usage_by_project = aggregate_company_storage(company.id for company in companies)
    storage_limits = {}
    for company in companies:
        subscription = subscriptions.get(company.id)
        storage_limits[company.id] = company_storage_limit_bytes(company, subscription)
    storage_warning_levels = {
        company.id: ("critical" if storage_limits[company.id] and usage[company.id] / storage_limits[company.id] >= 0.9
                     else "warning" if storage_limits[company.id] and usage[company.id] / storage_limits[company.id] >= 0.8
                     else "") for company in companies
    }
    near_storage_limit = {company_id: bool(level) for company_id, level in storage_warning_levels.items()}
    growth_since = datetime.combine(date.today() - timedelta(days=30), datetime.min.time())
    growth_30d = sum(
        db.session.query(func.coalesce(func.sum(model.file_size), 0))
        .filter(model.uploaded_at >= growth_since).scalar() or 0
        for model in STORAGE_FILE_MODELS
    )
    stripe_key_ready = stripe_api_key_is_valid()
    stripe_mode = "live" if app.config.get("APP_ENV") == "production" else "test"
    stripe_webhook_ready = bool(os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip())
    standard_plans = [plan for plan in plans if plan.active and not plan.custom_pricing]
    missing_price_plans = []
    for plan in standard_plans:
        missing_intervals = ["monthly"] if not plan.stripe_monthly_price_id else []
        if bool(plan.annual_amount_cents) != bool(plan.stripe_annual_price_id):
            missing_intervals.append("annual price and amount must both be configured")
        if missing_intervals:
            missing_price_plans.append(f"{plan.name} ({', '.join(missing_intervals)})")
    stripe_ready = stripe_key_ready and stripe_webhook_ready and not missing_price_plans
    database_url = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    database_scheme = database_url.split(":", 1)[0].lower()
    postgres_ready = database_scheme in {"postgres", "postgresql"} or database_scheme.startswith("postgresql+")
    smtp_ready = bool(os.environ.get("SMTP_HOST", "").strip())
    enterprise_inbox_ready = bool(ENTERPRISE_INQUIRY_EMAIL)
    r2_requested = cloud_storage_enabled()
    r2_credentials_ready = bool(
        os.environ.get("R2_BUCKET_NAME", "").strip()
        and os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        and os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
        and (os.environ.get("R2_ENDPOINT_URL", "").strip() or os.environ.get("R2_ACCOUNT_ID", "").strip())
    )
    setup_checks = [
        {"label": "Database", "state": "ready" if postgres_ready else "local",
         "status": "Managed database" if postgres_ready else "Local SQLite",
         "detail": "PostgreSQL is configured." if postgres_ready else "SQLite is suitable for local development; configure PostgreSQL before production."},
        {"label": f"Stripe {stripe_mode} billing", "state": "ready" if stripe_ready else "pending",
         "status": "Ready" if stripe_ready else "Setup needed",
         "detail": (f"Stripe {stripe_mode} key, webhook secret and active plan prices are configured." if stripe_ready else
                    "; ".join(part for part in [
                        f"add STRIPE_SECRET_KEY ({stripe_mode} mode)" if not stripe_key_ready else "",
                        "add STRIPE_WEBHOOK_SECRET" if not stripe_webhook_ready else "",
                        ("configure plan prices: " + "; ".join(missing_price_plans)) if missing_price_plans else "",
                    ] if part))},
        {"label": "Email delivery", "state": "ready" if smtp_ready else "pending",
         "status": "Configured" if smtp_ready else "Setup needed",
         "detail": "SMTP is configured for verification and invitations." if smtp_ready else "Add SMTP_HOST and credentials to deliver account verification, password reset and invitations."},
        {"label": "Enterprise request routing",
         "state": "ready" if smtp_ready and enterprise_inbox_ready else "pending",
         "status": "Private routing configured" if smtp_ready and enterprise_inbox_ready else "Saved for owner review",
         "detail": ("New requests are emailed to the private owner inbox; its address is hidden here."
                    if smtp_ready and enterprise_inbox_ready else
                    "Requests are saved in the owner dashboard. Configure SMTP and ENTERPRISE_INQUIRY_EMAIL to send private notifications.")},
        {"label": "Customer file storage", "state": "ready" if r2_requested and r2_credentials_ready else ("local" if not r2_requested else "pending"),
         "status": "Private R2 enabled" if r2_requested and r2_credentials_ready else ("Local storage" if not r2_requested else "Setup needed"),
         "detail": "New customer files use the configured private bucket." if r2_requested and r2_credentials_ready else ("Local uploads remain enabled. Configure R2 credentials before enabling cloud storage." if not r2_requested else "Complete the bucket, endpoint/account ID and access key settings before uploads can use R2.")},
    ]
    return render_template("owner.html", companies=companies, plans=plans,
                           plan_features=PLAN_FEATURES,
                           enterprise_requests=requests, refund_requests=refund_requests,
                           subscriptions=subscriptions, usage=usage,
                           pending_admins=pending_admins,
                           storage_limits=storage_limits, usage_by_project=usage_by_project,
                           storage_warning_levels=storage_warning_levels,
                           near_storage_limit=near_storage_limit,
                           stripe_mode=stripe_mode,
                           total_storage_bytes=sum(usage.values()), storage_growth_30d=growth_30d,
                           storage_cost_source="Provider billing data is not connected",
                           setup_checks=setup_checks,
                           local_enterprise_setup_url=session.pop("local_enterprise_setup_url", ""))


@app.post("/owner/plans/<int:plan_id>")
@platform_owner_required
def update_subscription_plan(plan_id):
    plan = db.get_or_404(SubscriptionPlan, plan_id)
    if plan.custom_pricing or plan.name != "Basic":
        flash("Enterprise plan settings are preserved and cannot be changed here.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    def optional_int(key, minimum=0, maximum=2_147_483_647):
        raw = request.form.get(key, "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
            return value if minimum <= value <= maximum else None
        except ValueError:
            return None
    monthly_amount = optional_int("monthly_amount_cents")
    annual_amount = optional_int("annual_amount_cents")
    monthly_price_id = request.form.get("stripe_monthly_price_id", "").strip()[:160]
    annual_price_id = request.form.get("stripe_annual_price_id", "").strip()[:160]
    if monthly_amount != BASIC_MONTHLY_PRICE_CENTS:
        flash("Construct IV Basic is fixed at $399 per month.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if monthly_price_id and not re.fullmatch(r"price_[A-Za-z0-9]+", monthly_price_id):
        flash("Enter a valid Stripe monthly Price ID.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if annual_amount is not None and annual_amount <= 0:
        flash("Annual price must be positive when configured.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if annual_price_id and not re.fullmatch(r"price_[A-Za-z0-9]+", annual_price_id):
        flash("Enter a valid Stripe annual Price ID.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if bool(annual_amount) != bool(annual_price_id):
        flash("Set both the approved annual amount and annual Stripe Price ID, or leave both blank.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    plan.monthly_amount_cents = BASIC_MONTHLY_PRICE_CENTS
    plan.annual_amount_cents = annual_amount
    plan.project_limit = BASIC_PROJECT_LIMIT
    plan.employee_limit = BASIC_ACCOUNT_LIMIT
    plan.storage_limit_bytes = BASIC_STORAGE_LIMIT_BYTES
    plan.stripe_monthly_price_id = monthly_price_id
    plan.stripe_annual_price_id = annual_price_id
    plan.active = True
    db.session.commit()
    audit("subscription_plan_updated", "plan", plan.id, f"Updated plan {plan.name}")
    flash(f"{plan.name} plan settings saved.", "success")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/companies/create")
@platform_owner_required
def create_owner_company():
    """Provision a manually negotiated Enterprise workspace, pending verified billing."""
    name = request.form.get("company_name", "").strip()[:180]
    contact = request.form.get("contact_name", "").strip()[:160]
    email = request.form.get("email", "").strip().lower()
    price_id = request.form.get("stripe_price_id", "").strip()[:160]
    interval = request.form.get("billing_interval", "monthly")

    def positive_int(key, maximum=2_147_483_647):
        try:
            value = int(request.form.get(key, ""))
            return value if 1 <= value <= maximum else None
        except (TypeError, ValueError):
            return None

    amount = positive_int("custom_amount_cents")
    projects = positive_int("project_limit")
    employees = positive_int("employee_limit")
    storage_gb = positive_int("storage_gb")
    if (not name or not contact or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not re.fullmatch(r"price_[A-Za-z0-9]+", price_id)
            or interval not in {"monthly", "annual"}
            or None in (amount, projects, employees, storage_gb)):
        flash("Enter valid company and admin details, a Stripe Price ID, custom amount, and positive workspace limits.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if User.query.filter(or_(User.username == email, User.email == email)).first():
        flash("That email already has an account. Review its existing workspace before provisioning.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    plan = SubscriptionPlan.query.filter_by(name="Enterprise", custom_pricing=True).first()
    if not plan:
        flash("The custom Enterprise plan has not been initialized.", "error")
        return redirect(url_for("platform_owner_dashboard"))

    company = Company(name=name, status="pending", storage_limit_bytes=storage_gb * 1024**3,
                      project_limit=projects, employee_limit=employees)
    user = User(username=email, email=email, email_verified=False, name=contact,
                active=False, is_admin=True)
    user.set_password(secrets.token_urlsafe(40))
    db.session.add_all([company, user])
    db.session.flush()
    membership = CompanyMembership(company_id=company.id, user_id=user.id,
                                    role="company_admin", status="pending")
    subscription = CompanySubscription(company_id=company.id, plan_id=plan.id, status="pending",
        billing_interval=interval, stripe_price_id_override=price_id, custom_amount_cents=amount)
    raw_token = secrets.token_urlsafe(32)
    token = EmailActionToken(user_id=user.id, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        purpose="enterprise_setup", expires_at=now_utc() + timedelta(hours=48))
    db.session.add_all([membership, subscription, token])
    db.session.commit()

    setup_url = absolute_app_url("enterprise_setup", token=raw_token)
    try:
        delivered = send_account_email(email, "Set up your Construct IV Enterprise account",
            f"Hello {contact},\n\nYour Construct IV workspace is ready for setup. Create your password and continue to secure subscription checkout here: {setup_url}\n\nThe workspace remains inactive until Stripe confirms payment.")
    except Exception:
        app.logger.exception("Manual Enterprise invitation email delivery failed")
        delivered = False
    if delivered:
        flash("Enterprise workspace created. A secure setup invitation was sent; access stays pending until verified Stripe payment.", "success")
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_enterprise_setup_url"] = setup_url
        flash("Workspace created pending billing. SMTP is not configured; use the local setup link shown above.", "success")
    else:
        flash("Workspace created pending billing, but the setup email could not be delivered. Configure SMTP and resend from the request record.", "error")
    audit("company_manually_provisioned", "company", company.id,
          "Enterprise workspace created; pending verified Stripe confirmation")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/companies/<int:company_id>/resend-invite")
@platform_owner_required
def resend_manual_company_invite(company_id):
    company = db.get_or_404(Company, company_id)
    membership = CompanyMembership.query.filter_by(company_id=company.id, role="company_admin").first()
    user = membership.user if membership else None
    subscription = (CompanySubscription.query.filter_by(company_id=company.id)
                    .order_by(CompanySubscription.id.desc()).first())
    if (not user or user.email_verified or company.status != "pending" or not subscription
            or subscription.status != "pending" or not subscription.plan or not subscription.plan.custom_pricing):
        flash("No pending setup invitation is available for this workspace.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    EmailActionToken.query.filter_by(user_id=user.id, purpose="enterprise_setup", used_at=None).update(
        {"used_at": now_utc()}, synchronize_session=False)
    raw_token = secrets.token_urlsafe(32)
    db.session.add(EmailActionToken(user_id=user.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), purpose="enterprise_setup",
        expires_at=now_utc() + timedelta(hours=48)))
    db.session.commit()
    setup_url = absolute_app_url("enterprise_setup", token=raw_token)
    try:
        delivered = send_account_email(user.email, "Set up your Construct IV Enterprise account",
            f"Hello {user.name},\n\nCreate your password and continue to secure subscription checkout here: {setup_url}\n\nThe workspace remains inactive until Stripe confirms payment.")
    except Exception:
        app.logger.exception("Manual Enterprise setup invitation resend failed")
        delivered = False
    if delivered:
        flash("A new secure setup invitation was sent.", "success")
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_enterprise_setup_url"] = setup_url
        flash("SMTP is not configured. Use the local setup link shown above.", "success")
    else:
        flash("The invitation could not be delivered. Check SMTP settings and try again.", "error")
    audit("company_setup_invite_resent", "company", company.id, "Manual Enterprise setup invitation resent")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/companies/<int:company_id>/status")
@platform_owner_required
def update_company_status(company_id):
    company = db.get_or_404(Company, company_id)
    target = request.form.get("status")
    if target not in {"suspended", "active"}:
        abort(400)
    subscription = CompanySubscription.query.filter_by(company_id=company.id).order_by(CompanySubscription.id.desc()).first()
    if target == "active" and (not subscription or subscription.status not in {"active", "trialing"}):
        flash("The company cannot be reactivated without a verified active subscription.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    company.status = target
    for membership in CompanyMembership.query.filter_by(company_id=company.id).all():
        membership.user.active = target == "active" and membership.user.email_verified
        membership.status = target if target == "suspended" else "active"
    db.session.commit()
    audit("company_status_updated", "company", company.id, target)
    flash(f"{company.name} is {target}.", "success")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/companies/<int:company_id>/limits")
@platform_owner_required
def update_company_limits(company_id):
    company = db.get_or_404(Company, company_id)

    def optional_positive_int(name, maximum=2_147_483_647):
        raw = request.form.get(name, "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
            return value if 1 <= value <= maximum else False
        except ValueError:
            return False

    project_limit = optional_positive_int("project_limit")
    employee_limit = optional_positive_int("employee_limit")
    storage_gb = optional_positive_int("storage_gb", 2_097_151)
    if False in (project_limit, employee_limit, storage_gb):
        flash("Enter positive whole-number limits, or leave a field blank to use its plan allowance.", "error")
        return redirect(url_for("platform_owner_dashboard"))

    company.project_limit = project_limit
    company.employee_limit = employee_limit
    company.storage_limit_override_bytes = storage_gb * 1024**3 if storage_gb is not None else None
    db.session.commit()
    audit("company_limits_updated", "company", company.id,
          f"Project limit={project_limit or 'plan default'}, employee limit={employee_limit or 'plan default'}, "
          f"storage override={storage_gb if storage_gb is not None else 'plan default'} GB")
    flash(f"Limits updated for {company.name}.", "success")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/enterprise/<int:request_id>/status")
@platform_owner_required
def update_enterprise_request_status(request_id):
    item = db.get_or_404(EnterpriseRequest, request_id)
    target = request.form.get("status")
    if target not in {"new", "reviewing", "rejected"}:
        abort(400)
    if item.company_id and target == "rejected":
        flash("This request already has a pending customer workspace; suspend it from Companies if needed.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    item.status = target
    db.session.commit()
    audit("enterprise_request_reviewed", "enterprise_request", item.id, target)
    flash("Enterprise request status updated.", "success")
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/enterprise/<int:request_id>/resend")
@platform_owner_required
def resend_enterprise_setup(request_id):
    item = db.get_or_404(EnterpriseRequest, request_id)
    user = db.session.get(User, item.admin_user_id) if item.admin_user_id else None
    if not item.company_id or not user or user.email_verified:
        flash("There is no pending Enterprise setup invitation to resend.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    EmailActionToken.query.filter_by(user_id=user.id, purpose="enterprise_setup", used_at=None).update(
        {"used_at": now_utc()}, synchronize_session=False)
    raw_token = secrets.token_urlsafe(32)
    db.session.add(EmailActionToken(user_id=user.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), purpose="enterprise_setup",
        expires_at=now_utc() + timedelta(hours=48)))
    setup_url = absolute_app_url("enterprise_setup", token=raw_token)
    db.session.commit()
    try:
        delivered = send_account_email(user.email, "Set up your Construct IV Enterprise account",
            f"Create your password and continue to secure checkout: {setup_url}")
    except Exception:
        app.logger.exception("Enterprise setup resend failed")
        delivered = False
    if delivered:
        item.status = "approved_invited"
        flash("A new one-time setup invitation was sent.", "success")
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_enterprise_setup_url"] = setup_url
        flash("SMTP is not configured. Use the local setup link below.", "success")
    else:
        item.status = "approved_email_pending"
        flash("Invitation email could not be delivered. Check SMTP configuration.", "error")
    db.session.commit()
    return redirect(url_for("platform_owner_dashboard"))


@app.post("/owner/enterprise/<int:request_id>/provision")
@platform_owner_required
def provision_enterprise_request(request_id):
    item = db.get_or_404(EnterpriseRequest, request_id)
    if item.company_id:
        flash("This Enterprise request already has a customer workspace.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    if item.status == "rejected":
        flash("A rejected request cannot be provisioned. Change its status first.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    email = item.email.strip().lower()
    if User.query.filter(or_(User.username == email, User.email == email)).first():
        flash("That contact email already has an account. Review the existing workspace before provisioning.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    enterprise_plan = SubscriptionPlan.query.filter_by(name="Enterprise", custom_pricing=True).first()
    if not enterprise_plan:
        flash("The custom Enterprise plan has not been initialized.", "error")
        return redirect(url_for("platform_owner_dashboard"))
    price_id = request.form.get("stripe_price_id", "").strip()[:160]
    interval = request.form.get("billing_interval", "monthly")
    def positive_int(key, maximum=2_147_483_647):
        try:
            value = int(request.form.get(key, ""))
            return value if 1 <= value <= maximum else None
        except (TypeError, ValueError):
            return None
    amount = positive_int("custom_amount_cents")
    projects = positive_int("project_limit")
    employees = positive_int("employee_limit")
    storage_gb = positive_int("storage_gb")
    if (not re.fullmatch(r"price_[A-Za-z0-9]+", price_id) or interval not in {"monthly", "annual"}
            or None in (amount, projects, employees, storage_gb)):
        flash("Enter a Stripe Price ID, positive custom amount, project and employee limits, storage allowance, and billing interval.", "error")
        return redirect(url_for("platform_owner_dashboard"))

    company = Company(name=item.company_name, status="pending", storage_limit_bytes=storage_gb * 1024**3,
                      project_limit=projects, employee_limit=employees)
    user = User(username=email, email=email, email_verified=False, name=item.contact_name,
                active=False, is_admin=True)
    user.set_password(secrets.token_urlsafe(40))
    db.session.add_all([company, user])
    db.session.flush()
    membership = CompanyMembership(company_id=company.id, user_id=user.id, role="company_admin", status="pending")
    subscription = CompanySubscription(company_id=company.id, plan_id=enterprise_plan.id, status="pending",
                                       billing_interval=interval, stripe_price_id_override=price_id,
                                       custom_amount_cents=amount)
    db.session.add_all([membership, subscription])
    raw_token = secrets.token_urlsafe(32)
    db.session.add(EmailActionToken(user_id=user.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), purpose="enterprise_setup",
        expires_at=now_utc() + timedelta(hours=48)))
    item.company_id, item.admin_user_id, item.status = company.id, user.id, "approved_pending_setup"
    db.session.commit()

    setup_url = absolute_app_url("enterprise_setup", token=raw_token)
    try:
        delivered = send_account_email(email, "Set up your Construct IV Enterprise account",
            f"Hello {item.contact_name},\n\nYour Enterprise request has been approved. Create your account password and continue to secure subscription checkout here: {setup_url}\n\nThe workspace will remain inactive until Stripe confirms payment.")
    except Exception:
        app.logger.exception("Enterprise invitation email delivery failed")
        delivered = False
    if delivered:
        item.status = "approved_invited"
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_enterprise_setup_url"] = setup_url
        flash("Workspace created in pending state. Use this local setup link to continue.", "success")
    else:
        item.status = "approved_email_pending"
        flash("Workspace is pending, but the invitation email could not be delivered. Configure SMTP and resend the invite.", "error")
    db.session.commit()
    audit("enterprise_workspace_provisioned", "enterprise_request", item.id,
          f"Approved {item.company_name}; subscription is pending Stripe confirmation")
    return redirect(url_for("platform_owner_dashboard"))


@app.get("/enterprise/setup/<token>")
@app.post("/enterprise/setup/<token>")
def enterprise_setup(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    action = EmailActionToken.query.filter_by(token_hash=digest, purpose="enterprise_setup", used_at=None).first()
    if not action or action.expires_at < now_utc():
        flash("That Enterprise setup link is invalid or expired. Ask your company contact for a new invite.", "error")
        return redirect(url_for("login"))
    user = db.session.get(User, action.user_id)
    membership = CompanyMembership.query.filter_by(user_id=action.user_id, role="company_admin").first()
    if not user or not membership:
        abort(404)
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12 or password != confirm:
            flash("Use matching passwords with at least 12 characters.", "error")
        else:
            user.set_password(password)
            user.email_verified = True
            action.used_at = now_utc()
            membership.status = "verified_pending_billing"
            subscription = (CompanySubscription.query.filter_by(company_id=membership.company_id)
                            .order_by(CompanySubscription.id.desc()).first())
            item = EnterpriseRequest.query.filter_by(admin_user_id=user.id).first()
            if item:
                item.status = "awaiting_payment"
            if not subscription:
                abort(409)
            session["pending_checkout_subscription_id"] = subscription.id
            db.session.commit()
            audit("enterprise_admin_setup", "company", membership.company_id, "Password set; awaiting verified billing")
            return redirect(url_for("subscription_checkout", subscription_id=subscription.id))
    return render_template("enterprise_setup.html", email=user.email)


@app.post("/logout")
@login_required
def logout():
    audit("logout", "user", current_user.id, "User signed out")
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    features = {key: company_feature_enabled(key) for key in PLAN_FEATURES}
    selected_date = parse_date(request.args.get("date"))
    project = request.args.get("project", "").strip()[:160]
    direction = request.args.get("direction", "").strip()
    tag = request.args.get("tag", "").strip()[:40]
    q = request.args.get("q", "").strip()[: app.config["MAX_SEARCH_LENGTH"]]

    available_dates = ([r[0] for r in Flight.query.with_entities(Flight.date).distinct().order_by(Flight.date.desc()).all()]
                       if features["flight_scans"] else [])
    if not selected_date:
        selected_date = available_dates[0] if available_dates else now_utc().date()

    query = Flight.query.filter(Flight.date == selected_date) if features["flight_scans"] else Flight.query.filter(False)
    if project:
        query = query.filter(Flight.project == project)
    if direction in DIRECTIONS:
        query = query.filter(Flight.direction == direction)
    if tag:
        query = query.filter(Flight.tags.ilike(f'%"{tag}"%'))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Flight.flight_id.ilike(like), Flight.title.ilike(like), Flight.description.ilike(like), Flight.area.ilike(like), Flight.notes.ilike(like), Flight.tags.ilike(like)))
    flights = query.order_by(Flight.direction, Flight.uploaded_at.desc()).all()

    docs_query = ProjectFile.query if features["documents"] else ProjectFile.query.filter(False)
    # Date is a flight-library filter. A documents-only workspace has no flight
    # date selector, so keep its document archive browsable across all dates.
    if features["documents"] and features["flight_scans"]:
        docs_query = docs_query.filter(ProjectFile.date == selected_date)
    if project:
        docs_query = docs_query.filter(ProjectFile.project == project)
    if q:
        like = f"%{q}%"
        docs_query = docs_query.filter(or_(ProjectFile.name.ilike(like), ProjectFile.notes.ilike(like), ProjectFile.tags.ilike(like)))
    docs = docs_query.order_by(ProjectFile.uploaded_at.desc()).all()

    projects = [r[0] for r in Flight.query.with_entities(Flight.project).distinct().order_by(Flight.project).all() if r[0]]
    membership = current_membership()
    company = membership.company if membership else None
    subscription = (CompanySubscription.query.filter_by(company_id=company.id)
                    .order_by(CompanySubscription.id.desc()).first() if company else None)
    storage_limit = company_storage_limit_bytes(company, subscription)
    storage_used = company_storage_bytes(current_company_id()) if current_company_id() else 0
    storage_percent = min(100, storage_used * 100 / storage_limit) if storage_limit else 0
    storage_warning_level = ("critical" if storage_percent >= 90 else
                             "warning" if storage_percent >= 80 else "")
    latest_refund_request = (AnnualRefundRequest.query.filter_by(
        company_id=company.id, subscription_id=subscription.id)
        .order_by(AnnualRefundRequest.requested_at.desc()).first()
        if company and subscription and current_user.is_admin else None)
    billing_summary = None
    if current_user.is_admin and subscription:
        plan = subscription.plan
        amount_cents = subscription.custom_amount_cents
        if amount_cents is None and plan:
            amount_cents = plan.annual_amount_cents if subscription.billing_interval == "annual" else plan.monthly_amount_cents
        billing_grace_end = None
        if subscription.status == "past_due":
            try:
                grace_days = max(0, min(30, int(os.environ.get("BILLING_GRACE_DAYS", "7"))))
            except ValueError:
                grace_days = 7
            billing_grace_end = (subscription.past_due_since or subscription.updated_at) + timedelta(days=grace_days)
        billing_summary = {
            "plan": plan.name if plan else "Custom workspace",
            "interval": subscription.billing_interval or "monthly",
            "price": f"${amount_cents / 100:,.2f} / {subscription.billing_interval or 'month'}" if amount_cents is not None else "Price configured by Construct IV",
            "status": ("Canceling" if subscription.cancel_at_period_end and subscription.status in {"active", "trialing"}
                       else subscription.status.replace("_", " ").title()),
            "period_end": subscription.current_period_end,
            "period_start": subscription.current_period_start,
            "grace_end": billing_grace_end,
            "can_manage": bool(subscription.stripe_customer_id),
            "can_cancel": bool(subscription.stripe_subscription_id and
                                subscription.status in {"active", "trialing", "past_due"} and
                                not subscription.cancel_at_period_end),
            "can_reverse_cancel": bool(subscription.stripe_subscription_id and
                                        subscription.cancel_at_period_end and subscription.current_period_end and
                                        subscription.current_period_end > now_utc()),
            "can_request_refund": bool(subscription.billing_interval == "annual" and
                                        subscription.stripe_subscription_id and
                                        subscription.status in {"active", "trialing", "past_due"} and
                                        (not latest_refund_request or latest_refund_request.status not in OPEN_REFUND_STATUSES)),
        }
    return render_template(
        "dashboard.html", flights=flights, docs=docs, dates=available_dates,
        selected_date=selected_date, directions=DIRECTIONS, projects=projects,
        selected_project=project, selected_direction=direction, selected_tag=tag, search_query=q,
        storage_used=storage_used, storage_limit=storage_limit,
        storage_percent=storage_percent, storage_warning_level=storage_warning_level,
        latest_refund_request=latest_refund_request, billing_summary=billing_summary,
        features=features,
        can_upload=bool(features["flight_scans"] and (current_user.is_admin or (membership and ProjectAssignment.query.filter_by(
            company_id=membership.company_id, user_id=current_user.id).first()))),
    )


@app.get("/flight/<int:flight_id>")
@login_required
def flight_view(flight_id):
    flight = db.get_or_404(Flight, flight_id)
    if not company_feature_enabled("flight_scans", flight.company_id):
        abort(403)
    flight.view_count = (flight.view_count or 0) + 1
    db.session.commit()
    audit("view", "flight", flight.id, f"Viewed {flight.flight_id}")
    return render_template("flight.html", flight=flight)


@app.get("/files/<path:filename>")
def protected_file(filename):
    # Never allow traversal; only serve files in our upload directory. Public
    # viewers may access files only from the project that their code unlocks.
    safe = filename if is_r2_filename(filename) else Path(filename).name
    viewer_code = session.get("viewer_code")
    if current_user.is_authenticated:
        flight_record = Flight.query.filter_by(filename=safe).first()
        document_record = ProjectFile.query.filter_by(filename=safe).first()
        map_record = ProjectSiteMap.query.filter_by(filename=safe).first()
        photo_record = ProjectPhotoReference.query.filter_by(filename=safe).first()
        pinned_photo_record = ProjectPinnedPhoto.query.filter_by(filename=safe).first()
        if not any((flight_record, document_record, map_record, photo_record, pinned_photo_record)):
            abort(404)
        if flight_record and not company_feature_enabled("flight_scans", flight_record.company_id):
            abort(403)
        if document_record and not company_feature_enabled("documents", document_record.company_id):
            abort(403)
        if map_record and not company_feature_enabled("site_maps", map_record.company_id):
            abort(403)
        if photo_record and not company_feature_enabled("photos", photo_record.company_id):
            abort(403)
        if pinned_photo_record and not company_feature_enabled("photos", pinned_photo_record.company_id):
            abort(403)
    else:
        g.allow_public_viewer_scope = True
        access = ProjectAccess.query.filter_by(code=viewer_code).first() if viewer_code else None
        if not viewer_access_active(access):
            abort(403)
        flight_record = Flight.query.filter_by(filename=safe, project=access.project, company_id=access.company_id).first()
        document_record = ProjectFile.query.filter_by(filename=safe, project=access.project, company_id=access.company_id).first()
        map_record = ProjectSiteMap.query.filter_by(filename=safe, project=access.project, company_id=access.company_id).first()
        photo_record = ProjectPhotoReference.query.filter_by(filename=safe, project=access.project, company_id=access.company_id).first()
        pinned_photo_record = ProjectPinnedPhoto.query.filter_by(filename=safe, project=access.project,
                                                                  company_id=access.company_id).first()
        if not any((flight_record, document_record, map_record, photo_record, pinned_photo_record)):
            abort(403)
        if flight_record and not company_feature_enabled("flight_scans", access.company_id):
            abort(403)
        if document_record and not access.allow_documents:
            abort(403)
        if document_record and not company_feature_enabled("documents", access.company_id):
            abort(403)
        if map_record and not company_feature_enabled("site_maps", access.company_id):
            abort(403)
        if photo_record and not company_feature_enabled("photos", access.company_id):
            abort(403)
        if pinned_photo_record and not company_feature_enabled("photos", access.company_id):
            abort(403)
    path = UPLOAD_DIR / safe
    if not is_r2_filename(safe) and (not path.exists() or not path.is_file()):
        abort(404)
    download = request.args.get("download") == "1"
    flight = flight_record
    doc = document_record
    if download:
        if flight:
            flight.download_count = (flight.download_count or 0) + 1
            resource = ("flight", flight.id, flight.flight_id)
        elif doc:
            doc.download_count = (doc.download_count or 0) + 1
            resource = ("document", doc.id, doc.name)
        else:
            resource = ("file", safe, safe)
        db.session.commit()
        audit("download", resource[0], resource[1], resource[2])
    else:
        audit("stream", "file", safe, "Authenticated file access")
    content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    if is_r2_filename(safe):
        try:
            target = R2Storage().signed_download(
                r2_object_key(safe), flight.original_filename if flight else (doc.original_filename or doc.name) if doc else safe,
                content_type, download,
            )
        except Exception:
            app.logger.exception("Could not create authorized R2 read URL")
            abort(503)
        return redirect(target, code=302)
    return send_from_directory(str(UPLOAD_DIR), safe, as_attachment=download, conditional=True, mimetype=content_type)


@app.route("/admin", endpoint="admin")
@app.route("/admin/site-capture", endpoint="site_capture")
@admin_required
def admin():
    if request.endpoint == "site_capture" and not company_feature_enabled("site_maps"):
        abort(403)
    member_ids = [row.user_id for row in CompanyMembership.query.filter_by(company_id=current_company_id()).all()]
    users = User.query.filter(User.id.in_(member_ids)).order_by(User.created_at.desc()).all() if member_ids else []
    assignments = {}
    for row in ProjectAssignment.query.filter_by(company_id=current_company_id()).all():
        assignments.setdefault(row.user_id, set()).add(row.project)
    flights = Flight.query.order_by(Flight.date.desc(), Flight.uploaded_at.desc()).limit(250).all()
    docs = ProjectFile.query.order_by(ProjectFile.date.desc(), ProjectFile.uploaded_at.desc()).limit(250).all()
    audit_logs = AuditLog.query.filter(AuditLog.user_id.in_(member_ids)).order_by(AuditLog.created_at.desc()).limit(100).all() if member_ids else []
    project_names = company_project_names(current_company_id())
    access_rows = {row.project: row for row in ProjectAccess.query.all()}
    selected_map_project = request.args.get("map_project", "").strip()
    if selected_map_project not in project_names:
        selected_map_project = "Main Project" if "Main Project" in project_names else (sorted(project_names, key=str.casefold)[0] if project_names else "")
    selected_site_map = ProjectSiteMap.query.filter_by(project=selected_map_project).first() if selected_map_project else None
    selected_photo_reference = ProjectPhotoReference.query.filter_by(project=selected_map_project).first() if selected_map_project else None
    selected_photo_annotations = ProjectPhotoAnnotation.query.filter_by(project=selected_map_project).first() if selected_map_project else None
    try:
        selected_photo_annotation_data = json.loads(selected_photo_annotations.data) if selected_photo_annotations else {"areas": [], "pins": []}
    except (TypeError, json.JSONDecodeError):
        selected_photo_annotation_data = {"areas": [], "pins": []}
    selected_photo_asset_data = [{"pin_id": photo.pin_id,
                                  "url": url_for("protected_file", filename=photo.filename),
                                  "name": photo.original_filename}
                                 for photo in ProjectPinnedPhoto.query.filter_by(
                                     project=selected_map_project).order_by(ProjectPinnedPhoto.id).all()] if selected_map_project else []
    selected_map_pins = ProjectMapPin.query.filter_by(project=selected_map_project).order_by(ProjectMapPin.id).all() if selected_map_project else []
    selected_site_setup = ProjectSiteSetup.query.filter_by(project=selected_map_project).first() if selected_map_project else None
    if not selected_site_setup and selected_map_project:
        selected_site_setup = ProjectSiteSetup(project=selected_map_project,
                                               module="image" if selected_site_map or selected_photo_reference else "map")
    latest_project_date = db.session.query(func.max(Flight.date)).filter_by(project=selected_map_project).scalar() if selected_map_project else None
    map_date = parse_date(request.args.get("map_date")) or latest_project_date or now_utc().date()
    map_paths = ProjectFlightPath.query.filter_by(project=selected_map_project, date=map_date).order_by(ProjectFlightPath.created_at).all() if selected_map_project else []
    map_project_flights = Flight.query.filter_by(project=selected_map_project, date=map_date).order_by(Flight.title).all() if selected_map_project else []
    template_name = "site_capture.html" if request.path in {"/admin/site-capture", "/admin/upload"} else "admin.html"
    return render_template(template_name, users=users, flights=flights, docs=docs, audit_logs=audit_logs,
                           project_names=sorted(project_names, key=str.casefold), access_rows=access_rows,
                           projects=sorted(project_names, key=str.casefold), directions=DIRECTIONS,
                           initial_view="uploads" if request.path == "/admin/upload" else "videos",
                           selected_map_project=selected_map_project, selected_site_map=selected_site_map,
                           selected_photo_reference=selected_photo_reference,
                           selected_photo_annotations=selected_photo_annotations,
                           selected_photo_annotation_data=selected_photo_annotation_data,
                           selected_photo_asset_data=selected_photo_asset_data,
                           selected_map_pins=selected_map_pins, map_directions=MAP_DIRECTIONS,
                           selected_site_setup=selected_site_setup, map_date=map_date,
                           map_paths=map_paths, map_project_flights=map_project_flights, assignments=assignments,
                           features={key: company_feature_enabled(key) for key in PLAN_FEATURES},
                           current_time=now_utc(),
                           r2_multipart_enabled=cloud_storage_enabled(),
                           local_employee_invite_url=session.pop("local_employee_invite_url", ""))


@app.get("/admin/fieldview-sandbox")
@admin_required
def fieldview_sandbox():
    """Open the standalone interactive site-capture sandbox."""
    return render_template("fieldview_sandbox.html")


@app.post("/admin/projects/photo-reference")
@admin_required
@company_feature_required("photos")
def upload_project_photo_reference():
    project = request.form.get("project", "").strip()[:160]
    photo = request.files.get("photo_reference")
    if not project or not photo or not photo.filename or extension(photo.filename) not in {"jpg", "jpeg", "png", "webp"}:
        flash("Choose a JPG, PNG, or WebP site image.", "error")
        return redirect(f"/admin/upload?map_project={quote(project)}#photos")
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(f"/admin/upload?map_project={quote(project)}#photos")
    stored = None
    try:
        reference = ProjectPhotoReference.query.filter_by(project=project).first()
        old_filename = reference.filename if reference else None
        stored = save_upload(photo, replacing_size=reference.file_size if reference else 0)
        filename, size, _checksum, original, reservation_id = stored
        if reference:
            reference.filename, reference.original_filename, reference.file_size, reference.uploaded_at = filename, original, size, now_utc()
        else:
            db.session.add(ProjectPhotoReference(project=project, filename=filename, original_filename=original, file_size=size))
        complete_storage_reservation(reservation_id)
        db.session.commit()
        if old_filename:
            delete_stored_file(old_filename)
        audit("upload_photo_reference", "project", project, f"Uploaded photo coverage reference {original}")
        flash("Photo coverage image uploaded.", "success")
    except Exception:
        db.session.rollback()
        if stored:
            delete_stored_file(stored[0])
            release_storage_reservation(stored[4])
        app.logger.exception("Photo reference upload failed")
        flash("Photo reference upload failed.", "error")
    return redirect(f"/admin/upload?map_project={quote(project)}#photos")


@app.post("/admin/projects/photo-annotations")
@upload_user_required
@company_feature_required("photos")
def save_project_photo_annotations():
    payload = request.get_json(silent=True) or {}
    project = str(payload.get("project", "")).strip()[:160]
    data = payload.get("annotations")
    if not project or not isinstance(data, dict) or not isinstance(data.get("areas"), list) or not isinstance(data.get("pins"), list):
        abort(400, "Invalid photo annotations")
    if not user_can_upload_project(project):
        abort(403)
    if not project_capacity_available(project):
        abort(403, "The project limit for this plan has been reached")
    if len(data["areas"]) > 300 or len(data["pins"]) > 1000:
        abort(400, "Too many annotations")
    clean = {"areas": [], "pins": []}
    for area in data["areas"]:
        if not isinstance(area, dict) or not isinstance(area.get("points"), list) or not 3 <= len(area["points"]) <= 500:
            abort(400, "Invalid coverage area")
        points = []
        for point in area["points"]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                abort(400, "Invalid coverage point")
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                abort(400, "Invalid coverage point")
            if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 1000 or not 0 <= y <= 620:
                abort(400, "Coverage point is outside the image")
            points.append([x, y])
        clean["areas"].append({"label": str(area.get("label", "Coverage area"))[:80], "points": points})
    pin_ids = set()
    for pin in data["pins"]:
        if not isinstance(pin, dict):
            abort(400, "Invalid photo pin")
        try:
            x, y = float(pin.get("x")), float(pin.get("y"))
        except (TypeError, ValueError):
            abort(400, "Invalid photo pin")
        if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 1000 or not 0 <= y <= 620:
            abort(400, "Photo pin is outside the image")
        pin_id = str(pin.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", pin_id) or pin_id in pin_ids:
            pin_id = secrets.token_urlsafe(18)
        pin_ids.add(pin_id)
        clean["pins"].append({
            "id": pin_id,
            "x": x,
            "y": y,
            "label": str(pin.get("label", "Site photo"))[:80],
            "area": str(pin.get("area", "Unassigned area"))[:80],
        })
    row = ProjectPhotoAnnotation.query.filter_by(project=project).first()
    orphaned_assets = []
    for asset in ProjectPinnedPhoto.query.filter_by(project=project).all():
        if asset.pin_id not in pin_ids:
            orphaned_assets.append(asset.filename)
            db.session.delete(asset)
    if row:
        row.data = json.dumps(clean)
    else:
        db.session.add(ProjectPhotoAnnotation(project=project, data=json.dumps(clean)))
    db.session.commit()
    for filename in orphaned_assets:
        delete_stored_file(filename)
    audit("save_photo_annotations", "project", project, f"Saved {len(clean['areas'])} areas and {len(clean['pins'])} photo pins")
    return jsonify({"ok": True, "annotations": clean})


@app.post("/admin/projects/photo-pin-photos")
@upload_user_required
@company_feature_required("photos")
def upload_photo_pin_images():
    project = request.form.get("project", "").strip()[:160]
    pin_id = request.form.get("pin_id", "").strip()
    if not project or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", pin_id):
        abort(400, "Choose a valid photo location")
    if not user_can_upload_project(project):
        abort(403)
    if not project_capacity_available(project):
        abort(403, "The project limit for this plan has been reached")
    annotation_row = ProjectPhotoAnnotation.query.filter_by(project=project).first()
    try:
        annotations = json.loads(annotation_row.data) if annotation_row else {}
    except (TypeError, json.JSONDecodeError):
        annotations = {}
    pins = annotations.get("pins", []) if isinstance(annotations, dict) else []
    if not any(isinstance(pin, dict) and pin.get("id") == pin_id for pin in pins):
        abort(404, "Photo location was not found")
    photos = [photo for photo in request.files.getlist("photos") if photo and photo.filename]
    if not photos or len(photos) > 20:
        abort(400, "Choose between 1 and 20 images for this location")
    max_photo_bytes = 25 * 1024 * 1024
    total_photo_bytes = 0
    stored_files = []
    rows = []
    try:
        for photo in photos:
            ext = extension(photo.filename)
            if ext not in {"jpg", "jpeg", "png", "webp"}:
                raise ValueError("Choose JPG, PNG, or WebP images.")
            stream = photo.stream
            position = stream.tell()
            header = stream.read(12)
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(position)
            valid_image = ((ext in {"jpg", "jpeg"} and header.startswith(b"\xff\xd8\xff"))
                           or (ext == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                           or (ext == "webp" and header.startswith(b"RIFF") and header[8:12] == b"WEBP"))
            if not valid_image:
                raise ValueError("One of the files does not match its JPG, PNG, or WebP format.")
            if size <= 0 or size > max_photo_bytes:
                raise ValueError("Each photo must be smaller than 25 MB.")
            total_photo_bytes += size
            if total_photo_bytes > 100 * 1024 * 1024:
                raise ValueError("Upload no more than 100 MB of photos at a time.")
            stored = save_upload(photo)
            stored_files.append(stored)
            filename, size, _checksum, original, reservation_id = stored
            rows.append(ProjectPinnedPhoto(project=project, pin_id=pin_id, filename=filename,
                                           original_filename=original, file_size=size))
        db.session.add_all(rows)
        for stored in stored_files:
            complete_storage_reservation(stored[4])
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        for filename, _size, _checksum, _original, reservation_id in stored_files:
            delete_stored_file(filename)
            release_storage_reservation(reservation_id)
        if isinstance(exc, ValueError):
            return jsonify({"error": str(exc)}), 400
        app.logger.exception("Photo pin image upload failed")
        return jsonify({"error": "Photo upload failed. Please try again."}), 500
    audit("upload_pinned_photos", "project", project, f"Uploaded {len(rows)} images to a mapped photo pin")
    assets = [{"url": url_for("protected_file", filename=row.filename), "name": row.original_filename}
              for row in rows]
    return jsonify({"ok": True, "photos": assets})


@app.post("/admin/projects/access")
@admin_required
@company_feature_required("viewer_links")
def create_project_access():
    project = request.form.get("project", "").strip()[:160]
    if not project:
        abort(400, "Project is required")
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(url_for("admin"))
    raw_expiration = request.form.get("expires_on", "").strip()
    expiration_date = parse_date(raw_expiration) if raw_expiration else None
    if raw_expiration and not expiration_date:
        flash("Choose a valid viewer-link expiration date.", "error")
        return redirect(url_for("admin") + "#project-access")
    access = ProjectAccess.query.filter_by(project=project).first()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(100):
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        if not ProjectAccess.query.filter_by(code=code).first():
            break
    else:
        abort(503, "Could not allocate a unique code")
    if access:
        access.code = code
        access.share_token = secrets.token_urlsafe(32)
        access.created_at = now_utc()
    else:
        access = ProjectAccess(project=project, code=code, share_token=secrets.token_urlsafe(32))
        db.session.add(access)
    access.expires_at = datetime.combine(expiration_date, datetime.max.time()) if expiration_date else None
    access.revoked_at = None
    access.allow_overview = request.form.get("allow_overview") == "on"
    access.allow_media = request.form.get("allow_media") == "on"
    access.allow_documents = request.form.get("allow_documents") == "on"
    db.session.commit()
    audit("project_access", "project", project, "Generated or rotated a four-character viewer code")
    flash(f"Viewer access updated for {project}.", "success")
    return redirect(url_for("admin") + "#project-access")


@app.post("/admin/projects/access/<int:access_id>/revoke")
@admin_required
@company_feature_required("viewer_links")
def revoke_project_access(access_id):
    access = db.get_or_404(ProjectAccess, access_id)
    if not access.revoked_at:
        access.revoked_at = now_utc()
        db.session.commit()
        audit("project_access_revoked", "project", access.project, "Revoked public viewer access")
    flash(f"Viewer access revoked for {access.project}.", "success")
    return redirect(url_for("admin") + "#project-access")


@app.route("/admin/upload", methods=["GET", "POST"])
@upload_user_required
@company_feature_required("flight_scans")
def upload():
    if request.method == "GET":
        project_names = {r[0] for r in db.session.query(Flight.project).distinct().all() if r[0]}
        project_names.update(r[0] for r in db.session.query(ProjectFile.project).distinct().all() if r[0])
        project_names.update(r[0] for r in db.session.query(ProjectAccess.project).distinct().all())
        return render_template("upload.html", directions=DIRECTIONS,
                               projects=sorted(project_names, key=str.casefold),
                               r2_multipart_enabled=cloud_storage_enabled(),
                               can_manage_capture=current_user.is_admin and company_feature_enabled("site_maps"))
    if request.method == "POST":
        date_value = request.form.get("date", "")
        flight_id = request.form.get("flight_id", "").strip()[:80]
        direction = request.form.get("direction", "").strip()
        title = request.form.get("title", "").strip()[:200]
        description = request.form.get("description", "").strip()
        duration = request.form.get("duration", "").strip()[:30]
        project = request.form.get("project", "Main Project").strip()[:160] or "Main Project"
        area = request.form.get("area", "").strip()[:160]
        notes = request.form.get("notes", "").strip()
        drone = request.form.get("drone", "").strip()[:120]
        pilot = request.form.get("pilot", "").strip()[:120]
        client = request.form.get("client", "").strip()[:160]
        captured_at = parse_datetime(request.form.get("captured_at", ""))
        tags = tags_from_request()
        video = request.files.get("video")

        date_value = parse_date(date_value)
        if not date_value:
            flash("Choose a valid date.", "error")
            return redirect(url_for("upload"))
        if not user_can_upload_project(project):
            abort(403)
        if not flight_id or not title or direction not in DIRECTIONS:
            flash("Flight ID, title, and direction are required.", "error")
            return redirect(url_for("upload"))
        if not video or not video.filename:
            flash("Select a video file.", "error")
            return redirect(url_for("upload"))
        if not ext_allowed(video.filename, ALLOWED_VIDEO):
            flash("Allowed video types: MP4, MOV, M4V, WEBM, AVI.", "error")
            return redirect(url_for("upload"))
        if Flight.query.filter_by(flight_id=flight_id).first():
            flash("That Flight ID already exists.", "error")
            return redirect(url_for("upload"))
        if not project_capacity_available(project):
            flash("The project limit for this plan has been reached.", "error")
            return redirect(url_for("upload"))

        stored = None
        try:
            stored = save_upload(video)
            filename, size, checksum, original, reservation_id = stored
            # Avoid duplicate physical uploads even if the flight ID differs.
            duplicate = Flight.query.filter_by(checksum_sha256=checksum).first()
            if duplicate:
                delete_stored_file(filename)
                release_storage_reservation(reservation_id)
                flash(f"This video already exists as Flight {duplicate.flight_id}.", "error")
                return redirect(url_for("upload"))

            flight = Flight(
                flight_id=flight_id, date=date_value, direction=direction, title=title,
                description=description, duration=duration, filename=filename,
                original_filename=original, file_size=size, uploaded_by=current_user.id,
                project=project, area=area, tags=json.dumps(tags), notes=notes,
                drone=drone, pilot=pilot, client=client, captured_at=captured_at,
                checksum_sha256=checksum, status="ready",
            )
            db.session.add(flight)
            complete_storage_reservation(reservation_id)
            db.session.commit()
            audit("upload", "flight", flight.id, f"Uploaded {original}; tags={tags}")
            flash(f"Flight {flight_id} uploaded successfully.", "success")
            return redirect(url_for("admin") if current_user.is_admin else url_for("dashboard"))
        except Exception as exc:
            db.session.rollback()
            if stored:
                delete_stored_file(stored[0])
                release_storage_reservation(stored[4])
            app.logger.exception("Flight upload failed")
            flash(f"Upload failed: {exc}", "error")
            return redirect(url_for("upload"))

    project_names = {r[0] for r in db.session.query(Flight.project).distinct().all() if r[0]}
    project_names.update(r[0] for r in db.session.query(ProjectFile.project).distinct().all() if r[0])
    project_names.update(r[0] for r in db.session.query(ProjectAccess.project).distinct().all())
    return render_template("upload.html", directions=DIRECTIONS,
                           projects=sorted(project_names, key=str.casefold),
                           r2_multipart_enabled=cloud_storage_enabled(),
                           can_manage_capture=current_user.is_admin and company_feature_enabled("site_maps"))


@app.post("/admin/upload-batch")
@upload_user_required
@company_feature_required("flight_scans")
def upload_batch():
    """Upload multiple flights for one project/date with a direction per video."""
    date_value = parse_date(request.form.get("date"))
    project = request.form.get("project", "Main Project").strip()[:160] or "Main Project"
    tags = tags_from_request()
    area = request.form.get("area", "").strip()[:160]
    videos = request.files.getlist("videos")
    directions = request.form.getlist("directions")
    titles = request.form.getlist("titles")
    if not date_value or not videos:
        flash("Choose a date and add at least one video.", "error")
        return redirect(url_for("upload"))
    if not user_can_upload_project(project):
        abort(403)
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(url_for("upload"))
    if len(directions) != len(videos) or any(d not in DIRECTIONS for d in directions):
        flash("Choose a valid direction for every selected video.", "error")
        return redirect(url_for("upload"))

    created = 0
    failed = 0
    for index, (video, direction) in enumerate(zip(videos, directions)):
        if not video or not video.filename or not ext_allowed(video.filename, ALLOWED_VIDEO):
            failed += 1
            continue
        stored = None
        try:
            stored = save_upload(video)
            filename, size, checksum, original, reservation_id = stored
            if Flight.query.filter_by(checksum_sha256=checksum).first():
                delete_stored_file(filename)
                release_storage_reservation(reservation_id)
                failed += 1
                continue
            stem = Path(original).stem
            base_id = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:50] or "FLIGHT"
            flight_id = f"{date_value:%Y%m%d}-{direction.replace('-', '')}-{base_id}"
            suffix = 1
            candidate = flight_id
            while Flight.query.filter_by(flight_id=candidate).first():
                suffix += 1
                candidate = f"{flight_id}-{suffix}"
            supplied_title = titles[index].strip()[:200] if index < len(titles) else ""
            flight = Flight(
                flight_id=candidate, date=date_value, direction=direction,
                title=supplied_title or stem[:200], description="", filename=filename,
                original_filename=original, file_size=size, uploaded_by=current_user.id,
                project=project, area=area, tags=json.dumps(tags), checksum_sha256=checksum,
                status="ready",
            )
            db.session.add(flight)
            complete_storage_reservation(reservation_id)
            db.session.commit()
            audit("upload_batch", "flight", flight.id, f"Uploaded {original}; direction={direction}; date={date_value}")
            created += 1
        except Exception:
            db.session.rollback()
            if stored:
                delete_stored_file(stored[0])
                release_storage_reservation(stored[4])
            failed += 1
    flash(f"Batch upload complete: {created} uploaded, {failed} skipped/failed.", "success" if created else "error")
    if request.form.get("return_to") == "uploads":
        return redirect(f"/admin/upload?map_project={quote(project)}&map_date={date_value.isoformat()}#uploads")
    return redirect(url_for("admin") + "#recent-flights" if current_user.is_admin else url_for("dashboard"))


R2_UPLOAD_PART_SIZE = 64 * 1024 * 1024
R2_UPLOAD_LIFETIME = timedelta(hours=24)


def upload_transfer_json(transfer, storage):
    parts = storage.list_multipart_parts(transfer.object_key, transfer.multipart_upload_id) if transfer.multipart_upload_id else []
    part_count = math.ceil(transfer.file_size / R2_UPLOAD_PART_SIZE)
    return {
        "uploadId": transfer.id, "status": transfer.status, "partSize": R2_UPLOAD_PART_SIZE,
        "partCount": part_count,
        "uploadedParts": [{"partNumber": part["PartNumber"], "size": part.get("Size", 0)} for part in parts],
        "completedFlightId": transfer.flight_id,
    }


def expire_company_upload_transfers(company_id):
    stale = UploadTransfer.query.filter(
        UploadTransfer.company_id == company_id,
        UploadTransfer.status.in_(("starting", "uploading")),
        UploadTransfer.expires_at <= now_utc(),
    ).all()
    if not stale:
        return
    storage = None
    for transfer in stale:
        if transfer.multipart_upload_id:
            try:
                storage = storage or R2Storage()
                storage.abort_multipart(transfer.object_key, transfer.multipart_upload_id)
            except Exception:
                app.logger.exception("Could not abort expired R2 multipart upload %s", transfer.id)
        transfer.status = "expired"
    db.session.commit()


@app.post("/admin/r2-upload/start")
@upload_user_required
@company_feature_required("flight_scans")
def start_r2_upload():
    if not cloud_storage_enabled():
        return jsonify({"error": "Resumable uploads are available when private R2 storage is enabled."}), 409
    payload = request.get_json(silent=True) or {}
    original = safe_name(str(payload.get("filename", "")))
    date_value = parse_date(payload.get("date"))
    project = str(payload.get("project", "")).strip()[:160]
    direction = str(payload.get("direction", "")).strip()[:40]
    title = str(payload.get("title", "")).strip()[:200] or Path(original).stem[:200]
    area = str(payload.get("area", "")).strip()[:160]
    try:
        file_size = int(payload.get("size", 0))
        last_modified = int(payload.get("lastModified", 0))
    except (ValueError, TypeError, OverflowError):
        return jsonify({"error": "The selected file has invalid size information."}), 400
    if (not date_value or not project or direction not in DIRECTIONS or not ext_allowed(original, ALLOWED_VIDEO)
            or file_size < 1 or file_size > app.config["MAX_CONTENT_LENGTH"] or last_modified < 0):
        return jsonify({"error": "Choose a valid project, date, direction, and supported video file."}), 400
    if not user_can_upload_project(project):
        abort(403)
    if not project_capacity_available(project):
        return jsonify({"error": "The project limit for this plan has been reached."}), 403
    tags = parse_tags(payload.get("tags", []))
    fingerprint = hashlib.sha256(f"{original}\0{file_size}\0{last_modified}".encode()).hexdigest()
    company_id = current_company_id()
    company = Company.query.filter_by(id=company_id).first() if company_id else None
    if not company or company.status != "active":
        return jsonify({"error": "This workspace is not active for uploads."}), 403
    expire_company_upload_transfers(company.id)
    company = lock_company_quota(company.id)
    if not company or company.status != "active":
        return jsonify({"error": "This workspace is not active for uploads."}), 403
    existing = (UploadTransfer.query.filter_by(company_id=company.id, user_id=current_user.id,
                                                fingerprint=fingerprint)
                .order_by(UploadTransfer.created_at.desc()).first())
    if existing and existing.status == "uploading" and existing.expires_at > now_utc():
        try:
            return jsonify(upload_transfer_json(existing, R2Storage()))
        except Exception:
            app.logger.exception("Could not inspect resumable upload %s", existing.id)
            return jsonify({"error": "The saved upload could not be resumed. Try again shortly."}), 503
    if (existing and existing.status == "completed" and existing.created_at > now_utc() - timedelta(minutes=10)
            and existing.flight_id):
        return jsonify({"status": "completed", "completedFlightId": existing.flight_id, "duplicate": True})

    limit = company_storage_limit_bytes(company)
    used = company_storage_bytes(company.id)
    reserved = (db.session.query(func.coalesce(func.sum(UploadTransfer.file_size), 0))
                .filter(UploadTransfer.company_id == company.id,
                        UploadTransfer.status.in_(("starting", "uploading")),
                        UploadTransfer.expires_at > now_utc()).scalar() or 0)
    local_reserved = (db.session.query(func.coalesce(func.sum(StorageReservation.file_size), 0))
                      .filter(StorageReservation.company_id == company.id,
                              StorageReservation.expires_at > now_utc()).scalar() or 0)
    if not limit or used + reserved + local_reserved + file_size > limit:
        return jsonify({"error": "This file would exceed the company storage allowance."}), 413

    upload_id = str(uuid4())
    object_key = f"companies/{company.id}/uploads/{upload_id}-{original}"
    content_type = mimetypes.guess_type(original)[0] or "video/mp4"
    if not content_type.startswith("video/"):
        return jsonify({"error": "The selected file is not recognized as video media."}), 400
    transfer = UploadTransfer(
        id=upload_id, company_id=company.id, user_id=current_user.id, fingerprint=fingerprint,
        project=project, date=date_value, direction=direction, title=title, area=area,
        tags=json.dumps(tags), original_filename=original, file_size=file_size,
        content_type=content_type, object_key=object_key, status="starting",
        expires_at=now_utc() + R2_UPLOAD_LIFETIME,
    )
    db.session.add(transfer)
    storage = None
    try:
        db.session.flush()
        storage = R2Storage()
        transfer.multipart_upload_id = storage.start_multipart(object_key, content_type)
        transfer.status = "uploading"
        db.session.commit()
        return jsonify(upload_transfer_json(transfer, storage))
    except Exception:
        db.session.rollback()
        if storage and transfer.multipart_upload_id:
            try:
                storage.abort_multipart(object_key, transfer.multipart_upload_id)
            except Exception:
                app.logger.exception("Could not clean up failed multipart upload initialization")
        app.logger.exception("Could not initialize resumable R2 upload")
        return jsonify({"error": "Could not start cloud upload. Check storage configuration and try again."}), 503


@app.post("/admin/r2-upload/<upload_id>/part-url")
@upload_user_required
@company_feature_required("flight_scans")
def r2_upload_part_url(upload_id):
    transfer = UploadTransfer.query.filter_by(id=upload_id, company_id=current_company_id(),
                                               user_id=current_user.id, status="uploading").first_or_404()
    if not user_can_upload_project(transfer.project, transfer.company_id):
        abort(403)
    if transfer.expires_at <= now_utc() or not transfer.multipart_upload_id:
        abort(410)
    try:
        part_number = int((request.get_json(silent=True) or {}).get("partNumber", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid multipart part number."}), 400
    part_count = math.ceil(transfer.file_size / R2_UPLOAD_PART_SIZE)
    if not 1 <= part_number <= part_count:
        return jsonify({"error": "Multipart part number is outside this upload."}), 400
    try:
        signed_url = R2Storage().signed_upload_part(
            transfer.object_key, transfer.multipart_upload_id, part_number,
        )
    except Exception:
        app.logger.exception("Could not sign R2 multipart part")
        return jsonify({"error": "Could not authorize this upload part."}), 503
    return jsonify({"url": signed_url})


@app.post("/admin/r2-upload/<upload_id>/complete")
@upload_user_required
@company_feature_required("flight_scans")
def complete_r2_upload(upload_id):
    transfer = UploadTransfer.query.filter_by(id=upload_id, company_id=current_company_id(),
                                               user_id=current_user.id).first_or_404()
    if not user_can_upload_project(transfer.project, transfer.company_id):
        abort(403)
    if transfer.status == "completed" and transfer.flight_id:
        return jsonify({"id": transfer.flight_id, "duplicate": True})
    if transfer.status != "uploading" or transfer.expires_at <= now_utc():
        return jsonify({"error": "This upload has expired. Start it again to continue."}), 410
    company = lock_company_quota(transfer.company_id)
    if not company or company.status != "active":
        return jsonify({"error": "This workspace is not active for uploads."}), 403
    storage = R2Storage()
    parts = sorted(storage.list_multipart_parts(transfer.object_key, transfer.multipart_upload_id),
                   key=lambda part: part["PartNumber"])
    expected_count = math.ceil(transfer.file_size / R2_UPLOAD_PART_SIZE)
    expected_parts = list(range(1, expected_count + 1))
    if [part.get("PartNumber") for part in parts] != expected_parts:
        return jsonify({"error": "Some upload parts are missing. Retry the interrupted parts."}), 409
    for index, part in enumerate(parts):
        expected_size = (R2_UPLOAD_PART_SIZE if index < expected_count - 1
                         else transfer.file_size - R2_UPLOAD_PART_SIZE * (expected_count - 1))
        if int(part.get("Size", -1)) != expected_size or not part.get("ETag"):
            return jsonify({"error": "An upload part did not match its expected size. Retry that part."}), 409
    try:
        object_key = transfer.object_key
        storage.complete_multipart(transfer.object_key, transfer.multipart_upload_id, parts)
        if storage.object_size(object_key) != transfer.file_size:
            raise ValueError("R2 object size does not match the reserved upload size")
        if not project_capacity_available(transfer.project):
            raise ValueError("The project limit for this plan has been reached")
        stem = Path(transfer.original_filename).stem
        base_id = f"{transfer.date:%Y%m%d}-{re.sub(r'[^A-Za-z0-9_-]+', '-', transfer.direction).strip('-')}-{re.sub(r'[^A-Za-z0-9_-]+', '-', stem).strip('-')[:40]}".strip("-") or "FLIGHT"
        flight_id = base_id[:80]
        suffix = 1
        candidate = flight_id
        while Flight.query.filter_by(flight_id=candidate).first():
            suffix += 1
            ending = f"-{suffix}"
            candidate = f"{flight_id[:80-len(ending)]}{ending}"
        flight = Flight(
            flight_id=candidate, date=transfer.date, direction=transfer.direction, title=transfer.title,
            description="", filename=R2_FILENAME_PREFIX + transfer.object_key,
            original_filename=transfer.original_filename, file_size=transfer.file_size,
            uploaded_by=current_user.id, project=transfer.project, area=transfer.area,
            tags=transfer.tags, checksum_sha256="", status="ready",
        )
        db.session.add(flight)
        db.session.flush()
        transfer.status = "completed"
        transfer.flight_id = flight.id
        db.session.commit()
        audit("upload_r2_multipart", "flight", flight.id, f"Uploaded {transfer.original_filename}; date={transfer.date}")
        return jsonify({"id": flight.id, "title": flight.title, "direction": flight.direction})
    except Exception:
        db.session.rollback()
        try:
            storage.delete(object_key)
        except Exception:
            app.logger.exception("Could not remove an unreferenced R2 upload object")
        # The multipart session is already consumed if completion succeeded.
        try:
            failed_transfer = UploadTransfer.query.filter_by(id=upload_id, company_id=current_company_id()).first()
            if failed_transfer:
                failed_transfer.status = "failed"
                db.session.commit()
        except Exception:
            db.session.rollback()
        app.logger.exception("Could not finalize resumable R2 upload %s", upload_id)
        return jsonify({"error": "The uploaded video could not be finalized. Retry the upload."}), 500


@app.post("/admin/r2-upload/<upload_id>/abort")
@upload_user_required
@company_feature_required("flight_scans")
def abort_r2_upload(upload_id):
    transfer = UploadTransfer.query.filter_by(id=upload_id, company_id=current_company_id(),
                                          user_id=current_user.id, status="uploading").first_or_404()
    if not user_can_upload_project(transfer.project, transfer.company_id):
        abort(403)
    try:
        if transfer.multipart_upload_id:
            R2Storage().abort_multipart(transfer.object_key, transfer.multipart_upload_id)
        transfer.status = "aborted"
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not abort R2 multipart upload %s", upload_id)
        return jsonify({"error": "Could not cancel the cloud upload."}), 503
    return jsonify({"ok": True})


@app.post("/admin/projects/route-video")
@admin_required
@company_feature_required("flight_scans")
def upload_route_video():
    project = request.form.get("project", "").strip()[:160]
    date_value = parse_date(request.form.get("date", ""))
    video = request.files.get("video")
    title = request.form.get("title", "").strip()[:200]
    direction = request.form.get("direction", "Custom path").strip()[:40]
    if not project or not date_value or not video or not video.filename or not ext_allowed(video.filename, ALLOWED_VIDEO):
        return jsonify({"error": "Choose a valid project date and an MP4, MOV, M4V, WEBM, or AVI video."}), 400
    if not project_capacity_available(project):
        return jsonify({"error": "The project limit for this plan has been reached."}), 403
    stored = None
    try:
        stored = save_upload(video)
        filename, size, checksum, original, reservation_id = stored
        duplicate = Flight.query.filter_by(checksum_sha256=checksum).first()
        if duplicate:
            delete_stored_file(filename)
            release_storage_reservation(reservation_id)
            if duplicate.project == project and duplicate.date == date_value:
                return jsonify({"id": duplicate.id, "title": duplicate.title, "direction": duplicate.direction, "duplicate": True})
            return jsonify({"error": "This video is already uploaded to another project or date."}), 409
        title = title or Path(original).stem[:200]
        base_id = f"{date_value:%Y%m%d}-ROUTE-{re.sub(r'[^A-Za-z0-9_-]+', '-', Path(original).stem).strip('-')[:40]}".strip("-") or "FLIGHT"
        flight_id, suffix = base_id, 1
        while Flight.query.filter_by(flight_id=flight_id).first():
            suffix += 1
            flight_id = f"{base_id}-{suffix}"
        flight = Flight(
            flight_id=flight_id, date=date_value, direction=direction, title=title,
            description="", filename=filename, original_filename=original,
            file_size=size, uploaded_by=current_user.id, project=project,
            area="", tags="[]", notes="", checksum_sha256=checksum, status="ready",
        )
        db.session.add(flight)
        complete_storage_reservation(reservation_id)
        db.session.commit()
        audit("upload_route_video", "flight", flight.id, f"Uploaded {original} for {project} on {date_value}")
        return jsonify({"id": flight.id, "title": flight.title, "direction": flight.direction})
    except Exception:
        db.session.rollback()
        if stored:
            delete_stored_file(stored[0])
            release_storage_reservation(stored[4])
        app.logger.exception("Route video upload failed")
        return jsonify({"error": "Video upload failed. Please try again."}), 500


@app.post("/admin/projects/map")
@admin_required
@company_feature_required("site_maps")
def upload_project_map():
    project = request.form.get("project", "").strip()[:160]
    map_file = request.files.get("map_file")
    module = request.form.get("module", "image").strip()
    allowed = {"tif", "tiff"} if module == "ortho" else {"jpg", "jpeg", "png", "webp"}
    if not project or module not in {"image", "ortho"} or not map_file or not map_file.filename or extension(map_file.filename) not in allowed:
        flash("Choose an image for Site image mode or a GeoTIFF for Orthomosaic mode.", "error")
        return redirect(f"/admin/upload?map_project={quote(project)}#videos")
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(f"/admin/upload?map_project={quote(project)}#videos")
    stored = None
    try:
        site_map = ProjectSiteMap.query.filter_by(project=project).first()
        old_filename = site_map.filename if site_map else None
        stored = save_upload(map_file, replacing_size=site_map.file_size if site_map else 0)
        filename, size, _checksum, original, reservation_id = stored
        if site_map:
            site_map.filename = filename
            site_map.original_filename = original
            site_map.file_size = size
            site_map.uploaded_at = now_utc()
        else:
            site_map = ProjectSiteMap(project=project, filename=filename, original_filename=original, file_size=size)
            db.session.add(site_map)
        setup = ProjectSiteSetup.query.filter_by(project=project).first()
        if not setup:
            setup = ProjectSiteSetup(project=project)
            db.session.add(setup)
        setup.module = module
        complete_storage_reservation(reservation_id)
        db.session.commit()
        if old_filename:
            delete_stored_file(old_filename)
        audit("upload_map", "project", project, f"Uploaded {module} reference {original}")
        flash(f"{('Orthomosaic' if module == 'ortho' else 'Site image')} uploaded successfully.", "success")
    except Exception:
        db.session.rollback()
        if stored:
            delete_stored_file(stored[0])
            release_storage_reservation(stored[4])
        app.logger.exception("Project map upload failed")
        flash("Map upload failed.", "error")
    return redirect(f"/admin/upload?map_project={quote(project)}#videos")


@app.post("/admin/projects/site-setup")
@admin_required
@company_feature_required("site_maps")
def save_project_site_setup():
    project = request.form.get("project", "").strip()[:160]
    module = request.form.get("module", "map").strip()
    address = request.form.get("address", "").strip()[:300]
    if not project or module not in {"map", "image", "ortho"}:
        abort(400, "Choose a project and a valid reference type")
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(f"/admin/upload?map_project={quote(project)}#videos")
    try:
        lat = float(request.form.get("center_lat", ""))
        lng = float(request.form.get("center_lng", ""))
        zoom = int(request.form.get("zoom", "16"))
        if not (math.isfinite(lat) and -90 <= lat <= 90 and math.isfinite(lng) and -180 <= lng <= 180):
            raise ValueError
    except (TypeError, ValueError):
        lat = lng = None
        zoom = 16
    setup = ProjectSiteSetup.query.filter_by(project=project).first()
    if not setup:
        setup = ProjectSiteSetup(project=project)
        db.session.add(setup)
    setup.module, setup.address = module, address
    if lat is not None:
        setup.center_lat, setup.center_lng = lat, lng
    setup.zoom = max(2, min(20, zoom))
    setup.locked = True
    db.session.commit()
    audit("site_setup", "project", project, f"Saved {module} site reference")
    flash("Project site setup saved.", "success")
    return redirect(f"/admin/upload?map_project={quote(project)}#videos")


@app.post("/admin/projects/paths")
@admin_required
@company_feature_required("site_maps")
def save_project_flight_path():
    project = request.form.get("project", "").strip()[:160]
    name = request.form.get("name", "").strip()[:160]
    label = request.form.get("label", "").strip()[:64]
    direction = request.form.get("direction", "Custom path").strip()[:40]
    date_value = parse_date(request.form.get("date", ""))
    raw_points = request.form.get("points", "[]")
    route_id = request.form.get("route_id", type=int)
    flight_id = request.form.get("flight_id", type=int)
    try:
        points = json.loads(raw_points)
    except (TypeError, json.JSONDecodeError):
        points = None
    if not project or not name or not date_value or not isinstance(points, list) or not 2 <= len(points) <= 1000:
        abort(400, "Add a route name, date, and at least two path points")
    if not project_capacity_available(project):
        abort(403, "The project limit for this plan has been reached")
    normalized = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            abort(400, "Invalid route point")
        try:
            lat, lng = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            abort(400, "Invalid route coordinates")
        if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            abort(400, "Route coordinates are outside the valid map range")
        normalized.append([lat, lng])
    linked_flight = None
    if flight_id:
        linked_flight = Flight.query.filter_by(id=flight_id, project=project, date=date_value).first()
        if not linked_flight:
            abort(400, "Choose a flight from this project and date")
    path = ProjectFlightPath.query.filter_by(id=route_id, project=project).first() if route_id else None
    if route_id and not path:
        abort(404)
    if not path:
        path = ProjectFlightPath(project=project, date=date_value, name=name)
        db.session.add(path)
    path.date, path.name, path.label, path.direction = date_value, name, label, direction
    path.points, path.flight_id = json.dumps(normalized), linked_flight.id if linked_flight else None
    try:
        center_lat = float(request.form.get("center_lat", ""))
        center_lng = float(request.form.get("center_lng", ""))
        site_zoom = int(request.form.get("zoom", "16"))
        if not (math.isfinite(center_lat) and -90 <= center_lat <= 90 and math.isfinite(center_lng) and -180 <= center_lng <= 180):
            raise ValueError
        setup = ProjectSiteSetup.query.filter_by(project=project).first()
        if not setup:
            setup = ProjectSiteSetup(project=project)
            db.session.add(setup)
        setup.center_lat, setup.center_lng = center_lat, center_lng
        setup.zoom = max(12, min(20, site_zoom))
        setup.address = request.form.get("address", "").strip()[:300]
        setup.module = request.form.get("module", setup.module or "map") if request.form.get("module") in {"map", "image", "ortho"} else setup.module
        setup.locked = True
    except (TypeError, ValueError):
        pass
    db.session.commit()
    audit("save_flight_path", "project", project, f"Saved {name} for {date_value}")
    flash("Flight path saved.", "success")
    return redirect(f"/admin/upload?map_project={quote(project)}&map_date={date_value.isoformat()}#{'overview' if request.form.get('return_to') == 'overview' else 'videos'}")


@app.post("/admin/projects/paths/<int:path_id>/delete")
@admin_required
@company_feature_required("site_maps")
def delete_project_flight_path(path_id):
    path = db.get_or_404(ProjectFlightPath, path_id)
    project, date_value = path.project, path.date
    db.session.delete(path)
    db.session.commit()
    audit("delete_flight_path", "project", project, f"Deleted route {path.name}")
    return redirect(f"/admin/upload?map_project={quote(project)}&map_date={date_value.isoformat()}#videos")


@app.post("/admin/projects/pins")
@admin_required
@company_feature_required("site_maps")
def create_project_pin():
    project = request.form.get("project", "").strip()[:160]
    label = request.form.get("label", "").strip()[:120]
    direction = request.form.get("direction", "").strip().upper()
    try:
        x = float(request.form.get("x_percent", ""))
        y = float(request.form.get("y_percent", ""))
    except (TypeError, ValueError):
        abort(400, "Click a location on the map first")
    if not ProjectSiteMap.query.filter_by(project=project).first():
        abort(400, "Upload a site map before adding pins")
    if direction not in MAP_DIRECTIONS or not (0 <= x <= 100 and 0 <= y <= 100):
        abort(400, "Pin direction or map coordinates are invalid")
    db.session.add(ProjectMapPin(project=project, label=label or direction, direction=direction,
                                 x_percent=x, y_percent=y))
    db.session.commit()
    audit("add_map_pin", "project", project, f"Added {direction} pin at {x:.1f}%, {y:.1f}%")
    return redirect(url_for("site_capture", map_project=project) + "#videos")


@app.post("/admin/projects/pins/<int:pin_id>/delete")
@admin_required
@company_feature_required("site_maps")
def delete_project_pin(pin_id):
    pin = db.get_or_404(ProjectMapPin, pin_id)
    project = pin.project
    db.session.delete(pin)
    db.session.commit()
    audit("delete_map_pin", "project", project, f"Removed {pin.direction} pin")
    return redirect(url_for("site_capture", map_project=project) + "#videos")


@app.post("/admin/document")
@admin_required
@company_feature_required("documents")
def upload_document():
    date_value = parse_date(request.form.get("date"))
    name = request.form.get("name", "").strip()[:255]
    project = request.form.get("project", "Main Project").strip()[:160] or "Main Project"
    notes = request.form.get("notes", "").strip()
    tags = tags_from_request()
    file = request.files.get("file")
    if not date_value:
        flash("Choose a valid date.", "error")
        return redirect(url_for("admin"))
    if not name or not file or not file.filename:
        flash("Document name and file are required.", "error")
        return redirect(url_for("admin"))
    if not project_capacity_available(project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(url_for("admin"))
    if not ext_allowed(file.filename, ALLOWED_DOCS):
        flash("That document type is not allowed.", "error")
        return redirect(url_for("admin"))
    stored = None
    try:
        stored = save_upload(file)
        filename, size, checksum, original, reservation_id = stored
        if ProjectFile.query.filter_by(checksum_sha256=checksum).first():
            delete_stored_file(filename)
            release_storage_reservation(reservation_id)
            flash("That exact document already exists.", "error")
            return redirect(url_for("admin"))
        p = ProjectFile(
            date=date_value, name=name, filename=filename, category=extension(original).upper() or "Document",
            uploaded_by=current_user.id, project=project, tags=json.dumps(tags), notes=notes,
            original_filename=original, file_size=size, checksum_sha256=checksum,
        )
        db.session.add(p)
        complete_storage_reservation(reservation_id)
        db.session.commit()
        audit("upload", "document", p.id, f"Uploaded {original}; tags={tags}")
        flash("Document uploaded.", "success")
    except Exception as exc:
        db.session.rollback()
        if stored:
            delete_stored_file(stored[0])
            release_storage_reservation(stored[4])
        app.logger.exception("Document upload failed")
        flash(f"Document upload failed: {exc}", "error")
    return redirect(url_for("admin"))


@app.post("/admin/flight/<int:flight_id>/edit")
@admin_required
def edit_flight(flight_id):
    flight = db.get_or_404(Flight, flight_id)
    flight.title = request.form.get("title", flight.title).strip()[:200] or flight.title
    flight.description = request.form.get("description", flight.description).strip()
    target_project = request.form.get("project", flight.project).strip()[:160] or "Main Project"
    if target_project != flight.project and not project_capacity_available(target_project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(url_for("admin"))
    flight.project = target_project
    flight.area = request.form.get("area", flight.area).strip()[:160]
    flight.direction = request.form.get("direction", flight.direction).strip()
    if flight.direction not in DIRECTIONS:
        abort(400, "Invalid direction")
    flight.tags = json.dumps(tags_from_request())
    flight.notes = request.form.get("notes", flight.notes).strip()
    flight.drone = request.form.get("drone", flight.drone).strip()[:120]
    flight.pilot = request.form.get("pilot", flight.pilot).strip()[:120]
    flight.client = request.form.get("client", flight.client).strip()[:160]
    captured = parse_datetime(request.form.get("captured_at", ""))
    if captured:
        flight.captured_at = captured
    db.session.commit()
    audit("edit", "flight", flight.id, f"Updated metadata; tags={flight.tag_list}")
    flash("Flight metadata updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/document/<int:doc_id>/edit")
@admin_required
@company_feature_required("documents")
def edit_document(doc_id):
    doc = db.get_or_404(ProjectFile, doc_id)
    doc.name = request.form.get("name", doc.name).strip()[:255] or doc.name
    target_project = request.form.get("project", doc.project).strip()[:160] or "Main Project"
    if target_project != doc.project and not project_capacity_available(target_project):
        flash("The project limit for this plan has been reached.", "error")
        return redirect(url_for("admin"))
    doc.project = target_project
    doc.tags = json.dumps(tags_from_request())
    doc.notes = request.form.get("notes", doc.notes).strip()
    db.session.commit()
    audit("edit", "document", doc.id, f"Updated metadata; tags={doc.tag_list}")
    flash("Document metadata updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/create")
@admin_required
@company_feature_required("employee_accounts")
def create_user():
    email = request.form.get("email", "").strip().lower()[:254]
    name = request.form.get("name", "").strip()
    is_admin = request.form.get("is_admin") == "on"
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not name:
        flash("Enter a valid work email and full name.", "error")
        return redirect(url_for("admin"))
    if User.query.filter(or_(User.username == email, User.email == email)).first():
        flash("That email already has an account.", "error")
        return redirect(url_for("admin"))
    membership = current_membership()
    if membership:
        locked_company = lock_company_quota(membership.company_id)
        if not locked_company or locked_company.status != "active":
            db.session.rollback()
            flash("This workspace is not active for employee changes.", "error")
            return redirect(url_for("admin"))
    subscription = (CompanySubscription.query.filter_by(company_id=membership.company_id)
                    .order_by(CompanySubscription.id.desc()).first() if membership else None)
    plan = subscription.plan if subscription else None
    company = membership.company if membership else None
    employee_limit = company.employee_limit if company and company.employee_limit is not None else (plan.employee_limit if plan else None)
    if membership and employee_limit is not None:
        count = CompanyMembership.query.filter(
            CompanyMembership.company_id == membership.company_id,
            CompanyMembership.status.in_(("active", "pending"))).count()
        if count >= employee_limit:
            flash("The employee account limit for this plan has been reached.", "error")
            return redirect(url_for("admin"))
    username_stub = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0]).strip("-._")[:64] or "employee"
    username = f"{username_stub}-{hashlib.sha256(email.encode()).hexdigest()[:10]}"
    u = User(username=username, email=email, name=name, is_admin=is_admin, active=False, email_verified=False)
    u.set_password(secrets.token_urlsafe(40))
    db.session.add(u)
    db.session.flush()
    if membership:
        db.session.add(CompanyMembership(company_id=membership.company_id, user_id=u.id,
                                         role="company_admin" if is_admin else "employee", status="pending"))
        if not is_admin:
            allowed_projects = company_project_names(membership.company_id)
            chosen_projects = set(request.form.getlist("projects"))
            if not chosen_projects or not chosen_projects.issubset(allowed_projects):
                db.session.rollback()
                flash("Select at least one project from this company for the employee.", "error")
                return redirect(url_for("admin"))
            db.session.add_all(ProjectAssignment(company_id=membership.company_id, user_id=u.id, project=project)
                               for project in sorted(chosen_projects))
    raw_token = secrets.token_urlsafe(32)
    db.session.add(EmailActionToken(user_id=u.id, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        purpose="employee_invite", expires_at=now_utc() + timedelta(hours=48)))
    db.session.commit()
    invite_url = absolute_app_url("employee_setup", token=raw_token)
    try:
        delivered = send_account_email(email, "Set up your Construct IV account",
            f"Hello {name},\n\nYour company invited you to Construct IV. Choose a password using this secure link within 48 hours: {invite_url}")
    except Exception:
        app.logger.exception("Employee invitation email delivery failed")
        delivered = False
    if delivered:
        flash("Employee invitation sent. They can set their own password using the email link.", "success")
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_employee_invite_url"] = invite_url
        flash("SMTP is not configured. Use the local one-time invitation link shown above.", "success")
    else:
        flash("The employee account is pending, but the invitation email could not be delivered. Resend it after SMTP is configured.", "error")
    audit("invite_user", "user", u.id, f"Invited {email}; admin={is_admin}")
    return redirect(url_for("admin"))


@app.route("/employee/setup/<token>", methods=["GET", "POST"])
def employee_setup(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    action = EmailActionToken.query.filter_by(token_hash=digest, purpose="employee_invite", used_at=None).first()
    if not action or action.expires_at < now_utc():
        flash("That invitation is invalid or expired. Ask your company administrator to resend it.", "error")
        return redirect(url_for("login"))
    user = db.session.get(User, action.user_id)
    membership = CompanyMembership.query.filter_by(user_id=user.id, status="pending").first() if user else None
    if not user or not membership or not membership.company or membership.company.status != "active":
        abort(404)
    if not company_feature_enabled("employee_accounts", membership.company_id):
        flash("Employee accounts are not included in this workspace plan. Contact your company administrator.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12 or password != confirm:
            flash("Use matching passwords with at least 12 characters.", "error")
        else:
            user.set_password(password)
            user.email_verified = True
            user.active = True
            user.is_admin = membership.role == "company_admin"
            membership.status = "active"
            action.used_at = now_utc()
            db.session.commit()
            flash("Your account is ready. Sign in with your work email and new password.", "success")
            return redirect(url_for("login"))
    return render_template("employee_setup.html", email=user.email)


@app.post("/admin/users/<int:user_id>/resend-invite")
@admin_required
@company_feature_required("employee_accounts")
def resend_employee_invite(user_id):
    membership = current_membership()
    target = CompanyMembership.query.filter_by(company_id=membership.company_id, user_id=user_id,
                                                status="pending").first() if membership else None
    user = target.user if target else None
    if not user or user.email_verified or not EmailActionToken.query.filter_by(
            user_id=user.id, purpose="employee_invite", used_at=None).first():
        abort(404)
    EmailActionToken.query.filter_by(user_id=user.id, purpose="employee_invite", used_at=None).update(
        {"used_at": now_utc()}, synchronize_session=False)
    raw_token = secrets.token_urlsafe(32)
    db.session.add(EmailActionToken(user_id=user.id, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        purpose="employee_invite", expires_at=now_utc() + timedelta(hours=48)))
    db.session.commit()
    invite_url = absolute_app_url("employee_setup", token=raw_token)
    try:
        delivered = send_account_email(user.email, "Set up your Construct IV account",
            f"Hello {user.name},\n\nYour company invited you to Construct IV. Choose a password using this secure link within 48 hours: {invite_url}")
    except Exception:
        app.logger.exception("Employee invitation resend failed")
        delivered = False
    if delivered:
        flash("A new one-time invitation was emailed.", "success")
    elif os.environ.get("APP_ENV", "development") != "production":
        session["local_employee_invite_url"] = invite_url
        flash("SMTP is not configured. Use the replacement local invitation link shown above.", "success")
    else:
        flash("The invitation could not be delivered. Check SMTP settings and try again.", "error")
    audit("employee_invite_resent", "user", user.id, "Rotated pending invitation token")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/projects")
@admin_required
@company_feature_required("employee_accounts")
def update_user_projects(user_id):
    membership = current_membership()
    target_membership = (CompanyMembership.query.filter_by(company_id=membership.company_id, user_id=user_id).first()
                         if membership else None)
    if not target_membership or target_membership.role == "company_admin":
        abort(404)
    allowed_projects = company_project_names(membership.company_id)
    selected = set(request.form.getlist("projects"))
    if not selected or not selected.issubset(allowed_projects):
        flash("Select one or more projects in this company.", "error")
        return redirect(url_for("admin"))
    ProjectAssignment.query.filter_by(company_id=membership.company_id, user_id=user_id).delete(synchronize_session=False)
    db.session.add_all(ProjectAssignment(company_id=membership.company_id, user_id=user_id, project=project)
                       for project in sorted(selected))
    db.session.commit()
    audit("project_access_updated", "user", user_id, f"Assigned projects: {', '.join(sorted(selected))}")
    flash("Employee project access updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/toggle")
@admin_required
@company_feature_required("employee_accounts")
def toggle_user(user_id):
    membership = current_membership()
    user_member = CompanyMembership.query.filter_by(company_id=membership.company_id, user_id=user_id).first() if membership else None
    u = user_member.user if user_member else abort(404)
    if u.id == current_user.id:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("admin"))
    u.active = not u.active
    db.session.commit()
    audit("toggle_user", "user", u.id, f"active={u.active}")
    flash("User status updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/reset")
@admin_required
@company_feature_required("employee_accounts")
def reset_password(user_id):
    membership = current_membership()
    user_member = CompanyMembership.query.filter_by(company_id=membership.company_id, user_id=user_id).first() if membership else None
    u = user_member.user if user_member else abort(404)
    new_password = request.form.get("new_password", "")
    if len(new_password) < 10:
        flash("New password must be at least 10 characters.", "error")
        return redirect(url_for("admin"))
    u.set_password(new_password)
    db.session.commit()
    audit("reset_password", "user", u.id, "Password reset by administrator")
    flash("Password reset.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/flight/<int:flight_id>/delete")
@admin_required
def delete_flight(flight_id):
    f = db.get_or_404(Flight, flight_id)
    filename = f.filename
    flight_id_text = f.flight_id
    db.session.delete(f)
    db.session.commit()
    delete_stored_file(filename)
    audit("delete", "flight", flight_id, f"Deleted {flight_id_text}")
    flash("Flight deleted.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/document/<int:doc_id>/delete")
@admin_required
@company_feature_required("documents")
def delete_document(doc_id):
    d = db.get_or_404(ProjectFile, doc_id)
    filename = d.filename
    db.session.delete(d)
    db.session.commit()
    delete_stored_file(filename)
    audit("delete", "document", doc_id, f"Deleted {d.name}")
    flash("Document deleted.", "success")
    return redirect(url_for("admin"))


# --------------------------- JSON API ---------------------------
# These endpoints let a future React/mobile frontend use the same backend.

@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok", "service": "construction-drone-portal", "time_utc": now_utc().isoformat()})


@app.get("/api/flights")
@login_required
def api_flights():
    if not company_feature_enabled("flight_scans"):
        abort(403)
    query = Flight.query
    date_value = parse_date(request.args.get("date"))
    project = request.args.get("project", "").strip()[:160]
    direction = request.args.get("direction", "").strip()
    tag = request.args.get("tag", "").strip()[:40]
    q = request.args.get("q", "").strip()[: app.config["MAX_SEARCH_LENGTH"]]
    if date_value:
        query = query.filter(Flight.date == date_value)
    if project:
        query = query.filter(Flight.project == project)
    if direction in DIRECTIONS:
        query = query.filter(Flight.direction == direction)
    if tag:
        query = query.filter(Flight.tags.ilike(f'%"{tag}"%'))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Flight.flight_id.ilike(like), Flight.title.ilike(like), Flight.description.ilike(like), Flight.area.ilike(like), Flight.tags.ilike(like)))
    rows = query.order_by(Flight.date.desc(), Flight.uploaded_at.desc()).limit(500).all()
    return jsonify({"count": len(rows), "flights": [flight_json(f) for f in rows]})


def flight_json(f):
    return {
        "id": f.id, "flight_id": f.flight_id, "date": f.date.isoformat(), "direction": f.direction,
        "title": f.title, "description": f.description, "project": f.project, "area": f.area,
        "tags": f.tag_list, "notes": f.notes, "drone": f.drone, "pilot": f.pilot, "client": f.client,
        "captured_at": f.captured_at.isoformat() if f.captured_at else None,
        "file_size": f.file_size, "file_size_human": human_size(f.file_size),
        "duration": f.duration, "status": f.status, "view_count": f.view_count,
        "download_count": f.download_count,
        "stream_url": url_for("protected_file", filename=f.filename),
        "download_url": url_for("protected_file", filename=f.filename, download=1),
    }


@app.get("/api/tags")
@login_required
def api_tags():
    features = {key: company_feature_enabled(key) for key in PLAN_FEATURES}
    tags = {}
    if features["flight_scans"]:
        for raw in db.session.query(Flight.tags).all():
            for tag in parse_tags(raw[0]):
                tags[tag.casefold()] = tag
    if features["documents"]:
        for raw in db.session.query(ProjectFile.tags).all():
            for tag in parse_tags(raw[0]):
                tags[tag.casefold()] = tag
    return jsonify(sorted(tags.values(), key=str.casefold))


@app.get("/api/projects")
@login_required
def api_projects():
    features = {key: company_feature_enabled(key) for key in PLAN_FEATURES}
    values = set()
    if features["flight_scans"]:
        values.update(r[0] for r in Flight.query.with_entities(Flight.project).distinct().all() if r[0])
    if features["documents"]:
        values.update(r[0] for r in ProjectFile.query.with_entities(ProjectFile.project).distinct().all() if r[0])
    if features["site_maps"]:
        values.update(r[0] for r in ProjectSiteMap.query.with_entities(ProjectSiteMap.project).distinct().all() if r[0])
    return jsonify(sorted(values, key=str.casefold))


@app.get("/api/stats")
@login_required
def api_stats():
    features = {key: company_feature_enabled(key) for key in PLAN_FEATURES}
    total_flights = Flight.query.count() if features["flight_scans"] else 0
    total_docs = ProjectFile.query.count() if features["documents"] else 0
    total_bytes = sum(db.session.query(func.coalesce(func.sum(model.file_size), 0)).scalar() or 0
                      for model in STORAGE_FILE_MODELS)
    visible_projects = set()
    if features["flight_scans"]:
        visible_projects.update(r[0] for r in Flight.query.with_entities(Flight.project).distinct().all() if r[0])
    if features["documents"]:
        visible_projects.update(r[0] for r in ProjectFile.query.with_entities(ProjectFile.project).distinct().all() if r[0])
    if features["site_maps"]:
        visible_projects.update(r[0] for r in ProjectSiteMap.query.with_entities(ProjectSiteMap.project).distinct().all() if r[0])
    projects = len(visible_projects)
    by_project = {}
    for project, size in db.session.query(Flight.project, func.sum(Flight.file_size)).group_by(Flight.project).all():
        by_project[project or "Unassigned"] = int(size or 0)
    for project, size in db.session.query(ProjectFile.project, func.sum(ProjectFile.file_size)).group_by(ProjectFile.project).all():
        key = project or "Unassigned"
        by_project[key] = by_project.get(key, 0) + int(size or 0)
    for model in (ProjectSiteMap, ProjectPhotoReference):
        for project, size in db.session.query(model.project, func.sum(model.file_size)).group_by(model.project).all():
            key = project or "Unassigned"
            by_project[key] = by_project.get(key, 0) + int(size or 0)
    return jsonify({"flights": total_flights, "documents": total_docs, "storage_bytes": total_bytes,
                    "storage_human": human_size(total_bytes), "storage_by_project": by_project, "projects": projects})


@app.get("/api/audit")
@admin_required
def api_audit():
    limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return jsonify([{
        "id": r.id, "user_id": r.user_id, "action": r.action,
        "resource_type": r.resource_type, "resource_id": r.resource_id,
        "detail": r.detail, "ip_address": r.ip_address, "created_at": r.created_at.isoformat()
    } for r in rows])


@app.errorhandler(413)
def too_large(_):
    return "Upload is too large. The server limit is 8 GB.", 413


@app.errorhandler(403)
def forbidden(_):
    return render_template("login.html", error="You do not have permission to access that page."), 403


def init_db():
    with app.app_context():
        migrate_sqlite()
        admin_username = os.environ.get("ADMIN_USERNAME", "admin").strip().lower()
        admin_password = os.environ.get("ADMIN_PASSWORD")
        owner = User.query.filter_by(username=admin_username).first()
        if not owner:
            if not admin_password:
                raise RuntimeError("Set ADMIN_PASSWORD before initializing the administrator account.")
            owner = User(username=admin_username, name="Project Administrator", is_admin=True, platform_owner=True)
            owner.set_password(admin_password)
            db.session.add(owner)
            db.session.commit()
            print(f"Created administrator account: {admin_username}")
        elif not owner.platform_owner:
            owner.platform_owner = True
            db.session.commit()
        defaults = [
            ("Basic", "Core construction project workspace; limits and price set by the Platform Owner.", False),
            ("Enterprise", "Custom scope and pricing, reviewed by the Platform Owner.", True),
        ]
        for name, description, custom in defaults:
            if not SubscriptionPlan.query.filter_by(name=name).first():
                db.session.add(SubscriptionPlan(name=name, description=description, custom_pricing=custom,
                                                active=True, storage_limit_bytes=0))
        db.session.commit()
        apply_basic_plan_defaults()


@app.cli.command("init-db")
def init_db_command():
    """Apply additive compatibility migrations and create initial local defaults."""
    init_db()
    print("Database initialization complete.")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
