"""Laser scans on the project map.

Admins upload a LAS, LAZ or E57 scan for a project and capture date on the Site
capture page. It is converted in the background into:

* a georeferenced, transparent top-down overlay (when the scan records its
  coordinate system), shown under that date's flight paths on the drawing map,
  on the client Flight Scans map and in the side-by-side date comparison;
* a streamed point-cloud copy for the 3D view, where the same date's flight
  paths are drawn over the scan;
* a thumbnail.

The original stays available for download. Clients reach scans only through
their project share link, after unlocking it, and only for their project.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import timedelta
from uuid import uuid4

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_login import current_user

SCAN_TYPES = {"las", "laz", "e57"}
TYPE_HINT = "a LAS, LAZ or E57 laser scan"
MAX_SERVER_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
# Points kept in the 3D web copy (the original stays available for download).
DEFAULT_SCAN_MAX_POINTS = 2_000_000
SCAN_CONVERT_TIMEOUT = 30 * 60
SIGNED_SECONDS = 2 * 60 * 60
ASSETS = {
    "points": ("points_filename", "scan.cvpc", "application/octet-stream"),
    "overlay": ("overlay_filename", "scan-overlay.png", "image/png"),
    "thumb": ("thumb_filename", "scan.png", "image/png"),
}
PATH_COLORS = ["#e17749", "#397d59", "#4c83c8", "#8b66a5"]
# One conversion at a time on the machine (across web workers); each runs in its own process.
_scan_lock = threading.Lock()
LOCK_PATH = os.path.join(tempfile.gettempdir(), "capture-iv-scan-convert.lock")
# A conversion still "processing" after this long was lost (deploy or restart) and can be retried.
STALE_AFTER = timedelta(seconds=SCAN_CONVERT_TIMEOUT + 15 * 60)
DISK_MARGIN = 512 * 1024 * 1024


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""


def import_map_json():
    """Import map for the 3D viewer's three.js modules."""
    return json.dumps({"imports": {
        "three": url_for("static", filename="vendor/three/three.module.min.js"),
        "three/addons/": url_for("static", filename="vendor/three/addons/"),
    }}, separators=(",", ":"))


