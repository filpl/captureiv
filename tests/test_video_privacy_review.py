"""Local-only regressions for the video review; no real emails or payments."""
import os
os.environ.setdefault('APP_ENV','testing')
os.environ.setdefault('DATABASE_URL','sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY','video-review-testing-only')
import unittest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import app as portal


class VideoPrivacyReviewTests(unittest.TestCase):
    def setUp(self):
        portal.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        with portal.app.app_context():
            self.assertEqual(portal.db.engine.url.database, ':memory:')
            portal.db.session.remove()
            portal.db.drop_all()
            portal.db.create_all()
            company=portal.Company(name='Video review fixture',status='pending')
            user=portal.User(username='video-review@example.test',email='video-review@example.test',name='Test',email_verified=True,active=False)
            user.set_password('test-disposable-password')
            plan=portal.SubscriptionPlan(name='Basic',monthly_amount_cents=39900,annual_amount_cents=454860,
                stripe_monthly_price_id='price_fixture_monthly',stripe_annual_price_id='price_fixture_annual')
            portal.db.session.add_all([company,user,plan]);portal.db.session.flush()
            membership=portal.CompanyMembership(company_id=company.id,user_id=user.id,role='company_admin',status='verified_pending_billing')
            subscription=portal.CompanySubscription(company_id=company.id,plan_id=plan.id,status='pending',billing_interval='monthly')
            portal.db.session.add_all([membership,subscription]);portal.db.session.commit()
            self.subscription_id=subscription.id;self.user_id=user.id;self.plan_id=plan.id
        self.client=portal.app.test_client()
        with self.client.session_transaction() as state:
            state['pending_checkout_subscription_id']=self.subscription_id
            state['_csrf']='video-review-csrf'
        self.path=f'/account/checkout/{self.subscription_id}'
        self.environment=patch.dict(os.environ,{'STRIPE_SECRET_KEY':'sk_test_local_fixture'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        with portal.app.app_context():
            portal.db.session.remove();portal.db.drop_all()

    def post(self,accept=True):
        data={'_csrf':'video-review-csrf'}
        if accept:data['accept_billing_terms']='yes'
        return self.client.post(self.path,data=data)

    def test_disclosures_are_next_to_consent_and_payment_for_each_interval(self):
        for interval,amount,period in [('monthly','$399.00','month'),('annual','$4548.60','year')]:
            with portal.app.app_context():
                portal.db.session.get(portal.CompanySubscription,self.subscription_id).billing_interval=interval
                portal.db.session.commit()
            response=self.client.get(self.path);html=response.get_data(as_text=True)
            self.assertEqual(response.status_code,200)
            self.assertIn(amount+' per company per '+period,html)
            self.assertIn('I expressly agree to automatic recurring billing',html)
            self.assertLess(html.index('id="renewal-disclosure"'),html.index('Continue to secure payment'))
            self.assertIn('90-day read-only',html)
            self.assertIn('/billing',html);self.assertIn(portal.BILLING_TERMS_VERSION,html)

    def test_no_consent_or_unreviewed_or_changed_price_never_starts_checkout(self):
        with patch.object(portal,'urlopen') as api:
            self.assertEqual(self.post().status_code,302)
            self.client.get(self.path)
            self.assertEqual(self.post(False).status_code,302)
            with portal.app.app_context():
                portal.db.session.get(portal.SubscriptionPlan,self.plan_id).monthly_amount_cents=49900
                portal.db.session.commit()
            self.assertEqual(self.post().status_code,302)
            api.assert_not_called()
        with portal.app.app_context():
            self.assertEqual(portal.AuditLog.query.filter_by(action='billing_terms_accepted').count(),0)

    def test_accepted_agreement_is_saved_before_mocked_checkout(self):
        self.client.get(self.path)
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'url':'https://checkout.stripe.test/fixture'}).encode()
        def checkout(request,**kwargs):
            with portal.app.app_context():
                row=portal.AuditLog.query.filter_by(action='billing_terms_accepted').one()
                agreement=json.loads(row.detail)
                self.assertEqual(row.user_id,self.user_id)
                self.assertEqual(agreement['price_id'],'price_fixture_monthly')
                self.assertEqual(agreement['amount_cents'],39900)
                self.assertTrue(agreement['automatic_renewal'])
                self.assertEqual(agreement['version'],portal.BILLING_TERMS_VERSION)
            return response
        with patch.object(portal,'urlopen',side_effect=checkout):
            self.assertEqual(self.post().status_code,303)

    def test_failed_consent_save_cannot_start_payment(self):
        self.client.get(self.path)
        with patch.object(portal.db.session,'commit',side_effect=RuntimeError('local fixture failure')),patch.object(portal,'urlopen') as api:
            self.assertEqual(self.post().status_code,503)
            api.assert_not_called()

    def test_fonts_and_policy_links_work_without_a_login(self):
        root=Path(portal.__file__).parent
        for filename in ['style.css','site-capture.css','fieldview-sandbox/style.css']:
            css=(root/'static'/filename).read_text(encoding='utf-8')
            self.assertNotIn('fonts.googleapis.com',css)
            self.assertNotIn('fonts.gstatic.com',css)
            self.assertNotIn('display=swap',css)
            self.assertIn('url(/static/fonts/dm-sans-latin.woff2)',css)
        for filename in ['dm-sans-latin.woff2','manrope-latin.woff2']:
            response=self.client.get('/static/fonts/'+filename)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data[:4],b'wOF2')
            response.close()
        login=self.client.get('/login').get_data(as_text=True)
        for path in ['/privacy','/billing','/copyright']:self.assertIn(path,login)
        self.assertNotIn('HERSHEY CAPITAL LLC',login)


if __name__=='__main__':unittest.main()
