import unittest

from app import canonical_endpoint

class EndpointTests(unittest.TestCase):
    def test_signature_form_has_no_trailing_slash(self):
        self.assertEqual(canonical_endpoint('https://service.invalid/'), 'https://service.invalid')

if __name__ == '__main__':
    unittest.main()
