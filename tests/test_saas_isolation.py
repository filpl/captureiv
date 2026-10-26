"""Small local regression tests for the new SaaS authorization boundary."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

from io import BytesIO
import unittest
from datetime import date, timedelta
import re
import json
import hmac
import hashlib
import time
from unittest.mock import MagicMock, patch
from flask_login import login_user
from werkzeug.datastructures import FileStorage, MultiDict
import app as portal


class FakeMultipartStorage:
    def __init__(self):
        self.multipart = {}
        self.objects = {}

    def start_multipart(self, key, content_type):
        self.multipart["fake-upload"] = {"key": key, "content_type": content_type, "parts": []}
        return "fake-upload"

    def list_multipart_parts(self, key, upload_id):
        upload = self.multipart.get(upload_id)
        return list(upload["parts"]) if upload and upload["key"] == key else []

    def signed_upload_part(self, key, upload_id, part_number):
        return "https://r2-upload.test/part"

    def complete_multipart(self, key, upload_id, parts):
        self.objects[key] = sum(part["Size"] for part in parts)
        self.multipart.pop(upload_id, None)

    def object_size(self, key):
        return self.objects[key]

    def abort_multipart(self, key, upload_id):
        self.multipart.pop(upload_id, None)

    def delete(self, key):
        self.objects.pop(key, None)


class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with portal.app.app_context():
            portal.db.create_all()
            company_a = portal.Company(name="Tenant A", status="active", storage_limit_bytes=1000)
            company_b = portal.Company(name="Tenant B", status="active", storage_limit_bytes=1000)
            user_a = portal.User(username="tenant-a", name="A", is_admin=True, active=True)
            user_b = portal.User(username="tenant-b", name="B", is_admin=True, active=True)
            employee = portal.User(username="tenant-a-employee", name="A employee", is_admin=False, active=True)
            user_a.set_password("test-password-123"); user_b.set_password("test-password-123")
            employee.set_password("test-password-123")
            portal.db.session.add_all([company_a, company_b, user_a, user_b, employee]); portal.db.session.flush()
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=company_a.id, user_id=user_a.id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=company_b.id, user_id=user_b.id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=company_a.id, user_id=employee.id, role="employee", status="active"),
                portal.Flight(company_id=company_a.id, flight_id="A-FLIGHT", date=date(2026, 10, 19), direction="W-E", title="Tenant A flight", filename="a.mp4", original_filename="a.mp4", uploaded_by=user_a.id, project="A project"),
                portal.Flight(company_id=company_b.id, flight_id="B-FLIGHT", date=date(2026, 10, 19), direction="W-E", title="Tenant B flight", filename="b.mp4", original_filename="b.mp4", uploaded_by=user_b.id, project="B project"),
                portal.Flight(company_id=company_a.id, flight_id="A-SECRET", date=date(2026, 10, 19), direction="W-E", title="Unassigned A project", filename="a-secret.mp4", original_filename="a-secret.mp4", uploaded_by=user_a.id, project="Unassigned project"),
                portal.ProjectAccess(company_id=company_b.id, project="B project", code="BBBB", allow_media=False),
                portal.ProjectFile(company_id=company_b.id, date=date(2026, 10, 19), name="Private doc", filename="private.pdf", uploaded_by=user_b.id, project="B project", file_size=12),
                portal.ProjectAssignment(company_id=company_a.id, user_id=employee.id, project="A project"),
                portal.SubscriptionPlan(name="Basic", description="Core plan", active=True, custom_pricing=False),
                portal.SubscriptionPlan(name="Enterprise", description="Custom plan", active=True, custom_pricing=True),
            ])
            portal.db.session.commit()
            cls.user_a_id, cls.user_b_id, cls.employee_id = user_a.id, user_b.id, employee.id

    def test_api_and_direct_object_reads_are_company_scoped(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        response = client.get("/api/flights")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["title"] for row in response.get_json()["flights"]}, {"Tenant A flight", "Unassigned A project"})
        self.assertEqual(client.get("/flight/2").status_code, 404)

    def test_uploaded_files_are_not_exposed_by_the_public_static_route(self):
        path = portal.UPLOAD_DIR / "private-static-route-regression.txt"
        path.write_text("private test fixture", encoding="utf-8")
        try:
            response = portal.app.test_client().get("/static/uploads/private-static-route-regression.txt")
            self.assertEqual(response.status_code, 404)
        finally:
            path.unlink(missing_ok=True)

    def test_state_changing_post_requires_csrf_token(self):
        response = portal.app.test_client().post("/register", data={
            "company_name": "CSRF regression", "email": "csrf@example.test",
        })
        self.assertEqual(response.status_code, 400)

    def test_login_attempts_are_rate_limited(self):
        remote_addr = f"192.0.2.{(time.time_ns() % 253) + 1}"
        client = portal.app.test_client()
        page = client.get("/login", environ_base={"REMOTE_ADDR": remote_addr})
        self.assertIn(b"Sign in \xc2\xb7 Capture IV", page.data)
        self.assertIn(b"Intelligence Visuals", page.data)
        self.assertNotIn(b"Construction Flight Archive", page.data)
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', page.data).group(1).decode()
        for _ in range(15):
            response = client.post("/login", data={
                "_csrf": csrf, "username": "not-a-real-account", "password": "invalid-password",
            }, environ_base={"REMOTE_ADDR": remote_addr})
            self.assertEqual(response.status_code, 200)
        limited = client.post("/login", data={
            "_csrf": csrf, "username": "not-a-real-account", "password": "invalid-password",
        }, environ_base={"REMOTE_ADDR": remote_addr})
        self.assertEqual(limited.status_code, 429)
        subject_hash = hashlib.sha256((portal.app.config["SECRET_KEY"] + remote_addr).encode()).hexdigest()
        with portal.app.app_context():
            portal.RateLimitEvent.query.filter_by(route="login", subject_hash=subject_hash).delete()
            portal.db.session.commit()

    def test_expired_rate_limit_events_are_pruned_without_dropping_recent_attempts(self):
        previous_cleanup = portal._last_rate_limit_cleanup
        portal._last_rate_limit_cleanup = 0
        with portal.app.app_context():
            portal.db.session.add(portal.RateLimitEvent(
                route="login", subject_hash="expired-rate-limit-fixture",
                created_at=portal.now_utc() - timedelta(hours=2),
            ))
            portal.db.session.commit()
        remote_addr = f"198.51.100.{(time.time_ns() % 253) + 1}"
        client = portal.app.test_client()
        page = client.get("/login", environ_base={"REMOTE_ADDR": remote_addr})
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', page.data).group(1).decode()
        attempt = client.post("/login", data={
            "_csrf": csrf, "username": "not-a-real-account", "password": "invalid-password",
        }, environ_base={"REMOTE_ADDR": remote_addr})
        self.assertEqual(attempt.status_code, 200)
        with portal.app.app_context():
            self.assertEqual(portal.RateLimitEvent.query.filter_by(
                route="login", subject_hash="expired-rate-limit-fixture").count(), 0)
            subject_hash = hashlib.sha256((portal.app.config["SECRET_KEY"] + remote_addr).encode()).hexdigest()
            self.assertEqual(portal.RateLimitEvent.query.filter_by(
                route="login", subject_hash=subject_hash).count(), 1)
            portal.RateLimitEvent.query.filter_by(route="login", subject_hash=subject_hash).delete()
            portal.db.session.commit()
        portal._last_rate_limit_cleanup = previous_cleanup

    def test_r2_multipart_upload_resumes_enforces_quota_and_is_tenant_scoped(self):
        storage = FakeMultipartStorage()
        payload = {"filename": "site-flight.mp4", "size": 2048, "lastModified": 1760918400000,
                   "project": "A project", "date": "2026-10-19", "direction": "W-E",
                   "title": "Site flight", "tags": ["weekly"]}
        admin_client = portal.app.test_client()
        with admin_client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        with patch.dict(os.environ, {"STORAGE_BACKEND": "r2"}):
            page = admin_client.get("/admin/upload")
        self.assertIn(b'data-r2-multipart="true"', page.data)
        self.assertIn(b"r2-upload.js", page.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with portal.app.app_context():
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_a_id).one().company_id
            company = portal.db.session.get(portal.Company, company_id)
            old_storage_limit = company.storage_limit_bytes
            company.storage_limit_bytes = 1024 * 1024
            portal.db.session.commit()
        with patch.dict(os.environ, {"STORAGE_BACKEND": "r2"}), patch.object(portal, "R2Storage", return_value=storage):
            start = admin_client.post("/admin/r2-upload/start", json=payload, headers={"X-CSRF-Token": csrf})
            self.assertEqual(start.status_code, 200, start.get_data(as_text=True))
            transfer_id = start.get_json()["uploadId"]
            key = storage.multipart["fake-upload"]["key"]
            storage.multipart["fake-upload"]["parts"] = [{"PartNumber": 1, "ETag": '"part-etag"', "Size": 2048}]

            resumed = admin_client.post("/admin/r2-upload/start", json=payload, headers={"X-CSRF-Token": csrf})
            self.assertEqual(resumed.get_json()["uploadId"], transfer_id)
            self.assertEqual(resumed.get_json()["uploadedParts"], [{"partNumber": 1, "size": 2048}])
            oversized = dict(payload, filename="oversized.mp4", size=2 * 1024 * 1024)
            rejected = admin_client.post("/admin/r2-upload/start", json=oversized, headers={"X-CSRF-Token": csrf})
            self.assertEqual(rejected.status_code, 413)

            part_url = admin_client.post(f"/admin/r2-upload/{transfer_id}/part-url", json={"partNumber": 1},
                                         headers={"X-CSRF-Token": csrf})
            self.assertEqual(part_url.status_code, 200)
            self.assertEqual(part_url.get_json()["url"], "https://r2-upload.test/part")
            completed = admin_client.post(f"/admin/r2-upload/{transfer_id}/complete", json={},
                                          headers={"X-CSRF-Token": csrf})
            self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
            duplicate = admin_client.post(f"/admin/r2-upload/{transfer_id}/complete", json={},
                                          headers={"X-CSRF-Token": csrf})
            self.assertTrue(duplicate.get_json()["duplicate"])

            other_client = portal.app.test_client()
            with other_client.session_transaction() as state:
                state["_user_id"] = str(self.user_b_id); state["_fresh"] = True
            other_page = other_client.get("/admin/upload")
            other_csrf = re.search(rb'name="_csrf" value="([^"]+)"', other_page.data).group(1).decode()
            denied = other_client.post(f"/admin/r2-upload/{transfer_id}/part-url", json={"partNumber": 1},
                                       headers={"X-CSRF-Token": other_csrf})
            self.assertEqual(denied.status_code, 404)

        with portal.app.app_context():
            flight = portal.db.session.get(portal.Flight, completed.get_json()["id"])
            transfer = portal.db.session.get(portal.UploadTransfer, transfer_id)
            self.assertEqual(flight.company_id, company_id)
            self.assertEqual(flight.filename, "r2__" + key)
            self.assertEqual(transfer.status, "completed")
            portal.db.session.delete(flight)
            portal.db.session.delete(transfer)
            portal.db.session.get(portal.Company, company_id).storage_limit_bytes = old_storage_limit
            portal.db.session.commit()

    def test_r2_backend_stores_company_document_privately(self):
        class FakeFileStorage:
            def __init__(self):
                self.uploads = []
                self.objects = set()

            def upload_file(self, path, key, content_type):
                self.uploads.append((str(path), os.path.exists(path), key, content_type))
                self.objects.add(key)

            def signed_download(self, key, filename, content_type, download, expires=300):
                self.download = (key, filename, content_type, download, expires)
                return "https://private-storage.example/signed-document"

            def delete(self, key):
                self.objects.discard(key)

        storage = FakeFileStorage()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        page = client.get("/admin")
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', page.data).group(1).decode()
        with portal.app.app_context():
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_a_id).one().company_id
        with patch.dict(os.environ, {"STORAGE_BACKEND": "r2"}), patch.object(portal, "R2Storage", return_value=storage):
            response = client.post("/admin/document", data={
                "_csrf": csrf, "date": "2026-10-19", "name": "Private cloud document",
                "project": "A project", "file": (BytesIO(b"private document bytes"), "site-report.pdf"),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(len(storage.uploads), 1)
            temporary_path, existed_during_transfer, key, content_type = storage.uploads[0]
            self.assertTrue(existed_during_transfer)
            self.assertTrue(key.startswith(f"companies/{company_id}/"), key)
            self.assertEqual(content_type, "application/pdf")
            self.assertFalse(os.path.exists(temporary_path))
            with portal.app.app_context():
                document = portal.ProjectFile.query.filter_by(name="Private cloud document").one()
                filename = document.filename
                self.assertTrue(portal.is_r2_filename(filename))
            streamed = client.get(f"/files/{filename}")
            self.assertEqual(streamed.status_code, 302)
            self.assertEqual(streamed.headers["Location"], "https://private-storage.example/signed-document")
            self.assertEqual(storage.download[0], key)
            with portal.app.app_context():
                portal.db.session.delete(portal.ProjectFile.query.filter_by(filename=filename).one())
                portal.db.session.commit()
                portal.delete_stored_file(filename)
        self.assertFalse(storage.objects)

    def test_existing_batch_upload_screen_reports_progress_and_can_retry(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        response = client.get("/admin/upload")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"id=\"footage-form\"", response.data)
        self.assertIn(b"id=\"upload-progress\"", response.data)
        self.assertIn(b"id=\"retry-upload\"", response.data)
        self.assertIn(b"xhr.upload.onprogress", response.data)
        with patch.dict(os.environ, {"STORAGE_BACKEND": "r2"}):
            capture = client.get("/admin/site-capture")
        self.assertEqual(capture.status_code, 200)
        self.assertIn(b'data-r2-multipart="true"', capture.data)
        self.assertIn(b"r2-upload.js", capture.data)

    def test_company_admin_invites_employee_to_set_their_own_password(self):
        admin_client = portal.app.test_client()
        with admin_client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        page = admin_client.get("/admin")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        created = admin_client.post("/admin/users/create", data={
            "_csrf": csrf, "email": "new.employee@example.test", "name": "New Employee",
            "projects": ["A project"],
        })
        self.assertEqual(created.status_code, 302)
        admin_page = admin_client.get(created.location)
        self.assertIn(b"Invite pending", admin_page.data)
        invite_url = re.search(rb'href="(http://localhost/employee/setup/[^"]+)"', admin_page.data).group(1).decode()

        employee_client = portal.app.test_client()
        with portal.app.app_context():
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_a_id).one().company_id
            limited_plan = portal.SubscriptionPlan(name="No employee accounts fixture", active=True,
                custom_pricing=False, features_json=json.dumps(["documents"]))
            portal.db.session.add(limited_plan); portal.db.session.flush()
            limited_subscription = portal.CompanySubscription(company_id=company_id,
                plan_id=limited_plan.id, status="active")
            portal.db.session.add(limited_subscription); portal.db.session.commit()
            limited_plan_id, limited_subscription_id = limited_plan.id, limited_subscription.id
        blocked_invite = employee_client.get(invite_url)
        self.assertEqual(blocked_invite.status_code, 302)
        with portal.app.app_context():
            pending_user = portal.User.query.filter_by(email="new.employee@example.test").one()
            pending_membership = portal.CompanyMembership.query.filter_by(user_id=pending_user.id).one()
            self.assertEqual(pending_membership.status, "pending")
            portal.db.session.delete(portal.db.session.get(portal.CompanySubscription, limited_subscription_id))
            portal.db.session.delete(portal.db.session.get(portal.SubscriptionPlan, limited_plan_id))
            portal.db.session.commit()
        invite = employee_client.get(invite_url)
        self.assertEqual(invite.status_code, 200)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', invite.data).group(1).decode()
        completed = employee_client.post(invite_url, data={
            "_csrf": csrf, "password": "employee-password-123", "confirm_password": "employee-password-123",
        })
        self.assertEqual(completed.status_code, 302)
        self.assertTrue(completed.location.endswith("/login"))
        with portal.app.app_context():
            employee = portal.User.query.filter_by(email="new.employee@example.test").one()
            membership = portal.CompanyMembership.query.filter_by(user_id=employee.id).one()
            self.assertTrue(employee.email_verified)
            self.assertTrue(employee.active)
            self.assertEqual(membership.status, "active")
            self.assertEqual({row.project for row in portal.ProjectAssignment.query.filter_by(user_id=employee.id).all()}, {"A project"})
        self.assertNotEqual(employee_client.get(invite_url).status_code, 200)
        login_page = employee_client.get("/login")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', login_page.data).group(1).decode()
        login = employee_client.post("/login", data={
            "_csrf": csrf, "username": "new.employee@example.test", "password": "employee-password-123",
        })
        self.assertEqual(login.status_code, 302)
        visible_flights = employee_client.get("/api/flights").get_json()["flights"]
        self.assertEqual({row["project"] for row in visible_flights}, {"A project"})

    def test_viewer_tabs_default_to_flight_scans(self):
        client = portal.app.test_client()
        with portal.app.app_context():
            access = portal.ProjectAccess.query.filter_by(code="BBBB").one()
            token = access.share_token
        legacy = client.get("/project/BBBB?tab=media")
        self.assertEqual(legacy.status_code, 302)
        self.assertIn(f"/share/{token}", legacy.headers["Location"])
        challenge = client.get(legacy.headers["Location"])
        self.assertEqual(challenge.status_code, 200)
        self.assertIn(b"4-letter code", challenge.data)
        self.assertNotIn(b"B  B  B  B", challenge.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', challenge.data).group(1).decode()
        denied = client.post(f"/share/{token}?tab=media", data={"_csrf": csrf, "code": "ABCD", "next_tab": "media"})
        self.assertEqual(denied.status_code, 200)
        self.assertIn(b"code was not recognized", denied.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', denied.data).group(1).decode()
        unlocked = client.post(f"/share/{token}?tab=media", data={"_csrf": csrf, "code": "BBBB", "next_tab": "media"})
        self.assertEqual(unlocked.status_code, 302)
        self.assertIn("tab=media", unlocked.headers["Location"])
        media_denied = client.get(unlocked.headers["Location"])
        self.assertEqual(media_denied.status_code, 403)
        self.assertEqual(client.get("/files/private.pdf").status_code, 403)

    def test_viewer_links_expire_and_can_be_revoked(self):
        with portal.app.app_context():
            expired = portal.ProjectAccess(project="Expired project", code="OLD1", company_id=1,
                expires_at=portal.now_utc() - portal.timedelta(seconds=1))
            portal.db.session.add(expired); portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        page = client.get("/admin")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        expiration = (portal.now_utc() + portal.timedelta(days=1)).date().isoformat()
        response = client.post("/admin/projects/access", data={
            "_csrf": csrf, "project": "A project", "expires_on": expiration,
            "allow_media": "on",
        })
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            access = portal.ProjectAccess.query.filter_by(project="A project", company_id=1).one()
            access_id, access_code = access.id, access.code
            self.assertEqual(access.expires_at.date().isoformat(), expiration)
            self.assertTrue(access.allow_media)
        legacy = client.get(f"/project/{access_code}")
        self.assertEqual(legacy.status_code, 302)
        with portal.app.app_context():
            token = portal.ProjectAccess.query.filter_by(id=access_id).one().share_token
        challenge = client.get(f"/share/{token}")
        self.assertEqual(challenge.status_code, 200)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', challenge.data).group(1).decode()
        unlocked = client.post(f"/share/{token}", data={"_csrf": csrf, "code": access_code, "next_tab": "map"})
        self.assertEqual(unlocked.status_code, 302)
        media_redirect = client.get(unlocked.headers["Location"])
        self.assertEqual(media_redirect.status_code, 302)
        self.assertIn("tab=media", media_redirect.headers["Location"])
        self.assertEqual(client.get(media_redirect.headers["Location"]).status_code, 200)
        self.assertEqual(client.get("/project/OLD1").status_code, 404)
        page = client.get("/admin")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        self.assertIn(b"Revoke code", page.data)
        response = client.post(f"/admin/projects/access/{access_id}/revoke", data={"_csrf": csrf})
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            self.assertIsNotNone(portal.db.session.get(portal.ProjectAccess, access_id).revoked_at)
        self.assertEqual(client.get(f"/project/{access_code}").status_code, 404)

    def test_employee_is_limited_to_assigned_projects(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.employee_id); state["_fresh"] = True
        response = client.get("/api/flights")
        self.assertEqual([row["title"] for row in response.get_json()["flights"]], ["Tenant A flight"])
        self.assertEqual(client.get("/flight/3").status_code, 404)
        dashboard = client.get("/dashboard")
        self.assertIn(b"Upload media", dashboard.data)
        upload_page = client.get("/admin/upload")
        self.assertEqual(upload_page.status_code, 200)
        self.assertIn(b"A project", upload_page.data)
        self.assertNotIn(b"Unassigned project", upload_page.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', upload_page.data).group(1).decode()

        denied = client.post("/admin/upload-batch", data={
            "_csrf": csrf,
            "date": "2026-10-19", "project": "Unassigned project",
            "directions": ["W-E"], "titles": ["Forbidden upload"],
            "videos": (BytesIO(b"video"), "forbidden.mp4"),
        }, content_type="multipart/form-data")
        self.assertEqual(denied.status_code, 403)

        with patch.object(portal, "save_upload", return_value=(
                "employee-upload-test.mp4", 5, "employee-upload-test-checksum", "employee.mp4", None)), \
                patch.object(portal, "complete_storage_reservation"):
            uploaded = client.post("/admin/upload-batch", data={
                "_csrf": csrf,
                "date": "2026-10-19", "project": "A project",
                "directions": ["W-E"], "titles": ["Employee upload"],
                "videos": (BytesIO(b"video"), "employee.mp4"),
            }, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 302)
        with portal.app.app_context():
            record = portal.Flight.query.filter_by(flight_id="20261019-WE-employee").one()
            self.assertEqual(record.project, "A project")
            self.assertEqual(record.uploaded_by, self.employee_id)
            portal.db.session.delete(record)
            portal.db.session.commit()

    def test_project_limit_counts_setup_and_photo_only_projects(self):
        with portal.app.app_context():
            company = portal.Company(name="Project quota fixture", status="active", project_limit=1)
            portal.db.session.add(company)
            portal.db.session.flush()
            company_id = company.id
            portal.db.session.add_all([
                portal.ProjectSiteSetup(company_id=company_id, project="Setup only project"),
                portal.ProjectPhotoAnnotation(company_id=company_id, project="Photo only project", data='{"areas":[],"pins":[]}'),
            ])
            portal.db.session.flush()
            self.assertEqual(portal.company_project_names(company_id), {"Setup only project", "Photo only project"})
            with patch.object(portal, "current_company_id", return_value=company_id):
                self.assertTrue(portal.project_capacity_available("Setup only project"))
                self.assertFalse(portal.project_capacity_available("New project"))
            portal.db.session.rollback()

    def test_public_authorized_r2_flight_gets_only_short_lived_signed_url(self):
        with portal.app.app_context():
            flight = portal.Flight(company_id=2, flight_id="B-R2-FLIGHT", date=date(2026, 10, 19),
                                   direction="W-E", title="Private cloud flight",
                                   filename="r2__companies/2/cloud.mp4", original_filename="cloud.mp4",
                                   uploaded_by=self.user_b_id, project="B project")
            portal.db.session.add(flight); portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["viewer_code"] = "BBBB"
        class FakeStorage:
            def signed_download(self, key, filename, content_type, download, expires=300):
                self.args = (key, filename, content_type, download, expires)
                return "https://private.example/signed-object"
        fake = FakeStorage()
        with patch.object(portal, "R2Storage", return_value=fake):
            response = client.get("/files/r2__companies/2/cloud.mp4")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://private.example/signed-object")
        self.assertEqual(fake.args[0], "companies/2/cloud.mp4")
        self.assertEqual(fake.args[1], "cloud.mp4")
        self.assertEqual(fake.args[4], 300)

    def test_public_cannot_access_other_tenant_r2_file_even_when_key_is_known(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["viewer_code"] = "BBBB"
        with patch.object(portal, "R2Storage") as storage:
            response = client.get("/files/r2__companies/1/not-shared.mp4")
        self.assertEqual(response.status_code, 403)
        storage.assert_not_called()

    def test_workspace_storage_usage_includes_site_and_photo_references(self):
        with portal.app.app_context():
            portal.db.session.add_all([
                portal.ProjectSiteMap(company_id=1, project="A project", filename="site-map.png",
                                      original_filename="site-map.png", file_size=75),
                portal.ProjectPhotoReference(company_id=1, project="A project", filename="photo-ref.png",
                                             original_filename="photo-ref.png", file_size=25),
            ])
            portal.db.session.commit()
            self.assertEqual(portal.company_storage_bytes(1), 100)
            totals, by_project = portal.aggregate_company_storage([1, 2])
            self.assertEqual(totals[1], 100)
            self.assertEqual(by_project[1]["A project"], 100)
            self.assertEqual(totals[2], 12)

    def test_clients_cannot_view_pinned_photos_or_open_map_tabs(self):
        project, pin_id = "Pinned photo regression", "photo-pin-test-001"
        code = "PHTX"
        with portal.app.app_context():
            portal.db.session.add_all([
                portal.ProjectPhotoAnnotation(company_id=1, project=project,
                    data=json.dumps({"areas": [], "pins": [{"id": pin_id, "x": 315, "y": 220,
                        "label": "North facade", "area": "Building A"}]})),
                portal.ProjectSiteMap(company_id=1, project=project, filename="pinned-site-reference.png",
                    original_filename="site-reference.png", file_size=0),
                portal.ProjectSiteSetup(company_id=1, project=project, module="image",
                    center_lat=40.2744, center_lng=-76.8871, zoom=16),
                portal.ProjectAccess(company_id=1, project=project, code=code, allow_media=True),
            ])
            portal.db.session.commit()
            token = portal.ProjectAccess.query.filter_by(company_id=1, project=project).one().share_token

        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        capture_page = client.get(f"/admin/upload?map_project={project}")
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', capture_page.data).group(1).decode()
        uploads = MultiDict([
            ("_csrf", csrf), ("project", project), ("pin_id", pin_id),
            ("photos", (BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00test"), "north-1.jpg")),
            ("photos", (BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), "north-2.png")),
        ])
        stored = [
            ("pinned-photo-one.jpg", 9, "sha-one", "north-1.jpg", None),
            ("pinned-photo-two.png", 9, "sha-two", "north-2.png", None),
        ]
        with patch.object(portal, "save_upload", side_effect=stored), \
                patch.object(portal, "complete_storage_reservation"):
            uploaded = client.post("/admin/projects/photo-pin-photos", data=uploads,
                                   content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 200, uploaded.get_data(as_text=True))
        self.assertEqual(len(uploaded.get_json()["photos"]), 2)
        with portal.app.app_context():
            photos = portal.ProjectPinnedPhoto.query.filter_by(company_id=1, project=project).all()
            self.assertEqual({photo.pin_id for photo in photos}, {pin_id})
            self.assertEqual(portal.company_storage_bytes(1), 18)

        viewer = portal.app.test_client()
        challenge = viewer.get(f"/share/{token}?tab=map")
        viewer_csrf = re.search(rb'name="_csrf" value="([^\"]+)"', challenge.data).group(1).decode()
        unlock = viewer.post(f"/share/{token}?tab=map", data={
            "_csrf": viewer_csrf, "code": code, "next_tab": "map",
        })
        self.assertEqual(unlock.status_code, 302, unlock.get_data(as_text=True))
        page = viewer.get(unlock.headers["Location"])
        if page.status_code == 302:
            page = viewer.get(page.headers["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Project footage", page.data)
        self.assertNotIn(b"PROJECT_PHOTO_PINS", page.data)
        self.assertNotIn(b"North facade", page.data)
        self.assertNotIn(b"/files/pinned-photo-one.jpg", page.data)
        self.assertEqual(viewer.get(f"/share/{token}?tab=map").status_code, 302)
        self.assertEqual(viewer.get("/files/pinned-photo-one.jpg").status_code, 403)

        foreign_viewer = portal.app.test_client()
        with foreign_viewer.session_transaction() as state:
            state["viewer_code"] = "BBBB"
        self.assertEqual(foreign_viewer.get("/files/pinned-photo-one.jpg").status_code, 403)

        with portal.app.app_context():
            for model in (portal.ProjectPinnedPhoto, portal.ProjectPhotoAnnotation,
                          portal.ProjectSiteMap, portal.ProjectSiteSetup, portal.ProjectAccess):
                model.query.filter_by(project=project, company_id=1).delete(synchronize_session=False)
            portal.db.session.commit()

    def test_platform_owner_can_create_nonbillable_footage_only_demo(self):
        client = portal.app.test_client()
        with portal.app.app_context():
            owner = portal.User(username="demo-owner-test", name="Owner", platform_owner=True,
                                active=True, email_verified=True)
            owner.set_password("demo-owner-test-password")
            portal.db.session.add(owner)
            portal.db.session.commit()
            owner_id = owner.id
        with client.session_transaction() as state:
            state["_user_id"] = str(owner_id)
            state["_fresh"] = True
        owner_page = client.get("/owner")
        self.assertEqual(owner_page.status_code, 200)
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', owner_page.data).group(1).decode()
        with patch.object(portal, "send_account_email", return_value=True) as send_email:
            response = client.post("/owner/demo-workspace/create", data={
                "_csrf": csrf,
                "company_name": "Clark Construction Test Demo",
                "project_name": "York Data Center Test",
                "admin_email": "demo-admin@example.test",
            })
        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()
        result_page = client.get(response.headers["Location"])
        self.assertIn(b"Demo workspace is ready", result_page.data)
        self.assertIn(b"demo-admin@example.test", result_page.data)
        with portal.app.app_context():
            company = portal.Company.query.filter_by(name="Clark Construction Test Demo").one()
            subscription = portal.CompanySubscription.query.filter_by(company_id=company.id).one()
            access = portal.ProjectAccess.query.filter_by(company_id=company.id).one()
            membership = portal.CompanyMembership.query.filter_by(company_id=company.id).one()
            self.assertTrue(company.is_demo)
            self.assertEqual(company.status, "active")
            self.assertEqual(subscription.status, "trialing")
            self.assertFalse(subscription.stripe_customer_id)
            self.assertIsNone(subscription.stripe_subscription_id)
            self.assertEqual(access.project, "York Data Center Test")
            self.assertTrue(access.allow_media)
            self.assertFalse(access.allow_documents)
            self.assertFalse(access.allow_overview)
            self.assertGreater(access.expires_at, portal.now_utc() + portal.timedelta(days=6))
            self.assertEqual(membership.role, "company_admin")
            self.assertEqual(membership.status, "pending")
            code, company_id = access.code, company.id

        visitor = portal.app.test_client()
        landing = visitor.get("/")
        visitor_csrf = re.search(rb'name="_csrf" value="([^\"]+)"', landing.data).group(1).decode()
        unlocked = visitor.post("/", data={"_csrf": visitor_csrf, "code": code})
        self.assertEqual(unlocked.status_code, 302)
        viewer_page = visitor.get(unlocked.headers["Location"])
        self.assertEqual(viewer_page.status_code, 200)
        self.assertIn(b"Drone footage", viewer_page.data)
        self.assertNotIn(b"Documents", viewer_page.data)
        self.assertNotIn(b"Map", viewer_page.data)

        with portal.app.app_context():
            portal.db.session.add(portal.ProjectFile(company_id=company_id,
                date=date(2026, 10, 26), name="Private document", filename="demo-private.pdf",
                uploaded_by=owner_id, project="York Data Center Test", file_size=1))
            portal.db.session.commit()
        self.assertEqual(visitor.get("/files/demo-private.pdf").status_code, 403)
        with portal.app.app_context():
            owner_csrf = re.search(rb'name="_csrf" value="([^\"]+)"', client.get("/owner").data).group(1).decode()
        deleted = client.post(f"/owner/companies/{company_id}/delete-demo", data={"_csrf": owner_csrf})
        self.assertEqual(deleted.status_code, 302)
        with portal.app.app_context():
            demo_plan = portal.SubscriptionPlan.query.filter_by(name="Demo - Non-billable").first()
            if demo_plan:
                portal.db.session.delete(demo_plan)
                portal.AuditLog.query.filter_by(user_id=owner_id).update(
                    {"user_id": None}, synchronize_session=False)
                portal.db.session.delete(portal.db.session.get(portal.User, owner_id))
                portal.db.session.commit()

    def test_platform_owner_storage_metrics_render(self):
        with portal.app.app_context():
            user = portal.db.session.get(portal.User, self.user_a_id)
            user.platform_owner = True
            portal.db.session.add(portal.ProjectFile(
                company_id=1, date=date(2026, 10, 19), name="Near quota plan", filename="quota-plan.pdf",
                original_filename="quota-plan.pdf", uploaded_by=self.user_a_id, project="A project", file_size=900))
            portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        with patch.dict(portal.os.environ, {
            "STRIPE_SECRET_KEY": "sk_test_secret_not_for_display",
            "STRIPE_WEBHOOK_SECRET": "whsec_secret_not_for_display",
            "SMTP_HOST": "smtp.example.test",
        }):
            response = client.get("/owner")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Service readiness", response.data)
        self.assertIn(b"Stripe test billing", response.data)
        self.assertIn(b"Email delivery", response.data)
        self.assertIn(b"Enterprise request routing", response.data)
        self.assertIn(b"Saved for owner review", response.data)
        self.assertIn(b"Local SQLite", response.data)
        self.assertNotIn(b"sk_test_secret_not_for_display", response.data)
        self.assertNotIn(b"whsec_secret_not_for_display", response.data)
        self.assertIn(b"Tracked storage", response.data)
        self.assertIn(b"Project usage", response.data)
        self.assertIn(b"A project", response.data)
        self.assertIn(b"Approaching 90% storage limit", response.data)
        self.assertIn(b"GB remaining", response.data)
        private_recipient = "private-owner-inbox@example.test"
        with patch.dict(portal.os.environ, {"SMTP_HOST": "smtp.example.test"}), \
                patch.object(portal, "ENTERPRISE_INQUIRY_EMAIL", private_recipient):
            configured = client.get("/owner")
        self.assertEqual(configured.status_code, 200)
        self.assertIn(b"Private routing configured", configured.data)
        self.assertNotIn(private_recipient.encode(), configured.data)
        with portal.app.app_context():
            user = portal.db.session.get(portal.User, self.user_a_id)
            user.platform_owner = False
            portal.ProjectFile.query.filter_by(filename="quota-plan.pdf").delete()
            portal.db.session.commit()

    def test_plan_features_are_configurable_and_enforced_on_admin_and_api_routes(self):
        with portal.app.app_context():
            plan = portal.SubscriptionPlan.query.filter_by(name="Basic").one()
            original_features = list(plan.feature_keys)
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_a_id).one().company_id
            subscription = portal.CompanySubscription(company_id=company_id, plan_id=plan.id, status="active")
            restricted = portal.SubscriptionPlan(name="Documents only route test", description="", active=True,
                custom_pricing=False, features_json=json.dumps(["documents"]))
            owner = portal.db.session.get(portal.User, self.user_a_id)
            owner.platform_owner = True
            portal.db.session.add_all([subscription, restricted])
            portal.db.session.commit()
            subscription_id, plan_id, restricted_id = subscription.id, plan.id, restricted.id
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        owner_page = client.get("/owner")
        self.assertEqual(owner_page.status_code, 200, owner_page.headers.get("Location"))
        self.assertIn(b"Fixed Basic limits", owner_page.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', owner_page.data).group(1).decode()
        saved_plan = client.post(f"/owner/plans/{plan_id}", data={
            "_csrf": csrf, "monthly_amount_cents": "39900", "stripe_monthly_price_id": "",
            "annual_amount_cents": "", "stripe_annual_price_id": "",
        })
        self.assertEqual(saved_plan.status_code, 302)
        with portal.app.app_context():
            subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
            subscription.plan_id = restricted_id
            portal.db.session.commit()
        admin_page = client.get("/admin")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', admin_page.data).group(1).decode()
        self.assertEqual(client.get("/api/flights").status_code, 403)
        self.assertEqual(client.get("/admin/upload").status_code, 403)
        self.assertEqual(client.get("/admin/site-capture").status_code, 403)
        self.assertEqual(client.post("/admin/users/create", data={"_csrf": csrf}).status_code, 403)
        employee_client = portal.app.test_client()
        with employee_client.session_transaction() as state:
            state["_user_id"] = str(self.employee_id); state["_fresh"] = True
        employee_response = employee_client.get("/dashboard")
        self.assertEqual(employee_response.status_code, 302)
        self.assertIn("/login", employee_response.headers["Location"])
        with portal.app.test_request_context("/"):
            from flask_login import login_user
            login_user(portal.db.session.get(portal.User, self.user_a_id))
            self.assertTrue(portal.company_feature_enabled("documents", company_id))
            self.assertFalse(portal.company_feature_enabled("photos", company_id))
        with portal.app.app_context():
            portal.db.session.delete(portal.db.session.get(portal.CompanySubscription, subscription_id))
            portal.db.session.delete(portal.db.session.get(portal.SubscriptionPlan, restricted_id))
            portal.db.session.get(portal.User, self.user_a_id).platform_owner = False
            self.assertEqual(portal.db.session.get(portal.SubscriptionPlan, plan_id).feature_keys, original_features)
            portal.db.session.commit()

    def test_documents_only_plan_shows_documents_across_dates_without_flight_archive(self):
        with portal.app.app_context():
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_b_id).one().company_id
            plan = portal.SubscriptionPlan(
                name="Documents only test", description="", active=True,
                custom_pricing=False, features_json=json.dumps(["documents"]),
            )
            portal.db.session.add(plan); portal.db.session.flush()
            subscription = portal.CompanySubscription(company_id=company_id, plan_id=plan.id, status="active")
            first = portal.ProjectFile(company_id=company_id, date=date(2026, 10, 10),
                name="Older document", filename="older-documents-only-test.pdf", uploaded_by=self.user_b_id,
                project="B project", file_size=12)
            second = portal.ProjectFile(company_id=company_id, date=date(2026, 10, 19),
                name="Recent document", filename="recent-documents-only-test.pdf", uploaded_by=self.user_b_id,
                project="B project", file_size=18, tags='["doc-only-tag"]')
            hidden_flight = portal.Flight(company_id=company_id, flight_id="HIDDEN-FEATURE-FLIGHT",
                date=date(2026, 10, 19), direction="W-E", title="Hidden feature flight",
                filename="hidden-feature-flight.mp4", original_filename="hidden-feature-flight.mp4",
                uploaded_by=self.user_b_id, project="Flight-only project", tags='["flight-only-tag"]')
            portal.db.session.add_all([subscription, first, second, hidden_flight]); portal.db.session.commit()
            subscription_id, plan_id = subscription.id, plan.id
            file_ids = [first.id, second.id]
            flight_id = hidden_flight.id
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_b_id); state["_fresh"] = True
        response = client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Project documents", response.data)
        self.assertIn(b"Older document", response.data)
        self.assertIn(b"Recent document", response.data)
        self.assertNotIn(b"Flight archive", response.data)
        self.assertEqual(client.get("/api/tags").get_json(), ["doc-only-tag"])
        self.assertEqual(client.get("/api/projects").get_json(), ["B project"])
        stats = client.get("/api/stats").get_json()
        self.assertEqual(stats["flights"], 0)
        self.assertEqual(stats["documents"], 3)  # includes the pre-existing tenant document
        self.assertEqual(stats["projects"], 1)
        with portal.app.app_context():
            portal.db.session.delete(portal.db.session.get(portal.CompanySubscription, subscription_id))
            for file_id in file_ids:
                portal.db.session.delete(portal.db.session.get(portal.ProjectFile, file_id))
            portal.db.session.delete(portal.db.session.get(portal.Flight, flight_id))
            portal.db.session.delete(portal.db.session.get(portal.SubscriptionPlan, plan_id))
            portal.db.session.commit()

    def test_platform_owner_can_override_per_company_limits(self):
        with portal.app.app_context():
            owner = portal.db.session.get(portal.User, self.user_a_id)
            owner.platform_owner = True
            plan = portal.SubscriptionPlan(name="Limit Override Fixture", storage_limit_bytes=10 * 1024**3,
                                           project_limit=5, employee_limit=8)
            portal.db.session.add(plan)
            portal.db.session.flush()
            company = portal.Company(name="Limit Override Test", status="active", storage_limit_bytes=10 * 1024**3)
            portal.db.session.add(company)
            portal.db.session.flush()
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=plan.id, status="active")
            portal.db.session.add(subscription)
            portal.db.session.commit()
            company_id, subscription_id, plan_id = company.id, subscription.id, plan.id
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        page = client.get("/owner")
        csrf = re.search(rb'name="_csrf" value="([^\"]+)"', page.data).group(1).decode()
        response = client.post(f"/owner/companies/{company_id}/limits", data={
            "_csrf": csrf, "project_limit": "14", "employee_limit": "25", "storage_gb": "60",
        })
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            company = portal.db.session.get(portal.Company, company_id)
            self.assertEqual(company.project_limit, 14)
            self.assertEqual(company.employee_limit, 25)
            self.assertEqual(portal.company_storage_limit_bytes(company), 60 * 1024**3)
        response = client.post(f"/owner/companies/{company_id}/limits", data={
            "_csrf": csrf, "project_limit": "", "employee_limit": "", "storage_gb": "",
        })
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            company = portal.db.session.get(portal.Company, company_id)
            self.assertIsNone(company.project_limit)
            self.assertIsNone(company.employee_limit)
            self.assertIsNone(company.storage_limit_override_bytes)
            self.assertEqual(portal.company_storage_limit_bytes(company), 10 * 1024**3)
            portal.db.session.delete(portal.db.session.get(portal.CompanySubscription, subscription_id))
            portal.db.session.delete(company)
            portal.db.session.delete(portal.db.session.get(portal.SubscriptionPlan, plan_id))
            portal.db.session.get(portal.User, self.user_a_id).platform_owner = False
            portal.db.session.commit()

    def test_owner_can_manually_create_pending_enterprise_workspace(self):
        with portal.app.app_context():
            owner = portal.db.session.get(portal.User, self.user_a_id)
            owner.platform_owner = True
            portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        owner_page = client.get("/owner")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', owner_page.data).group(1).decode()
        with patch.object(portal, "send_account_email", return_value=False), patch.dict(portal.os.environ, {"APP_ENV": "development", "SMTP_HOST": ""}):
            response = client.post("/owner/companies/create", data={
                "_csrf": csrf,
                "company_name": "Manual Enterprise", "contact_name": "Jordan Admin",
                "email": "jordan-admin@example.test", "custom_amount_cents": "24000",
                "billing_interval": "annual", "stripe_price_id": "price_manual123",
                "project_limit": "12", "employee_limit": "40", "storage_gb": "750",
            })
        self.assertEqual(response.status_code, 302)
        owner_page = client.get("/owner")
        self.assertIn(b"Create an Enterprise workspace", owner_page.data)
        self.assertIn(b"local setup link", owner_page.data)
        with portal.app.app_context():
            user = portal.User.query.filter_by(email="jordan-admin@example.test").one()
            membership = portal.CompanyMembership.query.filter_by(user_id=user.id).one()
            company = portal.db.session.get(portal.Company, membership.company_id)
            subscription = portal.CompanySubscription.query.filter_by(company_id=company.id).one()
            self.assertFalse(user.active)
            self.assertEqual(company.status, "pending")
            self.assertEqual(membership.status, "pending")
            self.assertEqual(subscription.status, "pending")
            self.assertEqual(subscription.custom_amount_cents, 24000)
            self.assertEqual(company.storage_limit_bytes, 750 * 1024**3)
            company_id, user_id = company.id, user.id
            old_token = portal.EmailActionToken.query.filter_by(user_id=user.id, purpose="enterprise_setup").one()
            old_token_id = old_token.id
        with patch.object(portal, "send_account_email", return_value=True), patch.dict(portal.os.environ, {"APP_ENV": "production"}):
            response = client.post(f"/owner/companies/{company_id}/resend-invite", data={"_csrf": csrf})
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            old_token = portal.db.session.get(portal.EmailActionToken, old_token_id)
            new_token = (portal.EmailActionToken.query.filter_by(user_id=user_id, purpose="enterprise_setup")
                         .order_by(portal.EmailActionToken.id.desc()).first())
            self.assertIsNotNone(old_token.used_at)
            self.assertIsNone(new_token.used_at)
            self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "pending")
            portal.db.session.delete(subscription)
            portal.db.session.delete(membership)
            portal.db.session.delete(company)
            portal.EmailActionToken.query.filter_by(user_id=user_id).delete()
            portal.db.session.delete(user)
            portal.db.session.commit()
            portal.db.session.get(portal.User, self.user_a_id).platform_owner = False
            portal.db.session.commit()

    def test_registration_remains_pending_without_stripe_confirmation(self):
        client = portal.app.test_client()
        page = client.get("/register?plan=basic")
        token = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with portal.app.app_context():
            plan_id = portal.SubscriptionPlan.query.filter_by(name="Basic").first().id
        registration_data = {
            "_csrf": token, "company_name": "Pending Co", "first_name": "Pat", "last_name": "Example",
            "email": "pat@example.test", "password": "test-password-123", "confirm_password": "test-password-123",
            "plan_id": str(plan_id), "billing_interval": "monthly", "accept_policies": "yes",
        }
        without_consent = dict(registration_data)
        without_consent.pop("accept_policies")
        rejected = client.post("/register", data=without_consent)
        self.assertEqual(rejected.status_code, 400)
        response = client.post("/register", data=registration_data)
        self.assertEqual(response.status_code, 201)
        duplicate = client.post("/register", data=registration_data)
        self.assertEqual(duplicate.status_code, 400)
        with portal.app.app_context():
            user = portal.User.query.filter_by(email="pat@example.test").first()
            membership = portal.CompanyMembership.query.filter_by(user_id=user.id).first()
            subscription = portal.CompanySubscription.query.filter_by(company_id=membership.company_id).first()
            self.assertEqual(portal.Company.query.filter_by(name="Pending Co").count(), 1)
            self.assertEqual(portal.CompanySubscription.query.filter_by(company_id=membership.company_id).count(), 1)
            self.assertFalse(user.active)
            self.assertEqual(membership.status, "pending")
            self.assertEqual(subscription.status, "pending")
            consent = portal.AuditLog.query.filter_by(user_id=user.id, action="account_terms_accepted").one()
            consent_detail = json.loads(consent.detail)
            self.assertEqual(consent_detail["terms_version"], portal.ACCOUNT_TERMS_VERSION)
            self.assertEqual(consent_detail["privacy_notice_version"], portal.PRIVACY_NOTICE_VERSION)
            subscription.stripe_price_id_override = "price_live_mode_fixture"
            portal.db.session.commit()
            subscription_id = subscription.id
        verify_url = re.search(rb'href="(http://localhost/verify-email/[^"]+)"', response.data).group(1).decode()
        self.assertEqual(client.get(verify_url).status_code, 302)
        live_checkout_response = MagicMock()
        live_checkout_response.__enter__.return_value.read.return_value = json.dumps(
            {"url": "https://checkout.stripe.test/live-mode-fixture"}).encode()
        with patch.dict(portal.app.config, {"APP_ENV": "production",
                                            "PUBLIC_APP_URL": "https://platform.example.test"}), \
                patch.dict(portal.os.environ, {"STRIPE_SECRET_KEY": "sk_live_local-fixture"}), \
                patch.object(portal, "urlopen", return_value=live_checkout_response) as stripe_api:
            review = client.get(f"/account/checkout/{subscription_id}")
            self.assertEqual(review.status_code, 200)
            self.assertIn(b"Cancellation stops the next renewal", review.data)
            review_csrf = re.search(rb'name="_csrf" value="([^"]+)"', review.data).group(1).decode()
            live_checkout = client.post(f"/account/checkout/{subscription_id}", data={
                "_csrf": review_csrf, "accept_billing_terms": "yes"})
        self.assertEqual(live_checkout.status_code, 303, live_checkout.location)
        self.assertEqual(live_checkout.location, "https://checkout.stripe.test/live-mode-fixture")
        self.assertTrue(stripe_api.called)
        stripe_payload = stripe_api.call_args.args[0].data
        self.assertIn(b"success_url=https%3A%2F%2Fplatform.example.test%2Faccount%2Fcheckout-pending", stripe_payload)
        self.assertIn(b"cancel_url=https%3A%2F%2Fplatform.example.test%2Fregister", stripe_payload)
        with portal.app.app_context():
            user = portal.User.query.filter_by(email="pat@example.test").first()
            self.assertTrue(user.email_verified)
            self.assertFalse(user.active)

    def test_password_reset_response_does_not_reveal_if_email_exists(self):
        client = portal.app.test_client()
        page = client.get("/forgot-password")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with portal.app.app_context():
            user = portal.User(username="known-reset@example.test", email="known-reset@example.test",
                               name="Known Reset", active=True)
            user.set_password("existing-password-123")
            portal.db.session.add(user)
            portal.db.session.commit()
        with patch.object(portal, "send_account_email", return_value=True):
            known = client.post("/forgot-password", data={"_csrf": csrf, "email": "known-reset@example.test"})
            unknown = client.post("/forgot-password", data={"_csrf": csrf, "email": "nobody@example.test"})
        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(known.data, unknown.data)

    def test_verification_resend_rotates_token_without_revealing_account_existence(self):
        with portal.app.app_context():
            company = portal.Company(name="Unverified Co", status="pending")
            user = portal.User(username="resend@example.test", email="resend@example.test",
                               name="Resend User", email_verified=False, active=False)
            user.set_password("test-password-123")
            portal.db.session.add_all([company, user]); portal.db.session.flush()
            membership = portal.CompanyMembership(company_id=company.id, user_id=user.id,
                role="company_admin", status="pending")
            old_token = portal.EmailActionToken(user_id=user.id,
                token_hash=hashlib.sha256(b"old-verification-token").hexdigest(),
                purpose="verify_email", expires_at=portal.now_utc() + timedelta(hours=1))
            portal.db.session.add_all([membership, old_token]); portal.db.session.commit()
            user_id, old_token_id = user.id, old_token.id

        client = portal.app.test_client()
        page = client.get("/resend-verification")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with patch.dict(portal.os.environ, {"APP_ENV": "production", "SMTP_HOST": ""}), \
                patch.object(portal, "send_account_email", return_value=False) as send_mail:
            known = client.post("/resend-verification", data={"_csrf": csrf, "email": "resend@example.test"})
            unknown = client.post("/resend-verification", data={"_csrf": csrf, "email": "unknown@example.test"})
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.data, unknown.data)
        send_mail.assert_called_once()
        body = send_mail.call_args.args[2]
        token = re.search(r"/verify-email/([^\s]+)", body).group(1)
        with portal.app.app_context():
            old_token = portal.db.session.get(portal.EmailActionToken, old_token_id)
            new_token = portal.EmailActionToken.query.filter_by(user_id=user_id,
                purpose="verify_email", used_at=None).one()
            self.assertIsNotNone(old_token.used_at)
            self.assertEqual(new_token.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(client.get(f"/verify-email/{token}").status_code, 302)
        with portal.app.app_context():
            user = portal.db.session.get(portal.User, user_id)
            self.assertTrue(user.email_verified)
            self.assertFalse(user.active)

    def test_password_reset_token_is_hashed_expires_and_can_only_be_used_once(self):
        with portal.app.app_context():
            user = portal.User(username="reset-user@example.test", email="reset-user@example.test",
                               name="Reset User", active=True)
            user.set_password("before-reset-password")
            portal.db.session.add(user)
            portal.db.session.commit()
            user_id = user.id
        client = portal.app.test_client()
        page = client.get("/forgot-password")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with patch.object(portal, "send_account_email", return_value=True) as send_mail:
            response = client.post("/forgot-password", data={"_csrf": csrf, "email": "reset-user@example.test"})
        self.assertEqual(response.status_code, 200)
        body = send_mail.call_args.args[2]
        reset_url = re.search(r"https?://[^\s]+/reset-password/([^\s]+)", body).group(0)
        token = reset_url.rsplit("/", 1)[-1]
        with portal.app.app_context():
            stored = portal.EmailActionToken.query.filter_by(user_id=user_id, purpose="reset_password").one()
            self.assertEqual(stored.token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(stored.token_hash, token)

        reset_client = portal.app.test_client()
        reset_page = reset_client.get(f"/reset-password/{token}")
        reset_csrf = re.search(rb'name="_csrf" value="([^"]+)"', reset_page.data).group(1).decode()
        reset = reset_client.post(f"/reset-password/{token}", data={
            "_csrf": reset_csrf,
            "password": "new-secure-password-123", "confirm_password": "new-secure-password-123"})
        self.assertEqual(reset.status_code, 302)
        with portal.app.app_context():
            self.assertTrue(portal.db.session.get(portal.User, user_id).check_password("new-secure-password-123"))
            self.assertIsNotNone(portal.EmailActionToken.query.filter_by(
                user_id=user_id, purpose="reset_password").one().used_at)
        replay = reset_client.post(f"/reset-password/{token}", data={
            "_csrf": reset_csrf,
            "password": "another-secure-password-123", "confirm_password": "another-secure-password-123"})
        self.assertEqual(replay.status_code, 302)
        with portal.app.app_context():
            self.assertTrue(portal.db.session.get(portal.User, user_id).check_password("new-secure-password-123"))

        with portal.app.app_context():
            expired_raw = "expired-reset-token"
            portal.db.session.add(portal.EmailActionToken(
                user_id=user_id, token_hash=hashlib.sha256(expired_raw.encode()).hexdigest(),
                purpose="reset_password", expires_at=portal.now_utc() - timedelta(seconds=1)))
            portal.db.session.commit()
        expired = client.get(f"/reset-password/{expired_raw}")
        self.assertEqual(expired.status_code, 302)

    def test_billing_grace_uses_first_failure_time_and_keeps_workspace_data(self):
        with portal.app.app_context():
            now = portal.now_utc()
            company = portal.Company(name="Grace Period Test", status="active", storage_limit_bytes=1000)
            user = portal.User(username="grace-period@example.test", email="grace-period@example.test",
                               name="Grace Period", active=True, is_admin=True, email_verified=True)
            user.set_password("grace-period-password-123")
            portal.db.session.add_all([company, user])
            portal.db.session.flush()
            membership = portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                  role="company_admin", status="active")
            subscription = portal.CompanySubscription(
                company_id=company.id, status="past_due", billing_interval="monthly",
                past_due_since=now - timedelta(days=6), updated_at=now,
            )
            flight = portal.Flight(
                company_id=company.id, flight_id="GRACE-KEEP", date=date(2026, 10, 19),
                direction="W-E", title="Retained during billing pause", filename="grace-retained.mp4",
                original_filename="grace-retained.mp4", uploaded_by=user.id, project="Grace site", file_size=25,
            )
            portal.db.session.add_all([membership, subscription, flight])
            portal.db.session.commit()
            company_id, user_id, flight_id, subscription_id = company.id, user.id, flight.id, subscription.id

        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(user_id)
            state["_fresh"] = True
        with patch.dict(portal.os.environ, {"BILLING_GRACE_DAYS": "7"}):
            within_grace = client.get("/dashboard")
            self.assertEqual(within_grace.status_code, 200)
            self.assertIn(b"WORKSPACE BILLING", within_grace.data)
            self.assertIn(b"Past Due", within_grace.data)
            self.assertIn(b"Payment grace period ends", within_grace.data)
            with portal.app.app_context():
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                subscription.past_due_since = portal.now_utc() - timedelta(days=8)
                subscription.updated_at = portal.now_utc()
                portal.db.session.commit()
            after_grace = client.get("/dashboard")
        self.assertEqual(after_grace.status_code, 302)
        self.assertIn("/account/retrieve", after_grace.headers["Location"])
        with portal.app.app_context():
            self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "billing_suspended")
            self.assertIsNotNone(portal.db.session.get(portal.Flight, flight_id))
            portal.db.session.delete(portal.db.session.get(portal.Flight, flight_id))
            portal.db.session.delete(portal.db.session.get(portal.CompanySubscription, subscription_id))
            portal.CompanyMembership.query.filter_by(company_id=company_id).delete()
            portal.db.session.delete(portal.db.session.get(portal.Company, company_id))
            portal.db.session.delete(portal.db.session.get(portal.User, user_id))
            portal.db.session.commit()

    def test_local_upload_reservations_prevent_concurrent_quota_overrun(self):
        with portal.app.app_context():
            company = portal.Company(name="Concurrent Upload Test", status="active", storage_limit_bytes=1000)
            user = portal.User(username="concurrent-upload@example.test", email="concurrent-upload@example.test",
                               name="Concurrent Upload", active=True, is_admin=True, email_verified=True)
            user.set_password("concurrent-upload-password")
            portal.db.session.add_all([company, user])
            portal.db.session.flush()
            membership = portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                  role="company_admin", status="active")
            transfer = portal.UploadTransfer(
                id="pending-cloud-reservation-test", company_id=company.id, user_id=user.id,
                fingerprint="cloud-reservation-test", project="A project", date=date(2026, 10, 19),
                direction="W-E", title="Pending R2 file", original_filename="pending.mp4",
                file_size=400, content_type="video/mp4", object_key="companies/test/pending.mp4",
                status="uploading", expires_at=portal.now_utc() + timedelta(hours=1),
            )
            portal.db.session.add_all([membership, transfer])
            portal.db.session.commit()
            company_id, user_id = company.id, user.id

        uploaded = None
        with patch.dict(portal.os.environ, {"STORAGE_BACKEND": "local"}), portal.app.test_request_context("/admin/upload"):
            login_user(portal.db.session.get(portal.User, user_id))
            uploaded = portal.save_upload(FileStorage(stream=BytesIO(b"a" * 500), filename="large.mp4"))
            self.assertEqual(uploaded[1], 500)
            self.assertIsNotNone(uploaded[4])
            with self.assertRaisesRegex(ValueError, "storage allowance"):
                portal.save_upload(FileStorage(stream=BytesIO(b"b" * 200), filename="overflow.mp4"))
            reservation = portal.StorageReservation.query.filter_by(id=uploaded[4], company_id=company_id).one()
            self.assertEqual(reservation.file_size, 500)
            portal.release_storage_reservation(uploaded[4])
            self.assertEqual(portal.StorageReservation.query.filter_by(company_id=company_id).count(), 0)

        with portal.app.app_context():
            portal.delete_stored_file(uploaded[0])
            membership = portal.CompanyMembership.query.filter_by(company_id=company_id).one()
            portal.UploadTransfer.query.filter_by(id="pending-cloud-reservation-test").delete()
            portal.db.session.delete(membership)
            portal.db.session.delete(portal.db.session.get(portal.Company, company_id))
            portal.db.session.delete(portal.db.session.get(portal.User, user_id))
            portal.db.session.commit()

    def test_local_video_upload_commits_file_and_releases_quota_reservation(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id)
            state["_fresh"] = True
        page = client.get("/admin/upload")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with patch.dict(os.environ, {"STORAGE_BACKEND": "local"}):
            response = client.post("/admin/upload", data={
                "_csrf": csrf, "date": "2026-10-19", "flight_id": "LOCAL-RESERVATION-TEST",
                "direction": "W-E", "title": "Local reservation test", "project": "A project",
                "video": (BytesIO(b"local video test payload"), "reservation-test.mp4"),
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            flight = portal.Flight.query.filter_by(flight_id="LOCAL-RESERVATION-TEST").one()
            company_id = portal.CompanyMembership.query.filter_by(user_id=self.user_a_id).one().company_id
            saved_path = portal.UPLOAD_DIR / flight.filename
            self.assertTrue(saved_path.exists())
            self.assertEqual(portal.StorageReservation.query.filter_by(company_id=company_id).count(), 0)
            portal.delete_stored_file(flight.filename)
            portal.db.session.delete(flight)
            portal.db.session.commit()

    def test_enterprise_request_is_emailed_privately_and_saved_for_owner(self):
        client = portal.app.test_client()
        page = client.get("/enterprise/request")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        request_data = {
            "_csrf": csrf, "company_name": "Private Inquiry Co", "contact_name": "Jordan Example",
            "email": "jordan@example.test", "phone": "555-0100", "estimated_projects": "12",
            "estimated_users": "30", "estimated_storage": "2 TB", "comments": "Need a private workspace.",
        }
        with patch.object(portal, "ENTERPRISE_INQUIRY_EMAIL", "enterprise-test@example.test"), \
                patch.object(portal, "send_account_email", return_value=True) as send_mail:
            response = client.post("/enterprise/request", data=request_data)
        self.assertEqual(response.status_code, 201)
        self.assertIn(b"Our team has been notified", response.data)
        self.assertNotIn(b"enterprise-test@example.test", response.data)
        send_mail.assert_called_once()
        recipient, subject, body = send_mail.call_args.args
        self.assertEqual(recipient, "enterprise-test@example.test")
        self.assertIn("New Capture IV Enterprise pricing request", subject)
        for value in ("Private Inquiry Co", "jordan@example.test", "555-0100", "2 TB", "Need a private workspace."):
            self.assertIn(value, body)
        with portal.app.app_context():
            saved = portal.EnterpriseRequest.query.filter_by(company_name="Private Inquiry Co").one()
            self.assertEqual(saved.email, "jordan@example.test")

    def test_enterprise_request_without_private_inbox_is_still_saved_for_owner(self):
        client = portal.app.test_client()
        page = client.get("/enterprise/request")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        request_data = {
            "_csrf": csrf, "company_name": "Saved Inquiry Co", "contact_name": "Jordan Example",
            "email": "jordan@example.test", "phone": "", "estimated_projects": "",
            "estimated_users": "", "estimated_storage": "", "comments": "",
        }
        with patch.object(portal, "ENTERPRISE_INQUIRY_EMAIL", ""), \
                patch.object(portal, "send_account_email") as send_mail:
            response = client.post("/enterprise/request", data=request_data)
        self.assertEqual(response.status_code, 201)
        self.assertIn(b"saved in our system", response.data)
        send_mail.assert_not_called()
        with portal.app.app_context():
            self.assertTrue(portal.EnterpriseRequest.query.filter_by(company_name="Saved Inquiry Co").first())

    def test_enterprise_approval_setup_and_payment_gate(self):
        with portal.app.app_context():
            portal.db.session.add(portal.EnterpriseRequest(
                company_name="Enterprise Co", contact_name="Taylor Admin", email="taylor-enterprise@example.test",
                estimated_projects="8", estimated_users="15", estimated_storage="500 GB"))
            portal.db.session.commit()
            request_id = portal.EnterpriseRequest.query.filter_by(email="taylor-enterprise@example.test").first().id
            owner = portal.db.session.get(portal.User, self.user_a_id)
            owner.platform_owner = True
            portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.user_a_id); state["_fresh"] = True
        page = client.get("/owner")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        with patch.object(portal, "send_account_email", return_value=False), patch.dict(portal.os.environ, {"APP_ENV": "development", "SMTP_HOST": ""}):
            response = client.post(f"/owner/enterprise/{request_id}/provision", data={
                "_csrf": csrf, "custom_amount_cents": "12500", "billing_interval": "monthly",
                "stripe_price_id": "price_enterprise123", "project_limit": "8",
                "employee_limit": "15", "storage_gb": "500",
            })
        self.assertEqual(response.status_code, 302)
        owner_page = client.get("/owner")
        local_link = re.search(rb'href="(http://localhost/enterprise/setup/[^\"]+)"', owner_page.data)
        self.assertIsNotNone(local_link, owner_page.data.decode()[-3000:])
        setup_url = local_link.group(1).decode()
        setup = client.get(setup_url)
        setup_csrf = re.search(rb'name="_csrf" value="([^"]+)"', setup.data).group(1).decode()
        response = client.post(setup_url, data={
            "_csrf": setup_csrf, "password": "enterprise-password-123", "confirm_password": "enterprise-password-123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/account/checkout/", response.location)
        with portal.app.app_context():
            enterprise_user = portal.User.query.filter_by(email="taylor-enterprise@example.test").first()
            membership = portal.CompanyMembership.query.filter_by(user_id=enterprise_user.id).first()
            company = portal.db.session.get(portal.Company, membership.company_id)
            subscription = portal.CompanySubscription.query.filter_by(company_id=company.id).first()
            request_row = portal.db.session.get(portal.EnterpriseRequest, request_id)
            self.assertTrue(enterprise_user.email_verified)
            self.assertFalse(enterprise_user.active)
            self.assertEqual(membership.status, "verified_pending_billing")
            self.assertEqual(company.status, "pending")
            self.assertEqual(company.project_limit, 8)
            self.assertEqual(company.employee_limit, 15)
            self.assertEqual(company.storage_limit_bytes, 500 * 1024**3)
            self.assertEqual(subscription.stripe_price_id_override, "price_enterprise123")
            self.assertEqual(subscription.custom_amount_cents, 12500)
            self.assertEqual(request_row.status, "awaiting_payment")
            # Enterprise keeps its custom annual terms and configured price ID;
            # the Basic-only annual-price gate must not block it.
            subscription.billing_interval = "annual"
            portal.db.session.commit()
            subscription_id = subscription.id
            self.assertTrue(enterprise_user.check_password("enterprise-password-123"))
        returning_client = portal.app.test_client()
        login_page = returning_client.get("/login")
        login_csrf = re.search(rb'name="_csrf" value="([^"]+)"', login_page.data).group(1).decode()
        login = returning_client.post("/login", data={"_csrf": login_csrf,
            "username": "taylor-enterprise@example.test", "password": "enterprise-password-123"})
        self.assertEqual(login.status_code, 302)
        self.assertIn(f"/account/checkout/{subscription_id}", login.location)
        checkout_response = MagicMock()
        checkout_response.__enter__.return_value.read.return_value = json.dumps(
            {"url": "https://checkout.stripe.test/session-1"}).encode()
        with patch.dict(portal.os.environ, {"STRIPE_SECRET_KEY": "sk_test_checkout"}), \
                patch.object(portal, "urlopen", return_value=checkout_response) as stripe_api:
            review = returning_client.get(login.location)
            self.assertEqual(review.status_code, 200)
            self.assertIn(b"Annual", review.data)
            review_csrf = re.search(rb'name="_csrf" value="([^"]+)"', review.data).group(1).decode()
            checkout_form = {"_csrf": review_csrf, "accept_billing_terms": "yes"}
            resumed_checkout = returning_client.post(login.location, data=checkout_form)
            retry_checkout = returning_client.post(login.location, data=checkout_form)
        self.assertEqual(resumed_checkout.status_code, 303)
        self.assertEqual(resumed_checkout.location, "https://checkout.stripe.test/session-1")
        self.assertEqual(retry_checkout.location, resumed_checkout.location)
        self.assertEqual(stripe_api.call_count, 2)
        first_request = stripe_api.call_args_list[0].args[0]
        retry_request = stripe_api.call_args_list[1].args[0]
        self.assertEqual(first_request.get_header("Idempotency-key"),
                         f"constructiv-checkout-subscription-{subscription_id}")
        self.assertEqual(retry_request.get_header("Idempotency-key"),
                         first_request.get_header("Idempotency-key"))
        confirmation = returning_client.get("/account/checkout-pending")
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"verified Stripe webhook", confirmation.data)
        with portal.app.app_context():
            self.assertFalse(portal.db.session.get(portal.User, enterprise_user.id).active)
        with patch.dict(portal.os.environ, {"STRIPE_SECRET_KEY": "sk_test_enterprise",
                                            "STRIPE_WEBHOOK_SECRET": "whsec_enterprise"}):
            event = {"id": "evt_enterprise_paid", "type": "checkout.session.completed",
                     "created": int(time.time()), "data": {"object": {
                         "metadata": {"subscription_id": str(subscription_id)},
                         "status": "complete", "payment_status": "paid",
                         "subscription": "sub_enterprise", "customer": "cus_enterprise"}}}
            raw = json.dumps(event).encode()
            timestamp = str(int(time.time()))
            digest = hmac.new(b"whsec_enterprise", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            paid = portal.app.test_client().post("/api/billing/stripe-webhook", data=raw,
                headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={digest}"})
        self.assertEqual(paid.status_code, 200, paid.get_data(as_text=True))
        with patch.object(portal, "urlopen") as stripe_api:
            completed_checkout = returning_client.get(login.location)
        self.assertEqual(completed_checkout.status_code, 302)
        self.assertEqual(completed_checkout.location, "/dashboard")
        stripe_api.assert_not_called()
        with portal.app.app_context():
            enterprise_user = portal.User.query.filter_by(email="taylor-enterprise@example.test").first()
            membership = portal.CompanyMembership.query.filter_by(user_id=enterprise_user.id).first()
            company = portal.db.session.get(portal.Company, membership.company_id)
            subscription = portal.CompanySubscription.query.filter_by(company_id=company.id).first()
            self.assertTrue(enterprise_user.active)
            self.assertEqual(membership.status, "active")
            self.assertEqual(company.status, "active")
            self.assertEqual(subscription.status, "active")
            self.assertEqual(company.storage_limit_bytes, 500 * 1024**3)
            request_row = portal.EnterpriseRequest.query.filter_by(company_id=company.id).first()
            self.assertEqual(request_row.status, "active")

    def test_signed_stripe_test_webhook_activates_paid_signup_once(self):
        with portal.app.app_context():
            company = portal.Company(name="Webhook Co", status="pending", storage_limit_bytes=0)
            user = portal.User(username="webhook@example.test", email="webhook@example.test", name="Web Hook", email_verified=True, is_admin=False, active=False)
            user.set_password("test-password-123")
            portal.db.session.add_all([company, user]); portal.db.session.flush()
            membership = portal.CompanyMembership(company_id=company.id, user_id=user.id, role="company_admin", status="verified_pending_billing")
            plan = portal.SubscriptionPlan.query.filter_by(name="Basic").first()
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=plan.id, status="pending")
            portal.db.session.add_all([membership, subscription]); portal.db.session.commit()
            subscription_id, user_id, company_id = subscription.id, user.id, company.id

        old_key = os.environ.get("STRIPE_SECRET_KEY")
        old_webhook = os.environ.get("STRIPE_WEBHOOK_SECRET")
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_local_only"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_local_only"
        try:
            payload = {"id": "evt_local_paid", "type": "checkout.session.completed", "created": int(time.time()),
                       "data": {"object": {"metadata": {"subscription_id": str(subscription_id)},
                                           "status": "complete", "payment_status": "paid",
                                           "subscription": "sub_local", "customer": "cus_local"}}}
            raw = json.dumps(payload).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b"whsec_local_only", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            client = portal.app.test_client()
            response = client.post("/api/billing/stripe-webhook", data=raw,
                                   headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            duplicate = client.post("/api/billing/stripe-webhook", data=raw,
                                    headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"})
            self.assertTrue(duplicate.get_json()["duplicate"])
            with portal.app.app_context():
                user = portal.db.session.get(portal.User, user_id)
                company = portal.db.session.get(portal.Company, company_id)
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                self.assertTrue(user.active)
                self.assertTrue(user.is_admin)
                self.assertEqual(company.status, "active")
                self.assertEqual(subscription.status, "active")

            def send_event(event_id, event_type, created, obj):
                event_payload = {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}
                event_raw = json.dumps(event_payload).encode()
                signed_at = str(int(time.time()))
                digest = hmac.new(b"whsec_local_only", signed_at.encode() + b"." + event_raw, hashlib.sha256).hexdigest()
                return client.post("/api/billing/stripe-webhook", data=event_raw,
                    headers={"Content-Type": "application/json", "Stripe-Signature": f"t={signed_at},v1={digest}"})

            first_failure_at = int(time.time()) + 1
            failed = send_event("evt_local_failed", "invoice.payment_failed", first_failure_at,
                                {"subscription": "sub_local", "status": "open", "paid": False})
            self.assertEqual(failed.status_code, 200, failed.get_data(as_text=True))
            with portal.app.app_context():
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                self.assertEqual(subscription.status, "past_due")
                self.assertEqual(subscription.past_due_since, portal.datetime.utcfromtimestamp(first_failure_at))

            repeated_failure = send_event("evt_local_failed_retry", "invoice.payment_failed", first_failure_at + 1,
                                          {"subscription": "sub_local", "status": "open", "paid": False})
            self.assertEqual(repeated_failure.status_code, 200, repeated_failure.get_data(as_text=True))
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.CompanySubscription, subscription_id).past_due_since,
                                 portal.datetime.utcfromtimestamp(first_failure_at))

            recovered = send_event("evt_local_recovered", "invoice.payment_succeeded", int(time.time()) + 2,
                                   {"subscription": "sub_local", "status": "paid", "paid": True})
            self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
            with portal.app.app_context():
                user = portal.db.session.get(portal.User, user_id)
                company = portal.db.session.get(portal.Company, company_id)
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                membership = portal.CompanyMembership.query.filter_by(user_id=user_id).one()
                self.assertTrue(user.active)
                self.assertEqual(membership.status, "active")
                self.assertEqual(company.status, "active")
                self.assertEqual(subscription.status, "active")
                self.assertIsNone(subscription.past_due_since)

                # A later renewal must not override an explicit Platform Owner suspension.
                company.status = "suspended"
                user.active = False
                membership.status = "suspended"
                portal.db.session.commit()
            owner_suspension = send_event("evt_local_owner_suspended", "customer.subscription.updated", int(time.time()) + 3,
                                          {"id": "sub_local", "status": "active"})
            self.assertEqual(owner_suspension.status_code, 200, owner_suspension.get_data(as_text=True))
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "suspended")
                self.assertFalse(portal.db.session.get(portal.User, user_id).active)
        finally:
            if old_key is None: os.environ.pop("STRIPE_SECRET_KEY", None)
            else: os.environ["STRIPE_SECRET_KEY"] = old_key
            if old_webhook is None: os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
            else: os.environ["STRIPE_WEBHOOK_SECRET"] = old_webhook

    def test_subscription_price_changes_sync_plan_interval_and_storage(self):
        with portal.app.app_context():
            basic = portal.SubscriptionPlan.query.filter_by(name="Basic").one()
            original_basic = (basic.stripe_monthly_price_id, basic.stripe_annual_price_id, basic.storage_limit_bytes)
            basic.stripe_monthly_price_id = "price_basic_monthly_sync_test"
            basic.storage_limit_bytes = 1024**3
            pro = portal.SubscriptionPlan(name="Portal Upgrade Test", active=True, custom_pricing=False,
                stripe_monthly_price_id="price_pro_monthly_sync_test",
                stripe_annual_price_id="price_pro_annual_sync_test", project_limit=20,
                employee_limit=50, storage_limit_bytes=5 * 1024**3)
            company = portal.Company(name="Portal Sync Test", status="active", storage_limit_bytes=0)
            portal.db.session.add_all([pro, company]); portal.db.session.flush()
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=basic.id, status="active",
                billing_interval="monthly", stripe_subscription_id="sub_portal_sync_test")
            portal.db.session.add(subscription); portal.db.session.commit()
            company_id, subscription_id, pro_id, basic_id = company.id, subscription.id, pro.id, basic.id

        old_key, old_secret = os.environ.get("STRIPE_SECRET_KEY"), os.environ.get("STRIPE_WEBHOOK_SECRET")
        os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_WEBHOOK_SECRET"] = "sk_test_sync", "whsec_sync"
        client = portal.app.test_client()

        def send(event_id, created, price_id, interval):
            event = {"id": event_id, "type": "customer.subscription.updated", "created": created,
                     "data": {"object": {"id": "sub_portal_sync_test", "customer": "cus_portal_sync_test",
                         "status": "active", "current_period_end": int(time.time()) + 86400,
                         "items": {"data": [{"price": {"id": price_id,
                             "recurring": {"interval": interval}}}]}}}}
            raw = json.dumps(event).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b"whsec_sync", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            return client.post("/api/billing/stripe-webhook", data=raw,
                headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"})

        try:
            now = int(time.time())
            upgraded = send("evt_portal_upgrade_sync", now + 20, "price_pro_annual_sync_test", "year")
            self.assertEqual(upgraded.status_code, 200, upgraded.get_data(as_text=True))
            with portal.app.app_context():
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                company = portal.db.session.get(portal.Company, company_id)
                self.assertEqual(subscription.plan_id, pro_id)
                self.assertEqual(subscription.billing_interval, "annual")
                self.assertEqual(company.storage_limit_bytes, 5 * 1024**3)
                self.assertIsNotNone(subscription.current_period_end)

            # An older portal event delivered later must not roll the account back.
            stale = send("evt_portal_stale_sync", now + 19, "price_basic_monthly_sync_test", "month")
            self.assertEqual(stale.status_code, 200, stale.get_data(as_text=True))
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.CompanySubscription, subscription_id).plan_id, pro_id)

            downgraded = send("evt_portal_downgrade_sync", now + 21, "price_basic_monthly_sync_test", "month")
            self.assertEqual(downgraded.status_code, 200, downgraded.get_data(as_text=True))
            with portal.app.app_context():
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                company = portal.db.session.get(portal.Company, company_id)
                self.assertEqual(subscription.plan_id, basic_id)
                self.assertEqual(subscription.billing_interval, "monthly")
                self.assertEqual(company.storage_limit_bytes, 1024**3)
        finally:
            if old_key is None: os.environ.pop("STRIPE_SECRET_KEY", None)
            else: os.environ["STRIPE_SECRET_KEY"] = old_key
            if old_secret is None: os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
            else: os.environ["STRIPE_WEBHOOK_SECRET"] = old_secret
            with portal.app.app_context():
                for event_id in ("evt_portal_upgrade_sync", "evt_portal_stale_sync", "evt_portal_downgrade_sync"):
                    event = portal.StripeEvent.query.filter_by(event_id=event_id).first()
                    if event: portal.db.session.delete(event)
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                company = portal.db.session.get(portal.Company, company_id)
                pro = portal.db.session.get(portal.SubscriptionPlan, pro_id)
                if subscription: portal.db.session.delete(subscription)
                if company: portal.db.session.delete(company)
                if pro: portal.db.session.delete(pro)
                basic = portal.db.session.get(portal.SubscriptionPlan, basic_id)
                basic.stripe_monthly_price_id, basic.stripe_annual_price_id, basic.storage_limit_bytes = original_basic
                portal.db.session.commit()

    def test_verified_resubscription_restores_billing_suspended_company(self):
        with portal.app.app_context():
            plan = portal.SubscriptionPlan.query.filter_by(name="Basic").one()
            company = portal.Company(name="Billing Resume Test", status="active", storage_limit_bytes=0)
            portal.db.session.add(company); portal.db.session.flush()
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=plan.id, status="active",
                stripe_subscription_id="sub_billing_resume_test")
            portal.db.session.add(subscription); portal.db.session.commit()
            company_id, subscription_id = company.id, subscription.id

        old_key, old_secret = os.environ.get("STRIPE_SECRET_KEY"), os.environ.get("STRIPE_WEBHOOK_SECRET")
        os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_WEBHOOK_SECRET"] = "sk_test_resume", "whsec_resume"
        client = portal.app.test_client()

        def send(event_id, event_type, created, obj):
            raw = json.dumps({"id": event_id, "type": event_type, "created": created,
                              "data": {"object": obj}}).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b"whsec_resume", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            return client.post("/api/billing/stripe-webhook", data=raw,
                headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"})

        try:
            now = int(time.time())
            canceled = send("evt_billing_resume_cancel", "customer.subscription.deleted", now + 30,
                            {"id": "sub_billing_resume_test", "status": "canceled",
                             "current_period_end": now - 10})
            self.assertEqual(canceled.status_code, 200, canceled.get_data(as_text=True))
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "billing_suspended")

            renewed = send("evt_billing_resume_paid", "customer.subscription.updated", now + 31,
                           {"id": "sub_billing_resume_test", "status": "active",
                            "current_period_end": now + 86400})
            self.assertEqual(renewed.status_code, 200, renewed.get_data(as_text=True))
            with portal.app.app_context():
                company = portal.db.session.get(portal.Company, company_id)
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                self.assertEqual(company.status, "active")
                self.assertEqual(subscription.status, "active")
        finally:
            if old_key is None: os.environ.pop("STRIPE_SECRET_KEY", None)
            else: os.environ["STRIPE_SECRET_KEY"] = old_key
            if old_secret is None: os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
            else: os.environ["STRIPE_WEBHOOK_SECRET"] = old_secret
            with portal.app.app_context():
                for event_id in ("evt_billing_resume_cancel", "evt_billing_resume_paid"):
                    event = portal.StripeEvent.query.filter_by(event_id=event_id).first()
                    if event: portal.db.session.delete(event)
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                company = portal.db.session.get(portal.Company, company_id)
                if subscription: portal.db.session.delete(subscription)
                if company: portal.db.session.delete(company)
                portal.db.session.commit()

    def test_scheduled_cancellation_keeps_access_until_paid_through_and_preserves_files(self):
        with portal.app.app_context():
            plan = portal.SubscriptionPlan.query.filter_by(name="Basic").one()
            company = portal.Company(name="Scheduled Cancellation Test", status="active", storage_limit_bytes=0)
            user = portal.User(username="scheduled-cancel@example.test", email="scheduled-cancel@example.test",
                               name="Scheduled Cancel", active=True, is_admin=True, email_verified=True)
            user.set_password("scheduled-cancel-password")
            portal.db.session.add_all([company, user]); portal.db.session.flush()
            membership = portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                  role="company_admin", status="active")
            flight = portal.Flight(company_id=company.id, flight_id="SCHEDULED-CANCEL-FLIGHT",
                date=date(2026, 10, 19), direction="W-E", title="Retain after cancellation",
                filename="scheduled-cancel.mp4", original_filename="scheduled-cancel.mp4",
                uploaded_by=user.id, project="Cancellation project")
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=plan.id, status="active",
                stripe_subscription_id="sub_scheduled_cancel_test")
            portal.db.session.add_all([membership, flight, subscription]); portal.db.session.commit()
            company_id, user_id, flight_id, subscription_id = company.id, user.id, flight.id, subscription.id

        old_key, old_secret = os.environ.get("STRIPE_SECRET_KEY"), os.environ.get("STRIPE_WEBHOOK_SECRET")
        os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_WEBHOOK_SECRET"] = "sk_test_schedule", "whsec_schedule"
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(user_id); state["_fresh"] = True

        def send(event_id, event_type, created, obj):
            raw = json.dumps({"id": event_id, "type": event_type, "created": created,
                              "data": {"object": obj}}).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b"whsec_schedule", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            return client.post("/api/billing/stripe-webhook", data=raw,
                headers={"Content-Type": "application/json", "Stripe-Signature": f"t={timestamp},v1={signature}"})

        try:
            now = int(time.time())
            future_end = now + 86400
            scheduled = send("evt_scheduled_cancel", "customer.subscription.updated", now + 1,
                {"id": "sub_scheduled_cancel_test", "status": "active", "cancel_at_period_end": True,
                 "current_period_end": future_end})
            self.assertEqual(scheduled.status_code, 200, scheduled.get_data(as_text=True))
            dashboard = client.get("/dashboard")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn(b"Canceling", dashboard.data)
            self.assertIn(b"Access through", dashboard.data)
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "active")
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                self.assertEqual(subscription.status, "active")
                self.assertTrue(subscription.cancel_at_period_end)
                self.assertEqual(subscription.current_period_end, portal.datetime.utcfromtimestamp(future_end))

            ended = send("evt_scheduled_cancel_end", "customer.subscription.deleted", now + 2,
                {"id": "sub_scheduled_cancel_test", "status": "canceled", "current_period_end": now - 1})
            self.assertEqual(ended.status_code, 200, ended.get_data(as_text=True))
            denied = client.get("/dashboard")
            self.assertEqual(denied.status_code, 302)
            self.assertIn("/account/retrieve", denied.headers["Location"])
            with portal.app.app_context():
                self.assertEqual(portal.db.session.get(portal.Company, company_id).status, "billing_suspended")
                self.assertIsNotNone(portal.db.session.get(portal.Flight, flight_id))
        finally:
            if old_key is None: os.environ.pop("STRIPE_SECRET_KEY", None)
            else: os.environ["STRIPE_SECRET_KEY"] = old_key
            if old_secret is None: os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
            else: os.environ["STRIPE_WEBHOOK_SECRET"] = old_secret
            with portal.app.app_context():
                for event_id in ("evt_scheduled_cancel", "evt_scheduled_cancel_end"):
                    event = portal.StripeEvent.query.filter_by(event_id=event_id).first()
                    if event: portal.db.session.delete(event)
                flight = portal.db.session.get(portal.Flight, flight_id)
                membership = portal.CompanyMembership.query.filter_by(company_id=company_id).first()
                subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
                company = portal.db.session.get(portal.Company, company_id)
                user = portal.db.session.get(portal.User, user_id)
                for row in (flight, membership, subscription, company, user):
                    if row: portal.db.session.delete(row)
                portal.db.session.commit()

    def test_annual_refund_is_reviewed_and_never_automatically_cancels_subscription(self):
        with portal.app.app_context():
            plan = portal.SubscriptionPlan.query.filter_by(name="Basic").one()
            company = portal.Company(name="Annual Refund Review Test", status="active")
            user = portal.User(username="annual-review@example.test", email="annual-review@example.test",
                               name="Annual Review", active=True, is_admin=True, email_verified=True)
            owner = portal.User(username="annual-review-owner", name="Review Owner", active=True,
                                is_admin=True, platform_owner=True)
            user.set_password("annual-review-password-123")
            owner.set_password("annual-review-owner-password-123")
            portal.db.session.add_all([company, user, owner]); portal.db.session.flush()
            portal.db.session.add(portal.CompanyMembership(company_id=company.id, user_id=user.id,
                role="company_admin", status="active"))
            portal.db.session.add(portal.CompanyMembership(company_id=company.id, user_id=owner.id,
                role="employee", status="active"))
            subscription = portal.CompanySubscription(company_id=company.id, plan_id=plan.id,
                status="active", billing_interval="annual", custom_amount_cents=120000,
                stripe_subscription_id="sub_annual_review", current_period_start=portal.now_utc(),
                current_period_end=portal.now_utc() + timedelta(days=300))
            flight = portal.Flight(company_id=company.id, flight_id="ANNUAL-REVIEW-FLIGHT",
                date=date(2026, 10, 19), direction="W-E", title="Keep this project data",
                filename="annual-review.mp4", original_filename="annual-review.mp4",
                uploaded_by=user.id, project="Annual review project", file_size=42)
            portal.db.session.add_all([subscription, flight]); portal.db.session.commit()
            company_id, user_id, owner_id = company.id, user.id, owner.id
            subscription_id, flight_id = subscription.id, flight.id

        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(user_id); state["_fresh"] = True
        invoice_details = {"id": "in_annual_review", "amount_paid": 120000,
            "payment_intent": {"id": "pi_annual_review"},
            "lines": {"data": [{"period": {"start": 1792454400, "end": 1818374400}}]}}
        with patch.dict(portal.os.environ, {"STRIPE_SECRET_KEY": "sk_test_annual_review"}), \
                patch.object(portal, "stripe_request", return_value={"latest_invoice": invoice_details}) as customer_stripe:
            page = client.get("/account/billing/refund-request")
            self.assertEqual(page.status_code, 200)
            refund_csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
            submitted = client.post("/account/billing/refund-request", data={
                "_csrf": refund_csrf, "reason": "Project completed early",
                "comments": "Please review the unused annual period."})
        self.assertEqual(submitted.status_code, 302)
        self.assertEqual(customer_stripe.call_count, 1)
        self.assertEqual(customer_stripe.call_args.args[0], "GET")
        with portal.app.app_context():
            review = portal.AnnualRefundRequest.query.filter_by(company_id=company_id).one()
            refund_id, idempotency_key = review.id, review.idempotency_key
            self.assertEqual(review.status, "Submitted")
            self.assertEqual(review.original_amount_cents, 120000)
            self.assertEqual(review.stripe_payment_intent_id, "pi_annual_review")
            subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
            self.assertEqual(subscription.status, "active")
            self.assertFalse(subscription.cancel_at_period_end)

        owner_client = portal.app.test_client()
        owner_login_page = owner_client.get("/login")
        owner_login_csrf = re.search(rb'name="_csrf" value="([^"]+)"', owner_login_page.data).group(1).decode()
        owner_login = owner_client.post("/login", data={"_csrf": owner_login_csrf,
            "username": "annual-review-owner", "password": "annual-review-owner-password-123"})
        self.assertEqual(owner_login.status_code, 302)
        owner_page = owner_client.get("/owner")
        self.assertEqual(owner_page.status_code, 200, owner_page.headers.get("Location"))
        with owner_client.session_transaction() as state:
            owner_csrf = state["_csrf"]
        with patch.object(portal, "stripe_request", return_value={
                "id": "re_annual_review", "status": "succeeded", "amount": 30000,
                "created": int(time.time())}) as refund_stripe:
            decision = owner_client.post(f"/owner/refunds/{refund_id}", data={
                "_csrf": owner_csrf,
                "action": "approve_partial", "approved_amount_cents": "30000",
                "decision_explanation": "Approve the unused period after manual review.",
                "agreed_termination_date": ""})
        self.assertEqual(decision.status_code, 302)
        refund_stripe.assert_called_once()
        self.assertEqual(refund_stripe.call_args.args[:2], ("POST", "refunds"))
        self.assertEqual(refund_stripe.call_args.kwargs["idempotency_key"], idempotency_key)
        with portal.app.app_context():
            review = portal.db.session.get(portal.AnnualRefundRequest, refund_id)
            subscription = portal.db.session.get(portal.CompanySubscription, subscription_id)
            company = portal.db.session.get(portal.Company, company_id)
            self.assertEqual(review.status, "Completed")
            self.assertEqual(review.approved_amount_cents, 30000)
            self.assertEqual(subscription.status, "active")
            self.assertFalse(subscription.cancel_at_period_end)
            self.assertEqual(company.status, "active")
            self.assertIsNotNone(portal.db.session.get(portal.Flight, flight_id))
            portal.db.session.delete(review)
            portal.db.session.delete(portal.db.session.get(portal.Flight, flight_id))
            portal.db.session.delete(subscription)
            portal.CompanyMembership.query.filter_by(company_id=company_id).delete()
            portal.db.session.delete(company)
            portal.db.session.delete(portal.db.session.get(portal.User, user_id))
            portal.db.session.delete(portal.db.session.get(portal.User, owner_id))
            portal.db.session.commit()


if __name__ == "__main__":
    unittest.main()

