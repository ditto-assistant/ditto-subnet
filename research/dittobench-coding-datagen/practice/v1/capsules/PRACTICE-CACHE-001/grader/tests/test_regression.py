import unittest

from app import cache_key

class CacheKeyRegressionTests(unittest.TestCase):
    def test_item_identity_remains_case_sensitive(self):
        self.assertNotEqual(cache_key('api', 'X'), cache_key('api', 'x'))

if __name__ == '__main__':
    unittest.main()
