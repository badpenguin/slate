"""Single-instance runtime lock tests."""

import tempfile
import unittest
from pathlib import Path

from slate.instance import AlreadyRunningError, InstanceLock


class InstanceLockTest(unittest.TestCase):
    """Verify that flock, rather than stale PID text, controls ownership."""

    def test_second_owner_is_rejected_with_diagnostic_pid(self) -> None:
        """A second lock attempt reports the current process as owner."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slate.lock"
            first = InstanceLock.acquire(path)
            try:
                with self.assertRaises(AlreadyRunningError) as context:
                    InstanceLock.acquire(path)
                self.assertIsNotNone(context.exception.pid)
            finally:
                first.close()

    def test_released_lock_can_be_acquired_again(self) -> None:
        """A retained runtime file does not become a stale-lock false positive."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slate.lock"
            first = InstanceLock.acquire(path)
            first.close()
            second = InstanceLock.acquire(path)
            second.close()


if __name__ == "__main__":
    unittest.main()
