import sys
import os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
sys.path.append(PROJECT_ROOT)

from app import auto_fetch_data

if __name__ == "__main__":
    auto_fetch_data()
