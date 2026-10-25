import io
import unittest
from urllib.error import HTTPError
from scripts.check_launch_services import is_authorization_rejection
from scripts.check_upload_workflow import reset_request_state
from datetime import date
from unittest.mock import MagicMock, patch
import app as portal


class StorageAuthorizationProbeTests(unittest.TestCase):
    def error(self, status, body):
        return HTTPError('https://storage.example.invalid/object', status, 'failure', {}, io.BytesIO(body))

    def test_r2_missing_authorization_is_denied(self):
        self.assertTrue(is_authorization_rejection(self.error(400,
            b'<Error><Code>InvalidArgument</Code><Message>Authorization</Message></Error>')))

    def test_unrelated_bad_request_is_not_a_privacy_pass(self):
        self.assertFalse(is_authorization_rejection(self.error(400,
            b'<Error><Code>InvalidArgument</Code><Message>Something else</Message></Error>')))
        self.assertFalse(is_authorization_rejection(self.error(400,b'not XML')))
        self.assertFalse(is_authorization_rejection(self.error(500,b'')))

    def test_diagnostic_clients_have_independent_identity_and_viewer_scope(self):
        with portal.app.app_context():
            self.assertEqual(portal.db.engine.url.database, ':memory:')
            portal.app.config.update(TESTING=True,SESSION_COOKIE_SECURE=False)
            portal.db.create_all()
            try:
                companies=[portal.Company(name=n,status='active') for n in ('A','B')]
                users=[portal.User(username=n,name=n,is_admin=True) for n in ('probe-a','probe-b')]
                for user in users:
                    user.set_password('disposable-probe-password')
                portal.db.session.add_all(companies+users)
                portal.db.session.flush()
                for company,user in zip(companies,users):
                    portal.db.session.add(portal.CompanyMembership(company_id=company.id,user_id=user.id,role='company_admin',status='active'))
                filename='r2__companies/1/probe.mp4'
                portal.db.session.add(portal.Flight(company_id=companies[0].id,uploaded_by=users[0].id,
                    flight_id='PROBE',date=date.today(),direction='W-E',title='Probe',project='Probe',
                    filename=filename,original_filename='probe.mp4'))
                portal.db.session.commit()
                clients=[portal.app.test_client() for _ in range(3)]
                for client,user in zip((clients[0],clients[2]),users):
                    with client.session_transaction() as state:
                        state['_user_id']=str(user.id)
                        state['_fresh']=True
                fake=MagicMock()
                fake.signed_download.return_value='https://storage.example.invalid/signed'
                with patch.object(portal,'R2Storage',return_value=fake):
                    reset_request_state(portal)
                    self.assertEqual(clients[0].get('/files/'+filename).status_code,302)
                    reset_request_state(portal)
                    self.assertEqual(clients[1].get('/files/'+filename).status_code,403)
                    self.assertTrue(portal.g.allow_public_viewer_scope)
                    reset_request_state(portal)
                    self.assertEqual(clients[2].get('/files/'+filename).status_code,404)
            finally:
                portal.db.session.remove()
                portal.db.drop_all()
