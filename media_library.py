"""Media library: team members upload labelled, tagged videos and share them by link.

Team pages live under /media. Public links live under /share/video/<token> (one
video) and /share/library/<token> (the whole library, with an optional flight map
for one project), so they inherit the platform's /share/ POST rate limit.

Isolation: when VIEWER_BASE_URL is set (e.g. https://view.captureiv.com), public
links are served only on that host. The viewer host answers nothing but share
pages and static assets, uses its own session cookie (name and signing salt), and
sends a strict Content-Security-Policy. Platform pages on the main host redirect
share URLs to the viewer host. Without VIEWER_BASE_URL everything stays on one
host (development and tests).

Playback: originals are kept for download. When Cloudflare Stream is configured
(CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_STREAM_API_TOKEN), each upload is also copied
to Stream, which plays on every device and browser; viewers get a short-lived
signed Stream player URL and fall back to the original file until Stream is ready.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, time, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from flask import (Blueprint, abort, current_app, flash, g, has_request_context, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from flask.sessions import SecureCookieSessionInterface
from flask_login import current_user
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import or_, select, update
from werkzeug.security import check_password_hash, generate_password_hash


VIDEO_TYPES = {"mp4", "m4v", "mov", "webm"}
MAX_SERVER_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
KEYWORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{3,79}")
MAX_KEYWORD_TRIES = 5
LOCKOUT = timedelta(minutes=15)
UNLOCKED_KEY = "media_share_unlocked"
VIEWED_KEY = "media_share_viewed"
SESSION_LIST_CAP = 40
# Long enough for a viewer to watch and seek a long video from a single page load.
SIGNED_PLAYBACK_SECONDS = 2 * 60 * 60
STREAM_TOKEN_SECONDS = 2 * 60 * 60
# Reuse a signed Stream token until less than this much of its life remains.
STREAM_TOKEN_REUSE_MARGIN = timedelta(minutes=45)
# Ask Stream for a video's processing status at most this often.
STREAM_POLL_INTERVAL = timedelta(seconds=60)
STREAM_TIMEOUT_SECONDS = 4
# The viewer host serves only the static files share pages need.
VIEWER_STATIC_PREFIXES = ("media-share", "brand-logo.css", "favicon.svg", "vendor/leaflet/", "vendor/georaster",
                          "project-viewer-map.js", "project-viewer.css")
VIEWER_SESSION_COOKIE = "civ_view"
VIEWER_SESSION_SALT = "capture-iv-viewer-session"
PUBLIC_ENDPOINTS = {
    "media_library.shared_video", "media_library.shared_video_file", "media_library.shared_video_download",
    "media_library.shared_library", "media_library.shared_library_file", "media_library.shared_library_download",
    "media_library.shared_library_play", "media_library.shared_site_file", "media_library.shared_flight_file",
    "media_library.shared_flight_download",
}


def _video_mimetype(filename):
    """MP4, M4V and (H.264) MOV all play in browsers when served as video/mp4."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return "video/webm" if ext == "webm" else "video/mp4"


def _remember(key, value):
    """Add a value to a short, capped list kept in the visitor's session cookie."""
    values = [item for item in session.get(key, []) if item != value]
    values.append(value)
    session[key] = values[-SESSION_LIST_CAP:]


def viewer_base_url(app=None):
    app = app or current_app
    return (app.config.get("VIEWER_BASE_URL") or "").rstrip("/")


