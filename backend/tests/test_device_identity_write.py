"""The identity write must not need write permission on a parent that exists."""
import os, stat, tempfile
from pathlib import Path

import pytest

import app.services.openclaw.device_identity as di


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_write_identity_into_existing_dir_under_readonly_parent(tmp_path):
    grandparent = tmp_path / "home"
    identity_dir = grandparent / "identity"
    identity_dir.mkdir(parents=True)
    # The gateway resets its home to 0755; the identity dir itself stays writable.
    os.chmod(grandparent, stat.S_IRUSR | stat.S_IXUSR)
    try:
        target = identity_dir / "device.json"
        di._write_identity(target, di._generate_identity())
        assert target.exists()
    finally:
        os.chmod(grandparent, stat.S_IRWXU)


def test_write_identity_creates_a_missing_dir(tmp_path):
    target = tmp_path / "fresh" / "device.json"
    di._write_identity(target, di._generate_identity())
    assert target.exists()
