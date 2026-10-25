"""Navigation keeps authentication and tenant isolation across workspace pages."""
import os
os.environ.setdefault('APP_ENV', 'testing')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'navigation-tests-only')
import unittest
from datetime import date
import re
import app as portal


class NavigationSessionTests(unittest.TestCase):
    def setUp(self):
        portal.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        with portal.app.app_context():
            portal.db.create_all()
            a = portal.Company(name='Navigation A', status='active')
            b = portal.Company(name='Navigation B', status='active')
            suspended = portal.Company(name='Suspended', status='suspended')
            owner = portal.User(username='navigation-owner', name='Owner', is_admin=True, platform_owner=True, active=True)
            admin = portal.User(username='navigation-admin', name='Admin', is_admin=True, active=True)
            employee = portal.User(username='navigation-employee', name='Employee', active=True)
            for user in (owner, admin, employee):
                user.set_password('navigation-disposable-password')
            portal.db.session.add_all([a, b, suspended, owner, admin, employee])
            portal.db.session.flush()
            self.a, self.b, self.suspended = a.id, b.id, suspended.id
            self.owner, self.admin, self.employee = owner.id, admin.id, employee.id
            for user, role in [(admin, 'company_admin'), (employee, 'employee')]:
                portal.db.session.add(portal.CompanyMembership(company_id=a.id, user_id=user.id, role=role, status='active'))
            for company, title in [(a, 'Only company A'), (b, 'Only company B')]:
                portal.db.session.add(portal.Flight(company_id=company.id, flight_id=title, date=date(2026,10,25),
                    direction='W-E', title=title, project=title, filename=title+'.mp4', original_filename='test.mp4', uploaded_by=owner.id))
            portal.db.session.commit()

    def tearDown(self):
        with portal.app.app_context():
            portal.db.session.remove()
            portal.db.drop_all()

    def client(self, user):
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state['_user_id'] = str(user)
            state['_fresh'] = True
            state['_csrf'] = 'navigation-csrf'
        return client

    def check_page(self, client, path, label):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:500])
        html = response.get_data(as_text=True)
        active = re.findall(r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>', html)
        self.assertEqual(active, [label])
        self.assertNotIn('name="password"', html)
        return html

    def test_company_admin_can_switch_all_pages_without_login(self):
        client = self.client(self.admin)
        for path, label in [('/dashboard','Archive'),('/admin','Admin'),('/admin/upload','Upload'),
                            ('/admin/fieldview-sandbox','Sandbox'),('/admin/site-capture','Admin')]:
            self.check_page(client, path, label)
        with client.session_transaction() as state:
            self.assertEqual(state['_user_id'], str(self.admin))

    def test_unscoped_owner_sees_workspace_guide_instead_of_login(self):
        client = self.client(self.owner)
        for path, label in [('/dashboard','Archive'),('/admin','Admin'),('/admin/upload','Upload'),
                            ('/admin/fieldview-sandbox','Sandbox'),('/owner','Platform Owner')]:
            html = self.check_page(client, path, label)
            if path != '/owner':
                self.assertIn('Choose a company workspace', html)
        with client.session_transaction() as state:
            self.assertEqual(state['_user_id'], str(self.owner))

    def test_owner_workspace_selection_retains_scope_across_pages(self):
        client = self.client(self.owner)
        for company, visible, hidden in [(self.a,'Only company A','Only company B'), (self.b,'Only company B','Only company A')]:
            response = client.post('/owner/workspace', data={'_csrf':'navigation-csrf','company_id':company,'next_endpoint':'admin'})
            self.assertEqual(response.location, '/admin')
            for path, label in [('/dashboard','Archive'),('/admin','Admin'),('/admin/upload','Upload')]:
                html = self.check_page(client, path, label)
                self.assertIn(visible, html)
                self.assertNotIn(hidden, html)
            flights = client.get('/api/flights').get_json()['flights']
            self.assertEqual({f['title'] for f in flights}, {visible})
            self.check_page(client,'/admin/fieldview-sandbox','Sandbox')
        with portal.app.app_context():
            self.assertEqual(portal.CompanyMembership.query.filter_by(user_id=self.owner).count(), 0)

    def test_workspace_switch_requires_owner_csrf_and_active_company(self):
        owner = self.client(self.owner)
        self.assertEqual(owner.post('/owner/workspace',data={'company_id':self.a}).status_code,400)
        for value in [self.suspended,999999,'invalid']:
            self.assertEqual(owner.post('/owner/workspace',data={'_csrf':'navigation-csrf','company_id':value}).status_code,404)
        self.assertEqual(owner.post('/owner/workspace',data={'_csrf':'navigation-csrf','company_id':self.a,'next_endpoint':'https://evil.example'}).location,'/dashboard')
        admin = self.client(self.admin)
        self.assertEqual(admin.post('/owner/workspace',data={'_csrf':'navigation-csrf','company_id':self.b}).status_code,403)
        with admin.session_transaction() as state:
            state['owner_company_id'] = self.b
        self.assertEqual({f['title'] for f in admin.get('/api/flights').get_json()['flights']},{'Only company A'})

    def test_permission_denial_keeps_session_and_does_not_show_login_form(self):
        client = self.client(self.employee)
        response = client.get('/admin')
        self.assertEqual(response.status_code,403)
        self.assertIn('You are still signed in',response.get_data(as_text=True))
        self.assertNotIn('name="password"',response.get_data(as_text=True))
        with client.session_transaction() as state:
            self.assertEqual(state['_user_id'],str(self.employee))


if __name__ == '__main__':
    unittest.main()
