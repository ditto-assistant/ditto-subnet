import unittest

from app import eviction_candidate

class EvictionRegressionTests(unittest.TestCase):
    def test_order_does_not_change_result(self):
        self.assertEqual(eviction_candidate([('new', 9), ('old', 1)]), 'old')

if __name__ == '__main__':
    unittest.main()
