import unittest

from app import merge_config

class MergeRegressionTests(unittest.TestCase):
    def test_defaults_remain(self):
        self.assertEqual(merge_config({'host': 'local'}, {'port': 443}), {'host': 'local', 'port': 443})

if __name__ == '__main__':
    unittest.main()
