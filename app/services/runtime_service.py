import shutil
import subprocess

from app.config import Settings


class RuntimeService:
    @staticmethod
    def ffmpeg_status(settings: Settings) -> dict:
        ffmpeg_bin = settings.ffmpeg_bin.strip() or "ffmpeg"
        resolved_path = shutil.which(ffmpeg_bin)
        available = resolved_path is not None
        version: str | None = None
        error: str | None = None

        if available:
            try:
                result = subprocess.run(
                    [resolved_path, "-version"],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    version = (result.stdout.splitlines() or [None])[0]
                else:
                    available = False
                    error = (result.stderr or result.stdout or "ffmpeg returned non-zero status").strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                available = False
                error = str(exc)

        return {
            "media_transcode_enabled": settings.media_transcode_enabled,
            "ffmpeg_bin": ffmpeg_bin,
            "ffmpeg_available": available,
            "ffmpeg_path": resolved_path,
            "ffmpeg_version": version,
            "ffmpeg_error": error,
            "voice_ext": settings.media_transcode_voice_ext,
            "video_ext": settings.media_transcode_video_ext,
        }
