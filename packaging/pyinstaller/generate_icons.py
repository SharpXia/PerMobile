from __future__ import annotations

import argparse
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def render_icon(source: Path, destination: Path, *, size: int = 1024) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(QByteArray(source.read_bytes()))
    if not renderer.isValid():
        raise ValueError(f"invalid SVG icon: {source}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(Qt.GlobalColor.transparent))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(destination)):
        raise RuntimeError(f"Qt cannot write icon format: {destination.suffix}")
    application.processEvents()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PerfEval platform icons from the SVG source.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args()
    render_icon(args.source, args.output, size=args.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
