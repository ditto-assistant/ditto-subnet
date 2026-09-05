import unittest

from app import normalize_reference

class ReferenceRegressionTests(unittest.TestCase):
    def test_reference_is_not_numeric(self):
        self.assertEqual(normalize_reference(' AB-07 '), 'AB-07')
    def test_leading_zeroes_are_identity(self):
        self.assertEqual(normalize_reference('0000'), '0000')

if __name__ == '__main__':
    unittest.main()
