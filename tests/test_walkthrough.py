"""Site walkthrough service: entitlements, permissions, tenant isolation, sharing and API."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import subprocess
import sys
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

import app as portal

PDF = b"%PDF-1.4\n% walkthrough test drawing\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GLB = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class WalkthroughTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)
        with portal.app.app_context():
            portal.db.create_all()
            walk = portal.Company(name="WT Walk Co", status="active", storage_limit_bytes=10**9,
                                  service_drone=True, service_walkthrough=True)
            plain = portal.Company(name="WT Drone Co", status="active", storage_limit_bytes=10**9)
            other = portal.Company(name="WT Other Co", status="active", storage_limit_bytes=10**9,
                                   service_walkthrough=True)
            users = {
                "admin": portal.User(username="wt-admin", name="WT admin", is_admin=True, active=True),
                "employee": portal.User(username="wt-employee", name="WT employee", is_admin=False, active=True),
                "outsider": portal.User(username="wt-outsider", name="WT outsider", is_admin=False, active=True),
                "plain": portal.User(username="wt-plain", name="WT plain", is_admin=True, active=True),
                "other": portal.User(username="wt-other", name="WT other", is_admin=True, active=True),
                "owner": portal.User(username="wt-owner", name="WT owner", is_admin=True, active=True,
                                     platform_owner=True),
            }
            for user in users.values():
                user.set_password("test-password-123")
            portal.db.session.add_all([walk, plain, other, *users.values()])
            portal.db.session.flush()
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=walk.id, user_id=users["admin"].id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=walk.id, user_id=users["employee"].id, role="employee", status="active"),
                portal.CompanyMembership(company_id=walk.id, user_id=users["outsider"].id, role="employee", status="active"),
                portal.CompanyMembership(company_id=plain.id, user_id=users["plain"].id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=other.id, user_id=users["other"].id, role="company_admin", status="active"),
                portal.ProjectAssignment(company_id=walk.id, user_id=users["employee"].id, project="Tower A"),
                portal.ProjectAccess(company_id=walk.id, project="Tower A", code="WKTA", allow_media=True),
                portal.ProjectAccess(company_id=walk.id, project="Tower B", code="WKTB", allow_media=False),
            ])
            portal.db.session.commit()
            cls.ids = {key: user.id for key, user in users.items()}
            cls.walk_id, cls.plain_id, cls.other_id = walk.id, plain.id, other.id
            cls.share_a = portal.ProjectAccess.query.filter_by(code="WKTA").first().share_token
            cls.share_b = portal.ProjectAccess.query.filter_by(code="WKTB").first().share_token

    @classmethod
    def tearDownClass(cls):
        with portal.app.app_context():
            for model in portal.WALKTHROUGH_FILE_MODELS:
                for row in model.query.filter(model.company_id.in_([cls.walk_id, cls.other_id])).all():
                    portal.delete_stored_file(row.filename)

    def client_for(self, who):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.ids[who]); state["_fresh"] = True; state["_csrf"] = "wt-csrf"
        return client

    def post(self, client, path, **kwargs):
        return client.post(path, headers={"X-CSRF-Token": "wt-csrf"}, **kwargs)

    def build_project(self, client, project="Tower A"):
        sheet = self.post(client, "/walkthrough/api/sheets", content_type="multipart/form-data", data={
            "project": project, "floor": "Level 1", "name": "A-101", "file": (BytesIO(PDF), "plan.pdf")})
        self.assertEqual(sheet.status_code, 200, sheet.get_data(as_text=True))
        session = self.post(client, "/walkthrough/api/sessions", json={
            "sheet_id": sheet.get_json()["id"], "name": "Walk", "date": "2026-10-21", "color": "#123456"})
        self.assertEqual(session.status_code, 200, session.get_data(as_text=True))
        return sheet.get_json()["id"], session.get_json()["id"]

    # ------------------------------------------------------------ services
    def test_walkthrough_requires_the_granted_service(self):
        self.assertEqual(self.client_for("plain").get("/walkthrough/").status_code, 403)
        self.assertEqual(self.client_for("plain").get("/walkthrough/api/data?project=X").status_code, 403)
        page = self.client_for("plain").get("/dashboard")
        self.assertNotIn(b"/walkthrough/", page.data)
        self.assertEqual(self.client_for("admin").get("/walkthrough/").status_code, 200)
        self.assertIn(b"/walkthrough/", self.client_for("admin").get("/dashboard").data)

    def test_owner_grants_services_and_drone_features_disappear_for_walkthrough_only(self):
        owner = self.client_for("owner")
        refused = self.post(owner, f"/owner/companies/{self.other_id}/services", data={})
        self.assertEqual(refused.status_code, 302)
        with portal.app.app_context():
            self.assertTrue(portal.db.session.get(portal.Company, self.other_id).service_drone)
        self.assertEqual(self.post(self.client_for("admin"), f"/owner/companies/{self.other_id}/services",
                                   data={"service_walkthrough": "1"}).status_code, 403)
        response = self.post(owner, f"/owner/companies/{self.other_id}/services", data={"service_walkthrough": "1"})
        self.assertEqual(response.status_code, 302)
        try:
            with portal.app.app_context():
                company = portal.db.session.get(portal.Company, self.other_id)
                self.assertFalse(company.service_drone); self.assertTrue(company.service_walkthrough)
                self.assertFalse(portal.company_feature_enabled("flight_scans", self.other_id))
                self.assertFalse(portal.company_feature_enabled("site_maps", self.other_id))
                self.assertTrue(portal.company_feature_enabled("documents", self.other_id))
            other = self.client_for("other")
            self.assertEqual(other.get("/admin/upload").status_code, 403)
            self.assertEqual(other.get("/admin/site-capture").status_code, 403)
            nav = other.get("/walkthrough/").get_data(as_text=True)
            self.assertIn('href="/walkthrough/"', nav)
            self.assertNotIn('href="/admin/upload"', nav)
        finally:
            self.post(owner, f"/owner/companies/{self.other_id}/services",
                      data={"service_drone": "1", "service_walkthrough": "1"})

    # ------------------------------------------------------------- api flow
    def test_build_route_media_3d_and_renumber(self):
        admin = self.client_for("admin")
        bad = self.post(admin, "/walkthrough/api/sheets", content_type="multipart/form-data", data={
            "project": "Tower A", "file": (BytesIO(b"not a pdf"), "plan.pdf")})
        self.assertEqual(bad.status_code, 400)
        sheet_id, session_id = self.build_project(admin)
        point_ids = []
        for x, y in ((0.1, 0.2), (0.5, 0.5), (0.9, 0.8)):
            response = self.post(admin, "/walkthrough/api/points", json={"session_id": session_id, "x": x, "y": y})
            self.assertEqual(response.status_code, 200)
            point_ids.append(response.get_json()["id"])
        self.assertEqual(self.post(admin, "/walkthrough/api/points",
                                   json={"session_id": session_id, "x": 1.5, "y": 0.2}).status_code, 400)
        media = self.post(admin, f"/walkthrough/api/points/{point_ids[1]}/media", content_type="multipart/form-data",
                          data={"kind": "pano", "files": [(BytesIO(PNG), "pano.png"), (BytesIO(MP4), "walk.mp4")]})
        self.assertEqual(media.status_code, 200, media.get_data(as_text=True))
        model = self.post(admin, "/walkthrough/api/models", content_type="multipart/form-data", data={
            "project": "Tower A", "name": "Shell", "file": (BytesIO(GLB), "shell.glb")})
        self.assertEqual(model.status_code, 200, model.get_data(as_text=True))
        placed = self.post(admin, f"/walkthrough/api/points/{point_ids[1]}",
                           json={"model_id": model.get_json()["id"], "m": [1.5, 2, -3]})
        self.assertEqual(placed.status_code, 200)

        data = admin.get("/walkthrough/api/data?project=Tower%20A").get_json()
        session = next(s for s in data["sessions"] if s["id"] == session_id)
        self.assertEqual([p["seq"] for p in session["points"]], [1, 2, 3])
        middle = session["points"][1]
        self.assertEqual(middle["m"], [1.5, 2.0, -3.0])
        self.assertEqual(sorted(m["kind"] for m in middle["media"]), ["pano", "video"])
        file_response = admin.get(middle["media"][0]["url"])
        self.assertEqual(file_response.status_code, 200)
        file_response.close()
        sheet_file = admin.get(next(s for s in data["sheets"] if s["id"] == sheet_id)["url"])
        self.assertEqual(sheet_file.status_code, 200); sheet_file.close()

        self.assertEqual(self.post(admin, f"/walkthrough/api/points/{point_ids[0]}/delete").status_code, 200)
        data = admin.get("/walkthrough/api/data?project=Tower%20A").get_json()
        session = next(s for s in data["sessions"] if s["id"] == session_id)
        self.assertEqual([(p["id"], p["seq"]) for p in session["points"]], [(point_ids[1], 1), (point_ids[2], 2)])
        self.assertEqual(self.post(admin, f"/walkthrough/api/sheets/{sheet_id}/delete").status_code, 409)
        self.assertEqual(self.post(admin, f"/walkthrough/api/sessions/{session_id}/delete").status_code, 200)
        self.assertEqual(self.post(admin, f"/walkthrough/api/sheets/{sheet_id}/delete").status_code, 200)

    def test_csrf_is_required(self):
        admin = self.client_for("admin")
        self.assertEqual(admin.post("/walkthrough/api/sessions", json={"sheet_id": 1}).status_code, 400)

    # ---------------------------------------------------------- permissions
    def test_employee_scope_and_delete_rights(self):
        admin = self.client_for("admin")
        _sheet_id, session_id = self.build_project(admin)
        employee = self.client_for("employee")
        added = self.post(employee, "/walkthrough/api/points", json={"session_id": session_id, "x": 0.3, "y": 0.3})
        self.assertEqual(added.status_code, 200)
        self.assertEqual(self.post(employee, f"/walkthrough/api/sessions/{session_id}/delete").status_code, 403)
        outsider = self.client_for("outsider")
        self.assertEqual(outsider.get("/walkthrough/view?project=Tower%20A").status_code, 404)
        self.assertEqual(self.post(outsider, "/walkthrough/api/points",
                                   json={"session_id": session_id, "x": 0.3, "y": 0.3}).status_code, 404)

    def test_other_company_cannot_reach_records_or_files(self):
        admin = self.client_for("admin")
        sheet_id, session_id = self.build_project(admin)
        other = self.client_for("other")
        self.assertEqual(other.get(f"/walkthrough/file/sheet/{sheet_id}").status_code, 404)
        self.assertEqual(self.post(other, "/walkthrough/api/points",
                                   json={"session_id": session_id, "x": 0.5, "y": 0.5}).status_code, 404)
        self.assertEqual(self.post(other, f"/walkthrough/api/sessions/{session_id}/delete").status_code, 404)
        data = other.get("/walkthrough/api/data?project=Tower%20A")
        self.assertTrue(data.status_code == 404 or not data.get_json()["sessions"])

    # -------------------------------------------------------------- sharing
    def test_client_share_link_requires_code_and_media_permission(self):
        sheet_id, _session_id = self.build_project(self.client_for("admin"))
        visitor = portal.app.test_client()
        locked = visitor.get(f"/walkthrough/share/{self.share_a}")
        self.assertEqual(locked.status_code, 302)
        self.assertIn(f"/share/{self.share_a}", locked.headers["Location"])
        self.assertEqual(visitor.get(f"/walkthrough/api/data?share={self.share_a}").status_code, 403)
        with visitor.session_transaction() as state:
            state["viewer_code"] = "WKTA"; state["viewer_token"] = self.share_a
        self.assertEqual(visitor.get(f"/walkthrough/share/{self.share_a}").status_code, 200)
        payload = visitor.get(f"/walkthrough/api/data?share={self.share_a}").get_json()
        self.assertEqual(payload["project"], "Tower A")
        sheet_url = next(s["url"] for s in payload["sheets"] if s["id"] == sheet_id)
        response = visitor.get(sheet_url); self.assertEqual(response.status_code, 200); response.close()
        # The unlocked Tower A code must not open Tower B files or the media-less Tower B link.
        self.assertEqual(visitor.get(f"/walkthrough/share/{self.share_b}").status_code, 404)
        with visitor.session_transaction() as state:
            state["viewer_code"] = "WKTB"; state["viewer_token"] = self.share_b
        self.assertEqual(visitor.get(f"/walkthrough/api/data?share={self.share_b}").status_code, 404)

    def test_walkthrough_only_share_link_opens_the_walkthrough(self):
        owner = self.client_for("owner")
        self.post(owner, f"/owner/companies/{self.walk_id}/services", data={"service_walkthrough": "1"})
        try:
            visitor = portal.app.test_client()
            with visitor.session_transaction() as state:
                state["viewer_code"] = "WKTA"; state["viewer_token"] = self.share_a
            response = visitor.get(f"/share/{self.share_a}")
            self.assertEqual(response.status_code, 302)
            self.assertIn(f"/walkthrough/share/{self.share_a}", response.headers["Location"])
        finally:
            self.post(owner, f"/owner/companies/{self.walk_id}/services",
                      data={"service_drone": "1", "service_walkthrough": "1"})


class WalkthroughMigrationTests(unittest.TestCase):
    def test_migration_adds_services_and_tables_without_touching_existing_rows(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import os, sqlite3
from sqlalchemy import text, inspect
import app
with app.app.app_context():
    app.init_db()
    app.db.session.add(app.Company(name='Existing customer', status='active', storage_limit_bytes=5))
    app.db.session.commit()
    # Rewind to the schema before the walkthrough service existed.
    for table in ('walkthrough_media', 'walkthrough_point', 'walkthrough_session', 'walkthrough_model', 'walkthrough_sheet'):
        app.db.session.execute(text(f'DROP TABLE {table}'))
    app.db.session.execute(text('ALTER TABLE company DROP COLUMN service_drone'))
    app.db.session.execute(text('ALTER TABLE company DROP COLUMN service_walkthrough'))
    app.db.session.execute(text("DELETE FROM schema_migration WHERE revision='0020_site_walkthrough_service'"))
    app.db.session.commit()
    app.init_db(); app.init_db()
    row = app.db.session.execute(text("SELECT name, service_drone, service_walkthrough FROM company WHERE name='Existing customer'")).one()
    assert tuple(row) == ('Existing customer', 1, 0), row
    tables = set(inspect(app.db.engine).get_table_names())
    assert {'walkthrough_sheet', 'walkthrough_point', 'walkthrough_media'} <= tables, tables
print('ok')
"""
        with __import__("tempfile").TemporaryDirectory() as tmp:
            environment = os.environ.copy()
            environment.update(DATABASE_URL=f"sqlite:///{Path(tmp, 'm.db').as_posix()}", SECRET_KEY="migration-test",
                               APP_ENV="testing", ADMIN_USERNAME="migration-owner", ADMIN_PASSWORD="migration-password-1")
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
