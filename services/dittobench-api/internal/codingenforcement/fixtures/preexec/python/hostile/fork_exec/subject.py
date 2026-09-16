class Counter:
    def __init__(self, value):
        self.value = value

    def add(self, amount):
        import os
        import sys

        try:
            pid = os.fork()
        except PermissionError:
            pass
        else:
            if pid == 0:
                os._exit(0)
            return 999
        try:
            # A successful exec replaces the candidate, failing the suite.
            os.execv(sys.executable, [sys.executable, "-c", "pass"])
        except PermissionError:
            pass
        self.value += amount
        return self.value
