"""Site walkthrough service: drawings, capture routes, 360° media and 3D models.

This is a separate service from drone flights. The Platform Owner grants it per
company (Company.service_walkthrough). It reuses the platform's accounts, company
and project scoping, upload storage and client share codes.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime, timezone

from flask import (Blueprint, abort, current_app, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_login import current_user


SHEET_TYPES = {"pdf": "pdf", "jpg": "image", "jpeg": "image", "png": "image", "webp": "image"}
IMAGE_TYPES = {"jpg", "jpeg", "png", "webp"}
VIDEO_TYPES = {"mp4", "m4v", "mov", "webm"}
MODEL_TYPES = {"glb"}
MAX_SHEET_BYTES = 150 * 1024 * 1024
MAX_MODEL_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_BYTES = 80 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_POINTS_PER_SESSION = 2000
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
DEFAULT_COLORS = ("#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#a855f7", "#06b6d4", "#eab308", "#ec4899")


def _header_matches(ext, header):
    if ext == "pdf":
        return header.startswith(b"%PDF-")
    if ext in {"jpg", "jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if ext == "glb":
        return header.startswith(b"glTF")
    if ext in {"mp4", "m4v", "mov"}:
        return header[4:8] == b"ftyp"
    if ext == "webm":
        return header.startswith(b"\x1aE\xdf\xa3")
    return False


def _id(value):
    """Parse a record id from JSON; anything malformed becomes 0 (owned() then 404s)."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if 0 < number < 2**31 else 0


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _fraction(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def _seconds(value):
    """Seconds into a walk video (0 to 24 hours), or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 86400.0 else None


def _timestamp(value):
    """An ISO timestamp from the browser or camera, stored as naive UTC; None if unusable."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment if 2000 <= moment.year <= 2100 else None


def utc_iso(moment):
    """Stored times are naive UTC; mark them as UTC so browsers do not read them as local time."""
    return moment.isoformat(timespec="seconds") + "Z" if moment else None


def _coordinate(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and abs(number) <= 1_000_000 else None


def create_walkthrough_blueprint(core):
    """Build the blueprint against the main application module (passed in to avoid circular imports)."""
    bp = Blueprint("walkthrough", __name__, url_prefix="/walkthrough")
    db = core.db
    Sheet, Model3D = core.WalkthroughSheet, core.WalkthroughModel
    WalkSession, Point, Media = core.WalkthroughSession, core.WalkthroughPoint, core.WalkthroughMedia

    # ------------------------------------------------------------------ access
    def member():
        """The signed-in company membership, if the company has the walkthrough service."""
        if not current_user.is_authenticated or not current_user.active:
            abort(401)
        membership = core.current_membership()
        if (not membership or not membership.company or membership.company.status != "active"
                or not core.company_service_enabled("walkthrough", membership.company_id)):
            abort(403)
        return membership

    def unlocked_share(token, *, required=True):
        """A client share link that is unlocked in this browser and allows media."""
        g.allow_public_viewer_scope = True
        access = core.ProjectAccess.query.filter_by(share_token=token).first() if token else None
        if (not core.viewer_access_active(access) or not access.allow_media
                or not core.company_service_enabled("walkthrough", access.company_id)):
            abort(404)
        company = db.session.get(core.Company, access.company_id)
        if not company or company.status != "active":
            abort(404)
        if session.get("viewer_code") != access.code or session.get("viewer_token") != access.share_token:
            if required:
                abort(403)
            return None
        return access

    def visible_projects(membership):
        return sorted(core.company_project_names(membership.company_id))

    def can_edit(project):
        return bool(current_user.is_authenticated and core.user_can_upload_project(project))

    def can_delete():
        return bool(current_user.is_authenticated and current_user.is_admin)

    def require_edit(project):
        member()
        if not project or not can_edit(project):
            abort(403)

    def require_delete():
        member()
        if not can_delete():
            abort(403)

    def owned(model, item_id):
        """Load a record from the signed-in user's company (tenant scoping applies)."""
        membership = member()
        item = model.query.filter_by(id=item_id, company_id=membership.company_id).first()
        if not item:
            abort(404)
        return item

    def json_body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def error(message, status=400):
        return jsonify({"error": message}), status

    # ----------------------------------------------------------------- uploads
    def store(file, allowed, max_bytes, label):
        """Validate type and size, then save through the platform's storage (quota, local or R2)."""
        if not file or not file.filename:
            raise ValueError(f"Choose a {label} file.")
        ext = core.extension(file.filename)
        if ext not in allowed:
            raise ValueError(f"Unsupported {label} type. Use: {', '.join(sorted(allowed)).upper()}.")
        stream = file.stream
        position = stream.tell()
        header = stream.read(16)
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(position)
        if not _header_matches(ext, header):
            raise ValueError(f"The {label} file does not match its .{ext} format.")
        if size <= 0 or size > max_bytes:
            raise ValueError(f"The {label} file must be smaller than {max_bytes // (1024 * 1024)} MB.")
        return ext, core.save_upload(file)

    def commit_upload(rows, stored_files):
        try:
            db.session.add_all(rows)
            for stored in stored_files:
                core.complete_storage_reservation(stored[4])
            db.session.commit()
        except Exception:
            db.session.rollback()
            discard(stored_files)
            raise

    def discard(stored_files):
        for filename, _size, _checksum, _original, reservation_id in stored_files:
            core.delete_stored_file(filename)
            core.release_storage_reservation(reservation_id)

    def upload_failed(exc, stored_files, what):
        db.session.rollback()
        discard(stored_files)
        if isinstance(exc, ValueError):
            return error(str(exc))
        current_app.logger.exception("Walkthrough %s upload failed", what)
        return error("Upload failed. Please try again.", 500)

    # -------------------------------------------------------------- serialize
    def file_url(kind, item, token):
        # The version tag changes with the stored file, so a reused record id can never
        # show a browser's cached copy of a different, deleted file.
        params = {"kind": kind, "item_id": item.id, "v": hashlib.sha256(item.filename.encode()).hexdigest()[:12]}
        if token:
            params["share"] = token
        return url_for("walkthrough.file", **params)

    def project_payload(company_id, project, token=None):
        sheets = (Sheet.query.filter_by(company_id=company_id, project=project)
                  .order_by(Sheet.floor, Sheet.sort_order, Sheet.id).all())
        models = Model3D.query.filter_by(company_id=company_id, project=project).order_by(Model3D.id).all()
        sessions = (WalkSession.query.filter_by(company_id=company_id, project=project)
                    .order_by(WalkSession.capture_date, WalkSession.id).all())
        points = (Point.query.filter_by(company_id=company_id, project=project)
                  .order_by(Point.session_id, Point.seq).all())
        media = Media.query.filter_by(company_id=company_id, project=project).order_by(Media.id).all()
        media_by_point, walk_videos = {}, {}
        session_of_point = {point.id: point.session_id for point in points}
        for item in media:
            entry = {"id": item.id, "kind": item.kind, "name": item.original_filename,
                     "url": file_url("media", item, token),
                     "captured_at": utc_iso(item.captured_at)}
            if item.kind == "walkvideo":
                walk_videos[session_of_point.get(item.point_id)] = entry
            else:
                media_by_point.setdefault(item.point_id, []).append(entry)
        points_by_session = {}
        for point in points:
            points_by_session.setdefault(point.session_id, []).append({
                "id": point.id, "seq": point.seq, "x": point.x, "y": point.y, "label": point.label,
                "model_id": point.model_id,
                "m": [point.mx, point.my, point.mz] if point.mx is not None else None,
                "t": point.t, "captured_at": utc_iso(point.captured_at),
                "media": media_by_point.get(point.id, [])})
        return {
            "project": project,
            "sheets": [{"id": s.id, "floor": s.floor, "name": s.name, "kind": s.kind, "page": s.page,
                        "url": file_url("sheet", s, token)} for s in sheets],
            "models": [{"id": m.id, "floor": m.floor, "name": m.name,
                        "url": file_url("model", m, token)} for m in models],
            "sessions": [{"id": s.id, "sheet_id": s.sheet_id, "name": s.name,
                          "date": s.capture_date.isoformat(), "color": s.color, "kind": s.kind or "photo",
                          "video": walk_videos.get(s.id),
                          "points": points_by_session.get(s.id, [])} for s in sessions],
        }

    # ------------------------------------------------------------------ pages
    @bp.get("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        membership = member()
        projects = visible_projects(membership)
        counts = {}
        for row in WalkSession.query.filter_by(company_id=membership.company_id).all():
            counts[row.project] = counts.get(row.project, 0) + 1
        sheet_counts = {}
        for row in Sheet.query.filter_by(company_id=membership.company_id).all():
            sheet_counts[row.project] = sheet_counts.get(row.project, 0) + 1
        return render_template("walkthrough_index.html", projects=projects, session_counts=counts,
                               sheet_counts=sheet_counts,
                               can_start=bool(current_user.is_admin or membership.role == "company_admin"))

    @bp.get("/view")
    def view():
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.full_path))
        membership = member()
        project = _clean(request.args.get("project"), 160)
        if not project:
            return redirect(url_for("walkthrough.index"))
        known = project in visible_projects(membership)
        if not known:
            # Starting a new project requires edit rights and a free project slot on the plan.
            if not can_edit(project):
                abort(404)
            if not core.project_capacity_available(project):
                db.session.rollback()
                return render_template("walkthrough_index.html", projects=visible_projects(membership),
                                       session_counts={}, sheet_counts={}, can_start=True,
                                       limit_message="Your plan's project limit has been reached."), 403
            db.session.rollback()
        config = {
            "mode": "member", "project": project,
            "dataUrl": url_for("walkthrough.data", project=project),
            "apiBase": url_for("walkthrough.index").rstrip("/") + "/api",
            "canEdit": can_edit(project), "canDelete": can_delete(),
            "droneUrl": url_for("dashboard", project=project)
                if core.company_service_enabled("drone", membership.company_id) else "",
            "homeUrl": url_for("walkthrough.index"),
        }
        return render_template("walkthrough.html", wt=config, project=project, share=False)

    @bp.get("/share/<token>")
    def shared(token):
        access = unlocked_share(token, required=False)
        if access is None:
            return redirect(url_for("shared_project", token=token, tab="walkthrough"))
        config = {
            "mode": "share", "project": access.project,
            "dataUrl": url_for("walkthrough.data", share=token),
            "apiBase": "", "canEdit": False, "canDelete": False,
            "droneUrl": url_for("shared_project", token=token, tab="map")
                if core.company_feature_enabled("flight_scans", access.company_id) else "",
            "homeUrl": "",
        }
        return render_template("walkthrough.html", wt=config, project=access.project, share=True)

    # ------------------------------------------------------------------- data
    @bp.get("/api/data")
    def data():
        token = request.args.get("share", "")
        if token:
            access = unlocked_share(token)
            payload = project_payload(access.company_id, access.project, token)
        else:
            membership = member()
            project = _clean(request.args.get("project"), 160)
            if project not in visible_projects(membership) and not can_edit(project):
                abort(404)
            payload = project_payload(membership.company_id, project)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.get("/file/<kind>/<int:item_id>")
    def file(kind, item_id):
        model = {"sheet": Sheet, "model": Model3D, "media": Media}.get(kind)
        if not model:
            abort(404)
        token = request.args.get("share", "")
        if token:
            access = unlocked_share(token)
            item = model.query.filter_by(id=item_id, company_id=access.company_id, project=access.project).first()
        else:
            membership = member()
            item = model.query.filter_by(id=item_id, company_id=membership.company_id).first()
        if not item:
            abort(404)
        filename = item.filename
        content_type = ("model/gltf-binary" if kind == "model"
                        else core.mimetypes.guess_type(item.original_filename or filename)[0]
                        or "application/octet-stream")
        if core.is_r2_filename(filename):
            try:
                target = core.R2Storage().signed_download(core.r2_object_key(filename),
                                                          item.original_filename or "file", content_type, False)
            except Exception:
                current_app.logger.exception("Could not create authorized R2 read URL")
                abort(503)
            return redirect(target, code=302)
        safe = core.Path(filename).name
        path = core.UPLOAD_DIR / safe
        if not path.is_file():
            abort(404)
        response = send_from_directory(str(core.UPLOAD_DIR), safe, conditional=True, mimetype=content_type)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    # ----------------------------------------------------------- drawings / 3D
    @bp.post("/api/sheets")
    def upload_sheet():
        project = _clean(request.form.get("project"), 160)
        require_edit(project)
        if not core.project_capacity_available(project):
            db.session.rollback()
            return error("Your plan's project limit has been reached.", 403)
        name = _clean(request.form.get("name"), 160)
        floor = _clean(request.form.get("floor"), 120)
        try:
            page = max(1, min(9999, int(request.form.get("page") or 1)))
        except ValueError:
            page = 1
        stored_files = []
        try:
            ext, stored = store(request.files.get("file"), set(SHEET_TYPES), MAX_SHEET_BYTES, "drawing")
            stored_files.append(stored)
            row = Sheet(project=project, floor=floor, name=name or stored[3], kind=SHEET_TYPES[ext],
                        page=page if ext == "pdf" else 1, filename=stored[0], original_filename=stored[3],
                        file_size=stored[1])
            commit_upload([row], stored_files)
        except Exception as exc:
            return upload_failed(exc, stored_files, "drawing")
        core.audit("walkthrough_sheet_upload", "project", project, f"Uploaded drawing {row.name}")
        return jsonify({"ok": True, "id": row.id})

    @bp.post("/api/sheets/<int:sheet_id>")
    def update_sheet(sheet_id):
        sheet = owned(Sheet, sheet_id)
        require_edit(sheet.project)
        body = json_body()
        if "name" in body:
            sheet.name = _clean(body.get("name"), 160) or sheet.name
        if "floor" in body:
            sheet.floor = _clean(body.get("floor"), 120)
        db.session.commit()
        return jsonify({"ok": True})

    @bp.post("/api/sheets/<int:sheet_id>/delete")
    def delete_sheet(sheet_id):
        require_delete()
        sheet = owned(Sheet, sheet_id)
        if WalkSession.query.filter_by(sheet_id=sheet.id, company_id=sheet.company_id).first():
            return error("Delete this drawing's capture sessions first.", 409)
        filename, project = sheet.filename, sheet.project
        db.session.delete(sheet)
        db.session.commit()
        core.delete_stored_file(filename)
        core.audit("walkthrough_sheet_delete", "project", project, f"Deleted drawing {sheet_id}")
        return jsonify({"ok": True})

    @bp.post("/api/models")
    def upload_model():
        project = _clean(request.form.get("project"), 160)
        require_edit(project)
        if not core.project_capacity_available(project):
            db.session.rollback()
            return error("Your plan's project limit has been reached.", 403)
        stored_files = []
        try:
            _ext, stored = store(request.files.get("file"), MODEL_TYPES, MAX_MODEL_BYTES, "3D model")
            stored_files.append(stored)
            row = Model3D(project=project, floor=_clean(request.form.get("floor"), 120),
                          name=_clean(request.form.get("name"), 160) or stored[3],
                          filename=stored[0], original_filename=stored[3], file_size=stored[1])
            commit_upload([row], stored_files)
        except Exception as exc:
            return upload_failed(exc, stored_files, "3D model")
        core.audit("walkthrough_model_upload", "project", project, f"Uploaded 3D model {row.name}")
        return jsonify({"ok": True, "id": row.id})

    @bp.post("/api/models/<int:model_id>/delete")
    def delete_model(model_id):
        require_delete()
        model = owned(Model3D, model_id)
        Point.query.filter_by(model_id=model.id, company_id=model.company_id).update(
            {"model_id": None, "mx": None, "my": None, "mz": None}, synchronize_session=False)
        filename, project = model.filename, model.project
        db.session.delete(model)
        db.session.commit()
        core.delete_stored_file(filename)
        core.audit("walkthrough_model_delete", "project", project, f"Deleted 3D model {model_id}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------- sessions
    @bp.post("/api/sessions")
    def create_session():
        body = json_body()
        sheet = owned(Sheet, _id(body.get("sheet_id")))
        require_edit(sheet.project)
        capture_date = core.parse_date(body.get("date"))
        if not capture_date:
            return error("Choose the capture date.")
        color = body.get("color") if COLOR_RE.fullmatch(str(body.get("color") or "")) else None
        if not color:
            used = WalkSession.query.filter_by(company_id=sheet.company_id, project=sheet.project).count()
            color = DEFAULT_COLORS[used % len(DEFAULT_COLORS)]
        kind = "video" if body.get("kind") == "video" else "photo"
        default_name = ("Video walk " if kind == "video" else "Walk ") + capture_date.strftime("%b %d, %Y")
        row = WalkSession(project=sheet.project, sheet_id=sheet.id, capture_date=capture_date, color=color,
                          kind=kind, name=_clean(body.get("name"), 160) or default_name)
        db.session.add(row)
        db.session.commit()
        core.audit("walkthrough_session_create", "project", sheet.project, f"Created capture session {row.name}")
        return jsonify({"ok": True, "id": row.id})

    @bp.post("/api/sessions/<int:session_id>")
    def update_session(session_id):
        row = owned(WalkSession, session_id)
        require_edit(row.project)
        body = json_body()
        if "name" in body:
            row.name = _clean(body.get("name"), 160) or row.name
        if "date" in body:
            row.capture_date = core.parse_date(body.get("date"), row.capture_date)
        if "color" in body and COLOR_RE.fullmatch(str(body.get("color") or "")):
            row.color = body["color"]
        db.session.commit()
        return jsonify({"ok": True})

    @bp.post("/api/sessions/<int:session_id>/delete")
    def delete_session(session_id):
        require_delete()
        row = owned(WalkSession, session_id)
        point_ids = [p.id for p in Point.query.filter_by(session_id=row.id, company_id=row.company_id).all()]
        media = (Media.query.filter(Media.point_id.in_(point_ids), Media.company_id == row.company_id).all()
                 if point_ids else [])
        filenames = [item.filename for item in media]
        for item in media:
            db.session.delete(item)
        Point.query.filter_by(session_id=row.id, company_id=row.company_id).delete(synchronize_session=False)
        project, name = row.project, row.name
        db.session.delete(row)
        db.session.commit()
        for filename in filenames:
            core.delete_stored_file(filename)
        core.audit("walkthrough_session_delete", "project", project,
                   f"Deleted capture session {name} with {len(point_ids)} points and {len(filenames)} files")
        return jsonify({"ok": True})

    @bp.post("/api/sessions/<int:session_id>/video")
    def upload_walk_video(session_id):
        """Attach (or replace) the 360 video of a video walk."""
        row = owned(WalkSession, session_id)
        require_edit(row.project)
        if (row.kind or "photo") != "video":
            return error("This is a photo walk. Start a video walk to attach a walk video.")
        first = (Point.query.filter_by(session_id=row.id, company_id=row.company_id)
                 .order_by(Point.seq).first())
        if not first:
            return error("Mark at least one position on the plan before adding the video.")
        stored_files, old_files = [], []
        try:
            _ext, stored = store(request.files.get("file"), VIDEO_TYPES, MAX_VIDEO_BYTES, "video")
            stored_files.append(stored)
            point_ids = [p.id for p in Point.query.filter_by(session_id=row.id, company_id=row.company_id).all()]
            previous = Media.query.filter(Media.point_id.in_(point_ids), Media.kind == "walkvideo",
                                          Media.company_id == row.company_id).all()
            old_files = [item.filename for item in previous]
            for item in previous:
                db.session.delete(item)
            video = Media(project=row.project, point_id=first.id, kind="walkvideo", filename=stored[0],
                          original_filename=stored[3], file_size=stored[1],
                          captured_at=_timestamp(request.form.get("captured_at")))
            commit_upload([video], stored_files)
        except Exception as exc:
            return upload_failed(exc, stored_files, "walk video")
        for filename in old_files:
            core.delete_stored_file(filename)
        core.audit("walkthrough_video_upload", "project", row.project, f"Attached walk video to {row.name}")
        return jsonify({"ok": True, "id": video.id})

    @bp.post("/api/sessions/<int:session_id>/shift")
    def shift_session_times(session_id):
        """Line the marked positions up with the video by moving them all earlier or later."""
        row = owned(WalkSession, session_id)
        require_edit(row.project)
        try:
            delta = float(json_body().get("seconds"))
        except (TypeError, ValueError):
            return error("Enter how many seconds to shift.")
        if not math.isfinite(delta) or abs(delta) > 3600:
            return error("Shift by at most one hour.")
        for point in Point.query.filter_by(session_id=row.id, company_id=row.company_id).all():
            if point.t is not None:
                point.t = max(0.0, point.t + delta)
        db.session.commit()
        return jsonify({"ok": True})

    # --------------------------------------------------------------- points
    def renumber(session_row):
        for index, point in enumerate(Point.query.filter_by(session_id=session_row.id, company_id=session_row.company_id)
                                      .order_by(Point.seq, Point.id).all(), start=1):
            point.seq = index

    @bp.post("/api/points")
    def create_point():
        body = json_body()
        session_row = owned(WalkSession, _id(body.get("session_id")))
        require_edit(session_row.project)
        x, y = _fraction(body.get("x")), _fraction(body.get("y"))
        if x is None or y is None:
            return error("Place the point on the drawing.")
        count = Point.query.filter_by(session_id=session_row.id, company_id=session_row.company_id).count()
        if count >= MAX_POINTS_PER_SESSION:
            return error("This capture session has reached its point limit.")
        last = (Point.query.filter_by(session_id=session_row.id, company_id=session_row.company_id)
                .order_by(Point.seq.desc()).first())
        row = Point(project=session_row.project, session_id=session_row.id, seq=(last.seq + 1) if last else 1,
                    x=x, y=y, label=_clean(body.get("label"), 120),
                    t=_seconds(body.get("t")), captured_at=_timestamp(body.get("captured_at")))
        db.session.add(row)
        db.session.commit()
        return jsonify({"ok": True, "id": row.id, "seq": row.seq})

    @bp.post("/api/points/<int:point_id>")
    def update_point(point_id):
        row = owned(Point, point_id)
        require_edit(row.project)
        body = json_body()
        if "x" in body or "y" in body:
            x, y = _fraction(body.get("x")), _fraction(body.get("y"))
            if x is None or y is None:
                return error("Keep the point on the drawing.")
            row.x, row.y = x, y
        if "label" in body:
            row.label = _clean(body.get("label"), 120)
        if "t" in body:
            row.t = _seconds(body.get("t"))
        if body.get("clear_3d"):
            row.model_id = row.mx = row.my = row.mz = None
        elif "m" in body:
            coords = body.get("m")
            model = owned(Model3D, _id(body.get("model_id")))
            if model.project != row.project or not isinstance(coords, list) or len(coords) != 3:
                return error("Place the point on the 3D model.")
            values = [_coordinate(value) for value in coords]
            if None in values:
                return error("Place the point on the 3D model.")
            row.model_id = model.id
            row.mx, row.my, row.mz = values
        db.session.commit()
        return jsonify({"ok": True})

    @bp.post("/api/points/<int:point_id>/delete")
    def delete_point(point_id):
        row = owned(Point, point_id)
        require_edit(row.project)
        if not can_delete() and Media.query.filter_by(point_id=row.id, company_id=row.company_id).first():
            return error("Only an administrator can delete a point that has media.", 403)
        session_row = db.session.get(WalkSession, row.session_id)
        # A walk video hangs off one point of its session; keep it when that point goes.
        other = (Point.query.filter(Point.session_id == row.session_id, Point.company_id == row.company_id,
                                    Point.id != row.id).order_by(Point.seq).first())
        if other:
            Media.query.filter_by(point_id=row.id, company_id=row.company_id, kind="walkvideo").update(
                {"point_id": other.id}, synchronize_session=False)
        media = Media.query.filter_by(point_id=row.id, company_id=row.company_id).all()
        filenames = [item.filename for item in media]
        for item in media:
            db.session.delete(item)
        db.session.delete(row)
        db.session.flush()
        if session_row:
            renumber(session_row)
        db.session.commit()
        for filename in filenames:
            core.delete_stored_file(filename)
        return jsonify({"ok": True})

    @bp.post("/api/points/<int:point_id>/move")
    def reorder_point(point_id):
        """Move a point one step earlier or later in the route."""
        row = owned(Point, point_id)
        require_edit(row.project)
        step = -1 if json_body().get("direction") == "up" else 1
        neighbour = Point.query.filter_by(session_id=row.session_id, company_id=row.company_id,
                                          seq=row.seq + step).first()
        if neighbour:
            row.seq, neighbour.seq = neighbour.seq, row.seq
            db.session.commit()
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- media
    @bp.post("/api/points/<int:point_id>/media")
    def upload_media(point_id):
        row = owned(Point, point_id)
        require_edit(row.project)
        files = [item for item in request.files.getlist("files") if item and item.filename]
        if not files or len(files) > 20:
            return error("Choose between 1 and 20 files for this location.")
        requested = request.form.get("kind", "auto")
        captured_at = _timestamp(request.form.get("captured_at"))
        stored_files, rows = [], []
        try:
            for item in files:
                ext = core.extension(item.filename)
                if ext in VIDEO_TYPES:
                    _ext, stored = store(item, VIDEO_TYPES, MAX_VIDEO_BYTES, "video")
                    kind = "video"
                else:
                    _ext, stored = store(item, IMAGE_TYPES, MAX_IMAGE_BYTES, "image")
                    kind = "pano" if requested == "pano" else "photo"
                stored_files.append(stored)
                rows.append(Media(project=row.project, point_id=row.id, kind=kind, filename=stored[0],
                                  original_filename=stored[3], file_size=stored[1], captured_at=captured_at))
            commit_upload(rows, stored_files)
        except Exception as exc:
            return upload_failed(exc, stored_files, "media")
        core.audit("walkthrough_media_upload", "project", row.project,
                   f"Uploaded {len(rows)} files to capture point {row.id}")
        return jsonify({"ok": True, "ids": [item.id for item in rows]})

    @bp.post("/api/media/<int:media_id>/delete")
    def delete_media(media_id):
        require_delete()
        item = owned(Media, media_id)
        filename, project = item.filename, item.project
        db.session.delete(item)
        db.session.commit()
        core.delete_stored_file(filename)
        core.audit("walkthrough_media_delete", "project", project, f"Deleted walkthrough file {media_id}")
        return jsonify({"ok": True})

    @bp.errorhandler(401)
    def unauthorized(_exc):
        if request.path.startswith("/walkthrough/api/") or request.path.startswith("/walkthrough/file/"):
            return error("Sign in to continue.", 401)
        return redirect(url_for("login", next=request.full_path))

    return bp