def _normal_host(value):
    """Host without port, case or a trailing dot ("View.Example.com.:443" -> "view.example.com")."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        value = value.split("]", 1)[0] + "]"
    else:
        value = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    return value.rstrip(".")


def viewer_host(app=None):
    base = viewer_base_url(app)
    return _normal_host(urlsplit(base).hostname or "") if base else ""


def on_viewer_host():
    host = viewer_host()
    return bool(host and has_request_context() and _normal_host(request.host) == host)


class ViewerSessionInterface(SecureCookieSessionInterface):
    """Platform and public viewer keep separate, non-interchangeable session cookies."""

    def get_cookie_name(self, app):
        return VIEWER_SESSION_COOKIE if on_viewer_host() else super().get_cookie_name(app)

    def get_signing_serializer(self, app):
        if not on_viewer_host():
            return super().get_signing_serializer(app)
        if not app.secret_key:
            return None
        return URLSafeTimedSerializer(app.secret_key, salt=VIEWER_SESSION_SALT, serializer=self.serializer,
                                      signer_kwargs={"key_derivation": self.key_derivation,
                                                     "digest_method": self.digest_method})


# ----------------------------------------------------------- Cloudflare Stream
def stream_settings():
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_STREAM_API_TOKEN", "").strip()
    return (account, token) if account and token else None


def stream_api(method, path, payload=None, timeout=STREAM_TIMEOUT_SECONDS):
    settings = stream_settings()
    if not settings:
        raise RuntimeError("Cloudflare Stream is not configured")
    account, token = settings
    url = f"https://api.cloudflare.com/client/v4/accounts/{quote(account)}/stream{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, method=method,
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
    except HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"Stream video not found for {method} {path}") from exc
        raise RuntimeError(f"Stream API {method} {path} failed with {exc.code}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Stream API {method} {path} unavailable") from exc
    if not body.get("success", True):
        raise RuntimeError(f"Stream API {method} {path} returned an error")
    return body.get("result") or {}


def stream_delete_uid(uid):
    """Remove a Stream copy (best effort; used by video and workspace deletion)."""
    if uid and stream_settings():
        try:
            stream_api("DELETE", f"/{quote(uid)}")
        except LookupError:
            pass
        except Exception:
            current_app.logger.warning("Could not delete Stream video %s", uid)


def create_media_library_blueprint(core):
    """Build the blueprint against the main application module (avoids circular imports)."""
    bp = Blueprint("media_library", __name__)
    db = core.db
    Video, Share, Transfer = core.MediaVideo, core.MediaShare, core.UploadTransfer
    share_table, video_table = Share.__table__, Video.__table__

    # ------------------------------------------------------- host isolation
    @bp.before_app_request
    def isolate_viewer_host():
        host = viewer_host()
        if not host:
            return None
        endpoint = request.endpoint or ""
        if on_viewer_host():
            if endpoint in PUBLIC_ENDPOINTS:
                return None
            if endpoint == "static":
                filename = (request.view_args or {}).get("filename", "")
                if filename.startswith(VIEWER_STATIC_PREFIXES) and ".." not in filename:
                    return None
                abort(404)
            if request.path == "/":
                return redirect(current_app.config.get("MARKETING_SITE_URL") or "https://captureiv.com")
            abort(404)
        if endpoint in PUBLIC_ENDPOINTS:
            # Platform host never serves public share pages; send visitors to the viewer host.
            return redirect(viewer_base_url() + request.full_path.rstrip("?"), code=301)
        return None

    def viewer_csp():
        r2_hosts = "https://*.r2.cloudflarestorage.com"
        endpoint = os.environ.get("R2_ENDPOINT_URL", "")
        endpoint_host = urlsplit(endpoint).netloc if endpoint else ""
        if endpoint_host:
            r2_hosts += f" https://{endpoint_host}"
        stream = "https://*.cloudflarestream.com"
        tiles = "https://*.tile.openstreetmap.org https://server.arcgisonline.com"
        return ("default-src 'none'; script-src 'self'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' data: blob: {tiles} {r2_hosts} {stream}; media-src 'self' blob: {r2_hosts} {stream}; "
                f"connect-src 'self' {r2_hosts} {stream}; frame-src {stream}; font-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

    def lockdown(response):
        response.headers["Content-Security-Policy"] = viewer_csp()
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        return response

    VIEWER_ERRORS = {400: "This request couldn't be understood.", 403: "This link needs its keyword first.",
                     404: "This link isn't available. It may have expired or been turned off.",
                     405: "That action isn't allowed here.", 410: "This link has expired.",
                     429: "Too many attempts. Wait a few minutes and try again.",
                     500: "Something went wrong. Please try again shortly."}

    @bp.after_app_request
    def lock_down_viewer_host(response):
        """Viewer-host responses (and public share errors anywhere) get the viewer policy and,
        for errors, a plain viewer page with no links into the platform. Platform error
        handlers are left untouched for every other request."""
        viewer = on_viewer_host()
        if not viewer and request.endpoint not in PUBLIC_ENDPOINTS:
            return response
        if response.status_code >= 400 and response.mimetype == "text/html":
            code = response.status_code
            response.set_data(render_template("media_share_error.html", code=code,
                                              message=VIEWER_ERRORS.get(code, VIEWER_ERRORS[500])))
            response.headers["Cache-Control"] = "no-store"
        return lockdown(response)

    def page(template, **context):
        response = current_app.make_response(render_template(template, **context))
        response.headers["Cache-Control"] = "no-store"
        return lockdown(response)

    # ------------------------------------------------------------------ access
    def member():
        """The signed-in company membership; media is open to every active member."""
        if not current_user.is_authenticated or not current_user.active:
            abort(401)
        membership = core.current_membership()
        if not membership or not membership.company or membership.company.status != "active":
            abort(403)
        return membership

    def is_admin(membership):
        return bool(current_user.is_admin or getattr(membership, "role", "") == "company_admin")

    def visible_projects(membership):
        """Projects this user may see (employees: assigned projects only)."""
        return sorted(core.company_project_names(membership.company_id))

    def owned_video(video_id):
        membership = member()
        video = Video.query.filter_by(id=video_id, company_id=membership.company_id).first()
        if not video:
            abort(404)
        return membership, video

    def can_manage(membership, video):
        return is_admin(membership) or video.uploaded_by == current_user.id

    def wants_json():
        return (request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json
                or request.accept_mimetypes.best == "application/json")

    def clean_tags(raw):
        return [tag for tag in (core.clean_line(tag, 40) for tag in core.parse_tags(raw)) if tag]

    def share_active(share):
        return bool(share and not share.revoked_at
                    and (not share.expires_at or share.expires_at > core.now_utc()))

    def share_url(share):
        endpoint = "media_library.shared_video" if share.kind == "video" else "media_library.shared_library"
        base = viewer_base_url()
        if base:
            return base + url_for(endpoint, token=share.token)
        return url_for(endpoint, token=share.token, _external=True)

    def share_projects(share):
        """The projects a library link covers, or None for every company project."""
        if share.projects_json is None:
            return None
        try:
            values = json.loads(share.projects_json)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(value) for value in values] if isinstance(values, list) else []

    def library_videos(share):
        query = Video.query.filter_by(company_id=share.company_id)
        projects = share_projects(share)
        if projects is not None:
            if not projects:
                return []
            query = query.filter(Video.project.in_(projects))
        return query.order_by(Video.uploaded_at.desc(), Video.id.desc()).all()

    def map_enabled(share):
        return bool(share.kind == "library" and share.show_map and share.map_project
                    and core.company_feature_enabled("flight_scans", share.company_id))

    def validate_project(membership, project, new_project):
        """Return (project, error, status)."""
        project = new_project or project
        if not project:
            return None, "Choose the project this video belongs to.", 400
        known = core.company_project_names(membership.company_id)
        if new_project and project not in known:
            if not is_admin(membership):
                return None, "Only company admins can start a new project.", 403
            available = core.project_capacity_available(project)
            db.session.rollback()
            if not available:
                return None, "Your plan's project limit has been reached. Choose an existing project.", 400
        elif project not in known or not core.user_can_upload_project(project):
            return None, "You can only upload to projects you're assigned to.", 403
        return project, None, 200

    def project_still_fits(company_id, project):
        """Re-check the plan's project limit at the moment a video is saved (call under the quota lock)."""
        return project in core.company_project_names(company_id) or core.project_capacity_available(project)

    # ------------------------------------------------------------ files
    def stream_original(filename, original_filename, download=False):
        content_type = _video_mimetype(original_filename or filename)
        return serve(filename, original_filename or "video", content_type, download,
                     expires=SIGNED_PLAYBACK_SECONDS)

    def serve(filename, original_filename, content_type, download=False, expires=300):
        if core.is_r2_filename(filename):
            try:
                target = core.R2Storage().signed_download(core.r2_object_key(filename), original_filename or "file",
                                                          content_type, download, expires=expires)
            except Exception:
                current_app.logger.exception("Could not create authorized R2 read URL for media")
                abort(503)
            return redirect(target, code=302)
        safe = core.Path(filename).name
        if not safe or not (core.UPLOAD_DIR / safe).is_file():
            abort(404)
        response = send_from_directory(str(core.UPLOAD_DIR), safe, conditional=True, mimetype=content_type,
                                       as_attachment=download,
                                       download_name=core.Path(original_filename or safe).name)
        response.headers["Cache-Control"] = "private, max-age=600"
        return response

    # -------------------------------------------------------- Stream helpers
    def stream_ingest(video):
        """Copy a freshly uploaded R2 original into Cloudflare Stream (best effort)."""
        if not stream_settings() or not core.is_r2_filename(video.filename):
            return
        try:
            source = core.R2Storage().signed_download(core.r2_object_key(video.filename),
                                                      video.original_filename or "video",
                                                      _video_mimetype(video.original_filename), False,
                                                      expires=6 * 60 * 60)
            result = stream_api("POST", "/copy", {"url": source, "requireSignedURLs": True,
                                                  "meta": {"name": video.title[:120]}})
            video.stream_uid = str(result.get("uid") or "")[:64]
            video.stream_status = "processing" if video.stream_uid else "error"
        except Exception:
            current_app.logger.exception("Could not send media video %s to Cloudflare Stream", video.id)
            video.stream_status = "error"
        db.session.commit()

    def stream_refresh(video):
        """Ask Stream whether a processing video is ready (throttled per video)."""
        if not video.stream_uid or video.stream_status != "processing" or not stream_settings():
            return
        now = core.now_utc()
        if video.stream_checked_at and now - video.stream_checked_at < STREAM_POLL_INTERVAL:
            return
        video.stream_checked_at = now
        try:
            result = stream_api("GET", f"/{quote(video.stream_uid)}")
        except LookupError:
            video.stream_status = "error"
            db.session.commit()
            return
        except Exception:
            current_app.logger.warning("Could not read Stream status for media video %s", video.id)
            db.session.commit()
            return
        state = (result.get("status") or {}).get("state", "")
        hls = (result.get("playback") or {}).get("hls", "")
        if result.get("readyToStream") and hls:
            video.stream_status, video.stream_host = "ready", urlsplit(hls).netloc[:160]
        elif state == "error":
            video.stream_status = "error"
        db.session.commit()

    def stream_embed(video):
        """A signed Stream player URL, or "" to fall back to the original file.

        Tokens are cached on the video and reused until close to expiry, so viewers
        never trigger more than one Cloudflare API call per video every ~75 minutes.
        """
        stream_refresh(video)
        host = video.stream_host or ""
        if (video.stream_status != "ready" or not host.endswith(".cloudflarestream.com")
                or not re.fullmatch(r"[a-z0-9.-]+", host)):
            return ""
        now = core.now_utc()
        if not (video.stream_token and video.stream_token_expires
                and video.stream_token_expires - now > STREAM_TOKEN_REUSE_MARGIN):
            if not stream_settings():
                return ""
            try:
                expires = now + timedelta(seconds=STREAM_TOKEN_SECONDS)
                exp = int((expires - datetime(1970, 1, 1)).total_seconds())
                token = stream_api("POST", f"/{quote(video.stream_uid)}/token", {"exp": exp}).get("token", "")
            except Exception:
                current_app.logger.warning("Could not sign Stream playback for media video %s", video.id)
                return ""
            if not token:
                return ""
            video.stream_token, video.stream_token_expires = token, expires
            db.session.commit()
        return f"https://{host}/{quote(video.stream_token)}/iframe?preload=metadata"

    stream_delete = stream_delete_uid

    def after_upload(video, original_name):
        core.audit("media_video_uploaded", "media_video", video.id,
                   json.dumps({"project": video.project, "title": video.title, "file": original_name}))
        stream_ingest(video)

    # ============================================================ team pages
    @bp.get("/media/")
    def index():
        membership = member()
        projects = visible_projects(membership)
        admin = is_admin(membership)
        uploadable = [project for project in projects if core.user_can_upload_project(project)]
        videos = (Video.query.filter_by(company_id=membership.company_id)
                  .order_by(Video.uploaded_at.desc(), Video.id.desc()).all())
        # Check a few processing videos per page load (each check is throttled and quick).
        for video in [item for item in videos if item.stream_status == "processing"][:3]:
            stream_refresh(video)
        all_tags = sorted({tag for video in videos for tag in video.tag_list}, key=str.casefold)
        tag = core.clean_line(request.args.get("tag"), 40)
        project = core.clean_line(request.args.get("project"), 160)
        search = core.clean_line(request.args.get("q"), 120)
        shown = [video for video in videos
                 if (not tag or tag.casefold() in {t.casefold() for t in video.tag_list})
                 and (not project or video.project == project)
                 and (not search or search.casefold() in (video.title or "").casefold())]
        share_query = Share.query.filter_by(company_id=membership.company_id)
        if not admin:
            share_query = share_query.filter_by(created_by=current_user.id)
        shares = share_query.order_by(Share.created_at.desc(), Share.id.desc()).all()
        # Titles straight from the table: a link may point at a video in a project this
        # employee no longer sees, which is different from a deleted video.
        linked_ids = {share.video_id for share in shares if share.video_id}
        titles = {}
        if linked_ids:
            rows = db.session.execute(select(video_table.c.id, video_table.c.title).where(
                video_table.c.company_id == membership.company_id, video_table.c.id.in_(linked_ids))).all()
            titles = {row.id: row.title for row in rows}
        visible_ids = {video.id for video in videos}
        creators = {}
        creator_ids = {share.created_by for share in shares} | {video.uploaded_by for video in videos}
        creator_ids.discard(None)
        if creator_ids:
            creators = {user.id: (user.name or user.username)
                        for user in core.User.query.filter(core.User.id.in_(creator_ids)).all()}
        created = None
        created_id = request.args.get("created", type=int)
        if created_id:
            created = next((share for share in shares if share.id == created_id), None)
        r2 = core.cloud_storage_enabled()
        return render_template(
            "media_library.html", videos=shown, total_videos=len(videos), all_tags=all_tags,
            projects=projects, uploadable=uploadable, admin=admin, tag=tag, project=project, search=search,
            shares=shares, titles=titles, visible_ids=visible_ids, creators=creators, created=created,
            share_url=share_url, share_active=share_active,
            sharing_enabled=core.company_feature_enabled("viewer_links", membership.company_id),
            map_available=core.company_feature_enabled("flight_scans", membership.company_id),
            can_manage=lambda video: can_manage(membership, video), now=core.now_utc(),
            today=core.now_utc().date(), r2_multipart=r2, stream_on=bool(stream_settings()),
            max_bytes=(current_app.config["MAX_CONTENT_LENGTH"] if r2 else MAX_SERVER_UPLOAD_BYTES))

    @bp.get("/media/file/<int:video_id>")
    def team_file(video_id):
        _membership, video = owned_video(video_id)
        return stream_original(video.filename, video.original_filename)

    @bp.get("/media/download/<int:video_id>")
    def team_download(video_id):
        _membership, video = owned_video(video_id)
        core.audit("media_video_downloaded", "media_video", video.id, "team")
        return stream_original(video.filename, video.original_filename, download=True)

    @bp.post("/media/upload")
    def upload():
        """Server-side upload (local storage, or files up to 2 GB when the browser can't use R2 direct)."""
        membership = member()

        def fail(message, status=400):
            if wants_json():
                return jsonify({"error": message}), status
            flash(message, "error")
            return redirect(url_for("media_library.index"))

        if request.content_length and request.content_length > MAX_SERVER_UPLOAD_BYTES + 1024 * 1024:
            return fail("Videos uploaded this way must be smaller than 2 GB.", 413)
        project, error, status = validate_project(membership, core.clean_line(request.form.get("project"), 160),
                                                  core.clean_line(request.form.get("new_project"), 160))
        if error:
            return fail(error, status)
        file = request.files.get("file")
        title = core.clean_line(request.form.get("title"), 200)
        if not title and file and file.filename:
            title = core.clean_line(core.Path(file.filename).stem.replace("_", " ").replace("-", " "), 200)
        tags = clean_tags(request.form.get("tags"))
        stored = None
        try:
            if not file or not file.filename:
                raise ValueError("Choose a video file.")
            ext = core.extension(file.filename)
            if ext not in VIDEO_TYPES:
                raise ValueError("Unsupported video type. Use MP4, MOV, M4V or WEBM.")
            stream_ = file.stream
            position = stream_.tell()
            header = stream_.read(core.UPLOAD_HEAD_BYTES)
            stream_.seek(0, os.SEEK_END)
            size = stream_.tell()
            stream_.seek(position)
            if not core.upload_signature_ok(ext, header):
                raise ValueError(f"The file does not look like a real .{ext} video.")
            if size <= 0 or size > MAX_SERVER_UPLOAD_BYTES:
                raise ValueError("Videos uploaded this way must be smaller than 2 GB.")
            stored = core.save_upload(file)
            core.lock_company_quota(membership.company_id)
            if not project_still_fits(membership.company_id, project):
                raise ValueError("Your plan's project limit has been reached. Choose an existing project.")
            video = Video(company_id=membership.company_id, project=project, title=title or "Untitled video",
                          tags=json.dumps(tags), filename=stored[0], original_filename=stored[3],
                          file_size=stored[1], checksum_sha256=stored[2] or "", uploaded_by=current_user.id)
            db.session.add(video)
            core.complete_storage_reservation(stored[4])
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            if stored:
                core.delete_stored_file(stored[0])
                core.release_storage_reservation(stored[4])
            if isinstance(exc, ValueError):
                return fail(str(exc))
            current_app.logger.exception("Media library upload failed")
            return fail("Upload failed. Please try again.", 500)
        after_upload(video, video.original_filename)
        if wants_json():
            return jsonify({"id": video.id, "title": video.title, "redirect": url_for("media_library.index")})
        flash(f"Uploaded “{video.title}”.", "success")
        return redirect(url_for("media_library.index"))

    # -------------------------------------- resumable direct-to-R2 uploads
    def transfer_json(transfer, storage):
        data = core.upload_transfer_json(transfer, storage)
        data.pop("completedFlightId", None)  # media uploads never complete as flights
        return data

    def own_transfer(upload_id, **filters):
        membership = member()
        transfer = Transfer.query.filter_by(id=upload_id, company_id=membership.company_id,
                                            user_id=current_user.id,
                                            direction=core.MEDIA_TRANSFER_DIRECTION, **filters).first()
        if not transfer:
            abort(404)
        return membership, transfer

    @bp.post("/media/r2-upload/start")
    def r2_start():
        membership = member()
        if not core.cloud_storage_enabled():
            return jsonify({"error": "Direct cloud uploads need private R2 storage."}), 409
        payload = request.get_json(silent=True) or {}
        original = core.safe_name(str(payload.get("filename", "")))
        try:
            file_size = int(payload.get("size", 0))
            last_modified = int(payload.get("lastModified", 0))
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "The selected file has invalid size information."}), 400
        if (core.extension(original) not in VIDEO_TYPES or file_size < 1 or last_modified < 0
                or file_size > current_app.config["MAX_CONTENT_LENGTH"]):
            limit_gb = current_app.config["MAX_CONTENT_LENGTH"] // (1024 ** 3)
            return jsonify({"error": f"Choose an MP4, MOV, M4V or WEBM video up to {limit_gb} GB."}), 400
        project, error, status = validate_project(membership, core.clean_line(payload.get("project"), 160),
                                                  core.clean_line(payload.get("new_project"), 160))
        if error:
            return jsonify({"error": error}), status
        title = core.clean_line(payload.get("title"), 200) or core.clean_line(
            core.Path(original).stem.replace("_", " ").replace("-", " "), 200) or "Untitled video"
        tags = clean_tags(payload.get("tags"))
        # Namespaced so a media upload can never resume as a drone-flight upload.
        fingerprint = hashlib.sha256(f"media\0{original}\0{file_size}\0{last_modified}\0{project}".encode()).hexdigest()
        core.expire_company_upload_transfers(membership.company_id)
        company = core.lock_company_quota(membership.company_id)
        if not company or company.status != "active":
            return jsonify({"error": "This workspace is not active for uploads."}), 403
        existing = (Transfer.query.filter_by(company_id=company.id, user_id=current_user.id, fingerprint=fingerprint,
                                             direction=core.MEDIA_TRANSFER_DIRECTION, status="uploading")
                    .filter(Transfer.expires_at > core.now_utc()).order_by(Transfer.created_at.desc()).first())
        if existing:
            try:
                return jsonify(transfer_json(existing, core.R2Storage()))
            except Exception:
                current_app.logger.exception("Could not inspect resumable media upload %s", existing.id)
                return jsonify({"error": "The saved upload could not be resumed. Try again shortly."}), 503
        now = core.now_utc()
        limit = core.company_storage_limit_bytes(company)
        used = core.company_storage_bytes(company.id)
        if not limit or used + core.company_reserved_bytes(company.id, now) + file_size > limit:
            db.session.rollback()
            return jsonify({"error": "This video would exceed your company's storage allowance."}), 413
        upload_id = str(uuid4())
        object_key = f"companies/{company.id}/media/{upload_id}-{original}"
        transfer = Transfer(id=upload_id, company_id=company.id, user_id=current_user.id, fingerprint=fingerprint,
                            project=project, date=now.date(), direction=core.MEDIA_TRANSFER_DIRECTION,
                            title=title, area="", tags=json.dumps(tags), original_filename=original,
                            file_size=file_size, content_type=_video_mimetype(original), object_key=object_key,
                            status="starting", expires_at=now + core.R2_UPLOAD_LIFETIME)
        db.session.add(transfer)
        storage = None
        try:
            db.session.flush()
            storage = core.R2Storage()
            transfer.multipart_upload_id = storage.start_multipart(object_key, transfer.content_type)
            transfer.status = "uploading"
            db.session.commit()
            return jsonify(transfer_json(transfer, storage))
        except Exception:
            db.session.rollback()
            if storage and transfer.multipart_upload_id:
                try:
                    storage.abort_multipart(object_key, transfer.multipart_upload_id)
                except Exception:
                    current_app.logger.exception("Could not clean up a failed media multipart start")
            current_app.logger.exception("Could not start resumable media upload")
            return jsonify({"error": "Could not start the cloud upload. Try again shortly."}), 503

    @bp.post("/media/r2-upload/<upload_id>/part-url")
    def r2_part_url(upload_id):
        _membership, transfer = own_transfer(upload_id, status="uploading")
        if transfer.expires_at <= core.now_utc() or not transfer.multipart_upload_id:
            abort(410)
        try:
            part_number = int((request.get_json(silent=True) or {}).get("partNumber", 0))
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "Invalid part number."}), 400
        if not 1 <= part_number <= math.ceil(transfer.file_size / core.R2_UPLOAD_PART_SIZE):
            return jsonify({"error": "That part is outside this upload."}), 400
        try:
            url = core.R2Storage().signed_upload_part(transfer.object_key, transfer.multipart_upload_id, part_number)
        except Exception:
            current_app.logger.exception("Could not sign media upload part")
            return jsonify({"error": "Could not authorize this upload part."}), 503
        return jsonify({"url": url})

    @bp.post("/media/r2-upload/<upload_id>/complete")
    def r2_complete(upload_id):
        membership, transfer = own_transfer(upload_id)
        if transfer.status == "completed":
            return jsonify({"redirect": url_for("media_library.index"), "duplicate": True})
        if transfer.status != "uploading" or transfer.expires_at <= core.now_utc():
            return jsonify({"error": "This upload has expired. Start it again to continue."}), 410
        if not core.user_can_upload_project(transfer.project) and not is_admin(membership):
            abort(403)
        company = core.lock_company_quota(transfer.company_id)
        if not company or company.status != "active":
            return jsonify({"error": "This workspace is not active for uploads."}), 403
        # A retried or parallel "complete" may have finished while we waited for the lock.
        db.session.refresh(transfer)
        if transfer.status == "completed":
            return jsonify({"redirect": url_for("media_library.index"), "duplicate": True})
        if transfer.status != "uploading":
            return jsonify({"error": "This upload can't be finished. Start it again."}), 410
        storage = core.R2Storage()
        part_size = core.R2_UPLOAD_PART_SIZE
        try:
            parts = sorted(storage.list_multipart_parts(transfer.object_key, transfer.multipart_upload_id),
                           key=lambda part: part["PartNumber"])
        except Exception:
            current_app.logger.exception("Could not list parts for media upload %s", upload_id)
            return jsonify({"error": "The upload could not be checked. Press Upload again to resume."}), 409
        expected = math.ceil(transfer.file_size / part_size)
        if [part.get("PartNumber") for part in parts] != list(range(1, expected + 1)):
            return jsonify({"error": "Some upload parts are missing. Retry to resume."}), 409
        for index, part in enumerate(parts):
            size = part_size if index < expected - 1 else transfer.file_size - part_size * (expected - 1)
            if int(part.get("Size", -1)) != size or not part.get("ETag"):
                return jsonify({"error": "An upload part didn't match its expected size. Retry to resume."}), 409
        object_key, multipart_id = transfer.object_key, transfer.multipart_upload_id
        try:
            if not project_still_fits(transfer.company_id, transfer.project):
                raise ValueError("Your plan's project limit has been reached")
            storage.complete_multipart(object_key, multipart_id, parts)
            if storage.object_size(object_key) != transfer.file_size:
                raise ValueError("Uploaded object size does not match the reserved size")
            ext = core.extension(transfer.original_filename)
            if not core.upload_signature_ok(ext, storage.read_prefix(object_key, core.UPLOAD_HEAD_BYTES)):
                storage.delete(object_key)
                transfer.status = "failed"
                db.session.commit()
                return jsonify({"error": f"The file does not look like a real .{ext} video."}), 400
            video = Video(company_id=transfer.company_id, project=transfer.project, title=transfer.title,
                          tags=transfer.tags or "[]", filename=core.R2_FILENAME_PREFIX + object_key,
                          original_filename=transfer.original_filename, file_size=transfer.file_size,
                          checksum_sha256="", uploaded_by=current_user.id)
            db.session.add(video)
            transfer.status = "completed"
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            try:
                storage.abort_multipart(object_key, multipart_id)  # parts of an unfinished upload
            except Exception:
                pass  # already completed or gone
            try:
                storage.delete(object_key)
            except Exception:
                current_app.logger.exception("Could not remove an unreferenced media upload object")
            if isinstance(exc, ValueError) and "project limit" in str(exc):
                failed = Transfer.query.filter_by(id=upload_id, company_id=membership.company_id).first()
                if failed:
                    failed.status = "failed"
                    db.session.commit()
                return jsonify({"error": "Your plan's project limit has been reached. Choose an existing project."}), 400
            failed = Transfer.query.filter_by(id=upload_id, company_id=membership.company_id).first()
            if failed:
                failed.status = "failed"
                db.session.commit()
            current_app.logger.exception("Could not finalize media upload %s", upload_id)
            return jsonify({"error": "The video could not be finalized. Please upload it again."}), 500
        after_upload(video, video.original_filename)
        return jsonify({"id": video.id, "title": video.title, "redirect": url_for("media_library.index")})

    @bp.post("/media/r2-upload/<upload_id>/abort")
    def r2_abort(upload_id):
        _membership, transfer = own_transfer(upload_id, status="uploading")
        try:
            if transfer.multipart_upload_id:
                core.R2Storage().abort_multipart(transfer.object_key, transfer.multipart_upload_id)
            transfer.status = "aborted"
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Could not abort media upload %s", upload_id)
            return jsonify({"error": "Could not cancel the cloud upload."}), 503
        return jsonify({"ok": True})

    # ------------------------------------------------------- manage videos
    @bp.post("/media/<int:video_id>/edit")
    def edit(video_id):
        membership, video = owned_video(video_id)
        if not can_manage(membership, video):
            abort(403)
        title = core.clean_line(request.form.get("title"), 200)
        if title:
            video.title = title
        video.tags = json.dumps(clean_tags(request.form.get("tags")))
        db.session.commit()
        core.audit("media_video_updated", "media_video", video.id, video.title)
        flash("Video details saved.", "success")
        return redirect(url_for("media_library.index"))

    @bp.post("/media/<int:video_id>/delete")
    def delete(video_id):
        membership, video = owned_video(video_id)
        if not can_manage(membership, video):
            abort(403)
        filename, title, uid = video.filename, video.title, video.stream_uid
        Share.query.filter_by(company_id=membership.company_id, video_id=video.id).delete(synchronize_session=False)
        db.session.delete(video)
        db.session.commit()
        core.delete_stored_file(filename)
        stream_delete(uid)
        core.audit("media_video_deleted", "media_video", video_id, title)
        flash(f"Deleted “{title}” and its share links.", "success")
        return redirect(url_for("media_library.index"))

    # ---------------------------------------------------------- share links
    def client_offset_minutes():
        """Browser timezone offset (minutes behind UTC, as JS getTimezoneOffset reports it)."""
        try:
            offset = int(request.form.get("tz_offset", "0"))
        except (TypeError, ValueError):
            offset = 0
        return max(-14 * 60, min(14 * 60, offset))

    @bp.post("/media/share")
    def create_share():
        membership = member()
        if not core.company_feature_enabled("viewer_links", membership.company_id):
            abort(403)
        kind = "library" if request.form.get("kind") == "library" else "video"
        mode = "keyword" if request.form.get("mode") == "keyword" else "open"
        projects = visible_projects(membership)
        share = Share(company_id=membership.company_id, kind=kind, mode=mode, created_by=current_user.id)

        def fail(message):
            flash(message, "error")
            return redirect(url_for("media_library.index") + "#share-links")

        if kind == "video":
            video = Video.query.filter_by(id=request.form.get("video_id", type=int),
                                          company_id=membership.company_id).first()
            if not video:
                abort(404)
            share.video_id = video.id
        else:
            if not is_admin(membership):
                if not projects:
                    return fail("You aren't assigned to any projects yet, so there's nothing to share.")
                share.projects_json = json.dumps(projects)
            if request.form.get("show_map"):
                map_project = core.clean_line(request.form.get("map_project"), 160)
                if map_project not in projects:
                    return fail("Choose a project for the flight map.")
                if not core.company_feature_enabled("flight_scans", membership.company_id):
                    return fail("Flight maps aren't part of your plan.")
                share.show_map, share.map_project = True, map_project
        if mode == "keyword":
            keyword = core.clean_line(request.form.get("keyword"), 80)
            if not KEYWORD_RE.fullmatch(keyword):
                return fail("Keywords need 4–80 letters, numbers, spaces, dashes or underscores.")
            share.keyword_hash = generate_password_hash(keyword.casefold())
        raw_expiry = request.form.get("expires_on")
        expires_on = core.parse_date(raw_expiry)
        if raw_expiry and not expires_on:
            return fail("Enter a valid expiry date.")
        if expires_on:
            # The date is the creator's local calendar day: it stays valid to the end of that day.
            offset = timedelta(minutes=client_offset_minutes())
            local_today = (core.now_utc() - offset).date()
            if expires_on < local_today:
                return fail("The expiry date can't be in the past.")
            share.expires_at = datetime.combine(expires_on, time.max) + offset
        db.session.add(share)
        db.session.commit()
        core.audit("media_share_created", "media_share", share.id,
                   json.dumps({"kind": kind, "mode": mode, "video_id": share.video_id,
                               "expires_at": share.expires_at.isoformat() if share.expires_at else None}))
        return redirect(url_for("media_library.index", created=share.id) + "#share-links")

    @bp.post("/media/share/<int:share_id>/revoke")
    def revoke_share(share_id):
        membership = member()
        share = Share.query.filter_by(id=share_id, company_id=membership.company_id).first()
        if not share:
            abort(404)
        if not (is_admin(membership) or share.created_by == current_user.id):
            abort(403)
        if not share.revoked_at:
            share.revoked_at = core.now_utc()
            db.session.commit()
            core.audit("media_share_revoked", "media_share", share.id, share.kind)
        flash("Link revoked. Anyone opening it now sees that it's no longer available.", "success")
        return redirect(url_for("media_library.index") + "#share-links")

    # ========================================================== public links
    def resolve(token, kind):
        # Public pages: no tenant scope from any signed-in account; every query below
        # filters explicitly by the link's company.
        g.allow_public_viewer_scope = True
        share = Share.query.filter_by(token=token, kind=kind).first() if token and len(token) <= 64 else None
        if not share_active(share):
            abort(404)
        company = db.session.get(core.Company, share.company_id)
        if not company or company.status != "active":
            abort(404)
        return share

    def unlocked(share):
        return share.mode == "open" or share.id in session.get(UNLOCKED_KEY, [])

    def gate(share, endpoint):
        """Keyword gate. Every attempt is counted atomically BEFORE the keyword is checked,
        so parallel guesses cannot slip past the 5-try lockout."""
        now = core.now_utc()
        if request.method == "POST":
            attempts = db.session.execute(
                update(share_table)
                .where(share_table.c.id == share.id,
                       or_(share_table.c.locked_until.is_(None), share_table.c.locked_until <= now))
                .values(failed_attempts=share_table.c.failed_attempts + 1)
                .returning(share_table.c.failed_attempts)).scalar()
            db.session.commit()
            if attempts is None or attempts > MAX_KEYWORD_TRIES:
                if attempts is not None:
                    lock(share.id, now)
                return redirect(url_for(endpoint, token=share.token))
            submitted = core.clean_line(request.form.get("keyword"), 80)
            if submitted and share.keyword_hash and check_password_hash(share.keyword_hash, submitted.casefold()):
                db.session.execute(update(share_table).where(share_table.c.id == share.id)
                                   .values(failed_attempts=0, locked_until=None))
                db.session.commit()
                _remember(UNLOCKED_KEY, share.id)
                core.audit("media_share_unlocked", "media_share", share.id, share.kind)
                return redirect(url_for(endpoint, token=share.token))
            if attempts >= MAX_KEYWORD_TRIES:
                lock(share.id, now)
            else:
                core.audit("media_share_failed", "media_share", share.id, share.kind)
                left = MAX_KEYWORD_TRIES - attempts
                flash(f"That keyword isn't right. {left} {'try' if left == 1 else 'tries'} left.", "error")
            return redirect(url_for(endpoint, token=share.token))
        db.session.refresh(share)
        locked = bool(share.locked_until and share.locked_until > now)
        return page("media_share_gate.html", share=share, locked=locked, action=url_for(endpoint, token=share.token))

    def lock(share_id, now):
        db.session.execute(update(share_table).where(share_table.c.id == share_id)
                           .values(failed_attempts=0, locked_until=now + LOCKOUT))
        db.session.commit()
        core.audit("media_share_locked", "media_share", share_id, "")

    def count_view(share):
        if share.id in session.get(VIEWED_KEY, []):
            return
        db.session.execute(update(share_table).where(share_table.c.id == share.id)
                           .values(view_count=share_table.c.view_count + 1, last_viewed_at=core.now_utc()))
        db.session.commit()
        _remember(VIEWED_KEY, share.id)

    def file_response(response):
        return lockdown(response)

    @bp.route("/share/video/<token>", methods=["GET", "POST"])
    def shared_video(token):
        share = resolve(token, "video")
        if not unlocked(share) or request.method == "POST":
            if share.mode == "open":
                return redirect(url_for("media_library.shared_video", token=token))
            return gate(share, "media_library.shared_video")
        video = Video.query.filter_by(id=share.video_id, company_id=share.company_id).first()
        if not video:
            abort(404)
        count_view(share)
        return page("media_share_video.html", share=share, video=video, embed=stream_embed(video),
                    src=url_for("media_library.shared_video_file", token=token),
                    download=url_for("media_library.shared_video_download", token=token))

    @bp.get("/share/video/<token>/file")
    def shared_video_file(token):
        share = resolve(token, "video")
        if not unlocked(share):
            abort(403)
        video = Video.query.filter_by(id=share.video_id, company_id=share.company_id).first()
        if not video:
            abort(404)
        return file_response(stream_original(video.filename, video.original_filename))

    @bp.get("/share/video/<token>/download")
    def shared_video_download(token):
        share = resolve(token, "video")
        if not unlocked(share):
            abort(403)
        video = Video.query.filter_by(id=share.video_id, company_id=share.company_id).first()
        if not video:
            abort(404)
        core.audit("media_video_downloaded", "media_share", share.id, str(video.id))
        return file_response(stream_original(video.filename, video.original_filename, download=True))

    def library_video_or_404(share, video_id):
        video = next((item for item in library_videos(share) if item.id == video_id), None)
        if not video:
            abort(404)
        return video

    @bp.route("/share/library/<token>", methods=["GET", "POST"])
    def shared_library(token):
        share = resolve(token, "library")
        if not unlocked(share) or request.method == "POST":
            if share.mode == "open":
                return redirect(url_for("media_library.shared_library", token=token))
            return gate(share, "media_library.shared_library")
        tab = "map" if request.args.get("tab") == "map" and map_enabled(share) else "media"
        videos = library_videos(share)
        count_view(share)
        context = map_context(share) if tab == "map" else {}
        company = db.session.get(core.Company, share.company_id)
        return page("media_share_library.html", share=share, tab=tab, videos=videos, company=company,
                    tags=sorted({tag for video in videos for tag in video.tag_list}, key=str.casefold),
                    show_map=map_enabled(share), **context)

    @bp.get("/share/library/<token>/file/<int:video_id>")
    def shared_library_file(token, video_id):
        share = resolve(token, "library")
        if not unlocked(share):
            abort(403)
        video = library_video_or_404(share, video_id)
        return file_response(stream_original(video.filename, video.original_filename))

    @bp.get("/share/library/<token>/download/<int:video_id>")
    def shared_library_download(token, video_id):
        share = resolve(token, "library")
        if not unlocked(share):
            abort(403)
        video = library_video_or_404(share, video_id)
        core.audit("media_video_downloaded", "media_share", share.id, str(video.id))
        return file_response(stream_original(video.filename, video.original_filename, download=True))

    @bp.get("/share/library/<token>/play/<int:video_id>")
    def shared_library_play(token, video_id):
        """What the library player should load: a signed Stream player, or the original file."""
        share = resolve(token, "library")
        if not unlocked(share):
            abort(403)
        video = library_video_or_404(share, video_id)
        response = jsonify({
            "embed": stream_embed(video),
            "src": url_for("media_library.shared_library_file", token=token, video_id=video.id),
            "download": url_for("media_library.shared_library_download", token=token, video_id=video.id),
        })
        response.headers["Cache-Control"] = "no-store"
        return lockdown(response)

    # ------------------------------------------------------ shared flight map
    def map_context(share):
        company_id, project = share.company_id, share.map_project
        flights = (core.Flight.query.filter_by(company_id=company_id, project=project)
                   .order_by(core.Flight.date.desc(), core.Flight.uploaded_at.desc()).all())
        paths = (core.ProjectFlightPath.query.filter_by(company_id=company_id, project=project)
                 .order_by(core.ProjectFlightPath.date, core.ProjectFlightPath.created_at).all())
        dates = sorted({flight.date for flight in flights} | {path.date for path in paths}, reverse=True)
        selected = core.parse_date(request.args.get("date"))
        if selected not in dates:
            selected = dates[0] if dates else core.now_utc().date()
        site_map = core.ProjectSiteMap.query.filter_by(company_id=company_id, project=project).first()
        photo_reference = core.ProjectPhotoReference.query.filter_by(company_id=company_id, project=project).first()
        setup = core.ProjectSiteSetup.query.filter_by(company_id=company_id, project=project).first()
        module = setup.module if setup else ("image" if site_map or photo_reference else "map")
        image_url = ortho_url = ""
        if module == "image" and (site_map or photo_reference):
            image_url = url_for("media_library.shared_site_file", token=share.token, kind="image")
        if module == "ortho" and site_map:
            ortho_url = url_for("media_library.shared_site_file", token=share.token, kind="ortho")
        pins = (core.ProjectMapPin.query.filter_by(company_id=company_id, project=project)
                .order_by(core.ProjectMapPin.id).all())
        paths_by_date = {}
        for path in paths:
            try:
                points = json.loads(path.points or "[]")
            except (TypeError, json.JSONDecodeError):
                points = []
            paths_by_date.setdefault(path.date.isoformat(), []).append({
                "id": path.id, "name": path.name, "label": path.label, "direction": path.direction,
                "points": points, "flight_id": path.flight_id,
                "flight_title": path.flight.title if path.flight else ""})
        day_flights = [flight for flight in flights if flight.date == selected]
        flight = None
        flight_id = request.args.get("flight", type=int)
        if flight_id:
            flight = next((item for item in flights if item.id == flight_id), None)
        return {
            "map_project": project, "dates": dates, "selected_date": selected, "day_flights": day_flights,
            "site_module": module, "site_image_url": image_url, "site_ortho_url": ortho_url,
            "center_lat": (setup.center_lat if setup and setup.center_lat is not None else 40.2744),
            "center_lng": (setup.center_lng if setup and setup.center_lng is not None else -76.8871),
            "zoom": (setup.zoom if setup and setup.zoom else 16),
            "map_pins": [{"x": pin.x_percent, "y": pin.y_percent, "label": pin.label, "direction": pin.direction}
                         for pin in pins],
            "flight_paths": paths_by_date.get(selected.isoformat(), []), "paths_by_date": paths_by_date,
            "flight": flight,
            "flight_src": url_for("media_library.shared_flight_file", token=share.token, flight_id=flight.id)
            if flight else "",
            "flight_download": url_for("media_library.shared_flight_download", token=share.token, flight_id=flight.id)
            if flight else "",
        }

    def map_flight_or_404(share, flight_id):
        flight = core.Flight.query.filter_by(id=flight_id, company_id=share.company_id,
                                             project=share.map_project).first()
        if not flight:
            abort(404)
        return flight

    @bp.get("/share/library/<token>/site/<kind>")
    def shared_site_file(token, kind):
        share = resolve(token, "library")
        if not unlocked(share) or not map_enabled(share):
            abort(403)
        company_id, project = share.company_id, share.map_project
        site_map = core.ProjectSiteMap.query.filter_by(company_id=company_id, project=project).first()
        if kind == "ortho":
            record = site_map
        elif kind == "image":
            record = site_map or core.ProjectPhotoReference.query.filter_by(
                company_id=company_id, project=project).first()
        else:
            abort(404)
        if not record:
            abort(404)
        content_type = core.mimetypes.guess_type(record.original_filename or record.filename)[0] or "application/octet-stream"
        return file_response(serve(record.filename, record.original_filename, content_type))

    @bp.get("/share/library/<token>/flight/<int:flight_id>")
    def shared_flight_file(token, flight_id):
        share = resolve(token, "library")
        if not unlocked(share) or not map_enabled(share):
            abort(403)
        flight = map_flight_or_404(share, flight_id)
        return file_response(stream_original(flight.filename, flight.original_filename))

    @bp.get("/share/library/<token>/flight/<int:flight_id>/download")
    def shared_flight_download(token, flight_id):
        share = resolve(token, "library")
        if not unlocked(share) or not map_enabled(share):
            abort(403)
        flight = map_flight_or_404(share, flight_id)
        core.audit("flight_downloaded", "media_share", share.id, str(flight.id))
        return file_response(stream_original(flight.filename, flight.original_filename, download=True))

    @bp.errorhandler(401)
    def sign_in_first(_error):
        # Team pages send signed-out visitors to sign in instead of showing an error.
        if request.path.startswith("/media/r2-upload/"):
            return jsonify({"error": "Sign in to continue."}), 401
        return redirect(url_for("login"))

    return bp
