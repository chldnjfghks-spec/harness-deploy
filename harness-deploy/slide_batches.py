"""작은 로컬 모델을 위한 슬라이드 분할 생성. 원문은 자르지 않고 여러 요청에 배분합니다."""

import json
import re
from pathlib import Path


def split_text(text, limit=1600):
    """문단 경계를 우선하며 모든 글자를 빠짐없이 순서대로 나눕니다."""
    while text:
        end = min(len(text), limit)
        if end < len(text):
            boundary = text.rfind("\n", limit // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield text[:end]
        text = text[end:]


def blank_slide(slot, kind, title, status="자료 미제공"):
    """표지·목차 같은 프로그램 관리 페이지의 기본 구조입니다."""
    from storyboard import FIELDS

    slide = {
        key: [] if spec["type"] == "array" else ""
        for key, spec in FIELDS.items()
    }
    slide.update(slot_id=slot, kind=kind, title=title, status=status,
                 evidence_mode="none")
    return slide


def create_batched_storyboard(report, manifest, out, title, image_ids=None):
    """원고 조각과 출처 조각을 독립 처리한 뒤 정책 순서대로 병합합니다.

    글자 수는 토큰 수의 근사치일 뿐입니다. 출력 잘림은 성공으로 취급하지 않습니다.
    이미지 직접 판독을 주장하지 않도록 이 경로에서는 텍스트 기반 페이지만 생성합니다.
    """
    from llm_client import call_model
    from roles import ROOT, load_settings
    from storyboard import SECTION_IDS, assemble_plan, validate_plan

    out = Path(out)
    runtime = json.loads((ROOT / "templates/local_runtime.json").read_text(encoding="utf-8"))
    # 역할 지시문·재시도 안내·채팅 템플릿을 위한 여유를 남깁니다.
    budget = min(6500, runtime.get("max_prompt_chars", 24000) - 2500)
    if budget < 4000:
        raise ValueError("슬라이드 분할 생성에 필요한 최소 입력 여유가 부족합니다.")
    source_ids = {s["id"] for s in manifest["sources"]}
    schema = {
        "type": "object", "properties": {
            "slides": {"type": "array", "minItems": 1, "maxItems": 2,
                       "items": {"type": "object", "properties": {
                           "slot_id": {"type": "string", "enum": SECTION_IDS},
                           "title": {"type": "string"},
                           "takeaway": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}},
                           "source_ids": {"type": "array", "items": {"type": "string"}},
                           "notes": {"type": "string"},
                       }, "required": ["slot_id", "title", "takeaway", "bullets", "source_ids", "notes"],
                       "additionalProperties": False}}},
        "required": ["slides"], "additionalProperties": False,
    }
    # 상태는 모델이 지어내지 않고 출처 유무를 기준으로 프로그램에서 지정합니다.
    instructions = (
        "전체 발표 중 현재 조각만 한국어 슬라이드 1~2장으로 재작성하세요. "
        "표지·목차·마무리는 만들지 마세요. 원고는 주장이고 출처 원문은 대조 근거입니다. "
        "코드 존재와 실행 결과를 구분하고 근거 없는 성과·숫자를 추가하지 마세요. "
        "관련 없는 절은 만들지 마세요. slot_id는 제공한 절 중 선택하세요. "
        "제목 28자, takeaway 60자 이하. bullets는 2~3개, 각각 44자 이하. "
        "제목·takeaway·bullets에 [S001] 등 출처 기호를 넣지 말고 source_ids와 notes에 보관하세요. "
        "이미지 직접 검토는 수행하지 않았습니다. 구조화 JSON만 반환하세요.\n절: "
        + json.dumps(SECTION_IDS, ensure_ascii=False)
    )
    tasks = []
    # 보고서 전체와 모든 출처 텍스트를 별도 조각으로 보관하여 누락·자의적 요약을 피합니다.
    for number, text in enumerate(split_text(report), 1):
        ids = sorted(set(re.findall(r"\[(S\d+)\]", text)) & source_ids)
        tasks.append({"kind": "report", "part": number, "text": text, "source_ids": ids})
    for source in manifest["sources"]:
        text = str(source.get("text") or "")
        if not text:
            continue
        for number, text_part in enumerate(split_text(text), 1):
            tasks.append({"kind": "source", "id": source["id"], "part": number,
                          "text": text_part, "source_ids": [source["id"]]})
    if not tasks:
        raise ValueError("슬라이드로 만들 보고서와 근거 텍스트가 없습니다.")
    (out / "batch_inputs.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    slides = []
    seen = set()
    for index, task in enumerate(tasks, 1):
        # 각 요청의 출처 메타데이터도 관련 ID만 전달합니다.
        ids = task["source_ids"]
        current = {"input": task, "sources": [
            {"id": s["id"], "path": s.get("path", ""), "kind": s["kind"]}
            for s in manifest["sources"] if s["id"] in ids
        ]}
        base = instructions + "\n현재 자료:\n" + json.dumps(current, ensure_ascii=False)
        if len(base) > budget:
            raise ValueError(f"{index}번째 자료의 메타데이터가 너무 큽니다. 출처 경로·ID 구성을 확인하세요.")
        checkpoint = out / f"batch_{index:04d}.json"
        # 완료된 조각은 재실행에서 사용합니다. 입력·규격이 같을 때만 캐시를 인정합니다.
        fingerprint = json.dumps([base, schema, load_settings()["slides"], runtime], ensure_ascii=False)
        cached = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
        accepted = cached.get("slides") if cached.get("input") == fingerprint else None
        errors = []
        for attempt in range(2) if accepted is None else []:
            prompt = base + ("\n이전 응답 오류: " + "; ".join(errors)[:500] if errors else "")
            response = call_model(prompt, out / f"batch_{index:04d}_try_{attempt}",
                                  role="slides", schema=schema)
            if response.get("output_truncated") or response.get("done_reason") == "length":
                raise ValueError(f"슬라이드 조각 {index}/{len(tasks)} 출력이 잘렸습니다. 완료 조각은 보관했습니다.")
            try:
                raw = json.loads(response["content"])["slides"]
                if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
                    raise ValueError("조각당 슬라이드는 1~2장이어야 합니다.")
                candidate = []
                for item in raw:
                    if set(item) != set(schema["properties"]["slides"]["items"]["required"]):
                        raise ValueError("슬라이드 필수 항목 불일치")
                    if not isinstance(item["source_ids"], list) or any(sid not in ids for sid in item["source_ids"]):
                        raise ValueError("현재 조각에 없는 근거 ID")
                    slide = blank_slide(item["slot_id"], "bullets", item["title"])
                    slide.update(item)
                    slide["status"] = "자료 확인" if slide["source_ids"] else "자료 미제공"
                    if slide["slot_id"] not in SECTION_IDS:
                        raise ValueError("알 수 없는 절")
                    candidate.append(slide)
                wrapper = [blank_slide("cover", "cover", "검사"), blank_slide("agenda", "agenda", "목차")]
                closing = blank_slide("conclusion", "closing", "결론")
                closing["bullets"] = ["구조 검사", "원문 대조 필요"]
                candidate.sort(key=lambda s: SECTION_IDS.index(s["slot_id"]))
                errors = validate_plan(assemble_plan({"title": title, "slides": wrapper + candidate + [closing]}), manifest, [])
                if not errors:
                    accepted = candidate
                    checkpoint.write_text(json.dumps({"input": fingerprint, "slides": accepted}, ensure_ascii=False, indent=2), encoding="utf-8")
                    break
            except (ValueError, KeyError, TypeError) as error:
                errors = [str(error)]
        if accepted is None:
            raise ValueError(f"슬라이드 조각 {index}/{len(tasks)} 검증 실패: " + "; ".join(errors))
        for slide in accepted:
            signature = json.dumps([slide["slot_id"], slide["title"], slide["bullets"], slide["source_ids"]], ensure_ascii=False)
            if signature not in seen:
                slides.append(slide)
                seen.add(signature)
    # 업로드한 이미지는 보존하지만, 텍스트 모델이 이미지를 읽었다고 주장하지 않습니다.
    selected = [s for s in manifest["sources"]
                if s["kind"] == "image" and s["id"] in (image_ids or [])][:6]
    selected_ids = [s["id"] for s in selected]
    for source in selected:
        slide = blank_slide("evaluation", "evidence", "제공된 그래프·이미지", "자료 확인")
        slide.update(evidence_mode="image", image_id=source["id"], source_ids=[source["id"]],
                     bullets=["업로드 자료에 포함된 원본 이미지", "수치·해석은 원본 문서와 함께 확인"],
                     caption="원본 이미지 · 모델의 시각 판독은 수행하지 않음",
                     notes=source.get("path", ""))
        slides.append(slide)
    slides.sort(key=lambda s: SECTION_IDS.index(s["slot_id"]))
    closing = blank_slide("conclusion", "closing", "결론 및 확인 범위")
    closing["bullets"] = ["보고서와 근거를 나누어 발표 자료로 구성", "사실성과 그림 수치는 원문 대조 후 사용"]
    closing["notes"] = "분할 생성 경로: 텍스트 기반 구성. 이미지 직접 검토 및 전체 의미 중복 검사는 수행하지 않음."
    plan = assemble_plan({"title": title, "slides": [
        blank_slide("cover", "cover", title[:42] or "프로젝트 보고서"),
        blank_slide("agenda", "agenda", "목차"), *slides, closing,
    ]})
    errors = validate_plan(plan, manifest, selected_ids)
    if errors:
        raise ValueError("분할 발표 구성 검증 실패: " + "; ".join(errors))
    (out / "storyboard.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan, selected_ids
