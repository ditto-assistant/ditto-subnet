import unittest

from app import normalize_reference

class ReferenceTests(unittest.TestCase):
    def test_surrounding_space_is_removed(self):
        self.assertEqual(normalize_reference(' 00042 '), '00042')

if __name__ == '__main__':
    unittest.main()
