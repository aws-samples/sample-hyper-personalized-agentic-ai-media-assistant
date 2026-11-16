"""load_media.py: zip members map to the documented S3 keys; re-runs skip unchanged objects."""
import zipfile

import pytest
from botocore.exceptions import ClientError

import load_media


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_keys_follow_the_bucket_layout(tmp_path):
    (tmp_path / "data/catalog").mkdir(parents=True)
    (tmp_path / "data/media").mkdir(parents=True)
    make_zip(tmp_path / "data/catalog/posters.zip", {"posters/tt1001001.jpg": b"a", "__MACOSX/posters/._x.jpg": b"",
                                                     "posters/": b"", "posters/.DS_Store": b""})
    make_zip(tmp_path / "data/media/microdramas-1.zip", {"microdramas/one-bar.mp4": b"b"})
    make_zip(tmp_path / "data/media/microdramas-2.zip", {"microdramas/neon-runner.mp4": b"c"})
    make_zip(tmp_path / "data/media/football.zip", {"football/horizontal/football.mp4": b"d",
                                                    "football/vertical/football.mp4": b"e", "README.txt": b""})
    keys = {part: [key for _, _, key in load_media.plan(*rules)] for part, rules in load_media.sources(tmp_path).items()}
    assert keys == {"catalog": ["posters/tt1001001.jpg"],
                    "clips": ["clips/microdramas/neon-runner.mp4", "clips/microdramas/one-bar.mp4"],
                    "sports": ["media/soccer/horizontal/football.mp4", "media/soccer/vertical/football.mp4"]}
    assert load_media.content_type("media/soccer/vertical/football.mp4") == "video/mp4"
    assert load_media.content_type("posters/a.jpg") == "image/jpeg"


def test_duplicate_keys_across_zips_are_refused(tmp_path):
    a = make_zip(tmp_path / "a.zip", {"microdramas/x.mp4": b"1"})
    b = make_zip(tmp_path / "b.zip", {"microdramas/x.mp4": b"2"})
    with pytest.raises(ValueError, match="in both"):
        load_media.plan([a, b], load_media.sources(tmp_path)["clips"][1])


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        data, extra = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": extra["Metadata"]}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        with open(filename, "rb") as handle:
            self.objects[key] = (handle.read(), ExtraArgs)


def test_upload_skips_unchanged_objects(tmp_path):
    zip_path = make_zip(tmp_path / "football.zip", {"football/horizontal/football.mp4": b"video"})
    items = load_media.plan([zip_path], load_media.sources(tmp_path)["sports"][1])
    s3 = FakeS3()
    assert load_media.upload(s3, "bucket", items, tmp_path) == {"uploaded": 1, "unchanged": 0}
    data, extra = s3.objects["media/soccer/horizontal/football.mp4"]
    assert data == b"video" and extra["ContentType"] == "video/mp4"
    assert load_media.upload(s3, "bucket", items, tmp_path) == {"uploaded": 0, "unchanged": 1}
    make_zip(zip_path, {"football/horizontal/football.mp4": b"other"})
    assert load_media.upload(s3, "bucket", items, tmp_path) == {"uploaded": 1, "unchanged": 0}
    assert not [p for p in tmp_path.iterdir() if p.suffix != ".zip"]
