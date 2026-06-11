"""pytest sets sys.path so test modules can `from strategy.X import Y`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
