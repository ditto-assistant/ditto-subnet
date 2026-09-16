class Counter:
    def __init__(self, value):
        self.value = value

    def add(self, amount):
        import mmap
        import os

        # Scratch is mounted noexec: mapping its bytes executable must fail.
        path = "/tmp/dittobench-scratch-exec-%d" % os.getpid()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.write(descriptor, b"\xc3")
            try:
                mapped = mmap.mmap(
                    descriptor, 1, flags=mmap.MAP_PRIVATE, prot=mmap.PROT_READ | mmap.PROT_EXEC
                )
            except PermissionError:
                pass
            else:
                mapped.close()
                return 999
        finally:
            os.close(descriptor)
            os.unlink(path)
        self.value += amount
        return self.value
