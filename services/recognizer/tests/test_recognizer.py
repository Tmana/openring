"""Recognizer crop / embedding tests.

The full path needs face_recognition + dlib installed — we skip when
the import fails so unit-level CI without the heavy deps still passes.
The geometry helpers (bbox padding) need numpy and the recognizer
module, so they're also skipped when numpy isn't on the test host.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")


class TestCropWithPadding:
    def test_no_padding_returns_bbox_only(self) -> None:
        from recognizer import _crop_with_padding
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        out = _crop_with_padding(img, (20, 30, 60, 80), padding_pct=0.0)
        assert out.shape[:2] == (50, 40)

    def test_padding_widens(self) -> None:
        from recognizer import _crop_with_padding
        img = np.zeros((1000, 1000, 3), dtype=np.uint8)
        out = _crop_with_padding(img, (400, 400, 600, 600), padding_pct=0.1)
        # 200×200 bbox + 10% on each side → 240×240
        assert out.shape[:2] == (240, 240)

    def test_clamps_to_frame_edges(self) -> None:
        from recognizer import _crop_with_padding
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        # bbox already at the corner — padding can't go negative
        out = _crop_with_padding(img, (0, 0, 50, 50), padding_pct=0.5)
        assert out.shape[:2][0] <= 100
        assert out.shape[:2][1] <= 100

    def test_zero_size_bbox_does_not_crash(self) -> None:
        from recognizer import _crop_with_padding
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        # x1==x2 — degenerate bbox; treat width as 1 so padding works.
        out = _crop_with_padding(img, (50, 50, 50, 50), padding_pct=0.1)
        assert out.size >= 0  # no crash


class TestEmbeddingsToArray:
    def test_empty_input(self) -> None:
        from recognizer import _embeddings_to_array
        out = _embeddings_to_array([])
        assert out.shape == (0, 128)

    def test_single_embedding(self) -> None:
        from recognizer import _embeddings_to_array
        v = np.arange(128, dtype=np.float32)
        out = _embeddings_to_array([v.tobytes()])
        assert out.shape == (1, 128)
        np.testing.assert_array_equal(out[0], v)

    def test_multiple_embeddings_stacked(self) -> None:
        from recognizer import _embeddings_to_array
        v1 = np.zeros(128, dtype=np.float32)
        v2 = np.ones(128, dtype=np.float32)
        out = _embeddings_to_array([v1.tobytes(), v2.tobytes()])
        assert out.shape == (2, 128)
        assert out[0].sum() == 0.0
        assert out[1].sum() == 128.0


# face_recognition / dlib path — only run if the wheel is installed.
face_recognition = pytest.importorskip("face_recognition")


class TestRecognizeCropIntegration:
    """Smoke tests with the real lib loaded.

    We don't assert specific identities here — that depends on the
    library's bundled model.  We just assert the function returns a
    well-formed RecognitionResult for the trivial "no faces in a
    blank canvas" case, so CI catches contract regressions without
    needing real face photos in the repo.
    """

    def test_blank_image_returns_no_face(self, tmp_path) -> None:
        from PIL import Image
        from recognizer import recognize_crop

        path = tmp_path / "blank.jpg"
        Image.new("RGB", (640, 480), color=(127, 127, 127)).save(path)

        result = recognize_crop(
            snapshot_path=str(path),
            bbox=(100, 100, 400, 400),
            padding_pct=0.1,
            known_faces=[],
            tolerance=0.6,
        )
        assert result.status == "no_face"
