import unittest

from app import is_balanced

class BalanceTests(unittest.TestCase):
    def test_equal_totals(self):
        self.assertTrue(is_balanced([3, 7], [10]))

if __name__ == '__main__':
    unittest.main()
