import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402


def jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (1280, 720), "grey").save(buf, "JPEG")
    return buf.getvalue()


class SaveCameraImages(unittest.TestCase):
    def test_skips_a_camera_whose_photo_is_truncated(self):
        good = jpeg_bytes()
        truncated = good[: len(good) // 2]
        photos = {"http://cam/good": good, "http://cam/bad": truncated}
        cams = [
            {"id": "2701", "name": "Woodlands", "image": "http://cam/good"},
            {"id": "4703", "name": "Tuas", "image": "http://cam/bad"},
        ]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(build, "get", side_effect=lambda url: mock.Mock(content=photos[url])):
            kept = build.save_camera_images(cams, Path(tmp))
            files = sorted(p.name for p in Path(tmp).iterdir())

        self.assertEqual([c["id"] for c in kept], ["2701"])
        self.assertEqual(files, ["cam-2701.jpg"])

    def test_retries_a_camera_once_before_giving_up(self):
        good = jpeg_bytes()
        answers = iter([good[:100], good])
        cams = [{"id": "2701", "name": "Woodlands", "image": "http://cam/good"}]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(build, "get", side_effect=lambda url: mock.Mock(content=next(answers))):
            kept = build.save_camera_images(cams, Path(tmp))

        self.assertEqual([c["id"] for c in kept], ["2701"])


if __name__ == "__main__":
    unittest.main()
