"""Media library: uploads, permissions, tenant isolation and public share links."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import json
import subprocess
import sys
import unittest
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import app as portal

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 200


class MediaLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)
        with portal.app.app_context():
            portal.db.create_all()
            main = portal.Company(name="ML Main Co", status="active", storage_limit_bytes=10**9)
            other = portal.Company(name="ML Other Co", status="active", storage_limit_bytes=10**9)
            users = {
                "admin": portal.User(username="ml-admin", name="ML admin", is_admin=True, active=True),
                "employee": portal.User(username="ml-employee", name="ML employee", is_admin=False, active=True),
                "employee2": portal.User(username="ml-employee2", name="ML employee two", is_admin=False, active=True),
                "other": portal.User(username="ml-other", name="ML other", is_admin=True, active=True),
            }
            for user in users.values():
                user.set_password("test-password-123")
            portal.db.session.add_all([main, other, *users.values()])
            portal.db.session.flush()
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=main.id, user_id=users["admin"].id, role="company_admin", status="active"),
                portal.CompanyMembership(company_id=main.id, user_id=users["employee"].id, role="employee", status="active"),
                portal.CompanyMembership(company_id=main.id, user_id=users["employee2"].id, role="employee", status="active"),
                portal.CompanyMembership(company_id=other.id, user_id=users["other"].id, role="company_admin", status="active"),
                portal.ProjectAssignment(company_id=main.id, user_id=users["employee"].id, project="ML Tower"),
                portal.ProjectAssignment(company_id=main.id, user_id=users["employee2"].id, project="ML Tower"),
                # Projects exist once any project record uses the name.
                portal.ProjectAccess(company_id=main.id, project="ML Tower", code="MLTW"),
                portal.ProjectAccess(company_id=main.id, project="ML Annex", code="MLAX"),
                portal.ProjectAccess(company_id=other.id, project="ML Elsewhere", code="MLEW"),
            ])
            portal.db.session.commit()
            cls.ids = {key: user.id for key, user in users.items()}
            cls.main_id, cls.other_id = main.id, other.id

    @classmethod
    def tearDownClass(cls):
        with portal.app.app_context():
            for row in portal.MediaVideo.query.filter(portal.MediaVideo.company_id.in_([cls.main_id, cls.other_id])).all():
                portal.delete_stored_file(row.filename)

    # ---------------------------------------------------------------- helpers
    def client_for(self, who):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = str(self.ids[who]); state["_fresh"] = True; state["_csrf"] = "ml-csrf"
        return client

    def anonymous(self):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state["_csrf"] = "ml-csrf"
        return client

    def post(self, client, path, **kwargs):
        return client.post(path, headers={"X-CSRF-Token": "ml-csrf", "X-Requested-With": "XMLHttpRequest"}, **kwargs)

    def upload(self, who="admin", project="ML Tower", title="Slab pour", tags="Concrete, Week 12",
               content=MP4, name="pour.mp4", **extra):
        data = {"project": project, "title": title, "tags": tags, "file": (BytesIO(content), name), **extra}
        return self.post(self.client_for(who), "/media/upload", data=data, content_type="multipart/form-data")

    def uploaded_id(self, **kwargs):
        response = self.upload(**kwargs)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def share(self, who="admin", **fields):
        client = self.client_for(who)
        response = client.post("/media/share", data={"_csrf": "ml-csrf", **fields})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        with portal.app.app_context():
            return portal.MediaShare.query.order_by(portal.MediaShare.id.desc()).first().token

    def share_row(self, token):
        with portal.app.app_context():
            return portal.MediaShare.query.filter_by(token=token).first()

    # ----------------------------------------------------------------- upload
    def test_upload_stores_title_tags_and_project(self):
        video_id = self.uploaded_id(title="Roof deck", tags="Roof, roof, Week 3")
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, video_id)
            self.assertEqual((video.project, video.title), ("ML Tower", "Roof deck"))
            self.assertEqual(video.tag_list, ["Roof", "Week 3"])
            self.assertEqual(video.file_size, len(MP4))
            self.assertEqual(video.company_id, self.main_id)
        page = self.client_for("admin").get("/media/").get_data(as_text=True)
        self.assertIn("Roof deck", page)
        self.assertIn("Week 3", page)

    def test_upload_rejects_wrong_formats_and_missing_project(self):
        self.assertEqual(self.upload(content=b"not a video at all" * 10).status_code, 400)
        self.assertEqual(self.upload(name="clip.avi").status_code, 400)
        self.assertEqual(self.upload(name="notes.txt").status_code, 400)
        self.assertEqual(self.upload(project="").status_code, 400)
        self.assertEqual(self.upload(content=WEBM, name="clip.webm").status_code, 200)

    def test_employee_uploads_only_to_assigned_projects(self):
        self.assertEqual(self.upload(who="employee", project="ML Tower").status_code, 200)
        self.assertEqual(self.upload(who="employee", project="ML Annex").status_code, 403)
        self.assertEqual(self.upload(who="employee", project="", new_project="ML Brand New").status_code, 403)

    def test_admin_can_start_a_new_project(self):
        self.assertEqual(self.upload(project="", new_project="ML Fresh Site").status_code, 200)

    def test_employee_sees_only_assigned_project_videos(self):
        self.uploaded_id(project="ML Annex", title="Annex only footage")
        self.uploaded_id(project="ML Tower", title="Tower visible footage")
        page = self.client_for("employee").get("/media/").get_data(as_text=True)
        self.assertIn("Tower visible footage", page)
        self.assertNotIn("Annex only footage", page)

    def test_other_company_cannot_reach_videos(self):
        video_id = self.uploaded_id(title="Private to main")
        other = self.client_for("other")
        self.assertEqual(other.get(f"/media/file/{video_id}").status_code, 404)
        self.assertNotIn("Private to main", other.get("/media/").get_data(as_text=True))
        response = other.post("/media/share", data={"_csrf": "ml-csrf", "kind": "video", "video_id": video_id})
        self.assertEqual(response.status_code, 404)

    def test_team_member_can_play_their_company_video(self):
        video_id = self.uploaded_id()
        response = self.client_for("admin").get(f"/media/file/{video_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/mp4")
        self.assertEqual(response.data, MP4)
        self.assertEqual(self.anonymous().get(f"/media/file/{video_id}").status_code, 302)  # to sign in

    def test_edit_and_delete_need_uploader_or_admin(self):
        video_id = self.uploaded_id(who="employee", title="Mine")
        other_employee = self.client_for("employee2")
        response = other_employee.post(f"/media/{video_id}/edit", data={"_csrf": "ml-csrf", "title": "Hijack"})
        self.assertEqual(response.status_code, 403)
        response = self.client_for("employee").post(f"/media/{video_id}/edit",
                                                    data={"_csrf": "ml-csrf", "title": "Renamed", "tags": "A, B"})
        self.assertEqual(response.status_code, 302)
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, video_id)
            self.assertEqual((video.title, video.tag_list), ("Renamed", ["A", "B"]))
        token = self.share(who="employee", kind="video", video_id=video_id)
        self.assertEqual(self.client_for("admin").post(f"/media/{video_id}/delete", data={"_csrf": "ml-csrf"}).status_code, 302)
        with portal.app.app_context():
            self.assertIsNone(portal.db.session.get(portal.MediaVideo, video_id))
        self.assertEqual(self.anonymous().get(f"/share/video/{token}").status_code, 404)

    # ------------------------------------------------------------ video links
    def test_open_video_link_plays_for_anyone_and_counts_views_once(self):
        video_id = self.uploaded_id(title="Open clip")
        token = self.share(kind="video", video_id=video_id, mode="open")
        visitor = self.anonymous()
        page = visitor.get(f"/share/video/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Open clip", page.get_data(as_text=True))
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        media = visitor.get(f"/share/video/{token}/file")
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.data, MP4)
        ranged = visitor.get(f"/share/video/{token}/file", headers={"Range": "bytes=0-9"})
        self.assertEqual(ranged.status_code, 206)
        visitor.get(f"/share/video/{token}")
        self.assertEqual(self.share_row(token).view_count, 1)
        self.anonymous().get(f"/share/video/{token}")
        self.assertEqual(self.share_row(token).view_count, 2)

    def test_signed_in_user_from_another_company_can_open_a_link(self):
        video_id = self.uploaded_id(title="Cross tenant viewer")
        token = self.share(kind="video", video_id=video_id)
        response = self.client_for("other").get(f"/share/video/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client_for("other").get(f"/share/video/{token}/file").status_code, 200)

    def test_keyword_link_requires_the_keyword(self):
        video_id = self.uploaded_id(title="Secret pour")
        token = self.share(kind="video", video_id=video_id, mode="keyword", keyword="Harbor Week 12")
        row = self.share_row(token)
        self.assertNotIn("Harbor", row.keyword_hash)
        visitor = self.anonymous()
        gate = visitor.get(f"/share/video/{token}").get_data(as_text=True)
        self.assertIn("Enter the keyword", gate)
        self.assertNotIn("Secret pour", gate)
        self.assertEqual(visitor.get(f"/share/video/{token}/file").status_code, 403)
        wrong = self.post(visitor, f"/share/video/{token}", data={"keyword": "nope nope"})
        self.assertEqual(wrong.status_code, 302)
        self.assertEqual(self.share_row(token).failed_attempts, 1)
        right = self.post(visitor, f"/share/video/{token}", data={"keyword": "harbor week 12"})
        self.assertEqual(right.status_code, 302)
        self.assertIn("Secret pour", visitor.get(f"/share/video/{token}").get_data(as_text=True))
        self.assertEqual(visitor.get(f"/share/video/{token}/file").status_code, 200)
        self.assertEqual(self.share_row(token).failed_attempts, 0)
        # Unlocking one link never opens another.
        other_token = self.share(kind="video", video_id=video_id, mode="keyword", keyword="Another Key")
        self.assertEqual(visitor.get(f"/share/video/{other_token}/file").status_code, 403)

    def test_keyword_gate_locks_after_five_wrong_tries(self):
        video_id = self.uploaded_id()
        token = self.share(kind="video", video_id=video_id, mode="keyword", keyword="RightKey1")
        visitor = self.anonymous()
        for _ in range(5):
            self.post(visitor, f"/share/video/{token}", data={"keyword": "WrongKey"})
        self.assertIsNotNone(self.share_row(token).locked_until)
        self.post(visitor, f"/share/video/{token}", data={"keyword": "RightKey1"})
        self.assertEqual(visitor.get(f"/share/video/{token}/file").status_code, 403)
        self.assertIn("Too many incorrect", visitor.get(f"/share/video/{token}").get_data(as_text=True))

    def test_keyword_rules_and_expiry_validation(self):
        video_id = self.uploaded_id()
        client = self.client_for("admin")
        with portal.app.app_context():
            count = portal.MediaShare.query.count()
        client.post("/media/share", data={"_csrf": "ml-csrf", "kind": "video", "video_id": video_id,
                                          "mode": "keyword", "keyword": "ab"})
        client.post("/media/share", data={"_csrf": "ml-csrf", "kind": "video", "video_id": video_id,
                                          "expires_on": (date.today() - timedelta(days=2)).isoformat()})
        with portal.app.app_context():
            self.assertEqual(portal.MediaShare.query.count(), count)

    def test_revoked_and_expired_links_stop_working(self):
        video_id = self.uploaded_id()
        token = self.share(kind="video", video_id=video_id)
        row = self.share_row(token)
        self.assertEqual(self.client_for("employee").post(f"/media/share/{row.id}/revoke",
                                                          data={"_csrf": "ml-csrf"}).status_code, 403)
        self.assertEqual(self.client_for("admin").post(f"/media/share/{row.id}/revoke",
                                                       data={"_csrf": "ml-csrf"}).status_code, 302)
        self.assertEqual(self.anonymous().get(f"/share/video/{token}").status_code, 404)
        self.assertEqual(self.anonymous().get(f"/share/video/{token}/file").status_code, 404)
        expiring = self.share(kind="video", video_id=video_id, expires_on=date.today().isoformat())
        self.assertEqual(self.anonymous().get(f"/share/video/{expiring}").status_code, 200)
        with portal.app.app_context():
            share = portal.MediaShare.query.filter_by(token=expiring).first()
            share.expires_at = portal.now_utc() - timedelta(minutes=1)
            portal.db.session.commit()
        self.assertEqual(self.anonymous().get(f"/share/video/{expiring}").status_code, 404)

    def test_employee_can_share_and_revoke_own_link(self):
        video_id = self.uploaded_id(who="employee")
        token = self.share(who="employee", kind="video", video_id=video_id)
        row = self.share_row(token)
        self.assertIn(f"/share/video/{token}", self.client_for("employee").get("/media/").get_data(as_text=True))
        self.assertEqual(self.client_for("employee2").post(f"/media/share/{row.id}/revoke",
                                                           data={"_csrf": "ml-csrf"}).status_code, 403)
        self.assertEqual(self.client_for("employee").post(f"/media/share/{row.id}/revoke",
                                                          data={"_csrf": "ml-csrf"}).status_code, 302)

    def test_unknown_or_wrong_kind_tokens_404(self):
        video_id = self.uploaded_id()
        token = self.share(kind="video", video_id=video_id)
        self.assertEqual(self.anonymous().get(f"/share/library/{token}").status_code, 404)
        self.assertEqual(self.anonymous().get("/share/video/not-a-real-token").status_code, 404)
        self.assertEqual(self.anonymous().get("/share/video/" + "x" * 300).status_code, 404)

    # ---------------------------------------------------------- library links
    def test_admin_library_link_shows_every_project_and_filters_by_tag(self):
        tower = self.uploaded_id(project="ML Tower", title="Lib tower clip", tags="Exterior")
        annex = self.uploaded_id(project="ML Annex", title="Lib annex clip", tags="Interior")
        token = self.share(kind="library", mode="open")
        visitor = self.anonymous()
        page = visitor.get(f"/share/library/{token}").get_data(as_text=True)
        self.assertIn("Lib tower clip", page)
        self.assertIn("Lib annex clip", page)
        self.assertIn('data-tag="interior"', page)
        self.assertEqual(visitor.get(f"/share/library/{token}/file/{annex}").status_code, 200)
        self.assertEqual(visitor.get(f"/share/library/{token}/file/{tower}").status_code, 200)
        self.assertNotIn("Flight map", page)

    def test_employee_library_link_covers_only_assigned_projects(self):
        annex = self.uploaded_id(project="ML Annex", title="Emp lib annex")
        self.uploaded_id(project="ML Tower", title="Emp lib tower")
        token = self.share(who="employee", kind="library")
        visitor = self.anonymous()
        page = visitor.get(f"/share/library/{token}").get_data(as_text=True)
        self.assertIn("Emp lib tower", page)
        self.assertNotIn("Emp lib annex", page)
        self.assertEqual(visitor.get(f"/share/library/{token}/file/{annex}").status_code, 404)

    def test_library_link_never_serves_another_company_video(self):
        other_client = self.client_for("other")
        response = self.post(other_client, "/media/upload", content_type="multipart/form-data", data={
            "project": "ML Elsewhere", "title": "Other co clip", "file": (BytesIO(MP4), "o.mp4")})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        other_video = response.get_json()["id"]
        token = self.share(kind="library")
        self.assertEqual(self.anonymous().get(f"/share/library/{token}/file/{other_video}").status_code, 404)
        self.assertNotIn("Other co clip", self.anonymous().get(f"/share/library/{token}").get_data(as_text=True))

    def test_keyword_library_link_gates_files(self):
        video_id = self.uploaded_id(title="Gated library clip")
        token = self.share(kind="library", mode="keyword", keyword="Library Key")
        visitor = self.anonymous()
        self.assertNotIn("Gated library clip", visitor.get(f"/share/library/{token}").get_data(as_text=True))
        self.assertEqual(visitor.get(f"/share/library/{token}/file/{video_id}").status_code, 403)
        self.post(visitor, f"/share/library/{token}", data={"keyword": "library key"})
        self.assertIn("Gated library clip", visitor.get(f"/share/library/{token}").get_data(as_text=True))

    def test_library_flight_map_tab_is_limited_to_its_project(self):
        with portal.app.app_context():
            filename = "mltest_flight_tower.mp4"
            (portal.UPLOAD_DIR / filename).write_bytes(MP4)
            tower_flight = portal.Flight(company_id=self.main_id, project="ML Tower", date=date(2025, 10, 24), flight_id="MLT-1", uploaded_by=self.ids["admin"],
                                         direction="N-S", title="Tower flight", filename=filename,
                                         original_filename="tower.mp4", file_size=len(MP4))
            annex_flight = portal.Flight(company_id=self.main_id, project="ML Annex", date=date(2025, 10, 24), flight_id="MLA-1", uploaded_by=self.ids["admin"],
                                         direction="N-S", title="Annex flight", filename="mltest_flight_annex.mp4",
                                         original_filename="annex.mp4", file_size=len(MP4))
            portal.db.session.add_all([tower_flight, annex_flight])
            portal.db.session.flush()
            portal.db.session.add(portal.ProjectFlightPath(company_id=self.main_id, project="ML Tower",
                                                          date=date(2025, 10, 24), name="Route A",
                                                          points=json.dumps([[40.1, -76.1], [40.2, -76.2]]),
                                                          flight_id=tower_flight.id))
            portal.db.session.commit()
            tower_id, annex_id = tower_flight.id, annex_flight.id
        try:
            token = self.share(kind="library", show_map="1", map_project="ML Tower")
            visitor = self.anonymous()
            media = visitor.get(f"/share/library/{token}").get_data(as_text=True)
            self.assertIn("Flight map", media)
            page = visitor.get(f"/share/library/{token}?tab=map").get_data(as_text=True)
            self.assertIn('id="viewer-site-map"', page)
            self.assertIn("Route A", page)
            self.assertIn("Tower flight", page)
            self.assertNotIn("Annex flight", page)
            self.assertEqual(visitor.get(f"/share/library/{token}/flight/{tower_id}").status_code, 200)
            self.assertEqual(visitor.get(f"/share/library/{token}/flight/{annex_id}").status_code, 404)
            plain = self.share(kind="library")
            self.assertEqual(visitor.get(f"/share/library/{plain}/flight/{tower_id}").status_code, 403)
            self.assertNotIn("viewer-site-map", visitor.get(f"/share/library/{plain}?tab=map").get_data(as_text=True))
        finally:
            (portal.UPLOAD_DIR / "mltest_flight_tower.mp4").unlink(missing_ok=True)

    def test_map_project_must_be_visible_to_the_sharer(self):
        client = self.client_for("employee")
        with portal.app.app_context():
            count = portal.MediaShare.query.count()
        client.post("/media/share", data={"_csrf": "ml-csrf", "kind": "library", "show_map": "1",
                                          "map_project": "ML Annex"})
        with portal.app.app_context():
            self.assertEqual(portal.MediaShare.query.count(), count)

    def test_gate_posts_are_rate_limited(self):
        video_id = self.uploaded_id()
        token = self.share(kind="video", video_id=video_id, mode="keyword", keyword="RateKey1")
        visitor = self.anonymous()
        statuses = [self.post(visitor, f"/share/video/{token}", data={"keyword": "x" * 5},
                              environ_base={"REMOTE_ADDR": "10.9.8.7"}).status_code for _ in range(25)]
        self.assertIn(429, statuses)

    def test_media_appears_in_navigation(self):
        page = self.client_for("employee").get("/media/").get_data(as_text=True)
        self.assertIn('href="/media/"', page)
        self.assertEqual(self.anonymous().get("/media/").status_code, 302)


    # ------------------------------------------------------- downloads
    def test_everything_is_downloadable_as_the_original(self):
        video_id = self.uploaded_id(title="Download me", name="site-walk.mp4")
        team = self.client_for("admin").get(f"/media/download/{video_id}")
        self.assertEqual(team.status_code, 200)
        self.assertIn("attachment", team.headers["Content-Disposition"])
        self.assertIn("site-walk.mp4", team.headers["Content-Disposition"])
        self.assertEqual(team.data, MP4)
        token = self.share(kind="video", video_id=video_id)
        visitor = self.anonymous()
        page = visitor.get(f"/share/video/{token}").get_data(as_text=True)
        self.assertIn(f"/share/video/{token}/download", page)
        public = visitor.get(f"/share/video/{token}/download")
        self.assertEqual(public.status_code, 200)
        self.assertIn("attachment", public.headers["Content-Disposition"])
        library = self.share(kind="library")
        self.assertIn(f"/share/library/{library}/download/{video_id}", visitor.get(f"/share/library/{library}").get_data(as_text=True))
        self.assertEqual(visitor.get(f"/share/library/{library}/download/{video_id}").status_code, 200)
        locked = self.share(kind="video", video_id=video_id, mode="keyword", keyword="Download Key")
        self.assertEqual(self.anonymous().get(f"/share/video/{locked}/download").status_code, 403)

    def test_public_pages_are_locked_down_with_csp_and_no_inline_scripts(self):
        video_id = self.uploaded_id()
        token = self.share(kind="video", video_id=video_id)
        library = self.share(kind="library")
        for path in (f"/share/video/{token}", f"/share/library/{library}"):
            response = self.anonymous().get(path)
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline' ;", csp.replace("style-src 'self' 'unsafe-inline'", ""))
            html = response.get_data(as_text=True)
            for chunk in html.split("<script")[1:]:
                head = chunk.split(">", 1)[0]
                self.assertTrue("src=" in head or 'type="application/json"' in head, head)
            self.assertNotIn(" onclick=", html)
            self.assertNotIn(" onchange=", html)

    def test_library_play_endpoint_falls_back_to_original_file(self):
        video_id = self.uploaded_id()
        library = self.share(kind="library")
        data = self.anonymous().get(f"/share/library/{library}/play/{video_id}").get_json()
        self.assertEqual(data["embed"], "")
        self.assertTrue(data["src"].endswith(f"/file/{video_id}"))
        self.assertTrue(data["download"].endswith(f"/download/{video_id}"))

    # ------------------------------------------------- Cloudflare Stream
    def test_stream_player_is_used_once_ready(self):
        import media_library
        from unittest import mock
        video_id = self.uploaded_id(title="Stream ready clip")
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, video_id)
            video.stream_uid, video.stream_status = "abc123uid", "processing"
            portal.db.session.commit()
        token = self.share(kind="video", video_id=video_id)
        calls = []

        def fake(method, path, payload=None):
            calls.append((method, path))
            if method == "GET":
                return {"readyToStream": True, "status": {"state": "ready"},
                        "playback": {"hls": "https://customer-xyz.cloudflarestream.com/abc123uid/manifest/video.m3u8"}}
            return {"token": "signed.jwt.token"}

        with mock.patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_STREAM_API_TOKEN": "tok"}), \
                mock.patch.object(media_library, "stream_api", side_effect=fake):
            html = self.anonymous().get(f"/share/video/{token}").get_data(as_text=True)
        self.assertIn("https://customer-xyz.cloudflarestream.com/signed.jwt.token/iframe", html)
        self.assertIn(("POST", "/abc123uid/token"), calls)
        with portal.app.app_context():
            self.assertEqual(portal.db.session.get(portal.MediaVideo, video_id).stream_status, "ready")
        # Until Stream has a ready copy (or without Stream), the page plays the original file.
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, video_id)
            video.stream_status, video.stream_token = "processing", ""
            portal.db.session.commit()
        self.assertIn(f"/share/video/{token}/file", self.anonymous().get(f"/share/video/{token}").get_data(as_text=True))

    # -------------------------------------------------- viewer host isolation
    def test_viewer_host_is_isolated_from_the_platform(self):
        video_id = self.uploaded_id(title="Isolated clip")
        token = self.share(kind="video", video_id=video_id)
        portal.app.config["VIEWER_BASE_URL"] = "https://view.example.test"
        try:
            # Links point at the viewer host.
            self.assertIn(f"https://view.example.test/share/video/{token}",
                          self.client_for("admin").get("/media/").get_data(as_text=True))
            # The platform host no longer serves share pages; it redirects to the viewer host.
            moved = self.anonymous().get(f"/share/video/{token}")
            self.assertEqual(moved.status_code, 301)
            self.assertEqual(moved.headers["Location"], f"https://view.example.test/share/video/{token}")
            viewer = portal.app.test_client()
            page = viewer.get(f"/share/video/{token}", base_url="https://view.example.test")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Isolated clip", page.get_data(as_text=True))
            self.assertEqual(viewer.get(f"/share/video/{token}/file", base_url="https://view.example.test").status_code, 200)
            # Nothing but share pages exists on the viewer host.
            for path in ("/media/", "/login", "/dashboard", "/admin", f"/media/file/{video_id}", "/owner"):
                self.assertEqual(viewer.get(path, base_url="https://view.example.test").status_code, 404, path)
            self.assertEqual(viewer.get("/", base_url="https://view.example.test").status_code, 302)
            # A signed-in platform session is never honoured on the viewer host.
            signed_in = self.client_for("admin")
            self.assertEqual(signed_in.get(f"/media/file/{video_id}", base_url="https://view.example.test").status_code, 404)
            # The viewer gets its own session cookie.
            keyword = self.share(kind="video", video_id=video_id, mode="keyword", keyword="Viewer Key")
            gate = viewer.get(f"/share/video/{keyword}", base_url="https://view.example.test")
            cookies = " ".join(gate.headers.getlist("Set-Cookie"))
            self.assertIn("civ_view=", cookies)
            self.assertNotIn("session=", cookies.replace("civ_view=", ""))
        finally:
            portal.app.config["VIEWER_BASE_URL"] = ""

    def test_platform_session_cookie_is_not_accepted_as_a_viewer_cookie(self):
        from flask.sessions import SecureCookieSessionInterface
        import media_library
        portal.app.config["VIEWER_BASE_URL"] = "https://view.example.test"
        try:
            with portal.app.test_request_context("/", base_url="https://app.example.test"):
                platform = SecureCookieSessionInterface().get_signing_serializer(portal.app).dumps({"_user_id": "1"})
            with portal.app.test_request_context("/", base_url="https://view.example.test"):
                serializer = media_library.ViewerSessionInterface().get_signing_serializer(portal.app)
                with self.assertRaises(Exception):
                    serializer.loads(platform)
        finally:
            portal.app.config["VIEWER_BASE_URL"] = ""

    # ------------------------------------------------ resumable uploads
    def test_direct_to_cloud_upload_creates_the_video(self):
        import media_library
        from unittest import mock
        objects = {}

        class FakeR2:
            def start_multipart(self, key, content_type): objects[key] = b""; return "mp-1"
            def signed_upload_part(self, key, upload_id, part_number, expires=900): return f"https://r2.test/{part_number}"
            def list_multipart_parts(self, key, upload_id): return [{"PartNumber": 1, "Size": len(MP4), "ETag": "e1"}] if objects.get(key + ":part") else []
            def complete_multipart(self, key, upload_id, parts): objects[key] = MP4
            def object_size(self, key): return len(objects.get(key, b""))
            def read_prefix(self, key, size=32): return objects[key][:size]
            def delete(self, key): objects.pop(key, None)
            def abort_multipart(self, key, upload_id): pass
            def signed_download(self, key, filename, content_type, download, expires=300): return "https://r2.test/get"

        admin = self.client_for("admin")
        headers = {"X-CSRF-Token": "ml-csrf"}
        with mock.patch.object(portal, "cloud_storage_enabled", return_value=True), \
                mock.patch.object(portal, "R2Storage", FakeR2):
            start = admin.post("/media/r2-upload/start", headers=headers, json={
                "filename": "big-tour.mp4", "size": len(MP4), "lastModified": 1, "project": "ML Tower",
                "title": "Big tour", "tags": "Drone, Big"})
            self.assertEqual(start.status_code, 200, start.get_data(as_text=True))
            upload_id = start.get_json()["uploadId"]
            # Drone-flight routes refuse media transfers.
            self.assertEqual(admin.post(f"/admin/r2-upload/{upload_id}/complete", headers=headers, json={}).status_code, 404)
            self.assertEqual(admin.post(f"/admin/r2-upload/{upload_id}/part-url", headers=headers, json={"partNumber": 1}).status_code, 404)
            part = admin.post(f"/media/r2-upload/{upload_id}/part-url", headers=headers, json={"partNumber": 1})
            self.assertEqual(part.status_code, 200)
            with portal.app.app_context():
                key = portal.db.session.get(portal.UploadTransfer, upload_id).object_key
            objects[key + ":part"] = True
            done = admin.post(f"/media/r2-upload/{upload_id}/complete", headers=headers, json={})
            self.assertEqual(done.status_code, 200, done.get_data(as_text=True))
            other = self.client_for("employee2").post(f"/media/r2-upload/{upload_id}/complete", headers=headers, json={})
            self.assertEqual(other.status_code, 404)
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, done.get_json()["id"])
            self.assertEqual((video.title, video.project, video.tag_list), ("Big tour", "ML Tower", ["Drone", "Big"]))
            self.assertTrue(video.filename.startswith("r2__companies/"))
            self.assertEqual(portal.db.session.get(portal.UploadTransfer, upload_id).status, "completed")
            portal.db.session.delete(video)
            portal.db.session.commit()

    def test_direct_upload_needs_cloud_storage_and_a_permitted_project(self):
        from unittest import mock
        headers = {"X-CSRF-Token": "ml-csrf"}
        body = {"filename": "a.mp4", "size": 100, "lastModified": 1, "project": "ML Annex"}
        self.assertEqual(self.client_for("employee").post("/media/r2-upload/start", headers=headers, json=body).status_code, 409)
        with mock.patch.object(portal, "cloud_storage_enabled", return_value=True):
            self.assertEqual(self.client_for("employee").post("/media/r2-upload/start", headers=headers, json=body).status_code, 403)
            bad = dict(body, filename="a.avi", project="ML Tower")
            self.assertEqual(self.client_for("employee").post("/media/r2-upload/start", headers=headers, json=bad).status_code, 400)

    # ------------------------------------------------ expiry in local time
    def test_expiry_uses_the_creators_local_day(self):
        from datetime import datetime, time
        video_id = self.uploaded_id()
        local_today = (portal.now_utc() - timedelta(minutes=300)).date()
        token = self.share(kind="video", video_id=video_id, expires_on=local_today.isoformat(), tz_offset="300")
        expected = datetime.combine(local_today, time.max) + timedelta(minutes=300)
        self.assertEqual(self.share_row(token).expires_at, expected)
        self.assertEqual(self.anonymous().get(f"/share/video/{token}").status_code, 200)

    def test_link_to_video_outside_employee_projects_is_labelled_not_deleted(self):
        video_id = self.uploaded_id(project="ML Tower", title="Moved away clip")
        self.share(who="employee", kind="video", video_id=video_id)
        with portal.app.app_context():
            portal.db.session.get(portal.MediaVideo, video_id).project = "ML Annex"
            portal.db.session.commit()
        page = self.client_for("employee").get("/media/").get_data(as_text=True)
        self.assertIn("Moved away clip", page)
        self.assertIn("not in your projects", page)


    # ---------------------------------------------- second review fixes
    def test_dot_segment_paths_cannot_reach_private_uploads(self):
        video_id = self.uploaded_id()
        with portal.app.app_context():
            stored = portal.db.session.get(portal.MediaVideo, video_id).filename
        for path in (f"/static/./uploads/{stored}", f"/static/x/../uploads/{stored}", f"/static/uploads/{stored}"):
            response = self.anonymous().get("/", environ_overrides={"PATH_INFO": path})
            self.assertEqual(response.status_code, 404, path)
        portal.app.config["VIEWER_BASE_URL"] = "https://view.example.test"
        try:
            viewer = portal.app.test_client()
            for path in (f"/static/./uploads/{stored}", "/static/style.css", "/static/project-viewer-ui.js"):
                response = viewer.get("/", base_url="https://view.example.test", environ_overrides={"PATH_INFO": path})
                self.assertEqual(response.status_code, 404, path)
            self.assertEqual(viewer.get("/static/media-share.css", base_url="https://view.example.test").status_code, 200)
        finally:
            portal.app.config["VIEWER_BASE_URL"] = ""

    def test_trailing_dot_or_case_variants_of_the_viewer_host_stay_isolated(self):
        portal.app.config["VIEWER_BASE_URL"] = "https://view.example.test"
        try:
            viewer = portal.app.test_client()
            for host in ("view.example.test.", "VIEW.Example.Test", "view.example.test.:443"):
                response = viewer.get("/login", headers={"Host": host}, base_url="https://view.example.test")
                self.assertEqual(response.status_code, 404, host)
                self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        finally:
            portal.app.config["VIEWER_BASE_URL"] = ""

    def test_viewer_errors_are_plain_pages_without_platform_links(self):
        video_id = self.uploaded_id()
        locked = self.share(kind="video", video_id=video_id, mode="keyword", keyword="Error Key")
        portal.app.config["VIEWER_BASE_URL"] = "https://view.example.test"
        try:
            viewer = portal.app.test_client()
            for path, code in ((f"/share/video/{locked}/file", 403), ("/share/video/nope", 404), ("/dashboard", 404)):
                response = viewer.get(path, base_url="https://view.example.test")
                self.assertEqual(response.status_code, code, path)
                html = response.get_data(as_text=True)
                self.assertIn("ms-card", html)
                self.assertNotIn("/login", html)
                self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        finally:
            portal.app.config["VIEWER_BASE_URL"] = ""
        # The platform's own error pages are unchanged.
        self.assertNotIn("ms-card", self.client_for("other").get("/media/file/999999").get_data(as_text=True))

    def test_employee_uploads_count_every_project_toward_the_quota(self):
        with portal.app.app_context():
            company = portal.db.session.get(portal.Company, self.main_id)
            used = portal.company_storage_bytes(self.main_id)
            company.storage_limit_bytes = used + len(MP4) + 10
            portal.db.session.commit()
        try:
            self.assertEqual(self.upload(who="admin", project="ML Annex").status_code, 200)  # fills the quota
            response = self.upload(who="employee", project="ML Tower")
            self.assertEqual(response.status_code, 400)
            self.assertIn("storage", response.get_json()["error"])
        finally:
            with portal.app.app_context():
                portal.db.session.get(portal.Company, self.main_id).storage_limit_bytes = 10**9
                portal.db.session.commit()

    def test_stream_tokens_are_cached_between_views(self):
        import media_library
        from unittest import mock
        video_id = self.uploaded_id()
        with portal.app.app_context():
            video = portal.db.session.get(portal.MediaVideo, video_id)
            video.stream_uid, video.stream_status, video.stream_host = "cacheuid", "ready", "customer-q.cloudflarestream.com"
            portal.db.session.commit()
        token = self.share(kind="video", video_id=video_id)
        fake = mock.Mock(return_value={"token": "cached.jwt"})
        with mock.patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_STREAM_API_TOKEN": "t"}), \
                mock.patch.object(media_library, "stream_api", fake):
            for _ in range(3):
                self.assertIn("cached.jwt/iframe", self.anonymous().get(f"/share/video/{token}").get_data(as_text=True))
        self.assertEqual(fake.call_count, 1)

    def test_retried_complete_returns_the_saved_video_instead_of_failing(self):
        from unittest import mock
        with portal.app.app_context():
            transfer = portal.UploadTransfer(id="ml-retry-1", company_id=self.main_id, user_id=self.ids["admin"],
                                             fingerprint="x", project="ML Tower", date=date.today(),
                                             direction=portal.MEDIA_TRANSFER_DIRECTION, title="t", original_filename="t.mp4",
                                             file_size=10, content_type="video/mp4", object_key="companies/x/media/retry",
                                             status="completed", expires_at=portal.now_utc() + timedelta(hours=1))
            portal.db.session.add(transfer)
            portal.db.session.commit()
        with mock.patch.object(portal, "cloud_storage_enabled", return_value=True):
            response = self.client_for("admin").post("/media/r2-upload/ml-retry-1/complete",
                                                     headers={"X-CSRF-Token": "ml-csrf"}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["duplicate"])


class MediaLibraryMigrationTests(unittest.TestCase):
    def test_migration_creates_media_tables_without_touching_existing_rows(self):
        root = Path(__file__).resolve().parents[1]
        code = """
from sqlalchemy import text, inspect
import app
with app.app.app_context():
    app.init_db()
    app.db.session.add(app.Company(name='Existing media customer', status='active', storage_limit_bytes=5))
    app.db.session.commit()
    for table in ('media_share', 'media_video'):
        app.db.session.execute(text(f'DROP TABLE {table}'))
    app.db.session.execute(text("DELETE FROM schema_migration WHERE revision='0025_media_library'"))
    app.db.session.commit()
    app.init_db(); app.init_db()
    tables = set(inspect(app.db.engine).get_table_names())
    assert {'media_video', 'media_share'} <= tables, tables
    assert app.SchemaMigration.query.filter_by(revision='0025_media_library').count() == 1
    assert app.Company.query.filter_by(name='Existing media customer').count() == 1
print('ok')
"""
        with __import__("tempfile").TemporaryDirectory() as tmp:
            environment = os.environ.copy()
            environment.update(DATABASE_URL=f"sqlite:///{Path(tmp, 'm.db').as_posix()}", SECRET_KEY="migration-test",
                               APP_ENV="testing", ADMIN_USERNAME="migration-owner", ADMIN_PASSWORD="migration-password-1")
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
