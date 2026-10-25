"""Owner recovery never creates a second owner or touches another account."""
import os
os.environ.setdefault('APP_ENV', 'testing')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'owner-recovery-testing-only')
import unittest
from unittest.mock import patch
import app as portal


class OwnerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.context = portal.app.app_context()
        self.context.push()
        self.assertEqual(portal.db.engine.url.database, ':memory:')
        portal.db.create_all()
        self.owner = portal.User(username='old-owner', name='Owner', is_admin=True, platform_owner=True)
        self.owner.set_password('old-disposable-password')
        self.other = portal.User(username='company-admin', name='Company', is_admin=True)
        self.other.set_password('other-disposable-password')
        portal.db.session.add_all([self.owner, self.other])
        portal.db.session.commit()
        self.owner_id = self.owner.id

    def tearDown(self):
        portal.db.session.remove()
        portal.db.drop_all()
        self.context.pop()

    def recover(self, **overrides):
        env = {'OWNER_CREDENTIAL_RESET': '1', 'ADMIN_USERNAME': 'platform-owner',
               'ADMIN_PASSWORD': 'new-disposable-password'}
        env.update(overrides)
        with patch.dict(os.environ, env):
            return portal.app.test_cli_runner().invoke(args=['reset-platform-owner'])

    def test_updates_existing_owner_and_authenticates_without_exposing_password(self):
        result = self.recover()
        self.assertEqual(result.exit_code, 0, result.output)
        owner = portal.db.session.get(portal.User, self.owner_id)
        self.assertEqual(owner.username, 'platform-owner')
        self.assertTrue(owner.check_password('new-disposable-password'))
        self.assertFalse(owner.check_password('old-disposable-password'))
        self.assertTrue(self.other.check_password('other-disposable-password'))
        self.assertEqual(portal.User.query.count(), 2)
        self.assertNotIn('new-disposable-password', result.output)
        self.assertIn('already match', self.recover().output)

    def test_requires_explicit_request(self):
        self.assertEqual(self.recover(OWNER_CREDENTIAL_RESET='').exit_code, 0)
        self.assertEqual(self.owner.username, 'old-owner')

    def test_rejects_collision_and_short_password_without_changes(self):
        self.assertNotEqual(self.recover(ADMIN_USERNAME='company-admin').exit_code, 0)
        self.assertNotEqual(self.recover(ADMIN_PASSWORD='short').exit_code, 0)
        self.assertEqual(self.owner.username, 'old-owner')
        self.assertTrue(self.owner.check_password('old-disposable-password'))

    def test_refuses_ambiguous_owner_selection(self):
        self.other.platform_owner = True
        portal.db.session.commit()
        self.assertNotEqual(self.recover().exit_code, 0)
        self.assertEqual(self.owner.username, 'old-owner')

    def test_owner_login_retains_session_without_a_company_subscription(self):
        self.assertEqual(self.recover().exit_code, 0)
        portal.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        client = portal.app.test_client()
        client.get('/login')
        with client.session_transaction() as state:
            token = state['_csrf']
        response = client.post('/login', data={'username': 'platform-owner',
            'password': 'new-disposable-password', '_csrf': token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Platform Owner', response.get_data(as_text=True))
        self.assertNotEqual(response.request.path, '/login')
        with client.session_transaction() as state:
            self.assertEqual(state['_user_id'], str(self.owner_id))
        self.assertEqual(client.get('/dashboard').status_code, 200)

    def test_non_owner_without_membership_still_cannot_access_owner_pages(self):
        portal.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state['_user_id'] = str(self.other.id)
            state['_fresh'] = True
        response = client.get('/owner')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)


if __name__ == '__main__':
    unittest.main()
