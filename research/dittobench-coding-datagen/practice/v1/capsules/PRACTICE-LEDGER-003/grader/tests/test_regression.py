import unittest

from app import is_balanced

class BalanceRegressionTests(unittest.TestCase):
    def test_equal_lengths_are_not_enough(self):
        self.assertFalse(is_balanced([1, 2], [1, 3]))
    def test_empty_ledger_is_balanced(self):
        self.assertTrue(is_balanced([], []))

if __name__ == '__main__':
    unittest.main()
