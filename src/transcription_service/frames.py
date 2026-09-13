from __future__ import annotations

import hashlib
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter


class FrameExtractionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    path: Path
    requested_ms: int
    actual_ms: int
    sha256: str


def prepare_frame_for_vision(path: Path) -> bool:
    """Добавляет к полному кадру увеличенные плитки с зелёным контуром."""
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    candidates = _green_tile_candidates(image)
    if not candidates:
        return False

    full = image.copy()
    if full.width > 2048:
        height = max(1, round(full.height * 2048 / full.width))
        full = full.resize((2048, height), Image.Resampling.LANCZOS)
    strip_height = min(640, max(320, full.height // 3))
    canvas = Image.new("RGB", (full.width, full.height + strip_height), (16, 22, 32))
    canvas.paste(full, (0, 0))

    cell_width = full.width // len(candidates)
    for index, box in enumerate(candidates):
        crop = image.crop(_padded_box(box, image.size))
        available = (max(1, cell_width - 24), max(1, strip_height - 24))
        scale = min(available[0] / crop.width, available[1] / crop.height)
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.Resampling.LANCZOS,
        )
        left = index * cell_width + (cell_width - crop.width) // 2
        top = full.height + (strip_height - crop.height) // 2
        canvas.paste(crop, (left, top))

    canvas.save(path, format="JPEG", quality=95, subsampling=0, optimize=True)
    return True


def _green_tile_candidates(image: Image.Image, limit: int = 2) -> list[tuple[int, int, int, int]]:
    scale = min(1.0, 1024 / image.width)
    scan_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    scan = image.resize(scan_size, Image.Resampling.BILINEAR) if scale < 1 else image
    mask = Image.new("L", scan.size)
    rgb = scan.tobytes()
    mask.putdata([
        255
        if green >= 95
        and green - red >= 35
        and green > blue * 1.08
        and green > red * 1.2
        else 0
        for red, green, blue in zip(rgb[0::3], rgb[1::3], rgb[2::3], strict=True)
    ])
    mask = mask.filter(ImageFilter.MaxFilter(5))
    pixels = mask.load()
    width, height = mask.size
    visited = bytearray(width * height)
    boxes: list[tuple[int, tuple[int, int, int, int]]] = []
    minimum_width = max(36, width // 45)
    minimum_height = max(22, height // 45)

    for y in range(height):
        for x in range(width):
            offset = y * width + x
            if visited[offset] or not pixels[x, y]:
                continue
            queue = deque([(x, y)])
            visited[offset] = 1
            left = right = x
            top = bottom = y
            count = 0
            while queue:
                current_x, current_y = queue.popleft()
                count += 1
                left = min(left, current_x)
                right = max(right, current_x)
                top = min(top, current_y)
                bottom = max(bottom, current_y)
                for next_x, next_y in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    next_offset = next_y * width + next_x
                    if visited[next_offset] or not pixels[next_x, next_y]:
                        continue
                    visited[next_offset] = 1
                    queue.append((next_x, next_y))
            box_width = right - left + 1
            box_height = bottom - top + 1
            aspect = box_width / box_height
            if (
                count >= 80
                and box_width >= minimum_width
                and box_height >= minimum_height
                and 0.45 <= aspect <= 6
                and box_width <= width * 0.9
                and box_height <= height * 0.9
            ):
                original_box = tuple(
                    round(value / scale)
                    for value in (left, top, right + 1, bottom + 1)
                )
                boxes.append((count, original_box))

    selected: list[tuple[int, int, int, int]] = []
    for _, box in sorted(boxes, key=lambda item: item[0], reverse=True):
        if any(_overlap_ratio(box, existing) > 0.6 for existing in selected):
            continue
        selected.append(box)
        if len(selected) == limit:
            break
    return selected


def _padded_box(
    box: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    padding_x = max(12, round((right - left) * 0.12))
    padding_y = max(12, round((bottom - top) * 0.2))
    return (
        max(0, left - padding_x),
        max(0, top - padding_y),
        min(size[0], right + padding_x),
        min(size[1], bottom + padding_y),
    )


def _overlap_ratio(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0
    intersection = (right - left) * (bottom - top)
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / min(first_area, second_area)


def choose_frame_times(start_ms: int, end_ms: int, max_frames: int = 3) -> list[int]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("Некорректный временной интервал")
    duration = end_ms - start_ms
    if duration < 1200 or max_frames == 1:
        return [start_ms + duration // 2]
    count = min(max_frames, 5)
    if count == 2:
        fractions = (0.4, 0.6)
    elif count == 3:
        fractions = (0.35, 0.5, 0.65)
    else:
        fractions = tuple((index + 1) / (count + 1) for index in range(count))
    margin = min(350, duration // 5)
    return [
        min(end_ms - margin, max(start_ms + margin, start_ms + int(duration * fraction)))
        for fraction in fractions
    ]


class FFmpegFrameExtractor:
    def __init__(self, executable: Path | None, timeout_seconds: int = 60) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    @property
    def ready(self) -> bool:
        return self.executable is not None and self.executable.is_file()

    def extract(self, source: Path, timestamp_ms: int, destination: Path) -> ExtractedFrame:
        if not self.ready or self.executable is None:
            raise FrameExtractionError("FFmpeg не настроен или недоступен")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            str(destination),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FrameExtractionError("Не удалось запустить FFmpeg") from exc
        invalid_output = not destination.is_file() or destination.stat().st_size == 0
        if completed.returncode != 0 or invalid_output:
            message = (completed.stderr or "FFmpeg не создал кадр").strip()[:500]
            raise FrameExtractionError(message)
        prepare_frame_for_vision(destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return ExtractedFrame(destination, timestamp_ms, timestamp_ms, digest)
