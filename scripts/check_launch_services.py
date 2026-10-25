"""Hosting-console diagnostics. No customer records, billing or secret output."""
import argparse
import hashlib
import sys
import tempfile
import subprocess
from xml.etree import ElementTree
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as portal


class DiagnosticFailure(ValueError):
    """Only deliberately authored, non-sensitive diagnostic messages."""


def is_authorization_rejection(error):
    if error.code in {401, 403, 404}:
        return True
    if error.code != 400:
        return False
    try:
        body = ElementTree.fromstring(error.read(4096))
        return body.findtext('Code') == 'InvalidArgument' and body.findtext('Message') == 'Authorization'
    except ElementTree.ParseError:
        return False


def read_url(url, *, data=None, method=None, headers=None):
    with urlopen(Request(url, data=data, method=method, headers=headers or {}), timeout=30) as response:
        return response.read(), response.headers


def storage_check(run_id):
    storage = portal.R2Storage()
    body = b'Capture IV private storage diagnostic. No customer data.\n'
    key = f'diagnostics/{run_id}/private-upload-check.txt'
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'private-upload-check.txt'
        path.write_bytes(body)
        storage.upload_file(path, key, 'text/plain')
    if storage.object_size(key) != len(body):
        raise DiagnosticFailure('Storage size mismatch')
    signed = storage.signed_download(key, 'private-upload-check.txt', 'text/plain', True, expires=60)
    downloaded, _ = read_url(signed)
    if hashlib.sha256(downloaded).digest() != hashlib.sha256(body).digest():
        raise DiagnosticFailure('Storage content mismatch')
    unsigned_parts = urlsplit(signed)
    unsigned = urlunsplit((unsigned_parts.scheme, unsigned_parts.netloc, unsigned_parts.path, '', ''))
    try:
        read_url(unsigned)
    except HTTPError as error:
        if not is_authorization_rejection(error):
            raise DiagnosticFailure('Unexpected private-access response') from None
    else:
        raise DiagnosticFailure('Unauthenticated object access succeeded')
    print('PASS: managed R2 upload, checksum download and unauthenticated access rejection.')
    # Exercise the same signed multipart PUT used for browser drone uploads.
    multipart_key = f'diagnostics/{run_id}/multipart-upload-check.txt'
    upload_id = storage.start_multipart(multipart_key, 'text/plain')
    part_url = storage.signed_upload_part(multipart_key, upload_id, 1)
    _, cors = read_url(part_url, method='OPTIONS', headers={
        'Origin': 'https://app.captureiv.com', 'Access-Control-Request-Method': 'PUT',
        'Access-Control-Request-Headers': 'content-type'})
    if cors.get('Access-Control-Allow-Origin') != 'https://app.captureiv.com':
        raise DiagnosticFailure('Browser upload CORS origin was not allowed')
    _, headers = read_url(part_url, data=body, method='PUT', headers={'Content-Type': 'text/plain'})
    etag = headers.get('ETag')
    if not etag:
        raise DiagnosticFailure('Multipart response missing ETag')
    parts = storage.list_multipart_parts(multipart_key, upload_id)
    if len(parts) != 1 or parts[0]['PartNumber'] != 1:
        raise DiagnosticFailure('Multipart part not listed')
    storage.complete_multipart(multipart_key, upload_id, [{'PartNumber': 1, 'ETag': etag}])
    multipart_url = storage.signed_download(multipart_key, 'multipart-upload-check.txt', 'text/plain', True, expires=60)
    if read_url(multipart_url)[0] != body:
        raise DiagnosticFailure('Multipart download mismatch')
    print('PASS: R2 upload, checksum download, private access, browser CORS and signed multipart transfer.')
    print('Two tiny diagnostic objects retained under diagnostics/' + run_id + '/.')


def email_check(recipient):
    sent = portal.send_account_email(recipient, 'Capture IV - launch delivery check',
        'This is the single delivery check you requested for Capture IV.\n\n'
        'The platform sent this through its production email connection.\n'
        'No password, verification code or payment information is included.\n\n'
        'Platform: https://app.captureiv.com/login\n')
    if not sent:
        raise DiagnosticFailure('Email service is not configured')
    print('PASS: SMTP authenticated and accepted the requested test email. Check provider delivery status.')


def run_once(run_id, name, callback):
    existing = portal.AuditLog.query.filter_by(action='launch_check_' + name,
        resource_type='diagnostic', resource_id=run_id).first()
    if existing:
        print('SKIP: ' + name + ' already completed for this run.')
        return True
    try:
        callback()
        portal.db.session.add(portal.AuditLog(action='launch_check_' + name,
            resource_type='diagnostic', resource_id=run_id, detail='Production diagnostic completed successfully.'))
        portal.db.session.commit()
        return True
    except Exception as error:
        portal.db.session.rollback()
        # Errors from HTTP libraries can contain signed URLs: never print them.
        status = getattr(error, 'code', None) or getattr(error, 'smtp_code', None)
        if not isinstance(status, int):
            status = None
        print('FAIL: ' + name + ' (' + type(error).__name__ +
              (', status ' + str(status) if status else '') + '). No credential details logged.')
        if isinstance(error, DiagnosticFailure):
            print('DETAIL: ' + str(error))
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--application-flow', action='store_true')
    args = parser.parse_args()
    if not portal.re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', args.run_id):
        parser.error('Run ID must contain only letters, numbers, underscores or hyphens.')
    with portal.app.app_context():
        storage_ok = run_once(args.run_id, 'storage', lambda: storage_check(args.run_id))
        email_ok = run_once(args.run_id, 'email', lambda: email_check(args.email))
        application_ok = True
        if args.application_flow and storage_ok:
            application_ok = run_once(args.run_id, 'upload_workflow', lambda: subprocess.run(
                [sys.executable, str(Path(__file__).with_name('check_upload_workflow.py'))], check=True))
    return 0 if storage_ok and email_ok and application_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
