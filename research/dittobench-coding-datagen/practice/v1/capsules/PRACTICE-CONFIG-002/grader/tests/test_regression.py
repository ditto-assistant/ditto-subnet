import unittest

from app import parse_bool

class BooleanRegressionTests(unittest.TestCase):
    def test_legacy_synonym_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bool('yes')
    def test_unknown_value_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bool('maybe')

if __name__ == '__main__':
    unittest.main()
