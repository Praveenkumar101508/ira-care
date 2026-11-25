# IRA CARE watch: blueprint sheets

Five A3 drawing sheets, design intent, in millimetres. Dimensions are to be confirmed by a hardware engineer.

| Sheet | What it shows |
|---|---|
| [1](sheet1.png) | General arrangement: front, right side, back and top views |
| [2](sheet2.png) | Section A-A: the internal stack and each layer's thickness |
| [3](sheet3.png) | Exploded view with the parts list |
| [4](sheet4.png) | Electronics block diagram and the software stack |
| [5](sheet5.png) | Charging dock and strap |

- [All sheets, blueprint blue (PDF)](IRA_CARE_watch_blueprint_blue.pdf)
- [All sheets, black on white for printing (PDF)](IRA_CARE_watch_blueprint_print.pdf)
- [3D blueprint](../viewer_blueprint.html): turn it, pull it apart, X-ray, tap a part.

Made by `scripts/hardware/blueprint.py` from the same measurements as `scripts/hardware/build_watch.py`:

```
python3 scripts/hardware/blueprint.py docs/hardware/blueprint blue
python3 scripts/hardware/blueprint.py /tmp/print print
```

The SVGs are the source; the PNGs and PDFs are rendered from them with cairosvg.
