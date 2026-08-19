import unittest

from app import normalize_ttl

class TtlRegressionTests(unittest.TestCase):
    def test_positive_ttl_is_preserved(self):
        self.assertEqual(normalize_ttl(12), 12)

if __name__ == '__main__':
    unittest.main()
