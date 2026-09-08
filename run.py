"""Launch the recorder:  python run.py

  python run.py --selftest    check this machine and write a report, no UI
"""

import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        from screen_recorder.selftest import REPORT_PATH, main as selftest

        code = selftest()
        print("report written to", REPORT_PATH)
        sys.exit(code)

    from screen_recorder.ui import main

    main()
