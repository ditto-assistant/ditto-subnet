import unittest

from app import cache_key

class CacheKeyTests(unittest.TestCase):
    def test_namespace_is_case_insensitive(self):
        self.assertEqual(cache_key('API', 'x'), cache_key('api', 'x'))

if __name__ == '__main__':
    unittest.main()
