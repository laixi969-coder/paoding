import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from paoding.domain.schemas import ANALYSIS_TYPES, Modality, NormalizedContent
from paoding.media.processing import MediaProcessor
from paoding.models.demo import demo_analysis
from paoding.models.gateway import ModelGateway, ProviderConfig, VideoOverview


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_real_video_normalization_audio_and_segment_timestamps(rt, tmp_path):
    video = tmp_path / "input.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    rt.settings.video_segment_seconds = 10
    processor = MediaProcessor(rt.settings)
    record = processor.prepare_video(video)
    assert record["has_audio"] and Path(record["audio"]).exists()
    assert [s["start"] for s in record["segments"]] == [0, 10]
    assert record["segments"][1]["duration"] == pytest.approx(2, abs=0.1)
    assert processor.prepare_video(video) == record
    content = rt.normalizer.local(video)
    assert content.video_segments and content.duration_seconds == pytest.approx(12, abs=0.1)


def test_native_qwen_doubao_video_transport(rt, tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"mp4-fixture")
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setenv("TEST_KEY", "key")
    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    for protocol in ("qwen", "doubao"):
        provider = ProviderConfig(
            protocol=protocol, base_url="https://provider.example/v1", api_key_env="TEST_KEY"
        )
        gateway.complete(provider, "model", "Analyze", [path], "video")
    for payload in requests:
        block = payload["messages"][1]["content"][1]
        assert block["type"] == "video_url"
        assert block["video_url"]["fps"] == 2
        assert block["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_long_video_merge_preserves_absolute_beats_and_suppresses_unheard_audio(rt, monkeypatch):
    rt.settings.demo_mode = False
    content = NormalizedContent(
        source_url="https://example.com/video",
        platform="web",
        title="Video",
        modality="video",
        duration_seconds=12,
        video_segments=[
            {"path": "a.mp4", "start": 0, "duration": 10},
            {"path": "b.mp4", "start": 10, "duration": 2},
        ],
    )

    def generate(schema, prompt, modality="text", media=None, validate_output=None):
        if schema == VideoOverview:
            example = ANALYSIS_TYPES[Modality.VIDEO].model_validate(demo_analysis(content))
            return VideoOverview(
                title="全片",
                summary="综合分析",
                cross_domain_dna=example.cross_domain_dna,
                reusable_script_formula="全片公式",
            ), {"model": "summary"}
        result = ANALYSIS_TYPES[Modality.VIDEO].model_validate(demo_analysis(content))
        result.golden_hook_0_to_3s.spoken_line = "invented audio"
        result.pacing_metrics.bgm_turns = ["invented BGM"]
        if validate_output:
            validate_output(result)
        return result, {"supports_audio": False, "provider": "qwen"}

    monkeypatch.setattr(rt.gateway, "generate", generate)
    result, trace = rt.gateway.analyze(content)
    assert [beat.start_seconds for beat in result.beat_sheet] == [0, 10]
    assert result.golden_hook_0_to_3s.spoken_line is None
    assert result.pacing_metrics.bgm_turns == []
    assert trace["segment_count"] == 2 and result.title == "全片"


def test_gemini_large_upload_poll_cleanup(rt, tmp_path, monkeypatch):
    path = tmp_path / "large.mp4"
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    monkeypatch.setenv("TEST_KEY", "key")
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(
                200, headers={"x-goog-upload-url": "https://gemini.example/upload-session"}
            )
        if request.url.path == "/upload-session":
            return httpx.Response(
                200,
                json={
                    "file": {
                        "name": "files/abc",
                        "state": "ACTIVE",
                        "uri": "https://gemini.example/files/abc",
                    }
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][1]["file_data"]["file_uri"].endswith("files/abc")
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}]}
        )

    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    provider = ProviderConfig(
        protocol="gemini", base_url="https://gemini.example/v1beta", api_key_env="TEST_KEY"
    )
    assert json.loads(gateway.complete(provider, "model", "analyze", [path], "video"))["ok"]
    assert calls[-1] == ("DELETE", "/v1beta/files/abc")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_audio_transcription_and_cache(rt, tmp_path, monkeypatch):
    audio = tmp_path / "voice.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "1",
            str(audio),
        ],
        check=True,
        timeout=20,
    )
    rt.settings.transcription_provider, rt.settings.transcription_model = "openai", "whisper-1"
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path.endswith("/audio/transcriptions")
        assert "multipart/form-data" in request.headers["content-type"]
        return httpx.Response(200, json={"text": "识别出的语音"})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    assert gateway.transcribe([audio]) == "识别出的语音"
    assert gateway.transcribe([audio]) == "识别出的语音" and len(calls) == 1
