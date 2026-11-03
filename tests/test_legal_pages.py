import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")

import json
import re
import unittest
from unittest.mock import patch

import app as portal


class LegalPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)
        with portal.app.app_context():
            portal.db.create_all()

    def test_every_policy_page_is_served_by_the_app(self):
        client = portal.app.test_client()
        self.assertEqual(client.get("/legal").status_code, 200)
        for slug, title in portal.LEGAL_PAGES.items():
            response = client.get(f"/legal/{slug}")
            self.assertEqual(response.status_code, 200, slug)
            self.assertIn(title.replace("&", "&amp;").encode(), response.data)
        self.assertEqual(client.get("/legal/unknown").status_code, 404)

    def test_pages_state_the_approved_billing_and_retention_policies(self):
        client = portal.app.test_client()
        billing = client.get("/legal/billing").data
        for fact in (b"USD 399.00", b"USD 4,548.60", b"90 days", b"renew automatically", b"end of the current paid billing period"):
            self.assertIn(fact, billing)
        refunds = client.get("/legal/refunds").data
        self.assertIn(b"not refunded automatically", refunds)
        self.assertIn(b"No prorated formula or refund is guaranteed", refunds)

    def test_cookie_policy_lists_only_the_session_cookie(self):
        page = portal.app.test_client().get("/legal/cookies").data
        self.assertIn(b"<code>session</code>", page)
        self.assertIn(b"do not use advertising or analytics cookies", page)

    def test_no_private_company_name_and_draft_notice_until_approved(self):
        client = portal.app.test_client()
        for slug in portal.LEGAL_PAGES:
            page = client.get(f"/legal/{slug}").data
            self.assertNotIn(b"HERSHEY", page.upper())
            self.assertIn(b"Draft for owner and legal review", page)
        with patch.dict(os.environ, {"LEGAL_TERMS_APPROVED": "1", "LEGAL_ENTITY_NAME": "Example Operator LLC",
                                     "LEGAL_GOVERNING_LAW": "the State of Example"}):
            page = client.get("/legal/terms").data
            self.assertNotIn(b"Draft for owner and legal review", page)
            self.assertIn(b"Example Operator LLC", page)
            self.assertIn(b"the State of Example", page)

    def test_terms_show_the_arbitration_section_clearly_and_signup_names_it(self):
        client = portal.app.test_client()
        terms = client.get("/legal/terms").get_data(as_text=True)
        self.assertIn('id="dispute-resolution"', terms)
        self.assertIn("binding arbitration and class-action waiver", terms)
        self.assertIn("30-day opt-out", terms)
        self.assertLess(terms.index("Please read Section 9"), terms.index("1. Accounts and roles"))
        signup = client.get("/register").get_data(as_text=True)
        self.assertIn("binding arbitration and a class-action waiver", signup)
        self.assertIn("/legal/terms#dispute-resolution", signup)

    def test_signup_checkout_and_viewer_links_point_to_app_legal_pages(self):
        client = portal.app.test_client()
        for path in ("/register", "/"):
            html = client.get(path).get_data(as_text=True)
            self.assertIn("/legal/terms", html, path)
            self.assertIn("/legal/privacy", html, path)
            self.assertNotIn("captureiv.com/terms", html, path)

    def test_invited_accounts_must_accept_the_terms(self):
        with portal.app.app_context():
            company = portal.Company(name="Legal Consent Co", status="active", storage_limit_bytes=1000)
            user = portal.User(username="legal-invitee", name="Invitee", email="legal-invitee@example.test",
                               is_admin=False, active=False, email_verified=False)
            user.set_password("placeholder-password-123")
            portal.db.session.add_all([company, user]); portal.db.session.flush()
            portal.db.session.add(portal.CompanyMembership(company_id=company.id, user_id=user.id,
                                                           role="employee", status="pending"))
            portal.db.session.commit()
            token = "legal-consent-invite-token"
            portal.db.session.add(portal.EmailActionToken(
                user_id=user.id, purpose="employee_invite",
                token_hash=portal.hashlib.sha256(token.encode()).hexdigest(),
                expires_at=portal.now_utc() + portal.timedelta(days=1)))
            portal.db.session.commit()
            user_id = user.id
        client = portal.app.test_client()
        page = client.get(f"/employee/setup/{token}")
        self.assertIn(b'name="accept_policies"', page.data)
        csrf = re.search(rb'name="_csrf" value="([^"]+)"', page.data).group(1).decode()
        refused = client.post(f"/employee/setup/{token}", data={"_csrf": csrf, "password": "a-long-password-1",
                                                                "confirm_password": "a-long-password-1"})
        self.assertIn(b"Please agree to the Terms", refused.data)
        accepted = client.post(f"/employee/setup/{token}", data={"_csrf": csrf, "password": "a-long-password-1",
                                                                 "confirm_password": "a-long-password-1",
                                                                 "accept_policies": "yes"})
        self.assertEqual(accepted.status_code, 302)
        with portal.app.app_context():
            record = portal.AuditLog.query.filter_by(user_id=user_id, action="account_terms_accepted").one()
            detail = json.loads(record.detail)
            self.assertEqual(detail["terms_version"], portal.ACCOUNT_TERMS_VERSION)
            self.assertEqual(detail["privacy_notice_version"], portal.PRIVACY_NOTICE_VERSION)


if __name__ == "__main__":
    unittest.main()
