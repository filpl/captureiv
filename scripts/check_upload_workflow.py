"""Real R2 transfer through app routes, using only an isolated disposable database."""
import base64
import hashlib
import os
import secrets
import struct
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote


class ProbeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def check():
    with tempfile.TemporaryDirectory(prefix='captureiv-upload-probe-') as directory:
        # Override only this subprocess: production DB, users and billing stay untouched.
        os.environ['DATABASE_URL'] = 'sqlite:///' + (Path(directory) / 'isolated.sqlite').as_posix()
        os.environ['APP_ENV'] = 'testing'
        os.environ['SECRET_KEY'] = secrets.token_urlsafe(48)
        os.environ['SESSION_COOKIE_SECURE'] = '0'
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import app as portal
        portal.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        portal.app.logger.disabled = True
        with portal.app.app_context():
            require(portal.db.engine.url.database == (Path(directory) / 'isolated.sqlite').as_posix(), 'Isolated database required')
            portal.db.create_all()
            company = portal.Company(id=2147483000, name='Isolated storage diagnostic', status='active', storage_limit_bytes=512*1024*1024)
            other = portal.Company(id=2147483001, name='Isolated other tenant', status='active', storage_limit_bytes=512*1024*1024)
            user = portal.User(username='upload-probe', name='Storage diagnostic', is_admin=True)
            user.set_password(secrets.token_urlsafe(32))
            outsider = portal.User(username='other-probe', name='Other tenant', is_admin=True)
            outsider.set_password(secrets.token_urlsafe(32))
            portal.db.session.add_all([company, other, user, outsider])
            portal.db.session.flush()
            portal.db.session.add_all([
                portal.CompanyMembership(company_id=company.id, user_id=user.id, role='company_admin', status='active'),
                portal.CompanyMembership(company_id=other.id, user_id=outsider.id, role='company_admin', status='active')])
            portal.db.session.commit()
            client = portal.app.test_client()
            csrf = secrets.token_urlsafe(32)
            with client.session_transaction() as session:
                session['_user_id'] = str(user.id)
                session['_fresh'] = True
                session['_csrf'] = csrf
            headers = {'X-CSRF-Token': csrf}
            fixture = base64.b64decode((Path(__file__).parent/'fixtures/storage-check.mp4.b64').read_text())
            # A valid MP4 'free' box pads our owned two-second clip past the 64 MiB part boundary.
            video = Path(directory)/'captureiv-diagnostic.mp4'
            padding_size = 64*1024*1024
            with video.open('wb') as output:
                output.write(fixture)
                output.write(struct.pack('>I4s', padding_size+8, b'free'))
                for _ in range(64):
                    output.write(bytes(1024*1024))
            expected = hashlib.sha256()
            with video.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    expected.update(block)
            payload = {'filename': video.name, 'size': video.stat().st_size, 'lastModified': 1,
                'date': portal.now_utc().date().isoformat(), 'project': 'Isolated diagnostic site',
                'direction': portal.DIRECTIONS[0], 'title': 'Diagnostic video'}
            started = client.post('/admin/r2-upload/start', json=payload, headers=headers)
            require(started.status_code == 200, 'Application rejected upload start (HTTP '+str(started.status_code)+')')
            upload = started.get_json()
            require(upload['partCount'] == 2, 'Two real multipart parts required')
            upload_id = upload['uploadId']
            with video.open('rb') as stream:
                for number in (1, 2):
                    signed = client.post(f'/admin/r2-upload/{upload_id}/part-url', json={'partNumber': number}, headers=headers)
                    require(signed.status_code == 200, 'Application did not authorize upload part')
                    chunk = stream.read(upload['partSize'])
                    with urlopen(Request(signed.get_json()['url'], data=chunk, method='PUT', headers={'Content-Type':'video/mp4'}), timeout=120) as response:
                        require(response.status == 200 and response.headers.get('ETag'), 'R2 did not accept part')
            resumed = client.post('/admin/r2-upload/start', json=payload, headers=headers)
            require(resumed.status_code == 200 and len(resumed.get_json()['uploadedParts']) == 2, 'Uploaded parts could not be resumed')
            completed = client.post(f'/admin/r2-upload/{upload_id}/complete', json={}, headers=headers)
            require(completed.status_code == 200, 'Application could not finalize uploaded video')
            flight = portal.db.session.get(portal.Flight, completed.get_json()['id'])
            require(flight is not None and flight.status == 'ready', 'Uploaded video metadata missing')
            route = '/files/' + quote(flight.filename, safe='/')
            authorized = client.get(route)
            require(authorized.status_code == 302, 'Authorized file did not receive a signed redirect')
            actual = hashlib.sha256()
            with urlopen(authorized.location, timeout=120) as response:
                for chunk in iter(lambda: response.read(1024*1024), b''):
                    actual.update(chunk)
            require(actual.digest() == expected.digest(), 'Application download checksum mismatch')
            with urlopen(Request(authorized.location, headers={'Range':'bytes=0-63'}), timeout=30) as response:
                require(response.status == 206 and response.read() == fixture[:64], 'Video range streaming failed')
            anonymous = portal.app.test_client()
            # This probe holds an outer app context; real HTTP requests do not.
            # Reset Flask-Login's per-request cache before changing test clients.
            portal.g.pop('_login_user', None)
            require(anonymous.get(route).status_code in (401,403,404), 'Anonymous file access was allowed')
            other_client = portal.app.test_client()
            with other_client.session_transaction() as session:
                session['_user_id'] = str(outsider.id)
                session['_fresh'] = True
            portal.g.pop('_login_user', None)
            require(other_client.get(route).status_code in (403,404), 'Other company could access the file')
            # Delete only the UUID object this isolated probe just created, never customer data.
            key = portal.r2_object_key(flight.filename)
            require(key.startswith('companies/2147483000/uploads/' + upload_id + '-'), 'Diagnostic cleanup key mismatch')
            storage = portal.R2Storage()
            storage.delete(key)
            # Also remove only this probe's UUID-named leftovers from interrupted runs.
            for page in storage.client.get_paginator('list_objects_v2').paginate(
                    Bucket=storage.bucket, Prefix='companies/2147483000/uploads/'):
                for item in page.get('Contents', []):
                    if portal.re.fullmatch(r'companies/2147483000/uploads/[0-9a-f-]{36}-captureiv-diagnostic\.mp4', item['Key']):
                        storage.delete(item['Key'])
            portal.db.session.remove()
            portal.db.engine.dispose()
    print('PASS: real 64+ MiB application upload, two multipart parts, resume, finalize, checksum download, range streaming and anonymous/cross-company denial. Diagnostic object removed; production records untouched.')


if __name__ == '__main__':
    try:
        check()
    except Exception as error:
        print('FAIL: isolated application upload check ('+type(error).__name__+'). No credentials or signed URLs logged.')
        if isinstance(error, ProbeFailure):
            print('DETAIL: '+str(error))
        raise SystemExit(1)
