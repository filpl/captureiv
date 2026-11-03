import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import json
import re
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

import app as portal


class WorkerAccountTests(unittest.TestCase):
    """Company worker (employee) accounts: add days, routes and documents on assigned
    projects only; admin-only tools stay with the company administrator."""

    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)
        with portal.app.app_context():
            portal.db.create_all()
            company = portal.Company(name="Worker Test Co", status="active", storage_limit_bytes=10**9)
            other = portal.Company(name="Worker Other Co", status="active", storage_limit_bytes=10**9)
            admin = portal.User(username="wt-admin", name="WT Admin", is_admin=True, active=True)
            worker = portal.User(username="wt-worker", name="WT Worker", is_admin=False, active=True)
            stranger = portal.User(username="wt-other-admin", name="Other", is_admin=True, active=True)
            for user in (admin, worker, stranger):
                user.set_password("worker-test-password-1")
            portal.db.session.add_all([company, other, admin, worker, stranger]); portal.db.session.flush()
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=company.id, user_id=admin.id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=company.id, user_id=worker.id, role="employee", status="active"),
                portal.CompanyMembership(company_id=other.id, user_id=stranger.id, role="company_admin", status="active"),
                portal.ProjectAssignment(company_id=company.id, user_id=worker.id, project="Assigned Site"),
            ])
            flights = [portal.Flight(company_id=company.id, flight_id=f"WT-{i}", date=date(2026, 11, 1), direction="W-E",
                                     title=f"{project} flight", filename=f"wt-{i}.mp4", original_filename="f.mp4",
                                     uploaded_by=admin.id, project=project)
                       for i, project in enumerate(("Assigned Site", "Unassigned Site"))]
            portal.db.session.add_all(flights)
            portal.db.session.add(portal.ProjectFile(company_id=company.id, date=date(2026, 11, 1), name="Unassigned doc",
                                                     filename="wt-unassigned.pdf", category="pdf", uploaded_by=admin.id,
                                                     project="Unassigned Site", file_size=1))
            portal.db.session.commit()
            cls.assigned_flight, cls.unassigned_flight = flights[0].id, flights[1].id
            cls.unassigned_doc = portal.ProjectFile.query.filter_by(filename="wt-unassigned.pdf").one().id

    def login(self, username):
        client = portal.app.test_client()
        page = client.get("/login")
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        response = client.post("/login", data={"_csrf": csrf, "username": username, "password": "worker-test-password-1"})
        self.assertEqual(response.status_code, 302, username)
        return client

    @staticmethod
    def csrf(client, path="/dashboard"):
        page = client.get(path)
        return re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()

    def test_worker_workspace_shows_assigned_project_and_hides_admin_tools(self):
        worker = self.login("wt-worker")
        page = worker.get("/admin/upload")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('class="worker-mode"', html)
        self.assertIn("Assigned Site", html)
        self.assertNotIn("Unassigned Site", html)
        self.assertIn('data-capture-view="documents"', html)
        self.assertIn('id="site-module-chooser" class="module-picker admin-only"', html)
        self.assertIn('class="site-boundary-editor admin-only"', html)
        # Workers see Videos & routes and Documents only; footage upload, photos and
        # overview are repetitive for them and hidden.
        self.assertIn('data-initial-view="videos"', html)
        self.assertIn('class="capture-tab worker-hidden', html)
        self.assertIn('id="uploadsView" class="worker-hidden workspace', html)

    def test_worker_can_add_a_day_route_on_assigned_project_only(self):
        worker = self.login("wt-worker")
        csrf = self.csrf(worker, "/admin/upload")
        route = {"_csrf": csrf, "date": "2026-11-05", "name": "North pass", "direction": "North → South",
                 "points": json.dumps([[40.27, -76.88], [40.28, -76.89]])}
        ok = worker.post("/admin/projects/paths", data={**route, "project": "Assigned Site"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(worker.post("/admin/projects/paths", data={**route, "project": "Unassigned Site"}).status_code, 403)
        with portal.app.app_context():
            self.assertEqual(portal.ProjectFlightPath.query.filter_by(project="Assigned Site", name="North pass").count(), 1)
            self.assertEqual(portal.ProjectFlightPath.query.filter_by(project="Unassigned Site").count(), 0)

    def test_worker_can_upload_and_edit_documents_on_assigned_project_only(self):
        worker = self.login("wt-worker")
        csrf = self.csrf(worker, "/admin/upload")
        upload = worker.post("/admin/document", data={
            "_csrf": csrf, "project": "Assigned Site", "date": "2026-11-05", "name": "Daily report",
            "return_to": "/admin/upload?map_project=Assigned%20Site#documents",
            "file": (BytesIO(b"%PDF-1.4 worker report"), "report.pdf")}, content_type="multipart/form-data")
        self.assertEqual(upload.status_code, 302)
        self.assertTrue(upload.headers["Location"].endswith("/admin/upload?map_project=Assigned%20Site#documents"))
        with portal.app.app_context():
            doc = portal.ProjectFile.query.filter_by(project="Assigned Site", name="Daily report").one()
            doc_id, filename = doc.id, doc.filename
        refused = worker.post("/admin/document", data={
            "_csrf": csrf, "project": "Unassigned Site", "date": "2026-11-05", "name": "Nope",
            "file": (BytesIO(b"%PDF-1.4 x"), "x.pdf")}, content_type="multipart/form-data")
        self.assertEqual(refused.status_code, 403)
        edited = worker.post(f"/admin/document/{doc_id}/edit", data={"_csrf": csrf, "project": "Assigned Site",
                                                                     "name": "Daily report (final)"})
        self.assertEqual(edited.status_code, 302)
        self.assertIn(worker.post(f"/admin/document/{self.unassigned_doc}/edit",
                                  data={"_csrf": csrf, "name": "Changed"}).status_code, (403, 404))
        with portal.app.app_context():
            self.assertEqual(portal.db.session.get(portal.ProjectFile, doc_id).name, "Daily report (final)")
            self.assertEqual(portal.db.session.get(portal.ProjectFile, self.unassigned_doc).name, "Unassigned doc")
        (Path(portal.UPLOAD_DIR) / filename).unlink(missing_ok=True)

    def test_worker_can_edit_footage_on_assigned_project_only(self):
        worker = self.login("wt-worker")
        csrf = self.csrf(worker, "/admin/upload")
        ok = worker.post(f"/admin/flight/{self.assigned_flight}/edit", data={
            "_csrf": csrf, "project": "Assigned Site", "title": "Renamed flight", "direction": "S-N"})
        self.assertEqual(ok.status_code, 302)
        # Unassigned projects are invisible to workers (404) or refused (403).
        self.assertIn(worker.post(f"/admin/flight/{self.unassigned_flight}/edit", data={
            "_csrf": csrf, "title": "Hijack", "direction": "S-N"}).status_code, (403, 404))
        moved = worker.post(f"/admin/flight/{self.assigned_flight}/edit", data={
            "_csrf": csrf, "project": "Unassigned Site", "title": "Move", "direction": "S-N"})
        self.assertEqual(moved.status_code, 403)
        with portal.app.app_context():
            self.assertEqual(portal.db.session.get(portal.Flight, self.assigned_flight).title, "Renamed flight")
            self.assertEqual(portal.db.session.get(portal.Flight, self.assigned_flight).project, "Assigned Site")
            self.assertEqual(portal.db.session.get(portal.Flight, self.unassigned_flight).title, "Unassigned Site flight")

    def test_admin_only_tools_stay_with_the_company_admin(self):
        worker = self.login("wt-worker")
        csrf = self.csrf(worker, "/admin/upload")
        self.assertEqual(worker.get("/admin").status_code, 403)
        self.assertEqual(worker.get("/admin/site-capture").status_code, 403)
        for path, data in (("/admin/projects/access", {"project": "Assigned Site"}),
                           ("/admin/users/create", {"email": "x@example.test", "name": "X"}),
                           ("/admin/projects/site-setup", {"project": "Assigned Site", "module": "map"}),
                           (f"/admin/flight/{self.assigned_flight}/delete", {})):
            self.assertEqual(worker.post(path, data={"_csrf": csrf, **data}).status_code, 403, path)

    def test_company_admin_still_gets_full_tools_and_documents_tab(self):
        admin = self.login("wt-admin")
        self.assertEqual(admin.get("/admin").status_code, 200)
        page = admin.get("/admin/site-capture?map_project=Unassigned%20Site")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn('class="worker-mode"', html)
        self.assertIn('data-capture-view="documents"', html)
        self.assertIn("Unassigned doc", html)

    def test_other_company_cannot_edit_this_companys_records(self):
        stranger = self.login("wt-other-admin")
        csrf = self.csrf(stranger)
        self.assertIn(stranger.post(f"/admin/flight/{self.assigned_flight}/edit", data={
            "_csrf": csrf, "title": "Cross-tenant", "direction": "S-N"}).status_code, (403, 404))
        with portal.app.app_context():
            self.assertNotEqual(portal.db.session.get(portal.Flight, self.assigned_flight).title, "Cross-tenant")


if __name__ == "__main__":
    unittest.main()
