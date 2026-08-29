import unittest

from app import canonical_endpoint

class EndpointRegressionTests(unittest.TestCase):
    def test_already_canonical(self):
        self.assertEqual(canonical_endpoint('https://service.invalid'), 'https://service.invalid')

if __name__ == '__main__':
    unittest.main()
