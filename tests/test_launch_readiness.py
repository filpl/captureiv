"""Isolated launch regressions: no external services or customer data."""
import os
os.environ.setdefault('APP_ENV', 'testing')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'launch-regression-only-secret')

import hashlib
import hmac
import json
import time
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
import app as portal


class LaunchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.context = portal.app.app_context()
        self.context.push()
        self.assertEqual(portal.db.engine.url.database, ':memory:')
        portal.app.config.update(TESTING=True, APP_ENV='testing', SESSION_COOKIE_SECURE=False)
        portal.db.create_all()

    def tearDown(self):
        portal.db.session.remove()
        portal.db.drop_all()
        self.context.pop()

    def make_workspace(self):
        company = portal.Company(name='Retrieval customer', status='billing_suspended')
        user = portal.User(username='retrieval-admin', name='Owner', is_admin=True,
                           active=True, email_verified=True)
        user.set_password('disposable-regression-password')
        portal.db.session.add_all([company, user])
        portal.db.session.flush()
        portal.db.session.add(portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                      role='company_admin', status='active'))
        subscription = portal.CompanySubscription(company_id=company.id, status='canceled',
            current_period_end=portal.now_utc() - timedelta(days=1), stripe_subscription_id='sub_launch')
        flight = portal.Flight(company_id=company.id, flight_id='OWN', date=portal.now_utc().date(),
            direction='W-E', title='Own footage', project='Our site', filename='r2__companies/1/own.mp4',
            original_filename='own.mp4', uploaded_by=user.id)
        portal.db.session.add_all([subscription, flight])
        portal.db.session.commit()
        client = portal.app.test_client()
        with client.session_transaction() as state:
            state['_user_id'] = str(user.id)
            state['_fresh'] = True
            state['_csrf'] = 'retrieval-test-csrf'
        return client, company, user, subscription, flight

    def test_restricted_keys_keep_live_and_test_separate(self):
        for prefix in ('sk_live_', 'rk_live_'):
            self.assertTrue(portal.stripe_api_key_is_valid(prefix + 'fixture', 'production'))
            self.assertFalse(portal.stripe_api_key_is_valid(prefix + 'fixture', 'testing'))
        for prefix in ('sk_test_', 'rk_test_'):
            self.assertTrue(portal.stripe_api_key_is_valid(prefix + 'fixture', 'testing'))
            self.assertFalse(portal.stripe_api_key_is_valid(prefix + 'fixture', 'production'))

    def test_billing_bootstrap_validates_prices_before_saving_and_is_idempotent(self):
        plan = portal.SubscriptionPlan(name='Basic', custom_pricing=False)
        portal.db.session.add(plan)
        portal.db.session.commit()
        def approved_price(method, path):
            monthly = path == 'prices/price_monthly'
            return {'id': path.split('/')[-1], 'active': True, 'livemode': False,
                    'currency': 'usd', 'unit_amount': 39900 if monthly else 454860,
                    'recurring': {'interval': 'month' if monthly else 'year', 'interval_count': 1}}
        runner = portal.app.test_cli_runner()
        args = ['configure-basic-billing', '--monthly-price-id', 'price_monthly',
                '--annual-price-id', 'price_annual']
        with patch.object(portal, 'stripe_request', side_effect=approved_price):
            self.assertEqual(runner.invoke(args=args).exit_code, 0)
            self.assertEqual(runner.invoke(args=args).exit_code, 0)
        portal.db.session.refresh(plan)
        self.assertEqual(plan.annual_amount_cents, 454860)
        self.assertEqual(plan.stripe_annual_price_id, 'price_annual')

    def test_billing_bootstrap_rejects_a_price_mismatch_without_partial_changes(self):
        plan = portal.SubscriptionPlan(name='Basic', custom_pricing=False)
        portal.db.session.add(plan)
        portal.db.session.commit()
        def wrong_annual_price(method, path):
            monthly = path == 'prices/price_monthly'
            return {'id': path.split('/')[-1], 'active': True, 'livemode': False,
                    'currency': 'usd', 'unit_amount': 39900 if monthly else 1,
                    'recurring': {'interval': 'month' if monthly else 'year', 'interval_count': 1}}
        with patch.object(portal, 'stripe_request', side_effect=wrong_annual_price):
            result = portal.app.test_cli_runner().invoke(args=['configure-basic-billing',
                '--monthly-price-id', 'price_monthly', '--annual-price-id', 'price_annual'])
        self.assertNotEqual(result.exit_code, 0)
        portal.db.session.refresh(plan)
        self.assertFalse(plan.stripe_monthly_price_id)
        self.assertFalse(plan.stripe_annual_price_id)

    def test_current_item_periods_and_legacy_periods(self):
        self.assertEqual(portal.stripe_period_timestamp({'items': {'data': [
            {'current_period_end': 1800000000}]}}), 1800000000)
        self.assertEqual(portal.stripe_period_timestamp({'current_period_end': 1800000001}), 1800000001)
        self.assertIsNone(portal.stripe_period_timestamp({'items': {'data': [
            {'current_period_end': 1800000000}, {'current_period_end': 1800000010}]}}))
        self.assertIsNone(portal.stripe_period_timestamp({'current_period_end': True}))

    def test_signed_modern_cancellation_preserves_retention_anchor(self):
        client, company, user, subscription, flight = self.make_workspace()
        period_end = int((portal.now_utc() - timedelta(days=2)).timestamp())
        event = {'id': 'evt_launch_cancel', 'type': 'customer.subscription.deleted',
                 'created': int(time.time()), 'data': {'object': {'id': 'sub_launch',
                    'status': 'canceled', 'items': {'data': [{'current_period_end': period_end}]}}}}
        body = json.dumps(event).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(b'whsec_fixture', timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        # Stripe calls the endpoint without the customer's browser session.
        webhook_client = portal.app.test_client()
        with patch.dict(os.environ, STRIPE_SECRET_KEY='rk_test_fixture', STRIPE_WEBHOOK_SECRET='whsec_fixture'):
            result = webhook_client.post('/api/billing/stripe-webhook', data=body,
                headers={'Stripe-Signature': f't={timestamp},v1={signature}'})
        self.assertEqual(result.status_code, 200)
        portal.db.session.refresh(subscription)
        self.assertEqual(subscription.current_period_end, portal.datetime.utcfromtimestamp(period_end))

    def test_retrieval_allows_own_download_and_blocks_other_tenant_and_edits(self):
        client, company, user, subscription, flight = self.make_workspace()
        other = portal.Company(name='Unrelated company', status='active')
        portal.db.session.add(other)
        portal.db.session.flush()
        portal.db.session.add(portal.Flight(company_id=other.id, flight_id='OTHER',
            date=portal.now_utc().date(), direction='W-E', title='Other footage', project='Other site',
            filename='r2__companies/2/private.mp4', original_filename='private.mp4', uploaded_by=user.id))
        portal.db.session.commit()
        page = client.get('/account/retrieve')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'own.mp4', page.data)
        self.assertNotIn(b'private.mp4', page.data)
        storage = MagicMock()
        storage.signed_download.return_value = 'https://example.test/short-lived-signed-file'
        with patch.object(portal, 'R2Storage', return_value=storage):
            self.assertEqual(client.get('/files/r2__companies/1/own.mp4?download=1').status_code, 302)
            self.assertEqual(client.get('/files/r2__companies/2/private.mp4?download=1').status_code, 404)
        self.assertEqual(client.post('/admin/upload', data={'_csrf': 'retrieval-test-csrf'}).status_code, 403)
        self.assertEqual(client.get('/admin').location, '/account/retrieve')
        self.assertEqual(client.get('/static/uploads/secret.mp4').status_code, 404)

    def test_retrieval_expires_at_90_days_and_never_overrides_owner_suspension(self):
        client, company, user, subscription, flight = self.make_workspace()
        subscription.current_period_end = portal.now_utc() - timedelta(days=91)
        portal.db.session.commit()
        self.assertNotEqual(client.get('/account/retrieve').status_code, 200)
        self.assertEqual(portal.Flight.query.count(), 1)
        subscription.current_period_end = portal.now_utc() - timedelta(days=1)
        company.status = 'suspended'
        portal.db.session.commit()
        with client.session_transaction() as state:
            state['_user_id'] = str(user.id)
        self.assertNotEqual(client.get('/account/retrieve').status_code, 200)
        self.assertEqual(portal.Flight.query.count(), 1)

    def test_delayed_checkout_activates_only_after_signed_payment_success(self):
        client, company, user, subscription, flight = self.make_workspace()
        company.status = 'pending'
        subscription.status = 'pending'
        user.active = False
        member = portal.CompanyMembership.query.filter_by(user_id=user.id).one()
        member.status = 'verified_pending_billing'
        portal.db.session.commit()
        event_time = int(time.time())

        def deliver(kind, payment_status, offset):
            obj = {'id': 'cs_launch', 'status': 'complete', 'payment_status': payment_status,
                   'subscription': 'sub_launch', 'customer': 'cus_launch',
                   'metadata': {'subscription_id': str(subscription.id)}}
            body = json.dumps({'id': 'evt_' + kind, 'type': kind,
                'created': event_time + offset, 'data': {'object': obj}}).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(b'whsec_fixture', timestamp.encode() + b'.' + body,
                                 hashlib.sha256).hexdigest()
            with patch.dict(os.environ, STRIPE_SECRET_KEY='rk_test_fixture',
                            STRIPE_WEBHOOK_SECRET='whsec_fixture'):
                return portal.app.test_client().post('/api/billing/stripe-webhook', data=body,
                    headers={'Stripe-Signature': f't={timestamp},v1={signature}'})

        self.assertEqual(deliver('checkout.session.completed', 'unpaid', 0).status_code, 200)
        portal.db.session.refresh(company)
        portal.db.session.refresh(user)
        self.assertNotEqual(company.status, 'active')
        self.assertFalse(user.active)
        self.assertEqual(deliver('checkout.session.async_payment_succeeded', 'paid', 1).status_code, 200)
        portal.db.session.refresh(company)
        portal.db.session.refresh(user)
        self.assertEqual(company.status, 'active')
        self.assertTrue(user.active)
        self.assertEqual(portal.StripeEvent.query.filter_by(
            event_type='checkout.session.async_payment_succeeded').count(), 1)


if __name__ == '__main__':
    unittest.main()
