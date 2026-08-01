"""Pravi ICON/app.ico iz ICON/PIGEONp.png (Pillow). Poziva build_installer.ps1."""
from __future__ import annotations

from pathlib import Path

from PIL import Image


def main() -> None:
    root = Path(__file__).resolve().parent
    src = root / "ICON" / "PIGEONp.png"
    out = root / "ICON" / "app.ico"
    im = Image.open(src).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    imgs = [im.resize(s, Image.Resampling.LANCZOS) for s in sizes]
    imgs[0].save(
        out,
        format="ICO",
        sizes=[(i.width, i.height) for i in imgs],
        append_images=imgs[1:],
    )


if __name__ == "__main__":
    main()
