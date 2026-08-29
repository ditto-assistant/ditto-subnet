import unittest

from app import allocate_cents

class AllocationTests(unittest.TestCase):
    def test_no_money_is_lost(self):
        self.assertEqual(sum(allocate_cents(10, 3)), 10)

if __name__ == '__main__':
    unittest.main()
