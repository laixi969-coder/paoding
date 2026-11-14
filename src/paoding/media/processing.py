import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from paoding.core.errors import ConfigurationError, SourceError


def run_media(command, timeout):
    if not shutil.which(command[0]):
        raise ConfigurationError(f"Required executable is not installed: {command[0]}")
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SourceError("Media processing exceeded its time limit") from exc
    if result.returncode:
        raise SourceError("Media tool could not decode or process this file")
    return result.stdout


class MediaProcessor:
    def __init__(self, settings):
        self.settings = settings

    def probe(self, path):
        data = run_media(
            [
                self.settings.ffprobe_command,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            30,
        )
        return json.loads(data)

    def audio(self, path, output):
        run_media(
            [
                self.settings.ffmpeg_command,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(output),
            ],
            self.settings.media_timeout,
        )
        return output

    def prepare_video(self, path):
        """A cached 720p archive, audio track and bounded, timestamped segments."""
        path = Path(path).resolve()
        metadata = self.probe(path)
        duration = float(metadata.get("format", {}).get("duration", 0))
        if duration <= 0 or not any(s.get("codec_type") == "video" for s in metadata["streams"]):
            raise SourceError("Video has no valid duration or video stream")
        checksum = hashlib.sha256(f":720:v2:{self.settings.video_segment_seconds}:".encode())
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                checksum.update(chunk)
        digest = checksum.hexdigest()
        root = self.settings.data_dir.resolve() / "media" / digest
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / "manifest.json"
        if manifest.exists():
            record = json.loads(manifest.read_text())
            if all(Path(p).is_file() for p in record["files"]):
                return record
        # Work in a per-attempt directory; do not expose half-written outputs to another worker.
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            temp = Path(tmp)
            video = temp / "video.mp4"
            run_media(
                [
                    self.settings.ffmpeg_command,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-vf",
                    "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "24",
                    "-maxrate",
                    "850k",
                    "-bufsize",
                    "1700k",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-movflags",
                    "+faststart",
                    str(video),
                ],
                self.settings.media_timeout,
            )
            has_audio = any(s.get("codec_type") == "audio" for s in metadata["streams"])
            audio = self.audio(path, temp / "audio.mp3") if has_audio else None
            segments = []
            step = self.settings.video_segment_seconds
            for index, start in enumerate(range(0, max(1, int(duration + 0.999)), step)):
                length = min(step, duration - start)
                segment = temp / f"segment-{index:05d}.mp4"
                # Re-encode boundary cuts to keep exact time offsets; copy mode seeks to keyframes.
                run_media(
                    [
                        self.settings.ffmpeg_command,
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        str(start),
                        "-i",
                        str(video),
                        "-t",
                        str(length),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "24",
                        "-maxrate",
                        "850k",
                        "-bufsize",
                        "1700k",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "64k",
                        "-movflags",
                        "+faststart",
                        str(segment),
                    ],
                    self.settings.media_timeout,
                )
                segments.append(
                    {"path": str(root / segment.name), "start": start, "duration": length}
                )
            files = []
            for file in temp.iterdir():
                from paoding.storage.vault import atomic_write

                target = root / file.name
                atomic_write(target, file.read_bytes())
                files.append(str(target))
            record = {
                "video": str(root / "video.mp4"),
                "audio": str(root / "audio.mp3") if audio else None,
                "duration": duration,
                "segments": segments,
                "files": files,
                "has_audio": has_audio,
            }
            from paoding.storage.vault import atomic_write

            atomic_write(manifest, json.dumps(record).encode())
            return record
