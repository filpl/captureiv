"""Laser scans on the project map: upload on Site capture, georeferenced overlay, 3D view with
the date's flight paths, client access through the project share link, and cleanup."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from io import BytesIO

import app as portal
import scan_convert

SCAN_DATE = date(2026, 10, 14)
OTHER_DATE = date(2026, 11, 2)


def make_las(path, count=6000, crs=True, rgb=True, extra_dims=0):
    """A small synthetic site: a sloped ground plane with a block 'building', 120 m x 80 m.
    Georeferenced in UTM zone 10N (Eugene, Oregon) unless crs=False."""
    import laspy
    import numpy as np
    rng = np.random.default_rng(3)
    header = laspy.LasHeader(point_format=3 if rgb else 1, version="1.4")
    header.offsets = [493000.0, 4877000.0, 100.0]
    header.scales = [0.01, 0.01, 0.01]
    for index in range(extra_dims):  # pad each record with 8-byte extra dimensions
        header.add_extra_dim(laspy.ExtraBytesParams(name=f"pad{index}", type="f8"))
    if crs:
        from pyproj import CRS
        header.add_crs(CRS.from_epsg(32610))
    las = laspy.LasData(header)
    x = rng.uniform(0, 120, count); y = rng.uniform(0, 80, count)
    z = 0.05 * x + np.where((x > 50) & (x < 70) & (y > 30) & (y < 50), 9.0, 0.0)
    las.x, las.y, las.z = x + 493000, y + 4877000, z + 100
    if rgb:
        las.red = (rng.uniform(0.2, 0.8, count) * 65535).astype("u2")
        las.green = (rng.uniform(0.2, 0.8, count) * 65535).astype("u2")
        las.blue = (rng.uniform(0.2, 0.8, count) * 65535).astype("u2")
    las.write(path)


def las_bytes(**kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "site.las")
        make_las(path, **kwargs)
        with open(path, "rb") as handle:
            return handle.read()


class ProjectScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True, SCAN_CONVERT_SYNC=True)
        with portal.app.app_context():
            portal.db.create_all()
            main = portal.Company(name="PS Main Co", status="active", storage_limit_bytes=10**9)
            other = portal.Company(name="PS Other Co", status="active", storage_limit_bytes=10**9)
            users = {
                "admin": portal.User(username="ps-admin", name="PS admin", is_admin=True, active=True),
                "employee": portal.User(username="ps-employee", name="PS employee", is_admin=False, active=True),
                "other": portal.User(username="ps-other", name="PS other", is_admin=True, active=True),
            }
            for user in users.values():
                user.set_password("test-password-123")
            portal.db.session.add_all([main, other, *users.values()])
            portal.db.session.flush()
            tower = portal.ProjectAccess(company_id=main.id, project="PS Tower", code="PSTW", allow_media=True,
                                         share_token="ps-tower-share-token")
            annex = portal.ProjectAccess(company_id=main.id, project="PS Annex", code="PSAX", allow_media=True,
                                         share_token="ps-annex-share-token")
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=main.id, user_id=users["admin"].id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=main.id, user_id=users["employee"].id, role="employee", status="active"),
                portal.CompanyMembership(company_id=other.id, user_id=users["other"].id, role="company_admin", status="active"),
                portal.ProjectAssignment(company_id=main.id, user_id=users["employee"].id, project="PS Tower"),
                tower, annex,
                portal.ProjectAccess(company_id=other.id, project="PS Elsewhere", code="PSEW", share_token="ps-else-token"),
                portal.ProjectFlightPath(company_id=main.id, project="PS Tower", date=SCAN_DATE, name="North pass",
                                         label="Route 1", direction="North → South",
                                         points=json.dumps([[44.04620, -123.08720], [44.04645, -123.08660],
                                                            [44.04670, -123.08605]])),
                portal.ProjectFlightPath(company_id=main.id, project="PS Tower", date=OTHER_DATE, name="Other day",
                                         points=json.dumps([[44.0463, -123.0871], [44.0466, -123.0862]])),
            ])
            portal.db.session.commit()
            cls.ids = {key: user.id for key, user in users.items()}
            cls.main_id, cls.other_id = main.id, other.id

    @classmethod
    def tearDownClass(cls):
        with portal.app.app_context():
            for scan in portal.ProjectScan.query.all():
                for name in [scan.filename] + scan.derived_files:
                    portal.delete_stored_file(name)

    # ---------------------------------------------------------------- helpers
    def client_for(self, who):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.ids[who]); state["_fresh"] = True; state["_csrf"] = "ps-csrf"
        return client

    def viewer(self, token="ps-tower-share-token", code="PSTW"):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["viewer_token"] = token; state["viewer_code"] = code; state["_csrf"] = "ps-csrf"
        return client

    def upload(self, who="admin", project="PS Tower", day=SCAN_DATE, content=None, name="site-topo.las", title="Topo scan"):
        content = las_bytes() if content is None else content
        return self.client_for(who).post(
            "/admin/projects/scans/upload",
            data={"project": project, "date": day.isoformat(), "name": title, "file": (BytesIO(content), name)},
            headers={"X-CSRF-Token": "ps-csrf", "X-Requested-With": "XMLHttpRequest"},
            content_type="multipart/form-data")

    def uploaded(self, **kwargs):
        response = self.upload(**kwargs)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with portal.app.app_context():
            scan = portal.db.session.get(portal.ProjectScan, response.get_json()["id"])
            portal.db.session.expunge(scan)
            return scan

    # ---------------------------------------------------------- conversion
    def test_georeferenced_scan_gets_a_map_overlay_and_3d_copy(self):
        scan = self.uploaded(title="North parcel topo")
        self.assertEqual(scan.status, "ready", scan.error)
        south_west, north_east = scan.overlay_bounds
        # UTM 10N 493000-493120 E, 4877000-4877080 N is 44.0461-44.0469 N, 123.0874-123.0859 W.
        self.assertAlmostEqual(south_west[0], 44.04613, delta=0.0002)
        self.assertAlmostEqual(south_west[1], -123.08738, delta=0.0002)
        self.assertAlmostEqual(north_east[0], 44.04685, delta=0.0002)
        self.assertAlmostEqual(north_east[1], -123.08588, delta=0.0002)
        self.assertGreater(north_east[0], south_west[0]); self.assertGreater(north_east[1], south_west[1])
        self.assertAlmostEqual((north_east[0] - south_west[0]) * 111_000, 80, delta=6)  # about 80 m tall
        for name in [scan.filename] + scan.derived_files:
            self.assertTrue((portal.UPLOAD_DIR / name).is_file(), name)
        self.assertEqual(len(scan.derived_files), 3)
        derived = sum((portal.UPLOAD_DIR / name).stat().st_size for name in scan.derived_files)
        self.assertEqual(scan.file_size, scan.source_size + derived)  # quota counts the copies
        admin = self.client_for("admin")
        page = admin.get(f"/admin/site-capture?map_project=PS%20Tower&map_date={SCAN_DATE.isoformat()}").get_data(as_text=True)
        self.assertIn("North parcel topo", page)
        self.assertIn(f"/admin/projects/scans/{scan.id}/3d", page)
        data = json.loads(re.search(r'<script type="application/json" id="site-scan-data">(.*?)</script>', page, re.S).group(1))
        mine = next(item for item in data if item["id"] == scan.id)
        self.assertEqual(mine["bounds"], scan.overlay_bounds)
        self.assertEqual(admin.get(mine["overlay"]).data[:4], b"\x89PNG")
        self.assertEqual(admin.get(mine["thumb"]).data[:4], b"\x89PNG")
        self.assertEqual(admin.get(f"/admin/projects/scans/{scan.id}/points").data[:4], b"CVPC")
        download = admin.get(mine["download"])
        self.assertEqual(download.data[:4], b"LASF")
        self.assertIn("attachment", download.headers["Content-Disposition"])
        # Another date of the same project doesn't list it.
        other_day = admin.get(f"/admin/site-capture?map_project=PS%20Tower&map_date={OTHER_DATE.isoformat()}").get_data(as_text=True)
        self.assertNotIn("North parcel topo", other_day)

    def test_3d_view_draws_that_dates_flight_paths_in_scan_coordinates(self):
        scan = self.uploaded()
        page = self.client_for("admin").get(f"/admin/projects/scans/{scan.id}/3d").get_data(as_text=True)
        self.assertIn('type="importmap"', page)
        self.assertIn("scan-viewer.js", page)
        config = json.loads(re.search(r'id="scan-3d-config">(.*?)</script>', page, re.S).group(1))
        self.assertTrue(config["src"].endswith(f"/admin/projects/scans/{scan.id}/points"))
        self.assertEqual([path["label"] for path in config["paths"]], ["Route 1"])  # not the other date's path
        span = scan_convert.read_header(portal.UPLOAD_DIR / scan.points_filename)["max"]
        for x, y in config["paths"][0]["points"]:
            self.assertTrue(-5 <= x <= span[0] + 5 and -5 <= y <= span[1] + 5, (x, y, span))

    def test_scan_without_coordinates_is_3d_only(self):
        scan = self.uploaded(content=las_bytes(crs=False), title="Local scan")
        self.assertEqual(scan.status, "ready", scan.error)
        self.assertIsNone(scan.overlay_bounds)
        self.assertEqual(scan.overlay_filename, "")
        page = self.client_for("admin").get(f"/admin/projects/scans/{scan.id}/3d").get_data(as_text=True)
        self.assertIn("no map coordinates", page)
        self.assertEqual(json.loads(re.search(r'id="scan-3d-config">(.*?)</script>', page, re.S).group(1))["paths"], [])

    def test_oversized_point_records_fail_cleanly(self):
        scan = self.uploaded(content=las_bytes(count=500, extra_dims=20), title="Padded scan")
        self.assertEqual(scan.status, "error")
        self.assertIn("unusually large point records", scan.error)
        self.assertEqual(scan.derived_files, [])

    # ------------------------------------------------------------- uploads
    def test_upload_rejects_fakes_wrong_types_and_unknown_projects(self):
        self.assertEqual(self.upload(content=b"not a scan" * 50).status_code, 400)
        self.assertEqual(self.upload(name="site.zip").status_code, 400)
        self.assertEqual(self.upload(project="PS Nowhere").status_code, 400)
        self.assertEqual(self.upload(project="PS Elsewhere").status_code, 400)  # another company's project

    def test_only_admins_upload_and_other_companies_cannot_reach_scans(self):
        self.assertEqual(self.upload(who="employee").status_code, 403)
        scan = self.uploaded()
        other = self.client_for("other")
        for path in (f"/admin/projects/scans/{scan.id}/overlay", f"/admin/projects/scans/{scan.id}/3d",
                     f"/admin/projects/scans/{scan.id}/download", f"/admin/projects/scans/{scan.id}/status"):
            self.assertEqual(other.get(path).status_code, 404, path)
        response = other.post(f"/admin/projects/scans/{scan.id}/delete", data={"_csrf": "ps-csrf"})
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_original_and_converted_files(self):
        scan = self.uploaded(title="Delete me")
        files = [scan.filename] + scan.derived_files
        response = self.client_for("admin").post(f"/admin/projects/scans/{scan.id}/delete", data={"_csrf": "ps-csrf"})
        self.assertEqual(response.status_code, 302)
        for name in files:
            self.assertFalse((portal.UPLOAD_DIR / name).exists(), name)
        with portal.app.app_context():
            self.assertIsNone(portal.db.session.get(portal.ProjectScan, scan.id))

    def test_reprocess_is_refused_while_running_and_allowed_when_stale(self):
        scan = self.uploaded(title="Stuck scan")
        admin = self.client_for("admin")
        with portal.app.app_context():
            row = portal.db.session.get(portal.ProjectScan, scan.id)
            row.status, row.processing_started_at = "processing", portal.now_utc()
            portal.db.session.commit()
        admin.post(f"/admin/projects/scans/{scan.id}/reprocess", data={"_csrf": "ps-csrf"})
        self.assertEqual(admin.get(f"/admin/projects/scans/{scan.id}/status").get_json()["status"], "processing")
        with portal.app.app_context():
            row = portal.db.session.get(portal.ProjectScan, scan.id)
            row.processing_started_at = portal.now_utc() - timedelta(hours=2)
            portal.db.session.commit()
        self.assertEqual(admin.get(f"/admin/projects/scans/{scan.id}/status").get_json()["status"], "error")
        admin.post(f"/admin/projects/scans/{scan.id}/reprocess", data={"_csrf": "ps-csrf"})
        self.assertEqual(admin.get(f"/admin/projects/scans/{scan.id}/status").get_json()["status"], "ready")

    # -------------------------------------------------------------- clients
    def test_client_map_shows_the_scan_for_its_date_and_compare(self):
        scan = self.uploaded(title="Client visible scan")
        client = self.viewer()
        page = client.get(f"/share/ps-tower-share-token?tab=map&date={SCAN_DATE.isoformat()}").get_data(as_text=True)
        scans = json.loads(re.search(r'id="viewer-scans-by-date" type="application/json">(.*?)</script>', page, re.S).group(1))
        mine = next(item for item in scans[SCAN_DATE.isoformat()] if item["id"] == scan.id)
        self.assertTrue(mine["overlay"].startswith(f"/share/ps-tower-share-token/scans/{scan.id}/"))
        self.assertIn('id="viewer-scan-toggle"', page)
        self.assertIn(mine["view3d"], page)
        self.assertIn(f'value="{SCAN_DATE.isoformat()}"', page)  # the scan date is offered for comparison
        self.assertEqual(client.get(mine["overlay"]).data[:4], b"\x89PNG")
        self.assertEqual(client.get(f"/share/ps-tower-share-token/scans/{scan.id}/points").data[:4], b"CVPC")
        view = client.get(mine["view3d"]).get_data(as_text=True)
        self.assertIn("Route 1", view)
        self.assertNotIn("/admin/", view)

    def test_client_scan_access_requires_unlocking_the_right_project(self):
        scan = self.uploaded()
        path = f"/share/ps-tower-share-token/scans/{scan.id}/overlay"
        self.assertEqual(portal.app.test_client().get(path).status_code, 403)  # not unlocked
        self.assertEqual(self.viewer(token="ps-tower-share-token", code="PSAX").get(path).status_code, 403)
        annex = self.viewer(token="ps-annex-share-token", code="PSAX")
        self.assertEqual(annex.get(f"/share/ps-annex-share-token/scans/{scan.id}/overlay").status_code, 404)
        self.assertEqual(self.viewer().get(f"/share/ps-tower-share-token/scans/{scan.id}/nope").status_code, 404)
        with portal.app.app_context():
            access = portal.ProjectAccess.query.filter_by(share_token="ps-tower-share-token").first()
            access.allow_media = False
            portal.db.session.commit()
        try:
            self.assertEqual(self.viewer().get(path).status_code, 403)
        finally:
            with portal.app.app_context():
                access = portal.ProjectAccess.query.filter_by(share_token="ps-tower-share-token").first()
                access.allow_media = True
                portal.db.session.commit()

    def test_scan_routes_never_become_flight_uploads(self):
        """Scan transfers use their own direction, so the flight upload routes can't complete them."""
        self.assertIn(portal.SCAN_TRANSFER_DIRECTION, portal.NON_FLIGHT_TRANSFER_DIRECTIONS)
        self.assertIn(portal.MEDIA_TRANSFER_DIRECTION, portal.NON_FLIGHT_TRANSFER_DIRECTIONS)


if __name__ == "__main__":
    unittest.main()
