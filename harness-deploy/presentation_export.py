"""슬라이드 설계와 파일 생성기를 연결하는 담당입니다.
Python으로 편집형 PPTX와 미리보기를 생성하고 그림을 PDF로 묶습니다."""

from __future__ import annotations
import hashlib, json, re, subprocess, unicodedata, os
from datetime import datetime
from pathlib import Path
from PIL import Image
from engine import ROOT
from storyboard import VERSION, CONTRACT_PATH


def plain(text):
    """Markdown의 이미지·링크·강조 기호를 제거해 일반 글자로 바꾸는 보조 함수입니다."""
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.replace("**", "").replace("`", "").strip()


def wrap(text, units):
    """한글과 영문의 글자 폭을 고려해 문자열을 여러 줄로 나눕니다.
    units는 대략적인 줄 너비이며 픽셀 수가 아닙니다."""
    result = []
    for original in text.splitlines():
        line = ""
        used = 0
        if not original.strip():
            result.append("")
            continue
        for c in original:
            width = 2 if unicodedata.east_asian_width(c) in "WF" else 1
            if used + width > units:
                result.append(line)
                line = ""
                used = 0
            line += c
            used += width
        if line:
            result.append(line)
    return result


def export_key(report, title, image_ids):
    """본문·제목·이미지·양식에 대한 식별값(해시)을 만듭니다.
    입력이 같으면 기존 결과를 재사용하고, 내용이나 양식이 바뀌면 새 파일을 만들기 위해 사용합니다."""
    from roles import load_settings

    model_settings = load_settings()["slides"]
    outline_hash = hashlib.sha256(
        (ROOT / "templates/local_report_outline.md").read_bytes()
    ).hexdigest()
    return hashlib.sha256(
        json.dumps(
            [
                report,
                title,
                image_ids,
                VERSION,
                hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
                outline_hash,
                model_settings,
                "streamlit-python-pptx-v2",
                hashlib.sha256(
                    (ROOT / "portable_renderer.py").read_bytes()
                ).hexdigest(),
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def build_pdf(paths, destination):
    # Release raw pixels after each page; keep the PDF's compressed page streams.
    """렌더링한 슬라이드 이미지들을 순서대로 한 PDF에 넣습니다.
    한 장씩 처리해 메모리 사용을 줄입니다. 이미지형 PDF라 글자 검색은 지원하지 않습니다."""
    import io
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for path in paths:
        buffer = io.BytesIO()
        with Image.open(path) as original:
            image = original.convert("RGB")
            image.save(buffer, format="PDF", resolution=144.0)
            image.close()
        buffer.seek(0)
        writer.append(PdfReader(buffer))
    with Path(destination).open("wb") as output:
        writer.write(output)
    writer.close()


def export_deck(report, manifest, run_dir, title="", image_ids=None):
    """슬라이드 설계 → PPTX·미리보기 생성 → PDF 변환을 연결합니다.
    완성된 PPTX와 PDF가 들어 있는 출력 폴더 경로를 반환합니다.
    실제 슬라이드 그리기는 portable_renderer.py가 담당합니다."""
    full_key = export_key(report, title, image_ids)
    key = full_key[:12]
    out = Path(run_dir) / ("presentation_" + key)
    if (out / "files/report.pptx").exists() and (out / "files/report.pdf").exists():
        return out
    # 실패한 부분 결과를 덮어쓰지 않고 새 폴더로 재시도합니다.
    if out.exists():
        out = Path(run_dir) / (out.name + "_" + datetime.now().strftime("%H%M%S_%f"))
    out.mkdir(parents=True)
    from storyboard import create_storyboard

    plan, selected_ids = create_storyboard(report, manifest, out, title, image_ids)
    # ID로 자료를 바로 찾기 위한 딕셔너리입니다. 예: source_map["S001"].
    source_map = {s["id"]: s for s in manifest["sources"]}
    plan["date"] = datetime.now().strftime("%Y.%m.%d")
    for slide in plan["slides"]:
        if slide["kind"] == "evidence" and slide["evidence_mode"] == "image":
            source = source_map[slide["image_id"]]
            with Image.open(source["file"]) as image:
                slide["image"] = {
                    "file": source["file"],
                    "ratio": image.width / image.height,
                }
        slide["notes"] += "\n\n근거 파일:\n" + "\n".join(
            f'[{sid}] {source_map[sid]["path"]}' for sid in slide["source_ids"]
        )
    planfile = out / "slides.json"
    planfile.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 배포용 생성기는 프로젝트 내부 모듈과 공개 라이브러리만 사용합니다.
    from portable_renderer import render_plan

    render_plan(plan, out)
    # 파일명을 slide-001처럼 0으로 채워 두어 문자열 정렬과 페이지 순서를 일치시킵니다.
    paths = sorted((out / "preview").glob("slide-*.png"))
    if len(paths) != len(plan["slides"]):
        raise RuntimeError("슬라이드 렌더링 개수가 일치하지 않습니다.")
    # PPTX와 동일한 렌더링을 PDF 페이지로 사용하므로 레이아웃이 같습니다.
    build_pdf(paths, out / "files/report.pdf")
    from pypdf import PdfReader

    if len(PdfReader(out / "files/report.pdf").pages) != len(plan["slides"]):
        raise RuntimeError("PDF 페이지 검증 실패")
    (Path(run_dir) / "latest_export.json").write_text(
        json.dumps({"directory": str(out), "input_hash": full_key}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out
