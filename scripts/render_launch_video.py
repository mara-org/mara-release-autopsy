#!/usr/bin/env python3
"""Render the Mara Release Autopsy launch video without generated text.

The renderer is deterministic: every character, transition, and safe-area
decision is produced locally. It requires Pillow, NumPy, and imageio-ffmpeg.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
FPS_NUM = 30_000
FPS_DEN = 1_001
FPS = FPS_NUM / FPS_DEN
DURATION = 15.0
FRAME_COUNT = round(DURATION * FPS)

BLACK = (4, 7, 10)
PANEL = (8, 14, 18)
WHITE = (235, 244, 246)
MUTED = (132, 153, 160)
CYAN = (40, 229, 221)
CYAN_DARK = (10, 92, 95)
RED = (255, 93, 107)
YELLOW = (255, 201, 82)

MONO_FONT = Path("/System/Library/Fonts/Menlo.ttc")
SANS_FONT = Path("/System/Library/Fonts/SFNS.ttf")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def phase(now: float, start: float, end: float) -> float:
    return clamp((now - start) / (end - start))


def ease_out_cubic(value: float) -> float:
    return 1.0 - (1.0 - value) ** 3


def ease_in_out(value: float) -> float:
    return value * value * (3.0 - 2.0 * value)


def ease_out_back(value: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (value - 1.0) ** 3 + c1 * (value - 1.0) ** 2


def font(size: int, *, bold: bool = False, sans: bool = False) -> ImageFont.FreeTypeFont:
    path = SANS_FONT if sans else MONO_FONT
    index = 1 if bold and not sans else 0
    return ImageFont.truetype(str(path), size=size, index=index)


FONTS = {
    "tiny": font(24),
    "small": font(28),
    "body": font(34),
    "command": font(43),
    "release": font(46, bold=True),
    "hero": font(58, bold=True, sans=True),
    "hero_small": font(46, sans=True),
    "brand": font(35, bold=True),
}


def base_background() -> Image.Image:
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
    rgb[:] = BLACK

    glow_a = np.exp(-(((xx - 330) / 620) ** 2 + ((yy - 190) / 520) ** 2))
    glow_b = np.exp(-(((xx - 1640) / 680) ** 2 + ((yy - 850) / 560) ** 2))
    rgb[..., 0] += glow_a * 2 + glow_b * 1
    rgb[..., 1] += glow_a * 17 + glow_b * 9
    rgb[..., 2] += glow_a * 19 + glow_b * 17

    vignette = np.clip(
        ((xx - WIDTH / 2) / (WIDTH * 0.75)) ** 2 + ((yy - HEIGHT / 2) / (HEIGHT * 0.8)) ** 2, 0, 1
    )
    rgb *= 1.0 - vignette[..., None] * 0.42
    return Image.fromarray(np.uint8(np.clip(rgb, 0, 255)), "RGB")


BACKGROUND = base_background()


def draw_grid(draw: ImageDraw.ImageDraw, now: float) -> None:
    offset_x = int((now * 18) % 120)
    offset_y = int((now * 8) % 120)
    for x in range(-120 + offset_x, WIDTH + 120, 120):
        draw.line((x, 0, x, HEIGHT), fill=(12, 36, 39), width=1)
    for y in range(-120 + offset_y, HEIGHT + 120, 120):
        draw.line((0, y, WIDTH, y), fill=(9, 28, 31), width=1)


def draw_timeline_collision(image: Image.Image, now: float) -> None:
    if now > 1.42:
        return
    draw = ImageDraw.Draw(image)
    progress = ease_in_out(phase(now, 0.04, 0.88))
    center_x = WIDTH // 2
    left_end = int(192 + (center_x - 192) * progress)
    right_start = int(1728 - (1728 - center_x) * progress)
    draw.line((192, 535, left_end, 535), fill=CYAN, width=4)
    draw.line((right_start, 565, 1728, 565), fill=WHITE, width=3)
    draw.text((192, 488), "GIT RELEASE", font=FONTS["tiny"], fill=CYAN)
    health_label = "HEALTH HISTORY"
    label_width = draw.textlength(health_label, font=FONTS["tiny"])
    draw.text((1728 - label_width, 585), health_label, font=FONTS["tiny"], fill=MUTED)

    if now >= 0.76:
        radius = 8 + int(44 * phase(now, 0.76, 1.20))
        alpha = int(190 * (1.0 - phase(now, 0.92, 1.42)))
        flash = Image.new("RGBA", image.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(flash)
        fd.ellipse(
            (center_x - radius, 550 - radius, center_x + radius, 550 + radius),
            fill=(*CYAN, max(alpha, 0)),
        )
        image.paste(flash, (0, 0), flash)


def terminal_bounds(now: float) -> tuple[int, int, int, int] | None:
    progress = phase(now, 0.78, 1.58)
    if progress <= 0:
        return None
    scale = ease_out_back(progress)
    final = (176, 120, 1744, 952)
    cx, cy = WIDTH // 2, 548
    half_w = (final[2] - final[0]) / 2 * scale
    half_h = (final[3] - final[1]) / 2 * scale
    return int(cx - half_w), int(cy - half_h), int(cx + half_w), int(cy + half_h)


def draw_terminal_shell(image: Image.Image, now: float) -> tuple[int, int, int, int] | None:
    bounds = terminal_bounds(now)
    if bounds is None:
        return None
    x1, y1, x2, y2 = bounds
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((x1 + 18, y1 + 26, x2 + 18, y2 + 26), radius=34, fill=(0, 0, 0, 120))
    draw.rounded_rectangle(
        (x1, y1, x2, y2), radius=32, fill=(*PANEL, 246), outline=(*CYAN_DARK, 220), width=2
    )
    draw.line((x1, y1 + 72, x2, y1 + 72), fill=(*CYAN_DARK, 170), width=2)
    for index, color in enumerate((RED, YELLOW, CYAN)):
        cx = x1 + 34 + index * 32
        draw.ellipse((cx - 7, y1 + 29, cx + 7, y1 + 43), fill=(*color, 220))
    draw.text((x1 + 138, y1 + 24), "mara.dev / local-only", font=FONTS["tiny"], fill=(*MUTED, 255))
    image.paste(layer, (0, 0), layer)
    return bounds


def reveal_text(
    image: Image.Image,
    xy: tuple[int, int],
    value: str,
    text_font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int],
    progress: float,
) -> None:
    if progress <= 0:
        return
    draw = ImageDraw.Draw(image)
    visible = value[: max(1, math.ceil(len(value) * clamp(progress)))]
    draw.text(xy, visible, font=text_font, fill=color)
    if progress < 0.98:
        scan_x = xy[0] + int(draw.textlength(visible, font=text_font))
        ImageDraw.Draw(image).rectangle(
            (scan_x, xy[1] - 2, scan_x + 4, xy[1] + text_font.size + 4), fill=CYAN
        )


def draw_command_scene(image: Image.Image, now: float, bounds: tuple[int, int, int, int]) -> None:
    if now >= 6.34:
        return
    x1, y1, _, _ = bounds
    command = "$ mara-autopsy export.zip ~/code --show"
    typed = int(len(command) * ease_out_cubic(phase(now, 1.34, 3.04)))
    draw = ImageDraw.Draw(image)
    draw.text((x1 + 64, y1 + 112), command[:typed], font=FONTS["command"], fill=WHITE)
    if int(now * 5) % 2 == 0 and typed < len(command):
        cursor_x = x1 + 64 + int(draw.textlength(command[:typed], font=FONTS["command"]))
        draw.rectangle((cursor_x + 4, y1 + 116, cursor_x + 25, y1 + 163), fill=CYAN)

    statuses = [
        "[1/3] Opening Apple Health. Nothing leaves this machine…",
        "[2/3] Reading 1 repo. Git remembers when you shipped…",
        "[3/3] Matching timelines. Coincidence gets a spreadsheet…",
    ]
    starts = (3.00, 3.92, 4.84)
    for index, (line, start) in enumerate(zip(statuses, starts, strict=True)):
        progress = ease_out_cubic(phase(now, start, start + 0.72))
        color = CYAN if index == 2 and progress > 0.98 else MUTED
        reveal_text(image, (x1 + 64, y1 + 250 + index * 82), line, FONTS["body"], color, progress)


def draw_glitch(image: Image.Image, now: float) -> None:
    if now < 6.08 or now > 6.54:
        return
    strength = math.sin(math.pi * phase(now, 6.08, 6.54))
    if strength <= 0:
        return
    source = image.copy()
    bands = ((180, 252, 22), (356, 430, -28), (518, 590, 18), (718, 770, -20))
    for y1, y2, offset in bands:
        crop = source.crop((176, y1, 1744, y2))
        image.paste(crop, (176 + int(offset * strength), y1))
    draw = ImageDraw.Draw(image)
    for y in (268, 484, 702):
        draw.rectangle((176, y, 1744, y + 4), fill=CYAN if y != 484 else RED)


def draw_score_bar(draw: ImageDraw.ImageDraw, x: int, y: int, progress: float) -> None:
    segment_w = 39
    gap = 10
    segment_count = 28
    lit = int(segment_count * 0.94 * clamp(progress))
    for index in range(segment_count):
        sx = x + index * (segment_w + gap)
        fill = CYAN if index < lit else (18, 43, 46)
        if index >= 24 and index < lit:
            fill = RED
        draw.rounded_rectangle((sx, y, sx + segment_w, y + 24), radius=5, fill=fill)


def draw_result_scene(image: Image.Image, now: float, bounds: tuple[int, int, int, int]) -> None:
    if now < 6.28:
        return
    x1, y1, x2, _ = bounds
    draw = ImageDraw.Draw(image)
    entrance = ease_out_cubic(phase(now, 6.30, 7.18))
    offset_y = int((1.0 - entrance) * 38)
    alpha_color = tuple(int(channel * (0.4 + 0.6 * entrance)) for channel in CYAN)
    draw.text(
        (x1 + 64, y1 + 106 + offset_y),
        "MARA // RELEASE AUTOPSY",
        font=FONTS["brand"],
        fill=alpha_color,
    )
    draw.text(
        (x1 + 64, y1 + 158 + offset_y),
        "your body kept the receipts",
        font=FONTS["small"],
        fill=MUTED,
    )

    release_progress = ease_out_cubic(phase(now, 7.18, 8.08))
    reveal_text(
        image,
        (x1 + 64, y1 + 245),
        "payments-api  v1.0.0",
        FONTS["release"],
        WHITE,
        release_progress,
    )
    score = int(94 * ease_out_cubic(phase(now, 7.46, 8.92)))
    score_text = f"{score:02d}/100  VERY LOUD"
    score_width = draw.textlength(score_text, font=FONTS["release"])
    draw.text((x2 - 64 - score_width, y1 + 245), score_text, font=FONTS["release"], fill=RED)
    draw_score_bar(draw, x1 + 64, y1 + 324, phase(now, 7.52, 8.92))

    metrics = [
        ("SLEEP", "6h", "usual 8h", "↓ 2h/night"),
        ("BEDTIME", "01:00", "usual 23:00", "→ 2h later"),
        ("STEPS", "5,000", "usual 10,000", "↓ 50.0%"),
    ]
    for index, (label, value, usual, change) in enumerate(metrics):
        start = 9.04 + index * 0.78
        progress = ease_out_cubic(phase(now, start, start + 0.70))
        if progress <= 0:
            continue
        y = y1 + 420 + index * 88
        x_offset = int((1.0 - progress) * 80)
        draw.rounded_rectangle(
            (x1 + 58, y - 8, x2 - 58, y + 55),
            radius=12,
            fill=(10, 24, 27),
            outline=(18, 52, 55),
            width=1,
        )
        draw.text((x1 + 80 + x_offset, y), label, font=FONTS["body"], fill=CYAN)
        draw.text((x1 + 340 + x_offset, y), value, font=FONTS["body"], fill=WHITE)
        draw.text((x1 + 650 + x_offset, y), usual, font=FONTS["body"], fill=MUTED)
        draw.text((x2 - 390 + x_offset, y), change, font=FONTS["body"], fill=RED)


def draw_punchline(image: Image.Image, now: float, bounds: tuple[int, int, int, int]) -> None:
    progress = ease_in_out(phase(now, 11.62, 12.28))
    if progress <= 0:
        return
    x1, y1, x2, y2 = bounds
    veil = Image.new("RGBA", image.size, (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    vd.rounded_rectangle(
        (x1 + 2, y1 + 74, x2 - 2, y2 - 2), radius=28, fill=(2, 7, 9, int(222 * progress))
    )
    image.paste(veil, (0, 0), veil)

    draw = ImageDraw.Draw(image)
    center_x = WIDTH // 2
    first = "The tag shipped."
    second = "Your bedtime requested a rollback."
    first_w = draw.textlength(first, font=FONTS["hero"])
    second_w = draw.textlength(second, font=FONTS["hero"])
    lift = int((1.0 - progress) * 28)
    draw.text((center_x - first_w / 2, 402 + lift), first, font=FONTS["hero"], fill=WHITE)
    draw.text((center_x - second_w / 2, 486 + lift), second, font=FONTS["hero"], fill=CYAN)
    draw.line((562, 592, 1358, 592), fill=CYAN_DARK, width=2)
    sub = "THE BODY KEPT THE RECEIPTS"
    sub_w = draw.textlength(sub, font=FONTS["small"])
    draw.text((center_x - sub_w / 2, 625), sub, font=FONTS["small"], fill=MUTED)

    if now >= 14.00:
        footer = "Interesting timing is not proof. This is not medical advice."
        footer_w = draw.textlength(footer, font=FONTS["small"])
        draw.text((center_x - footer_w / 2, 860), footer, font=FONTS["small"], fill=WHITE)
        cursor_on = int((now - 14.00) * 5) % 2 == 0
        if cursor_on:
            draw.rectangle((center_x - 10, 914, center_x + 10, 950), fill=CYAN)


def make_frame(now: float) -> Image.Image:
    image = BACKGROUND.copy()
    draw_grid(ImageDraw.Draw(image), now)
    draw_timeline_collision(image, now)
    bounds = draw_terminal_shell(image, now)
    if bounds is not None:
        draw_command_scene(image, now, bounds)
        draw_result_scene(image, now, bounds)
        draw_glitch(image, now)
        draw_punchline(image, now, bounds)
    return image


def add_tone(
    track: np.ndarray, start: float, duration: float, frequency: float, amplitude: float
) -> None:
    sample_rate = 48_000
    begin = int(start * sample_rate)
    length = int(duration * sample_rate)
    t = np.arange(length) / sample_rate
    envelope = np.sin(np.pi * np.arange(length) / max(length - 1, 1)) ** 1.8
    tone = np.sin(2 * np.pi * frequency * t) * envelope * amplitude
    end = min(len(track), begin + length)
    track[begin:end] += tone[: end - begin]


def add_click(track: np.ndarray, start: float, amplitude: float = 0.08) -> None:
    sample_rate = 48_000
    begin = int(start * sample_rate)
    length = int(0.028 * sample_rate)
    rng = np.random.default_rng(int(start * 100_000) + 17)
    noise = rng.normal(0, 1, length)
    envelope = np.exp(-np.linspace(0, 8, length))
    click = noise * envelope * amplitude
    end = min(len(track), begin + length)
    track[begin:end] += click[: end - begin]


def add_whoosh(track: np.ndarray, start: float, duration: float, amplitude: float = 0.055) -> None:
    sample_rate = 48_000
    begin = int(start * sample_rate)
    length = int(duration * sample_rate)
    rng = np.random.default_rng(int(start * 10_000) + 43)
    noise = rng.normal(0, 1, length)
    smoothed = np.convolve(noise, np.ones(48) / 48, mode="same")
    envelope = np.sin(np.pi * np.arange(length) / max(length - 1, 1)) ** 2
    whoosh = smoothed * envelope * amplitude
    end = min(len(track), begin + length)
    track[begin:end] += whoosh[: end - begin]


def render_audio(path: Path) -> None:
    sample_rate = 48_000
    mono = np.zeros(int((DURATION + 0.08) * sample_rate), dtype=np.float64)
    add_whoosh(mono, 0.36, 0.92, 0.10)
    add_tone(mono, 0.76, 0.72, 72, 0.18)
    for timestamp in np.linspace(1.42, 2.96, 18):
        add_click(mono, float(timestamp), 0.055)
    for timestamp in (3.08, 4.00, 4.92):
        add_click(mono, timestamp, 0.11)
        add_tone(mono, timestamp, 0.09, 680, 0.035)
    add_whoosh(mono, 5.98, 0.64, 0.14)
    add_tone(mono, 6.18, 0.82, 54, 0.27)
    for timestamp in (7.48, 9.08, 9.86, 10.64):
        add_tone(mono, timestamp, 0.10, 520 + timestamp * 28, 0.045)
    add_tone(mono, 11.64, 0.62, 92, 0.13)
    add_tone(mono, 14.12, 0.18, 760, 0.045)

    peak = max(float(np.max(np.abs(mono))), 1e-9)
    mono *= 0.72 / peak
    stereo = np.column_stack((mono, np.roll(mono, 57)))
    pcm = np.int16(np.clip(stereo, -1, 1) * 32767)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def run(command: list[str], *, stdin: subprocess.PIPE | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, stdin=stdin)


def render(output: Path, poster: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    poster.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    silent = output.with_name(f".{output.stem}.silent.mp4")
    audio = output.with_name(f".{output.stem}.wav")

    command = [
        ffmpeg,
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        f"{FPS_NUM}/{FPS_DEN}",
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-maxrate",
        "8M",
        "-bufsize",
        "16M",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-x264-params",
        "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "60",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-movflags",
        "+faststart",
        str(silent),
    ]
    process = run(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    poster_index = round(12.8 * FPS)
    for index in range(FRAME_COUNT):
        frame = make_frame(index / FPS)
        if index == poster_index:
            frame.save(poster, quality=95)
        process.stdin.write(np.asarray(frame, dtype=np.uint8).tobytes())
    process.stdin.close()
    if process.wait() != 0:
        raise SystemExit("video encoding failed")

    render_audio(audio)
    mux = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=False,
    )
    silent.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)
    if mux.returncode != 0:
        raise SystemExit("audio mux failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("media/mara-release-autopsy-x-linkedin.mp4"),
    )
    parser.add_argument(
        "--poster",
        type=Path,
        default=Path("media/mara-release-autopsy-poster.png"),
    )
    args = parser.parse_args()
    render(args.output, args.poster)


if __name__ == "__main__":
    main()
