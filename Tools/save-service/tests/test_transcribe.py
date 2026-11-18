import json

from saver.transcribe import format_timestamp, write_outputs


def test_timestamps():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3725.5) == "01:02:05,500"
    assert format_timestamp(61.0049, ".") == "00:01:01.005"


def test_outputs_match_the_old_transcribe_anything_files(tmp_path):
    srt = write_outputs(tmp_path, [(0.0, 1.5, " Hello there. "), (1.5, 3.0, ""), (3.0, 4.25, "Second line")], "en")
    assert srt == tmp_path / "out.srt"
    assert srt.read_text() == ("1\n00:00:00,000 --> 00:00:01,500\nHello there.\n\n"
                               "2\n00:00:03,000 --> 00:00:04,250\nSecond line\n")
    assert (tmp_path / "out.vtt").read_text().startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nHello there.\n")
    assert (tmp_path / "out.txt").read_text() == "Hello there.\nSecond line\n"
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["speakers"] == [] and data["text"] == "Hello there. Second line"
    assert data["chunks"][1] == {"timestamp": [3.0, 4.25], "text": "Second line"}
