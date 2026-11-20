"""Render the SVG icon masters in this folder to the PNG sizes the add-on uses.

Not used by Blender. Run it after editing an SVG:  python build_icons.py
Requires: pip install cairosvg
"""
import os

import cairosvg

SIZES = (32, 64, 128)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)

for file in sorted(os.listdir(HERE)):
    if file.endswith(".svg"):
        name = file[:-4]
        for px in SIZES:
            cairosvg.svg2png(url=os.path.join(HERE, file),
                             write_to=os.path.join(OUT, f"{name}_{px}.png"),
                             output_width=px, output_height=px)
        print("rendered", name)
