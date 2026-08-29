import unittest

from app import normalize_ttl

class TtlTests(unittest.TestCase):
    def test_negative_ttl_is_zero(self):
        self.assertEqual(normalize_ttl(-5), 0)

if __name__ == '__main__':
    unittest.main()
