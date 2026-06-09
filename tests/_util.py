"""Test helpers. Not collected (filename != test_*)."""
import os
import tempfile


def temporary_directory():
    """TemporaryDirectory under $PARLEY_TEST_TMP if set, else system default."""
    return tempfile.TemporaryDirectory(dir=os.getenv("PARLEY_TEST_TMP") or None)
