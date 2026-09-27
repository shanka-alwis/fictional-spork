"""Builds ffmpeg command lines for recording, live view and snapshots."""
import subprocess

SEGMENT_PATTERN = "%Y%m%d_%H%M%S.mp4"


def input_args(url: str) -> list[str]:
    args = ["-hide_banner", "-nostdin", "-loglevel", "error"]
    if url.startswith("rtsp://") or url.startswith("rtsps://"):
        args += ["-rtsp_transport", "tcp"]
    elif "://" not in url:
        # A local video file: play it in real time on a loop (handy for demos/tests).
        args += ["-re", "-stream_loop", "-1"]
    return args + ["-i", url]


def record_cmd(ffmpeg: str, url: str, out_dir: str, segment_seconds: int, audio: bool) -> list[str]:
    cmd = [ffmpeg, *input_args(url), "-map", "0:v:0", "-c:v", "copy"]
    if audio:
        # Camera audio is often G.711, which MP4 can't hold; AAC is safe everywhere.
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    return cmd + [
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_atclocktime", "1",
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-segment_format", "mp4",
        # Fragmented MP4 stays playable even if ffmpeg is killed mid-segment.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        f"{out_dir}/{SEGMENT_PATTERN}",
    ]


def mjpeg_cmd(ffmpeg: str, url: str, fps: int, width: int) -> list[str]:
    return [ffmpeg, *input_args(url), "-an", "-vf", f"fps={fps},scale={width}:-2",
            "-q:v", "7", "-f", "mjpeg", "pipe:1"]


def snapshot(ffmpeg: str, url: str, width: int = 960, timeout: float = 15) -> bytes:
    """Grab a single JPEG frame. Raises RuntimeError with ffmpeg's message on failure."""
    args = input_args(url)
    if "-re" in args:  # no need to pace a one-frame grab
        args = [a for a in args if a != "-re"]
    cmd = [ffmpeg, *args, "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timed out connecting to camera")
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found ({ffmpeg})")
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip()[-500:] or "no frame received")
    return proc.stdout
