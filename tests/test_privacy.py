# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from vg import cache
from vg import privacy
from vg.drives import load_prefs, save_prefs
from vg.privacy import pack_thumb_bytes, unpack_thumb_bytes, set_privacy, privacy_snapshot


class PrivacyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.old_vg = cache.VGDATA_DIR
        self.old_key = cache.KEY_FILE
        self.old_prefs = None
        try:
            from vg import config as cfg
            from vg import drives

            self.old_prefs_file = drives.PREFS_FILE if hasattr(drives, "PREFS_FILE") else None
            cache.VGDATA_DIR = self.base / "preview_cache"
            cache.KEY_FILE = cache.VGDATA_DIR / "vault.key"
            import vg.config as c

            self._c_vg = c.VGDATA_DIR
            self._c_key = c.KEY_FILE
            self._c_prefs = c.PREFS_FILE
            c.VGDATA_DIR = cache.VGDATA_DIR
            c.KEY_FILE = cache.KEY_FILE
            c.PREFS_FILE = cache.VGDATA_DIR / "prefs.json"
            drives.PREFS_FILE = c.PREFS_FILE
            drives.VGDATA_DIR = cache.VGDATA_DIR
        except Exception:
            pass
        cache.VGDATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import vg.config as c
        from vg import drives

        cache.VGDATA_DIR = self.old_vg
        cache.KEY_FILE = self.old_key
        c.VGDATA_DIR = self._c_vg
        c.KEY_FILE = self._c_key
        c.PREFS_FILE = self._c_prefs
        drives.PREFS_FILE = self._c_prefs
        drives.VGDATA_DIR = self._c_vg
        self.tmp.cleanup()

    def test_plain_and_encrypted_roundtrip(self):
        jpeg = b"\xff\xd8" + b"x" * 120 + b"\xff\xd9"
        set_privacy(encrypt_thumbs=True)
        enc = pack_thumb_bytes(jpeg)
        self.assertTrue(enc.startswith(b"VG1\0"))
        self.assertEqual(unpack_thumb_bytes(enc), jpeg)

        set_privacy(encrypt_thumbs=False)
        plain = pack_thumb_bytes(jpeg)
        self.assertEqual(plain[:2], b"\xff\xd8")
        self.assertEqual(unpack_thumb_bytes(plain), jpeg)
        # 仍可读旧加密文件
        self.assertEqual(unpack_thumb_bytes(enc), jpeg)

    def test_disk_cache_dir(self):
        root = self.base / "videos"
        root.mkdir()
        set_privacy(cache_location_value="disk")
        cache_dir = privacy.resolve_cache_dir_for_root(root)
        self.assertEqual(cache_dir, root / ".video_gallery_cache")
        self.assertTrue(cache_dir.is_dir())
        set_privacy(cache_location_value="program")
        prog = privacy.resolve_cache_dir_for_root(root)
        self.assertTrue(str(prog).startswith(str(cache.VGDATA_DIR)))


if __name__ == "__main__":
    unittest.main()
