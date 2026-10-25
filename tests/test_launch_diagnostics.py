import io
import unittest
from urllib.error import HTTPError
from scripts.check_launch_services import is_authorization_rejection


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