def create_project_scans_blueprint(core):
    bp = Blueprint("project_scans", __name__)
    db = core.db
    Scan, Transfer, FlightPath = core.ProjectScan, core.UploadTransfer, core.ProjectFlightPath
    admin_only = lambda fn: core.admin_required(core.company_feature_required("site_maps")(fn))  # noqa: E731

    @bp.app_context_processor
    def scan_template_helpers():
        return {"scan_import_map": import_map_json}

    # ------------------------------------------------------------ helpers
    def company_id():
        membership = core.current_membership()
        if not membership:
            abort(403)
        return membership.company_id

    def admin_scan(scan_id):
        scan = Scan.query.filter_by(id=scan_id, company_id=company_id()).first()
        if not scan:
            abort(404)
        return scan

    def wants_json():
        return (request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or request.accept_mimetypes.best == "application/json")

    def capture_url(project, date):
        return url_for("site_capture", map_project=project, map_date=date.isoformat(), _anchor="videos")

    def clean_name(value, original):
        name = core.clean_line(value, 160)
        if not name and original:
            name = core.clean_line(core.Path(original).stem.replace("_", " ").replace("-", " "), 160)
        return name or "Site scan"

    def validate_target(project, date):
        """Scans attach to an existing project (one the company already has) and a capture date."""
        if not project or project not in core.company_project_names(company_id()):
            return "Choose one of your projects for this scan."
        if not date:
            return "Choose the capture date for this scan."
        return ""

    def serve(filename, download_name, content_type, download=False):
        if core.is_r2_filename(filename):
            try:
                target = core.R2Storage().signed_download(core.r2_object_key(filename), download_name, content_type,
                                                          download, expires=SIGNED_SECONDS)
            except Exception:
                current_app.logger.exception("Could not sign a scan file URL")
                abort(503)
            return redirect(target, code=302)
        safe = core.Path(filename).name
        if not safe or not (core.UPLOAD_DIR / safe).is_file():
            abort(404)
        response = send_from_directory(str(core.UPLOAD_DIR), safe, conditional=True, mimetype=content_type,
                                       as_attachment=download, download_name=core.Path(download_name).name)
        response.headers["Cache-Control"] = "private, max-age=600"
        return response

    def asset_response(scan, asset):
        if asset == "download":
            return serve(scan.filename, scan.original_filename or "scan", "application/octet-stream", download=True)
        if asset not in ASSETS:
            abort(404)
        column, name, content_type = ASSETS[asset]
        filename = getattr(scan, column)
        if scan.status != "ready" or not filename:
            abort(404)
        return serve(filename, name, content_type)

    def stale(scan):
        started = scan.processing_started_at or scan.uploaded_at
        return scan.status == "processing" and (not started or core.now_utc() - started > STALE_AFTER)

    def scan_payload(scan, url_for_asset, view_url):
        meta = scan.meta
        lost = stale(scan)
        return {
            "id": scan.id, "name": scan.name, "date": scan.date.isoformat(),
            "status": "error" if lost else scan.status,
            "error": "Preparing this scan was interrupted. Convert it again." if lost else scan.error, "points": int(scan.point_count or 0), "units": meta.get("units", "m"),
            "bounds": scan.overlay_bounds,
            "overlay": url_for_asset("overlay") if scan.overlay_bounds else "",
            "thumb": url_for_asset("thumb") if scan.status == "ready" and scan.thumb_filename else "",
            "view3d": view_url if scan.status == "ready" and scan.points_filename else "",
            "download": url_for_asset("download"),
        }

    def team_payload(scan):
        return scan_payload(
            scan, lambda asset: url_for("project_scans.team_asset", scan_id=scan.id, asset=asset),
            url_for("project_scans.team_3d", scan_id=scan.id))

    def paths_for(scan):
        """That date's flight paths, mapped into the scan's 3D coordinates."""
        import scan_convert
        rows = FlightPath.query.filter_by(project=scan.project, date=scan.date,
                                          company_id=scan.company_id).order_by(FlightPath.created_at).all()
        out = []
        for index, row in enumerate(rows):
            try:
                points = json.loads(row.points or "[]")
                local = scan_convert.local_points_for_latlng(scan.meta, points) if len(points) > 1 else None
            except Exception:
                local = None
            if local:
                out.append({"label": row.label or row.name or f"Route {index + 1}", "name": row.name,
                            "color": PATH_COLORS[index % len(PATH_COLORS)], "points": local})
        return out

    def page_3d(scan, points_url, back_url, download_url):
        return render_template("scan_3d.html", scan=scan, back_url=back_url, download_url=download_url,
                               viewer={"src": points_url, "units": scan.meta.get("units", "m"),
                                       "paths": paths_for(scan)},
                               georeferenced=bool(scan.meta.get("crs_wkt")))

    # --------------------------------------------------------- conversion
    def start_conversion(scan):
        scan.status, scan.error, scan.processing_started_at = "processing", "", core.now_utc()
        db.session.commit()
        app = current_app._get_current_object()
        if app.config.get("SCAN_CONVERT_SYNC"):
            convert(app, scan.id)
        else:
            threading.Thread(target=convert, args=(app, scan.id), daemon=True, name=f"scan-convert-{scan.id}").start()

    def store_derived(company, path, suffix, content_type):
        name = f"{uuid4().hex[:24]}_scan.{suffix}"
        if core.cloud_storage_enabled():
            key = f"companies/{company}/scans/derived/{name}"
            core.R2Storage().upload_file(core.Path(path), key, content_type)
            return core.R2_FILENAME_PREFIX + key
        core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(path, core.UPLOAD_DIR / name)
        return name

    def convert(app, scan_id):
        with app.app_context(), _scan_lock, open(LOCK_PATH, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)  # other workers wait their turn
            try:
                with tempfile.TemporaryDirectory(prefix="civ-scan-") as tmp:
                    _convert(app, scan_id, tmp)
            finally:
                db.session.remove()
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _convert(app, scan_id, tmp):
            g.resolving_company_scope = True  # background thread: no signed-in user; ids are explicit
            scan = db.session.get(Scan, scan_id)
            if not scan:
                return
            company, ext, filename, source_size = (scan.company_id, _ext(scan.original_filename), scan.filename,
                                                   int(scan.source_size or 0))
            db.session.rollback()  # don't hold a transaction open during the download and conversion
            stored = []
            try:
                if core.is_r2_filename(filename):
                    if shutil.disk_usage(tmp).free < source_size * 1.2 + DISK_MARGIN:
                        raise ValueError("The server is short of temporary space for this scan. Try again later "
                                         "or export a smaller LAZ file.")
                    source = os.path.join(tmp, f"source.{ext}")
                    core.R2Storage().download_file(core.r2_object_key(filename), core.Path(source))
                else:
                    source = str(core.UPLOAD_DIR / core.Path(filename).name)
                outputs = {name: os.path.join(tmp, f"{name}.{suffix}")
                           for name, suffix in (("points", "cvpc"), ("thumb", "png"), ("overlay", "png"))}
                max_points = int(os.environ.get("SCAN_MAX_POINTS", DEFAULT_SCAN_MAX_POINTS))
                result = subprocess.run(
                    [sys.executable, "-m", "scan_convert", source, ext, outputs["points"], outputs["thumb"],
                     outputs["overlay"], str(max_points)],
                    cwd=os.path.dirname(os.path.abspath(__file__)), capture_output=True, text=True,
                    timeout=SCAN_CONVERT_TIMEOUT)
                lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
                meta = json.loads(lines[-1]) if lines else {"error": "The scan converter stopped unexpectedly."}
                if result.returncode != 0 or "error" in meta:
                    raise ValueError(meta.get("error") or "This scan couldn't be converted.")
                has_overlay = bool(meta.get("geo")) and os.path.exists(outputs["overlay"])
                derived_size = os.path.getsize(outputs["points"]) + os.path.getsize(outputs["thumb"])
                stored.append(store_derived(company, outputs["points"], "cvpc", "application/octet-stream"))
                stored.append(store_derived(company, outputs["thumb"], "png", "image/png"))
                if has_overlay:
                    derived_size += os.path.getsize(outputs["overlay"])
                    stored.append(store_derived(company, outputs["overlay"], "png", "image/png"))
                core.lock_company_quota(company)
                scan = db.session.get(Scan, scan_id, populate_existing=True, with_for_update=True)
                if not scan:  # deleted meanwhile
                    raise LookupError("deleted during conversion")
                company_row = db.session.get(core.Company, company)
                limit = core.company_storage_limit_bytes(company_row) if company_row else 0
                growth = source_size + derived_size - int(scan.file_size or 0)
                if limit and core.company_storage_bytes(company) + growth > limit:
                    raise ValueError("Not enough storage is left for this scan's map and 3D copies. "
                                     "Free up space, then convert it again.")
                old = scan.derived_files
                scan.points_filename, scan.thumb_filename = stored[0], stored[1]
                scan.overlay_filename = stored[2] if has_overlay else ""
                scan.file_size = source_size + derived_size
                scan.point_count = int(meta.get("count", 0))
                scan.meta_json = json.dumps(meta)
                scan.status, scan.error = "ready", ""
                db.session.commit()
                for name in old:
                    if name not in stored:
                        core.delete_stored_file(name)
            except Exception as exc:
                db.session.rollback()
                for name in stored:
                    core.delete_stored_file(name)
                if isinstance(exc, LookupError):
                    return
                message = str(exc) if isinstance(exc, ValueError) else "This scan couldn't be converted."
                if isinstance(exc, subprocess.TimeoutExpired):
                    message = "Converting this scan took too long. Export a smaller area or a LAZ file."
                app.logger.warning("Scan conversion failed for scan %s: %s", scan_id, exc)
                scan = db.session.get(Scan, scan_id, populate_existing=True)
                if scan:
                    scan.status, scan.error = "error", message[:300]
                    db.session.commit()

    # ------------------------------------------------- uploads (≤ 2 GB)
    @bp.post("/admin/projects/scans/upload")
    @admin_only
    def upload():
        project = core.clean_line(request.form.get("project"), 160)
        date = core.parse_date(request.form.get("date"))
        back = capture_url(project, date or core.now_utc().date())

        def fail(message, status=400):
            if wants_json():
                return jsonify({"error": message}), status
            flash(message, "error")
            return redirect(back)

        if request.content_length and request.content_length > MAX_SERVER_UPLOAD_BYTES + 1024 * 1024:
            return fail("Scans uploaded this way must be smaller than 2 GB.", 413)
        error = validate_target(project, date)
        if error:
            return fail(error)
        file = request.files.get("file")
        stored = None
        try:
            if not file or not file.filename:
                raise ValueError("Choose a scan file.")
            ext = core.extension(file.filename)
            if ext not in SCAN_TYPES:
                raise ValueError(f"Unsupported file type. Upload {TYPE_HINT}.")
            stored = core.save_upload(file)  # checks the file signature and reserves quota
            scan = Scan(company_id=company_id(), project=project, date=date,
                        name=clean_name(request.form.get("name"), stored[3]), filename=stored[0],
                        original_filename=stored[3], source_size=stored[1], file_size=stored[1],
                        status="processing", uploaded_by=current_user.id)
            db.session.add(scan)
            core.complete_storage_reservation(stored[4])
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            if stored:
                core.delete_stored_file(stored[0])
                core.release_storage_reservation(stored[4])
            if isinstance(exc, ValueError):
                return fail(str(exc))
            current_app.logger.exception("Scan upload failed")
            return fail("Upload failed. Please try again.", 500)
        core.audit("upload_scan", "project_scan", scan.id, f"Uploaded scan {scan.original_filename} for {project} on {date}")
        start_conversion(scan)
        if wants_json():
            return jsonify({"id": scan.id, "redirect": capture_url(project, date)})
        flash("Scan uploaded. It's being prepared for the map and 3D view.", "success")
        return redirect(capture_url(project, date))

    # ------------------------------------- resumable direct-to-R2 uploads
    def own_transfer(upload_id, **filters):
        transfer = Transfer.query.filter_by(id=upload_id, company_id=company_id(), user_id=current_user.id,
                                            direction=core.SCAN_TRANSFER_DIRECTION, **filters).first()
        if not transfer:
            abort(404)
        return transfer

    def transfer_json(transfer, storage):
        data = core.upload_transfer_json(transfer, storage)
        data.pop("completedFlightId", None)
        return data

    @bp.post("/admin/projects/scans/r2-upload/start")
    @admin_only
    def r2_start():
        if not core.cloud_storage_enabled():
            return jsonify({"error": "Direct cloud uploads need private R2 storage."}), 409
        payload = request.get_json(silent=True) or {}
        original = core.safe_name(str(payload.get("filename", "")))
        try:
            file_size = int(payload.get("size", 0))
            last_modified = int(payload.get("lastModified", 0))
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "The selected file has invalid size information."}), 400
        if (core.extension(original) not in SCAN_TYPES or file_size < 1 or last_modified < 0
                or file_size > current_app.config["MAX_CONTENT_LENGTH"]):
            limit_gb = current_app.config["MAX_CONTENT_LENGTH"] // (1024 ** 3)
            return jsonify({"error": f"Choose {TYPE_HINT} up to {limit_gb} GB."}), 400
        project = core.clean_line(payload.get("project"), 160)
        date = core.parse_date(payload.get("date"))
        error = validate_target(project, date)
        if error:
            return jsonify({"error": error}), 400
        name = clean_name(payload.get("name"), original)
        fingerprint = hashlib.sha256(
            f"scan\0{original}\0{file_size}\0{last_modified}\0{project}\0{date}".encode()).hexdigest()
        core.expire_company_upload_transfers(company_id())
        company = core.lock_company_quota(company_id())
        if not company or company.status != "active":
            return jsonify({"error": "This workspace is not active for uploads."}), 403
        existing = (Transfer.query.filter_by(company_id=company.id, user_id=current_user.id, fingerprint=fingerprint,
                                             direction=core.SCAN_TRANSFER_DIRECTION, status="uploading")
                    .filter(Transfer.expires_at > core.now_utc()).order_by(Transfer.created_at.desc()).first())
        if existing:
            try:
                return jsonify(transfer_json(existing, core.R2Storage()))
            except Exception:
                current_app.logger.exception("Could not inspect resumable scan upload %s", existing.id)
                return jsonify({"error": "The saved upload could not be resumed. Try again shortly."}), 503
        now = core.now_utc()
        limit = core.company_storage_limit_bytes(company)
        if not limit or (core.company_storage_bytes(company.id) + core.company_reserved_bytes(company.id, now)
                         + file_size > limit):
            db.session.rollback()
            return jsonify({"error": "This scan would exceed your company's storage allowance."}), 413
        upload_id = str(uuid4())
        object_key = f"companies/{company.id}/scans/{upload_id}-{original}"
        transfer = Transfer(id=upload_id, company_id=company.id, user_id=current_user.id, fingerprint=fingerprint,
                            project=project, date=date, direction=core.SCAN_TRANSFER_DIRECTION, title=name,
                            area="", tags="[]", original_filename=original, file_size=file_size,
                            content_type="application/octet-stream", object_key=object_key, status="starting",
                            expires_at=now + core.R2_UPLOAD_LIFETIME)
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
                    current_app.logger.exception("Could not clean up a failed scan multipart start")
            current_app.logger.exception("Could not start resumable scan upload")
            return jsonify({"error": "Could not start the cloud upload. Try again shortly."}), 503

    @bp.post("/admin/projects/scans/r2-upload/<upload_id>/part-url")
    @admin_only
    def r2_part_url(upload_id):
        transfer = own_transfer(upload_id, status="uploading")
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
            current_app.logger.exception("Could not sign scan upload part")
            return jsonify({"error": "Could not authorize this upload part."}), 503
        return jsonify({"url": url})

    @bp.post("/admin/projects/scans/r2-upload/<upload_id>/complete")
    @admin_only
    def r2_complete(upload_id):
        transfer = own_transfer(upload_id)
        done_url = capture_url(transfer.project, transfer.date)
        if transfer.status == "completed":
            return jsonify({"redirect": done_url, "duplicate": True})
        if transfer.status != "uploading" or transfer.expires_at <= core.now_utc():
            return jsonify({"error": "This upload has expired. Start it again to continue."}), 410
        company = core.lock_company_quota(transfer.company_id)
        if not company or company.status != "active":
            return jsonify({"error": "This workspace is not active for uploads."}), 403
        db.session.refresh(transfer)  # a retried "complete" may have finished while we waited
        if transfer.status == "completed":
            return jsonify({"redirect": done_url, "duplicate": True})
        if transfer.status != "uploading":
            return jsonify({"error": "This upload can't be finished. Start it again."}), 410
        storage = core.R2Storage()
        part_size = core.R2_UPLOAD_PART_SIZE
        try:
            parts = sorted(storage.list_multipart_parts(transfer.object_key, transfer.multipart_upload_id),
                           key=lambda part: part["PartNumber"])
        except Exception:
            current_app.logger.exception("Could not list parts for scan upload %s", upload_id)
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
            if transfer.project not in core.company_project_names(transfer.company_id):
                raise ValueError("project removed")
            storage.complete_multipart(object_key, multipart_id, parts)
            if storage.object_size(object_key) != transfer.file_size:
                raise ValueError("Uploaded object size does not match the reserved size")
            ext = core.extension(transfer.original_filename)
            if not core.upload_signature_ok(ext, storage.read_prefix(object_key, core.UPLOAD_HEAD_BYTES)):
                storage.delete(object_key)
                transfer.status = "failed"
                db.session.commit()
                return jsonify({"error": f"The file does not look like a real .{ext} scan."}), 400
            scan = Scan(company_id=transfer.company_id, project=transfer.project, date=transfer.date,
                        name=transfer.title or "Site scan", filename=core.R2_FILENAME_PREFIX + object_key,
                        original_filename=transfer.original_filename, source_size=transfer.file_size,
                        file_size=transfer.file_size, status="processing", uploaded_by=current_user.id)
            db.session.add(scan)
            transfer.status = "completed"
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                storage.abort_multipart(object_key, multipart_id)
            except Exception:
                pass  # already completed or gone
            try:
                storage.delete(object_key)
            except Exception:
                current_app.logger.exception("Could not remove an unreferenced scan upload object")
            failed = Transfer.query.filter_by(id=upload_id, company_id=transfer.company_id).first()
            if failed:
                failed.status = "failed"
                db.session.commit()
            current_app.logger.exception("Could not finalize scan upload %s", upload_id)
            return jsonify({"error": "The scan could not be finalized. Please upload it again."}), 500
        core.audit("upload_scan", "project_scan", scan.id,
                   f"Uploaded scan {scan.original_filename} for {scan.project} on {scan.date}")
        start_conversion(scan)
        return jsonify({"id": scan.id, "redirect": done_url})

    @bp.post("/admin/projects/scans/r2-upload/<upload_id>/abort")
    @admin_only
    def r2_abort(upload_id):
        transfer = own_transfer(upload_id, status="uploading")
        try:
            if transfer.multipart_upload_id:
                core.R2Storage().abort_multipart(transfer.object_key, transfer.multipart_upload_id)
            transfer.status = "aborted"
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Could not abort scan upload %s", upload_id)
            return jsonify({"error": "Could not cancel the cloud upload."}), 503
        return jsonify({"ok": True})

    # ------------------------------------------------------- manage scans
    @bp.get("/admin/projects/scans/<int:scan_id>/status")
    @admin_only
    def status(scan_id):
        response = jsonify(team_payload(admin_scan(scan_id)))
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.post("/admin/projects/scans/<int:scan_id>/delete")
    @admin_only
    def delete(scan_id):
        scan = admin_scan(scan_id)
        project, date, files = scan.project, scan.date, [scan.filename] + scan.derived_files
        db.session.delete(scan)
        db.session.commit()
        for name in files:
            core.delete_stored_file(name)
        core.audit("delete_scan", "project_scan", scan_id, f"Deleted scan for {project} on {date}")
        flash("Scan deleted.", "success")
        return redirect(capture_url(project, date))

    @bp.post("/admin/projects/scans/<int:scan_id>/reprocess")
    @admin_only
    def reprocess(scan_id):
        scan = admin_scan(scan_id)
        if scan.status == "processing" and not stale(scan):
            flash("This scan is still being prepared.", "error")
            return redirect(capture_url(scan.project, scan.date))
        start_conversion(scan)
        flash("Preparing the scan again. This usually takes a minute or two.", "success")
        return redirect(capture_url(scan.project, scan.date))

    @bp.get("/admin/projects/scans/<int:scan_id>/<asset>")
    @admin_only
    def team_asset(scan_id, asset):
        return asset_response(admin_scan(scan_id), asset)

    @bp.get("/admin/projects/scans/<int:scan_id>/3d")
    @admin_only
    def team_3d(scan_id):
        scan = admin_scan(scan_id)
        if scan.status != "ready" or not scan.points_filename:
            abort(404)
        return page_3d(scan, url_for("project_scans.team_asset", scan_id=scan.id, asset="points"),
                       capture_url(scan.project, scan.date),
                       url_for("project_scans.team_asset", scan_id=scan.id, asset="download"))

    # ----------------------------------------------- client project viewer
    def client_access(token):
        g.allow_public_viewer_scope = True
        access = core.ProjectAccess.query.filter_by(share_token=token).first()
        if not core.viewer_access_active(access):
            abort(404)
        unlocked = session.get("viewer_token") == access.share_token and session.get("viewer_code") == access.code
        if (not unlocked or not access.allow_media
                or not core.company_feature_enabled("flight_scans", access.company_id)):
            abort(403)
        return access

    def client_scan(access, scan_id):
        scan = Scan.query.filter_by(id=scan_id, project=access.project, company_id=access.company_id,
                                    status="ready").first()
        if not scan:
            abort(404)
        return scan

    @bp.get("/share/<token>/scans/<int:scan_id>/<asset>")
    def client_asset(token, scan_id, asset):
        access = client_access(token)
        return asset_response(client_scan(access, scan_id), asset)

    @bp.get("/share/<token>/scans/<int:scan_id>/3d")
    def client_3d(token, scan_id):
        access = client_access(token)
        scan = client_scan(access, scan_id)
        if not scan.points_filename:
            abort(404)
        return page_3d(scan, url_for("project_scans.client_asset", token=token, scan_id=scan.id, asset="points"),
                       url_for("shared_project", token=token, tab="map", date=scan.date.isoformat()),
                       url_for("project_scans.client_asset", token=token, scan_id=scan.id, asset="download"))

    def client_scans_by_date(access):
        """Ready scans for the client's map, keyed by ISO date (newest first within a date)."""
        out = {}
        token = access.share_token
        for scan in (Scan.query.filter_by(project=access.project, company_id=access.company_id, status="ready")
                     .order_by(Scan.date, Scan.uploaded_at.desc()).all()):
            out.setdefault(scan.date.isoformat(), []).append(scan_payload(
                scan, lambda asset, s=scan: url_for("project_scans.client_asset", token=token, scan_id=s.id, asset=asset),
                url_for("project_scans.client_3d", token=token, scan_id=scan.id)))
        return out

    def team_scans_for(project, date):
        if not project:
            return []
        return [team_payload(scan) for scan in Scan.query.filter_by(project=project, date=date, company_id=company_id())
                .order_by(Scan.uploaded_at.desc()).all()]

    def team_scan_dates(project):
        if not project:
            return []
        rows = (db.session.query(Scan.date).filter_by(project=project, company_id=company_id())
                .distinct().order_by(Scan.date.desc()).all())
        return [row[0] for row in rows]

    bp.client_scans_by_date = client_scans_by_date
    bp.team_scans_for = team_scans_for
    bp.team_scan_dates = team_scan_dates
    return bp
