"""개인 PC 경로 없이 같은 배치 정보로 편집형 PPTX와 PNG를 만듭니다.

글자 줄바꿈과 PNG는 Pillow, PPTX 저장은 python-pptx가 담당합니다.
표·차트·텍스트는 PPTX 안에서 편집할 수 있습니다.
"""

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1280, 720
NAVY, TEAL, INK, MUTED, LINE = "142C42", "087F8C", "18374A", "586B7B", "D9E4EA"


def font_configuration():
    """명시한 폰트 또는 운영체제의 공통 폰트를 찾습니다. 사용자 홈 경로는 쓰지 않습니다."""
    supplied = os.environ.get("HARNESS_FONT_PATH")
    candidates = [Path(supplied)] if supplied else []
    candidates += [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            index = 1 if path.suffix.lower() == ".ttc" else 0
            font = ImageFont.truetype(str(path), 24, index=index)
            return str(path), index, font.getname()[0]
    raise RuntimeError(
        "한글 폰트가 없습니다. fonts-noto-cjk를 설치하거나 HARNESS_FONT_PATH를 설정하세요."
    )


class Canvas:
    def __init__(self, background, config):
        self.image = Image.new("RGB", (WIDTH, HEIGHT), "#" + background)
        self.draw = ImageDraw.Draw(self.image)
        self.font_path, self.font_index, self.font_name = config
        self.scene = {"background": background, "objects": []}

    def rect(self, x, y, w, h, fill, line=None):
        self.draw.rectangle(
            (x, y, x + w, y + h), fill="#" + fill, outline="#" + line if line else None
        )
        self.scene["objects"].append(
            dict(type="rect", x=x, y=y, w=w, h=h, fill=fill, line=line)
        )

    def text(self, value, x, y, w, h, size=26, color=INK, bold=False, min_size=None):
        """원문을 자르지 않습니다. 공간을 넘으면 오류로 알려 다시 구성하게 합니다."""
        value = str(value or "")
        font = ImageFont.truetype(self.font_path, size, index=self.font_index)
        lines = []
        for paragraph in value.split("\n"):
            line = ""
            for char in paragraph:
                if line and self.draw.textlength(line + char, font=font) > w - 4:
                    lines.append(line.rstrip())
                    line = char
                else:
                    line += char
            lines.append(line.rstrip())
        step = round(size * 1.35)
        if len(lines) * step > h + 2 and min_size is not None and size > min_size:
            return self.text(
                value, x, y, w, h, max(min_size, size - 2), color, bold, min_size
            )
        if len(lines) * step > h + 2:
            raise ValueError(
                f"텍스트가 공간을 초과했습니다: {value[:50]} (필요 {len(lines)*step}px / {h}px)"
            )
        for i, line in enumerate(lines):
            self.draw.text(
                (x, y + i * step),
                line,
                font=font,
                fill="#" + color,
                anchor="lt",
                stroke_width=0.3 if bold else 0,
            )
        self.scene["objects"].append(
            dict(
                type="text",
                text="\n".join(lines),
                x=x,
                y=y,
                w=w,
                h=h,
                size=size,
                color=color,
                bold=bold,
                line_height=step,
            )
        )

    def image_box(self, file, x, y, w, h):
        with Image.open(file) as source:
            fitted = ImageOps.contain(source.convert("RGB"), (w, h))
            px, py = x + (w - fitted.width) // 2, y + (h - fitted.height) // 2
            self.image.paste(fitted, (px, py))
            self.scene["objects"].append(
                dict(
                    type="image",
                    file=str(Path(file).resolve()),
                    x=px,
                    y=py,
                    w=fitted.width,
                    h=fitted.height,
                )
            )

    def table(self, columns, rows, x=70, y=270, w=1140, h=300):
        if not columns or any(len(row) != len(columns) for row in rows):
            raise ValueError("표의 열 수가 일치하지 않습니다.")
        scene_start = len(self.scene["objects"])
        row_h, col_w = h / (len(rows) + 1), w / len(columns)
        for ri, row in enumerate([columns] + rows):
            for ci, value in enumerate(row):
                self.rect(
                    x + ci * col_w,
                    y + ri * row_h,
                    col_w,
                    row_h,
                    TEAL if ri == 0 else ("F0F6F8" if ri % 2 else "FFFFFF"),
                    LINE,
                )
                self.text(
                    value,
                    x + ci * col_w + 12,
                    y + ri * row_h + 9,
                    col_w - 24,
                    row_h - 12,
                    22,
                    "FFFFFF" if ri == 0 else INK,
                    ri == 0,
                )
        # PNG에서는 셀을 그리지만, PPTX에는 실제 편집 가능한 표를 넣습니다.
        del self.scene["objects"][scene_start:]
        self.scene["objects"].append(
            dict(
                type="table",
                columns=columns,
                rows=rows,
                x=x,
                y=y,
                w=w,
                h=h,
                row_h=row_h,
                size=22,
            )
        )

    def chart(self, categories, values, unit, x=70, y=270, w=740, h=315):
        if not categories or len(categories) != len(values):
            raise ValueError("차트의 항목과 값 개수가 일치하지 않습니다.")
        start = len(self.scene["objects"])
        low, high = min(0, min(values)), max(0, max(values))
        if low == high:
            high = low + 1
        low -= (high - low) * (0.1 if low < 0 else 0)
        high += (high - low) * 0.15
        plot_h = h - 65
        baseline = y + plot_h * high / (high - low)
        self.rect(x, baseline, w, 1, LINE)
        for i, (label, value) in enumerate(zip(categories, values)):
            cell_w = w / len(values)
            bx = x + cell_w * (i + 0.2)
            top = y + plot_h * (high - value) / (high - low)
            self.rect(
                bx, min(top, baseline), cell_w * 0.6, max(1, abs(baseline - top)), TEAL
            )
            self.text(
                f"{value:g}", bx, max(y, min(top, baseline) - 30), cell_w * 0.8, 30, 20
            )
            self.text(label, x + i * cell_w, y + plot_h + 15, cell_w - 5, 54, 18)
        del self.scene["objects"][start:]
        self.scene["objects"].append(
            dict(
                type="chart",
                categories=categories,
                values=values,
                unit=unit,
                x=x,
                y=y,
                w=w,
                h=h,
            )
        )


def draw_bullets(canvas, items, x=90, y=280, w=1090, size=28, gap=100):
    for index, item in enumerate(items):
        canvas.text("•", x - 25, y + index * gap, 25, 40, size, TEAL)
        canvas.text(item, x + 10, y + index * gap, w - 10, gap - 12, size)


def render_plan(plan, output):
    """기존 슬라이드 설계의 모든 종류를 처리하며 전체 페이지를 저장합니다."""
    output = Path(output)
    (output / "preview").mkdir(parents=True, exist_ok=True)
    (output / "files").mkdir(exist_ok=True)
    config = font_configuration()
    scenes = []
    for number, slide in enumerate(plan["slides"], 1):
        cover = slide["kind"] == "cover"
        canvas = Canvas(NAVY if cover else "FFFFFF", config)
        if cover:
            canvas.rect(70, 135, 85, 6, "45B9B0")
            canvas.text(
                slide["title"], 70, 190, 1130, 215, 60, "FFFFFF", True, min_size=44
            )
            canvas.text(slide.get("takeaway"), 70, 430, 1120, 100, 28, "C8DBE5")
        else:
            canvas.text(slide.get("section_label", ""), 70, 32, 1140, 40, 20, MUTED)
            canvas.text(slide["title"], 70, 92, 1140, 64, 42, INK, True, min_size=38)
            canvas.text(slide.get("takeaway"), 70, 175, 1140, 76, 25, MUTED)
            kind = slide["kind"]
            if kind == "agenda":
                for index, entry in enumerate(slide.get("agenda_entries", [])):
                    y = 258 + index * 51
                    canvas.text(f"{entry['number']:02d}", 70, y, 65, 39, 26, TEAL, True)
                    canvas.text(entry["title"], 150, y, 480, 40, 25, INK, True)
                    canvas.text(
                        entry.get("subtitle", ""), 655, y + 3, 430, 36, 19, MUTED
                    )
                    canvas.text(entry["page_text"], 1115, y + 3, 100, 36, 21, TEAL)
                    canvas.rect(70, y + 43, 1140, 1, LINE)
            elif kind == "table":
                canvas.table(slide["columns"], slide["rows"])
            elif kind == "flow":
                steps = slide["steps"]
                width = 1100 / len(steps)
                for i, step in enumerate(steps):
                    x = 70 + i * width
                    canvas.rect(x, 290, width - 28, 105, NAVY)
                    canvas.text(step, x + 14, 310, width - 56, 74, 25, "FFFFFF", True)
                    if i < len(steps) - 1:
                        canvas.text("→", x + width - 28, 320, 28, 45, 24, TEAL)
                draw_bullets(canvas, slide.get("bullets", []), y=465, size=25, gap=70)
            elif kind == "evidence":
                mode = slide.get("evidence_mode")
                if mode == "image":
                    canvas.image_box(slide["image"]["file"], 70, 270, 740, 310)
                elif mode == "chart":
                    canvas.chart(
                        slide["categories"], slide["values"], slide.get("unit", "")
                    )
                draw_bullets(
                    canvas,
                    slide.get("bullets", []),
                    x=865,
                    y=280,
                    w=330,
                    size=24,
                    gap=98,
                )
                caption = slide.get("caption", "")
                if mode == "chart" and slide.get("unit"):
                    caption += " · " + slide["unit"]
                canvas.text(caption, 70, 600, 1140, 54, 19, MUTED)
            else:
                draw_bullets(canvas, slide.get("bullets", []))
        canvas.rect(70, 665, 1140, 1, "416078" if cover else LINE)
        canvas.text(
            plan.get("title", "")[:65],
            70,
            680,
            1060,
            30,
            16,
            "C8DBE5" if cover else MUTED,
        )
        canvas.text(
            f"{number:02d}", 1160, 678, 60, 32, 20, "FFFFFF" if cover else MUTED
        )
        canvas.scene["notes"] = slide.get("notes", "")
        canvas.image.save(output / "preview" / f"slide-{number:03d}.png")
        scenes.append(canvas.scene)
    scene_path = output / "render_scene.json"
    scene_path.write_text(
        json.dumps(
            {"title": plan.get("title", ""), "font": config[2], "slides": scenes},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_powerpoint(
        scenes, output / "files/report.pptx", config[2], plan.get("title", "")
    )
    (output / "render.log").write_text(
        f"Python export complete: {len(scenes)} slides", encoding="utf-8"
    )
    return sorted((output / "preview").glob("slide-*.png"))


def write_powerpoint(scenes, destination, font_name, title):
    """Streamlit에서 pip로 설치할 수 있는 Python 패키지만 사용합니다."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_DATA_LABEL_POSITION
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width = Inches(WIDTH / 96)
    presentation.slide_height = Inches(HEIGHT / 96)
    presentation.core_properties.title = title
    for page in scenes:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(page["background"])
        for item in page["objects"]:
            box = [Inches(item[key] / 96) for key in ("x", "y", "w", "h")]
            if item["type"] == "rect":
                shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *box)
                shape.fill.solid()
                shape.fill.fore_color.rgb = RGBColor.from_string(item["fill"])
                if item.get("line"):
                    shape.line.color.rgb = RGBColor.from_string(item["line"])
                    shape.line.width = Pt(0.5)
                else:
                    shape.line.fill.background()
            elif item["type"] == "text":
                frame = slide.shapes.add_textbox(*box).text_frame
                frame.margin_left = frame.margin_right = frame.margin_top = (
                    frame.margin_bottom
                ) = 0
                frame.word_wrap = False
                for index, line in enumerate(item["text"].split("\n")):
                    paragraph = (
                        frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                    )
                    paragraph.text = line
                    paragraph.font.name = font_name
                    paragraph.font.size = Pt(item["size"] * 0.75)
                    paragraph.font.bold = item["bold"]
                    paragraph.font.color.rgb = RGBColor.from_string(item["color"])
                    paragraph.line_spacing = Pt(item["line_height"] * 0.75)
                    paragraph.space_after = Pt(0)
            elif item["type"] == "image":
                slide.shapes.add_picture(item["file"], *box)
            elif item["type"] == "table":
                rows = [item["columns"]] + item["rows"]
                table = slide.shapes.add_table(len(rows), len(rows[0]), *box).table
                for ri, values in enumerate(rows):
                    table.rows[ri].height = Inches(item["row_h"] / 96)
                    for ci, value in enumerate(values):
                        cell = table.cell(ri, ci)
                        cell.text = str(value)
                        cell.margin_left = cell.margin_right = Inches(12 / 96)
                        cell.margin_top = Inches(9 / 96)
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = RGBColor.from_string(
                            TEAL if ri == 0 else ("F0F6F8" if ri % 2 else "FFFFFF")
                        )
                        for paragraph in cell.text_frame.paragraphs:
                            paragraph.font.name = font_name
                            paragraph.font.size = Pt(item["size"] * 0.75)
                            paragraph.font.bold = ri == 0
                            paragraph.font.color.rgb = RGBColor.from_string(
                                "FFFFFF" if ri == 0 else INK
                            )
            elif item["type"] == "chart":
                data = CategoryChartData()
                data.categories = item["categories"]
                data.add_series(item["unit"] or "값", item["values"])
                chart = slide.shapes.add_chart(
                    XL_CHART_TYPE.COLUMN_CLUSTERED, *box, data
                ).chart
                chart.has_legend = False
                chart.category_axis.tick_labels.font.name = font_name
                chart.category_axis.tick_labels.font.size = Pt(13)
                chart.value_axis.tick_labels.font.name = font_name
                chart.value_axis.tick_labels.font.size = Pt(12)
                chart.plots[0].has_data_labels = True
                chart.plots[0].data_labels.position = XL_DATA_LABEL_POSITION.OUTSIDE_END
                chart.plots[0].data_labels.font.size = Pt(12)
                for series in chart.series:
                    series.format.fill.solid()
                    series.format.fill.fore_color.rgb = RGBColor.from_string(TEAL)
                # 일부 뷰어는 음수 축 ID를 읽지 못하므로 참조까지 같은 양수로 맞춥니다.
                for axis_reference in chart._chartSpace.xpath(
                    ".//c:axId | .//c:crossAx"
                ):
                    axis_id = int(axis_reference.get("val"))
                    if axis_id < 0:
                        axis_reference.set("val", str(axis_id + 2**32))
        slide.notes_slide.notes_text_frame.text = page.get("notes", "")
    presentation.save(destination)
