import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import re
import unittest
from datetime import date
from pathlib import Path

import app as portal


def csrf_from(response):
    return re.search(rb'name="_csrf" value="([^"]+)"', response.data).group(1).decode()


class ViewerSecurityTests(unittest.TestCase):
    """Client viewer access: share link + code, per-link lockout, timed sessions, headers."""

    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)
        with portal.app.app_context():
            portal.db.create_all()
            company = portal.Company(name="Viewer Security Co", status="active", storage_limit_bytes=10_000)
            uploader = portal.User(username="viewer-security-admin", name="VS", is_admin=True, active=True)
            uploader.set_password("viewer-security-password")
            portal.db.session.add_all([company, uploader]); portal.db.session.flush()
            access = portal.ProjectAccess(project="Viewer Security Site", code="VSEC", company_id=company.id,
                                          allow_media=True)
            portal.db.session.add(access); portal.db.session.flush()
            portal.db.session.add(portal.Flight(
                company_id=company.id, flight_id="VS-FLIGHT", date=date(2026, 11, 3), direction="W-E", title="Viewer security flight",
                filename="viewer-security-flight.mp4", original_filename="flight.mp4",
                uploaded_by=uploader.id, project="Viewer Security Site"))
            portal.db.session.commit()
            cls.token, cls.code = access.share_token, access.code
        cls.doc_path = Path(portal.UPLOAD_DIR) / "viewer-security-flight.mp4"
        cls.doc_path.write_bytes(b"\x00\x00\x00\x18ftypmp42 viewer-security")

    @classmethod
    def tearDownClass(cls):
        cls.doc_path.unlink(missing_ok=True)

    def setUp(self):
        self._clear_rate_limits()

    def tearDown(self):
        self._clear_rate_limits()

    @staticmethod
    def _clear_rate_limits():
        with portal.app.app_context():
            portal.RateLimitEvent.query.delete(synchronize_session=False)
            portal.db.session.commit()

    def _unlock(self, client):
        page = client.get(f"/share/{self.token}")
        return client.post(f"/share/{self.token}", data={"_csrf": csrf_from(page), "code": self.code,
                                                         "next_tab": "media"})

    def test_code_alone_on_home_page_does_not_open_a_project(self):
        client = portal.app.test_client()
        landing = client.get("/")
        self.assertIn(b"Open the project link", landing.data)
        self.assertNotIn(b'name="code"', landing.data)
        response = client.post("/", data={"_csrf": csrf_from(client.get("/login")), "code": self.code})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertNotIn(self.token.encode(), response.data)
        with client.session_transaction() as state:
            self.assertNotIn("viewer_code", state)
        self.assertNotEqual(client.get("/files/viewer-security-flight.mp4").status_code, 200)

    def test_legacy_code_url_never_reveals_the_share_link(self):
        client = portal.app.test_client()
        for code in (self.code, "ZZZZ"):
            response = client.get(f"/project/{code}")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Location", response.headers)
            self.assertNotIn(self.token.encode(), response.data)

    def test_share_link_plus_code_unlocks_viewer_and_files(self):
        client = portal.app.test_client()
        unlocked = self._unlock(client)
        self.assertEqual(unlocked.status_code, 302)
        self.assertEqual(client.get(f"/share/{self.token}?tab=media").status_code, 200)
        download = client.get("/files/viewer-security-flight.mp4")
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_link_locks_for_everyone_after_repeated_wrong_codes(self):
        attackers = [portal.app.test_client() for _ in range(portal.VIEWER_LINK_FAILURE_LIMIT)]
        for index, attacker in enumerate(attackers):
            page = attacker.get(f"/share/{self.token}")
            attacker.post(f"/share/{self.token}", data={"_csrf": csrf_from(page), "code": "QQQQ"},
                          environ_base={"REMOTE_ADDR": f"203.0.113.{index + 1}"})
        legitimate = portal.app.test_client()
        page = legitimate.get(f"/share/{self.token}")
        locked = legitimate.post(f"/share/{self.token}", data={"_csrf": csrf_from(page), "code": self.code},
                                 environ_base={"REMOTE_ADDR": "198.51.100.7"})
        self.assertEqual(locked.status_code, 429)
        self.assertIn(b"temporarily locked", locked.data)
        with legitimate.session_transaction() as state:
            self.assertNotIn("viewer_code", state)

    def test_wrong_codes_below_the_limit_do_not_lock_the_link(self):
        client = portal.app.test_client()
        for _ in range(portal.VIEWER_LINK_FAILURE_LIMIT - 1):
            page = client.get(f"/share/{self.token}")
            client.post(f"/share/{self.token}", data={"_csrf": csrf_from(page), "code": "QQQQ"})
        self.assertEqual(self._unlock(client).status_code, 302)

    def test_viewer_session_expires_and_code_is_asked_again(self):
        client = portal.app.test_client()
        self.assertEqual(self._unlock(client).status_code, 302)
        with client.session_transaction() as state:
            state["viewer_granted_at"] = int(portal.now_utc().timestamp()
                                             - portal.VIEWER_SESSION_MAX_AGE.total_seconds() - 60)
        page = client.get(f"/share/{self.token}?tab=media")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="code"', page.data)
        self.assertEqual(client.get("/files/viewer-security-flight.mp4").status_code, 403)

    def test_viewer_session_without_link_token_cannot_download(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["viewer_code"] = self.code
            state["viewer_granted_at"] = int(portal.now_utc().timestamp())
        self.assertEqual(client.get("/files/viewer-security-flight.mp4").status_code, 403)

    def test_security_headers(self):
        client = portal.app.test_client()
        for path in ("/", f"/share/{self.token}", "/login"):
            response = client.get(path)
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        viewer = client.get(f"/share/{self.token}")
        self.assertIn("noindex", viewer.headers.get("X-Robots-Tag", ""))
        self.assertEqual(viewer.headers.get("Cache-Control"), "no-store")
        self.assertNotIn("X-Robots-Tag", client.get("/login").headers)


if __name__ == "__main__":
    unittest.main()
