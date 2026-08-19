import unittest

from app import eviction_candidate

class EvictionTests(unittest.TestCase):
    def test_oldest_entry_is_evicted(self):
        self.assertEqual(eviction_candidate([('old', 1), ('new', 9)]), 'old')

if __name__ == '__main__':
    unittest.main()
