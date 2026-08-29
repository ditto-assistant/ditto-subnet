import unittest

from app import merge_config

class MergeTests(unittest.TestCase):
    def test_environment_wins(self):
        self.assertEqual(merge_config({'port': 80}, {'port': 443})['port'], 443)

if __name__ == '__main__':
    unittest.main()
