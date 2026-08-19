import unittest

from app import allocate_cents

class AllocationRegressionTests(unittest.TestCase):
    def test_remainder_is_stable(self):
        self.assertEqual(allocate_cents(10, 3), [4, 3, 3])
    def test_even_split_is_unchanged(self):
        self.assertEqual(allocate_cents(8, 2), [4, 4])

if __name__ == '__main__':
    unittest.main()
