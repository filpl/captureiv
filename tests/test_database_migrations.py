"""Exercise the versioned additive migration against the pre-SaaS SQLite schema."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_production_configuration_requires_strong_secret_and_managed_database(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import app
secret = 'x' * 48
database = 'postgresql+psycopg://user:pass@db.example.test/workspace'
app.validate_production_configuration('testing', 'sqlite:///:memory:', '')
app.validate_production_configuration('production', database, secret,
    storage_backend='r2', r2_configured=True, smtp_configured=True,
    stripe_key='sk_live_example', stripe_webhook_configured=True,
    public_app_url='https://app.example.test', billing_terms_approved=True,
    data_retention_policy_approved=True)
for db_url, key in [('sqlite:///local.db', secret), (database, 'short')]:
    try:
        app.validate_production_configuration('production', db_url, key)
    except RuntimeError:
        pass
    else:
        raise AssertionError('unsafe production configuration was accepted')
base = {'storage_backend': 'r2', 'r2_configured': True, 'smtp_configured': True,
         'stripe_key': 'sk_live_example', 'stripe_webhook_configured': True,
         'public_app_url': 'https://app.example.test', 'billing_terms_approved': True,
         'data_retention_policy_approved': True}
for options in [
    {'public_app_url': 'http://app.example.test'},
    {'public_app_url': 'https://app.example.test/platform'},
    {'storage_backend': 'local'},
    {'storage_backend': 'r2', 'r2_configured': False},
    {'storage_backend': 'r2', 'r2_configured': True, 'smtp_configured': False},
    {'storage_backend': 'r2', 'r2_configured': True, 'smtp_configured': True, 'stripe_key': 'sk_test_example'},
    {'storage_backend': 'r2', 'r2_configured': True, 'smtp_configured': True,
     'stripe_key': 'sk_live_example', 'stripe_webhook_configured': False},
    {'billing_terms_approved': False},
    {'data_retention_policy_approved': False},
]:
    try:
        app.validate_production_configuration('production', database, secret, **(base | options))
    except RuntimeError:
        pass
    else:
        raise AssertionError('incomplete production service setup was accepted')
"""
        environment = os.environ.copy()
        environment.update(DATABASE_URL="sqlite:///:memory:", SECRET_KEY="runtime-test-only", APP_ENV="testing")
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_startup_does_not_create_a_local_fallback_secret(self):
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.pop("SECRET_KEY", None)
        environment.update(DATABASE_URL="sqlite:///:memory:", APP_ENV="production")
        result = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Production requires a strong SECRET_KEY", result.stderr)

    def test_standard_managed_postgresql_urls_use_psycopg_driver(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import app
assert app.normalize_database_url('postgres://user:pass@db.example.test/workspace') == 'postgresql+psycopg://user:pass@db.example.test/workspace'
assert app.normalize_database_url('postgresql://user:pass@db.example.test/workspace') == 'postgresql+psycopg://user:pass@db.example.test/workspace'
assert app.normalize_database_url('postgresql+psycopg2://user:pass@db.example.test/workspace') == 'postgresql+psycopg2://user:pass@db.example.test/workspace'
assert app.normalize_database_url('sqlite:///local.db') == 'sqlite:///local.db'
assert app.stripe_api_key_is_valid('sk_test_example', 'testing')
assert not app.stripe_api_key_is_valid('sk_live_example', 'testing')
assert app.stripe_api_key_is_valid('sk_live_example', 'production')
assert not app.stripe_api_key_is_valid('sk_test_example', 'production')
"""
        environment = os.environ.copy()
        environment.update(DATABASE_URL="sqlite:///:memory:", SECRET_KEY="url-normalization-test", APP_ENV="testing")
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_postgresql_additive_migration_fragments_use_postgresql_types(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import app
assert app.migration_timestamp_type('postgresql') == 'TIMESTAMP'
assert app.migration_boolean_column(False, 'postgresql') == 'BOOLEAN NOT NULL DEFAULT FALSE'
assert app.migration_boolean_column(True, 'postgresql') == 'BOOLEAN NOT NULL DEFAULT TRUE'
assert app.migration_timestamp_type('sqlite') == 'DATETIME'
assert app.migration_boolean_column(False, 'sqlite') == 'BOOLEAN NOT NULL DEFAULT 0'
"""
        environment = os.environ.copy()
        environment.update(DATABASE_URL="sqlite:///:memory:", SECRET_KEY="dialect-test-only", APP_ENV="testing")
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_clean_initialization_creates_owner_and_plans_once(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="constructiv-clean-start-") as temp:
            db_path = Path(temp) / "fresh.db"
            code = """
import app
with app.app.app_context():
    app.init_db()
    app.init_db()
    assert app.User.query.filter_by(username='bootstrap-owner', platform_owner=True).count() == 1
    assert app.SubscriptionPlan.query.filter_by(name='Basic').count() == 1
    assert app.SubscriptionPlan.query.filter_by(name='Enterprise').count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.SCHEDULED_CANCELLATION_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.PLAN_FEATURE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.PINNED_PHOTO_SCHEMA_REVISION).count() == 1
    assert app.ProjectPinnedPhoto.query.count() == 0
    assert set(app.SubscriptionPlan.query.filter_by(name='Basic').one().feature_keys) == set(app.DEFAULT_PLAN_FEATURES)
"""
            environment = os.environ.copy()
            environment.update(DATABASE_URL=f"sqlite:///{db_path}", SECRET_KEY="clean-start-test",
                               ADMIN_USERNAME="bootstrap-owner", ADMIN_PASSWORD="temporary-test-password",
                               APP_ENV="testing")
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sample_sqlite_url_resolves_to_documented_instance_database(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import app
from pathlib import Path
with app.app.app_context():
    path = Path(app.db.engine.url.database).as_posix()
    assert path.endswith('/instance/drone_portal.db'), path
    assert '/instance/instance/' not in path, path
"""
        environment = os.environ.copy()
        environment.update(DATABASE_URL="sqlite:///drone_portal.db", SECRET_KEY="path-test-only", APP_ENV="testing")
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_records_are_backfilled_once_and_revision_is_recorded(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="constructiv-legacy-migration-") as temp:
            db_path = Path(temp) / "legacy.db"
            connection = sqlite3.connect(db_path)
            connection.executescript("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE,
                    name VARCHAR(120) NOT NULL, password_hash VARCHAR(255) NOT NULL,
                    is_admin BOOLEAN NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL
                );
                CREATE TABLE flight (
                    id INTEGER PRIMARY KEY, flight_id VARCHAR(80) NOT NULL,
                    date DATE NOT NULL, direction VARCHAR(30) NOT NULL,
                    title VARCHAR(200) NOT NULL, description TEXT,
                    filename VARCHAR(500) NOT NULL UNIQUE,
                    original_filename VARCHAR(500) NOT NULL,
                    file_size BIGINT DEFAULT 0, duration VARCHAR(30),
                    uploaded_at DATETIME NOT NULL, uploaded_by INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX ix_flight_flight_id ON flight (flight_id);
                CREATE TABLE project_file (
                    id INTEGER PRIMARY KEY, date DATE NOT NULL, name VARCHAR(255) NOT NULL,
                    filename VARCHAR(500) NOT NULL UNIQUE, category VARCHAR(50),
                    uploaded_at DATETIME NOT NULL, uploaded_by INTEGER NOT NULL
                );
                CREATE TABLE project_access (
                    id INTEGER PRIMARY KEY, project VARCHAR(160) NOT NULL,
                    code VARCHAR(4) NOT NULL UNIQUE, created_at DATETIME NOT NULL
                );
                CREATE UNIQUE INDEX ix_project_access_project ON project_access (project);
                CREATE TABLE project_photo_annotation (
                    id INTEGER PRIMARY KEY, project VARCHAR(160) NOT NULL,
                    data TEXT NOT NULL DEFAULT '{"areas":[],"pins":[]}',
                    updated_at DATETIME NOT NULL, company_id INTEGER
                );
                CREATE TABLE project_site_setup (
                    id INTEGER PRIMARY KEY, project VARCHAR(160) NOT NULL,
                    module VARCHAR(20) NOT NULL DEFAULT 'map', address VARCHAR(300) DEFAULT '',
                    center_lat FLOAT, center_lng FLOAT, zoom INTEGER DEFAULT 16,
                    locked BOOLEAN NOT NULL DEFAULT 1, updated_at DATETIME NOT NULL,
                    company_id INTEGER
                );
                INSERT INTO user (id, username, name, password_hash, is_admin, active, created_at)
                    VALUES (1, 'legacy-admin', 'Legacy Admin', 'hashed', 1, 1, '2026-01-01 00:00:00');
                INSERT INTO user (id, username, name, password_hash, is_admin, active, created_at)
                    VALUES (2, 'legacy-employee', 'Legacy Employee', 'hashed', 0, 1, '2026-01-01 00:00:00');
                INSERT INTO flight (id, flight_id, date, direction, title, description, filename,
                    original_filename, file_size, duration, uploaded_at, uploaded_by)
                    VALUES (1, 'OLD-1', '2026-01-01', 'W-E', 'Retained flight', '', 'old.mp4',
                    'old.mp4', 123, '', '2026-01-01 00:00:00', 1);
                INSERT INTO project_access (id, project, code, created_at)
                    VALUES (1, 'Legacy site', 'ABCD', '2026-01-01 00:00:00');
                INSERT INTO project_photo_annotation (id, project, data, updated_at)
                    VALUES (1, 'Photo-only legacy site', '{"areas":[],"pins":[]}', '2026-01-01 00:00:00');
                INSERT INTO project_site_setup (id, project, module, address, zoom, locked, updated_at)
                    VALUES (1, 'Setup-only legacy site', 'map', '', 16, 1, '2026-01-01 00:00:00');
            """)
            connection.commit(); connection.close()
            code = """
import app
with app.app.app_context():
    app.migrate_sqlite()
    assert app.User.query.count() == 2
    assert app.Flight.query.count() == 1
    assert app.ProjectAccess.query.count() == 1
    assert app.Flight.query.first().company_id is not None
    legacy_employee = app.User.query.filter_by(username='legacy-employee').one()
    assigned = {row.project for row in app.ProjectAssignment.query.filter_by(user_id=legacy_employee.id).all()}
    assert {'Photo-only legacy site', 'Setup-only legacy site', 'Legacy site'} <= assigned
    assert app.SchemaMigration.query.filter_by(revision=app.CURRENT_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.ENTERPRISE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.VIEWER_ACCESS_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.RATE_LIMIT_INDEX_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.R2_UPLOAD_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.SHARE_TOKEN_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.PAST_DUE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.STORAGE_RESERVATION_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.COMPANY_LIMIT_OVERRIDE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.SCHEDULED_CANCELLATION_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.PLAN_FEATURE_SCHEMA_REVISION).count() == 1
    assert 'features_json' in {column['name'] for column in app.inspect(app.db.engine).get_columns('subscription_plan')}
    assert 'past_due_since' in {column['name'] for column in app.inspect(app.db.engine).get_columns('company_subscription')}
    assert 'cancel_at_period_end' in {column['name'] for column in app.inspect(app.db.engine).get_columns('company_subscription')}
    assert 'storage_reservation' in app.inspect(app.db.engine).get_table_names()
    assert 'storage_limit_override_bytes' in {column['name'] for column in app.inspect(app.db.engine).get_columns('company')}
    assert 'ix_rate_limit_route_subject_created' in {
        index['name'] for index in app.inspect(app.db.engine).get_indexes('rate_limit_event')
    }
    assert 'ix_upload_transfer_company_status_expiry' in {
        index['name'] for index in app.inspect(app.db.engine).get_indexes('upload_transfer')
    }
    legacy_access = app.ProjectAccess.query.filter_by(code='ABCD').one()
    assert legacy_access.expires_at is None and legacy_access.revoked_at is None
    assert len(legacy_access.share_token) >= 40
    assert 'ix_project_access_share_token' in {
        index['name'] for index in app.inspect(app.db.engine).get_indexes('project_access')
    }
    counts = (app.User.query.count(), app.Flight.query.count(), app.ProjectAccess.query.count(), app.Company.query.count(), app.ProjectAssignment.query.count())
    app.migrate_sqlite()
    assert counts == (app.User.query.count(), app.Flight.query.count(), app.ProjectAccess.query.count(), app.Company.query.count(), app.ProjectAssignment.query.count())
"""
            environment = os.environ.copy()
            environment.update(DATABASE_URL=f"sqlite:///{db_path}", SECRET_KEY="migration-test-only", APP_ENV="testing")
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_past_due_migration_backfills_existing_failed_payment_start(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="constructiv-billing-migration-") as temp:
            db_path = Path(temp) / "billing.db"
            code = """
import app
from datetime import datetime
with app.app.app_context():
    app.db.create_all()
    with app.db.engine.begin() as connection:
        connection.execute(app.text('ALTER TABLE company_subscription DROP COLUMN past_due_since'))
        connection.execute(app.text('ALTER TABLE company_subscription DROP COLUMN cancel_at_period_end'))
        connection.execute(app.text('ALTER TABLE subscription_plan DROP COLUMN features_json'))
    company = app.Company(name='Grace migration fixture', status='active', storage_limit_bytes=0)
    app.db.session.add(company)
    app.db.session.flush()
    app.db.session.execute(app.text(\"\"\"
        INSERT INTO company_subscription (company_id, status, billing_interval, stripe_event_created, updated_at)
        VALUES (:company_id, 'past_due', 'monthly', 0, :updated_at)
    \"\"\"), {'company_id': company.id, 'updated_at': '2026-10-10 12:00:00'})
    app.db.session.add(app.SchemaMigration(revision=app.CURRENT_SCHEMA_REVISION))
    app.db.session.commit()
    app.migrate_sqlite()
    subscription = app.CompanySubscription.query.one()
    assert subscription.past_due_since == datetime(2026, 10, 10, 12, 0)
    assert subscription.cancel_at_period_end is False
    assert app.SchemaMigration.query.filter_by(revision=app.PAST_DUE_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.SCHEDULED_CANCELLATION_SCHEMA_REVISION).count() == 1
    assert app.SchemaMigration.query.filter_by(revision=app.PLAN_FEATURE_SCHEMA_REVISION).count() == 1
"""
            environment = os.environ.copy()
            environment.update(DATABASE_URL=f"sqlite:///{db_path}", SECRET_KEY="billing-migration-test", APP_ENV="testing")
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
