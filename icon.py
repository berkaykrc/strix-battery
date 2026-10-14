import os


def render_tray_icon(percentage: int, charging: bool, connected: bool):
    """Draw a high-contrast headset and battery tray icon."""
    from PIL import Image, ImageDraw, ImageFont

    size = (128, 128)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not connected:
        draw.arc(
            [24, 16, 104, 96], start=180, end=0, fill=(130, 130, 130, 200), width=10
        )
        draw.rounded_rectangle([16, 44, 38, 92], radius=8, fill=(100, 100, 100, 200))
        draw.rounded_rectangle([90, 44, 112, 92], radius=8, fill=(100, 100, 100, 200))
        draw.line([20, 20, 108, 108], fill=(230, 50, 50, 240), width=10)
        return img

    if charging:
        theme_color = (0, 190, 255)  # Charging: neon cyan
    elif percentage > 40:
        theme_color = (60, 220, 70)  # >40%: vivid green
    elif percentage > 15:
        theme_color = (255, 175, 20)  # 16-40%: orange
    else:
        theme_color = (240, 50, 50)  # <=15%: critical red

    draw.arc([22, 14, 106, 98], start=180, end=0, fill=(20, 20, 20, 240), width=14)
    draw.arc([24, 16, 104, 96], start=180, end=0, fill=(240, 240, 240, 255), width=10)

    draw.rounded_rectangle([14, 42, 40, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([17, 45, 37, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle([88, 42, 114, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([91, 45, 111, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle(
        [20, 96, 102, 122], radius=6, outline=(20, 20, 20, 240), width=4
    )
    draw.rounded_rectangle(
        [22, 98, 100, 120], radius=5, outline=(240, 240, 240, 255), width=2
    )
    draw.rectangle([102, 104, 108, 114], fill=(240, 240, 240, 255))

    fill_width = int(72 * (percentage / 100))
    if fill_width > 0:
        draw.rounded_rectangle(
            [25, 101, 25 + fill_width, 117], radius=3, fill=theme_color
        )

    if charging:
        bolt_points = [(68, 28), (50, 58), (64, 58), (58, 88), (80, 52), (66, 52)]
        draw.polygon(bolt_points, fill=(0, 190, 255))
        inner_bolt = [(x + (1 if x < 64 else -1), y) for x, y in bolt_points]
        draw.polygon(inner_bolt, fill=(255, 255, 255))
    else:
        font = None
        font_paths = [
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
            "/usr/share/fonts/cantarell/Cantarell-VF.otf",
            "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, 36)
                    break
                except (OSError, ValueError):
                    pass
        if not font:
            font = ImageFont.load_default()

        txt = f"{percentage}"
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (128 - tw) // 2
        ty = 42 + (40 - th) // 2

        for ox, oy in [
            (-2, 0),
            (2, 0),
            (0, -2),
            (0, 2),
            (-1, -1),
            (1, 1),
            (-1, 1),
            (1, -1),
        ]:
            draw.text((tx + ox, ty + oy), txt, fill=(0, 0, 0, 255), font=font)
        draw.text((tx, ty), txt, fill=(255, 255, 255, 255), font=font)

    return img


def pil_to_qpixmap(pil_img):
    """Convert a Pillow image directly to a Qt QPixmap."""
    from PyQt6.QtGui import QImage, QPixmap

    img = pil_img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg)
