import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for path in (TESTS_DIR, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)
