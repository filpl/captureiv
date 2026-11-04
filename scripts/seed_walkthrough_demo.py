"""Seed a LOCAL database with a walkthrough demo, using the real upload/API endpoints.

Refuses to run against production or a non-SQLite database. Usage:
    DATABASE_URL=sqlite:///path/demo.db SECRET_KEY=... DEMO_PASSWORD=... \
        python scripts/seed_walkthrough_demo.py SAMPLES_DIR
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.environ.get("APP_ENV", "development").lower() == "production" or \
        not os.environ.get("DATABASE_URL", "").startswith("sqlite:///"):
    raise SystemExit("Refusing to seed: use a local SQLite DATABASE_URL outside production.")

import app as portal  # noqa: E402

PROJECT = "Harbor Street Offices"
COMPANY = "Walkthrough Demo Co"


def main(samples: Path) -> None:
    password = os.environ.get("DEMO_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("Set DEMO_PASSWORD (12+ characters) for the local demo admin.")
    portal.init_db()
    with portal.app.app_context():
        company = portal.Company.query.filter_by(name=COMPANY).first()
        if company:
            print("Demo company already exists; nothing changed.")
            return
        company = portal.Company(name=COMPANY, status="active", storage_limit_bytes=20 * 1024**3,
                                 service_drone=True, service_walkthrough=True)
        user = portal.User(username="walkdemo", name="Demo Admin", is_admin=True, active=True,
                           email="walkdemo@example.test", email_verified=True)
        user.set_password(password)
        portal.db.session.add_all([company, user]); portal.db.session.flush()
        portal.db.session.add(portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                       role="company_admin", status="active"))
        portal.db.session.commit()
        user_id = user.id

    client = portal.app.test_client()
    with client.session_transaction() as state:
        state["_user_id"] = str(user_id); state["_fresh"] = True; state["_csrf"] = "seed"
    headers = {"X-CSRF-Token": "seed"}

    def post(path, **kwargs):
        response = client.post("/walkthrough/api" + path, headers=headers, **kwargs)
        if response.status_code != 200:
            raise SystemExit(f"{path} failed: {response.status_code} {response.get_data(as_text=True)[:300]}")
        return response.get_json()

    def upload(path, name, form):
        data = dict(form, project=PROJECT, file=(BytesIO((samples / name).read_bytes()), name))
        return post(path, data=data, content_type="multipart/form-data")

    sheet1 = upload("/sheets", "A-101 Level 1 plan.pdf", {"floor": "Level 1", "name": "A-101 Floor plan"})["id"]
    sheet2 = upload("/sheets", "A-102 Level 2 plan.pdf", {"floor": "Level 2", "name": "A-102 Floor plan"})["id"]
    model = upload("/models", "sample-building.glb", {"name": "Building shell", "floor": "All floors"})["id"]

    # Plan coordinates are fractions of the drawing; the building interior spans ~x .09-.81, y .13-.88.
    route_a = [(0.15, 0.78), (0.24, 0.62), (0.36, 0.62), (0.47, 0.70), (0.58, 0.62), (0.70, 0.62), (0.76, 0.42), (0.62, 0.28)]
    route_b = [(0.16, 0.76), (0.25, 0.60), (0.37, 0.64), (0.48, 0.68), (0.59, 0.60), (0.71, 0.64), (0.75, 0.40), (0.60, 0.30), (0.40, 0.25)]
    sessions = [("Early framing walk", "2026-10-02", "#f59e0b", route_a, "early"),
                ("Drywall progress walk", "2026-10-21", "#3b82f6", route_b, "late")]
    for name, day, color, route, phase in sessions:
        sid = post("/sessions", json={"sheet_id": sheet1, "name": name, "date": day, "color": color})["id"]
        for n, (x, y) in enumerate(route):
            pid = post("/points", json={"session_id": sid, "x": x, "y": y})["id"]
            pano = samples / f"pano-{phase}-{n % 4 + 1}.png"
            post(f"/points/{pid}/media", data={"kind": "pano", "files": (BytesIO(pano.read_bytes()), pano.name)},
                 content_type="multipart/form-data")
            # Map the 2D drawing onto the 3D shell (32 m x 21 m footprint) at eye height.
            post(f"/points/{pid}", json={"model_id": model, "m": [(x - 0.09) / 0.72 * 32, 1.6, (1 - (y - 0.13) / 0.75) * 21]})
    sid = post("/sessions", json={"sheet_id": sheet2, "name": "Level 2 layout walk", "date": "2026-10-21", "color": "#10b981"})["id"]
    for x, y in [(0.2, 0.7), (0.4, 0.55), (0.6, 0.55), (0.72, 0.35)]:
        pid = post("/points", json={"session_id": sid, "x": x, "y": y})["id"]
        pano = samples / "pano-late-2.png"
        post(f"/points/{pid}/media", data={"kind": "pano", "files": (BytesIO(pano.read_bytes()), pano.name)},
             content_type="multipart/form-data")
    print(f"Seeded '{PROJECT}' for local user 'walkdemo'.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
