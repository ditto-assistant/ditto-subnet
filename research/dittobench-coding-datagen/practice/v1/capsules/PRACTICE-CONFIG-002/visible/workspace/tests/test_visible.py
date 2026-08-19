import unittest

from app import parse_bool

class BooleanTests(unittest.TestCase):
    def test_true_and_false(self):
        self.assertTrue(parse_bool('true'))
        self.assertFalse(parse_bool('false'))

if __name__ == '__main__':
    unittest.main()
