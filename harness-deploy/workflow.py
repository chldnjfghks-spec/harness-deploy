"""LangGraph로 담당자 회의를 진행합니다.

노드(node)는 담당자 한 명의 작업, 엣지(edge)는 다음 담당자로 가는 연결입니다.
공유 상태(state)에 작업 결과를 저장하고 필요한 결과만 다음 담당자에게 전달합니다.
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from engine import ROOT, required_report_sections
from llm_client import call_model
from roles import ROLES, load_settings


class MeetingState(TypedDict, total=False):
    """담당자들이 공유하는 작업 기록입니다. 저장했다고 자동으로 모델에게 전달되지는 않습니다."""

    plan: str
    analysis: str
    debate_note: str
    supervisor_task: str
    next_worker: str
    outline: str
    draft: str
    review: dict
    checks: dict
    revision: int
    chat_turns: int
    supervisor_turns: int
    pending_question: str
    expanded_evidence: str
    call_count: int
    max_calls: int
    status: str
    messages: list


ALLOWED_WORKERS = ("analyst", "organizer", "writer", "finish")
REQUIRED_CALLS_FROM_WORKER = {
    "analyst": 4,
    "organizer": 3,
    "writer": 2,
    "finish": 0,
}


def parse_supervisor_decision(answer, default_next="organizer", remaining_budget=0):
    """슈퍼바이저 응답(JSON, [NEXT_WORKER: ...] 태그, 또는 일반 대화문)에서 다음 담당자와 지시를 안전하게 추출합니다.

    작은 모델이나 로컬 LLM이 JSON 형식을 지키지 못하더라도 오류를 내지 않고 기본 경로(default_next)로 폴백하며,
    남은 호출 예산(remaining_budget)으로 감당할 수 없는 배정은 자동으로 하향 조정합니다.
    """
    text = (answer or "").strip()
    chosen = default_next
    task = text
    reason = ""
    done = False

    # 1) JSON 형태로 응답한 경우 파싱 시도
    json_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, dict) and (
                "next_worker" in parsed or "done" in parsed
            ):
                done = bool(parsed.get("done", False))
                candidate = str(parsed.get("next_worker", default_next)).strip().lower()
                if candidate in ALLOWED_WORKERS:
                    chosen = candidate
                task = str(parsed.get("task") or parsed.get("summary") or text).strip()
                reason = str(parsed.get("reason") or "").strip()
        except ValueError:
            pass

    # 2) [NEXT_WORKER: writer] 같은 경량 태그로 응답한 경우 파싱 (로컬 LLM 친화적 방식)
    tag_match = re.search(
        r"\[NEXT_WORKER\s*:\s*(analyst|organizer|writer|finish)\]",
        text,
        flags=re.IGNORECASE,
    )
    if tag_match:
        chosen = tag_match.group(1).strip().lower()
        task = re.sub(
            r"\[NEXT_WORKER\s*:[^\]]*\]", "", text, flags=re.IGNORECASE
        ).strip()

    if done or chosen == "finish":
        return {"next_worker": "finish", "task": task, "reason": reason, "done": True}

    if chosen not in ALLOWED_WORKERS:
        chosen = default_next

    # 3) Python 예산 검증: 선택한 워커부터 보고서 완성까지 필요한 최소 호출 수가 남은 예산보다 크면 안전하게 단계 축소
    needed = REQUIRED_CALLS_FROM_WORKER.get(chosen, 2)
    if remaining_budget < needed:
        if (
            default_next in ALLOWED_WORKERS
            and remaining_budget >= REQUIRED_CALLS_FROM_WORKER[default_next]
        ):
            chosen = default_next
        elif remaining_budget >= 2:
            chosen = "writer"
        else:
            chosen = "finish"
            done = True

    return {"next_worker": chosen, "task": task, "reason": reason, "done": done}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "summary", "issues"],
    "additionalProperties": False,
}


def clip(text, limit):
    """전송량을 줄이되, 생략했다는 사실을 다음 담당자에게 숨기지 않습니다."""
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + "\n[전송 분량 제한으로 뒷부분 생략. 누락 내용을 미수행으로 단정하지 말 것.]"
    )


def evidence_packet(manifest, total=16000, per_source=4000):
    """작은 모델용 원본 근거 묶음입니다. 원본 파일은 작업 폴더에 그대로 남습니다."""
    records = []
    remaining = total
    # 실제 실행 결과와 README·평가 기록을 먼저 보내 코드 앞부분이 예산을 모두 쓰지 않게 합니다.
    ordered = sorted(
        manifest["sources"],
        key=lambda s: (
            0
            if s["path"].startswith("실제 실행/")
            else (
                1
                if any(
                    word in s["path"].lower()
                    for word in ["readme", "metric", "result", "history"]
                )
                else 2
            )
        ),
    )
    for source in ordered:
        text = source.get("text", "")
        available = min(per_source, max(0, remaining))
        sent_chars = min(len(text), available)
        records.append(
            {
                "id": source["id"],
                "path": source["path"],
                "kind": source["kind"],
                "text": (
                    clip(text, available)
                    if available
                    else "[본문 전송 생략: ID와 경로만 제공]"
                ),
                "truncated": source.get("truncated", False) or len(text) > available,
                "sent_chars": sent_chars,
            }
        )
        remaining -= sent_chars
    return json.dumps(records, ensure_ascii=False)


def expand_source_tail(
    manifest,
    source_ids,
    evidence_json="",
    start_offset=4000,
    max_chars=5000,
    existing_expanded="",
):
    """에이전트가 [READ_SOURCE: S001]로 요청한 원본 자료의 생략된 뒷부분을 꺼내 줍니다.

    초기 묶음(evidence_json)에서 본문 전송량이 0자였던 자료는 4000자를 건너뛰지 않고 0자(맨 앞)부터 읽으며,
    이전에 이미 열람한 추가 근거(existing_expanded)가 있으면 함께 보존합니다.
    """
    wanted = {sid.strip().upper() for sid in source_ids if sid.strip()}
    sent_offsets = {}
    if evidence_json:
        try:
            for item in json.loads(evidence_json):
                if isinstance(item, dict) and "id" in item:
                    if "sent_chars" in item:
                        sent_offsets[item["id"]] = int(item["sent_chars"])
                    elif str(item.get("text", "")).startswith("[본문 전송 생략"):
                        sent_offsets[item["id"]] = 0
        except ValueError:
            pass

    merged_by_id = {}
    if existing_expanded:
        try:
            for item in json.loads(existing_expanded):
                if isinstance(item, dict) and "id" in item:
                    merged_by_id[item["id"]] = item
        except ValueError:
            pass

    for source in manifest.get("sources", []):
        if source["id"] in wanted and source.get("kind") == "text":
            raw = source.get("text", "")
            offset = sent_offsets.get(source["id"], start_offset)
            if len(raw) > offset:
                segment = raw[offset : offset + max_chars]
                end_pos = min(len(raw), offset + max_chars)
                range_label = f"{offset}~{end_pos}자"
            else:
                segment = raw[:max_chars]
                range_label = f"0~{min(len(raw), max_chars)}자"
            merged_by_id[source["id"]] = {
                "id": source["id"],
                "path": source["path"],
                "range": range_label,
                "expanded_text": segment,
            }
    expanded = list(merged_by_id.values())
    return json.dumps(expanded, ensure_ascii=False) if expanded else ""


def check_report(report, manifest):
    """모델이 아닌 Python으로 근거 ID와 필수 대제목을 검사합니다."""
    known = {source["id"] for source in manifest["sources"]}
    return {
        "unknown_source_ids": sorted(set(re.findall(r"\[(S\d+)\]", report)) - known),
        "missing_citations": bool(known)
        and not (set(re.findall(r"\[(S\d+)\]", report)) & known),
        "unresolved_control_tags": bool(
            re.search(r"\[(?:READ_SOURCE|ASK_ANALYST|NEXT_WORKER)\s*:", report)
        ),
        "missing_section_numbers": [
            number
            for number in required_report_sections()
            if not re.search(rf"(?m)^##\s+{number}[.\s、)]", report)
        ],
        "note": "구조·근거 ID 검사와 AI 검토 결과입니다. 사실성 전체를 보증하지 않습니다.",
    }


def run_meeting(
    manifest,
    run_dir,
    title="",
    purpose="",
    image_ids=None,
    settings=None,
    max_revisions=1,
    on_event=None,
    caller=None,
    stream=False,
    debate=False,
    max_calls=None,
    max_chat_turns=1,
):
    """회의를 진행하고 초안·검토 결과·저장 폴더를 반환합니다.

    on_event는 화면에 시작/완료 발언을 보여 주는 함수입니다.
    caller는 테스트에서 가짜 모델을 주입할 때만 바꾸며, 기본값은 실제 모델 호출입니다.
    debate가 True이면 자율 에이전트 티키타카(쟁점 조율 + 구성↔분석 되묻기 + 원본 추가 열람)가 활성화됩니다.
    max_calls는 회의 전체에서 허용하는 최대 모델 호출 횟수(한도 차단기)입니다.
    """
    if max_revisions not in (0, 1):
        raise ValueError("프로토타입에서는 수정 횟수를 0 또는 1로 지정하세요.")
    default_budget = 5 + (2 * max_revisions) + (3 if debate else 0)
    call_budget = default_budget if max_calls is None else int(max_calls)
    if call_budget < 5 or call_budget > 12:
        raise ValueError("모델 호출 한도(max_calls)는 5~12회 사이로 지정하세요.")
    run_dir = Path(run_dir).resolve()
    meeting_dir = (
        run_dir
        / "meetings"
        / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    )
    meeting_dir.mkdir(parents=True)
    settings = settings or load_settings()
    caller = caller or call_model
    selected = [
        s
        for s in manifest["sources"]
        if s["kind"] == "image" and s["id"] in (image_ids or [])
    ][:6]
    images = [s["file"] for s in selected]
    from roles import is_local_model, report_policy

    local_only = all(is_local_model(item["model"]) for item in settings.values())
    evidence = (
        evidence_packet(manifest, total=6000, per_source=1500)
        if local_only
        else evidence_packet(manifest)
    )
    # 전체 원본 대신 작은 묶음을 사용합니다. 실제 전송한 자료도 파일로 남겨 확인할 수 있습니다.
    (meeting_dir / "evidence_packet.json").write_text(evidence, encoding="utf-8")
    policy = report_policy(settings)
    brief = json.dumps(
        {"title": title, "purpose": purpose, "image_ids": [s["id"] for s in selected]},
        ensure_ascii=False,
    )
    state = MeetingState(
        revision=0,
        chat_turns=0,
        supervisor_turns=0,
        supervisor_task="",
        next_worker="",
        pending_question="",
        expanded_evidence="",
        call_count=0,
        max_calls=call_budget,
        status="running",
        messages=[],
    )
    messages = []
    sequence = 0

    def persist(current):
        """각 단계가 끝날 때 기록을 저장합니다. 서버나 DB 없이 JSON 파일로 보관합니다."""
        (meeting_dir / "state.json").write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "latest_meeting.json").write_text(
            json.dumps({"directory": str(meeting_dir)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def ask(role, prompt, with_images=False, schema=None):
        nonlocal sequence
        if sequence >= call_budget:
            raise RuntimeError(
                f"모델 호출 한도({call_budget}회)에 도달하여 추가 호출을 차단했습니다."
            )
        sequence += 1
        if on_event:
            on_event(
                {
                    "type": "started",
                    "role": role,
                    "model": settings[role]["model"],
                    "call_index": sequence,
                    "max_calls": call_budget,
                }
            )
        options = dict(
            role=role,
            images=images if with_images else [],
            schema=schema,
            settings=settings,
        )
        if stream:

            def receive_text(text):
                if on_event:
                    on_event(
                        {
                            "type": "delta",
                            "role": role,
                            "model": settings[role]["model"],
                            "text": text,
                            "call_index": sequence,
                            "max_calls": call_budget,
                        }
                    )

            options["on_delta"] = receive_text
        result = caller(prompt, meeting_dir / f"{sequence:02d}_{role}", **options)
        message = {
            "role": role,
            "name": ROLES[role]["name"],
            "model": result["model"],
            "content": result["content"],
            "usage": result.get("usage"),
            "seconds": result.get("seconds"),
            "call_index": sequence,
            "max_calls": call_budget,
            "images_reviewed": result.get("images_reviewed", True),
            "unreviewed_images": result.get("unreviewed_images", []),
            "output_truncated": result.get("output_truncated", False),
        }
        if result.get("warning"):
            message["warning"] = result["warning"]
        messages.append(message)
        if on_event:
            on_event(dict(message, type="completed"))
        return result["content"]

    def planner(current):
        source_index = [
            {"id": s["id"], "path": s["path"], "kind": s["kind"]}
            for s in manifest["sources"]
        ]
        answer = ask(
            "planner",
            "사용자 요청:\n"
            + brief
            + "\n자료 목록:\n"
            + json.dumps(source_index, ensure_ascii=False)
            + "\n사용할 대항목:\n"
            + json.dumps(
                json.loads(
                    (ROOT / "templates/deck_policy.json").read_text(encoding="utf-8")
                )["groups"],
                ensure_ascii=False,
            )
            + "\n첫 문단은 @자료·실행 결과 분석자님과 @보고서 구성 담당님에게 건네는 핵심 티키타카 대화(2~3문장, 180자 이내)로만 짧게 쓰고, 빈 줄 아래에 핵심 작업 계획만 불릿 3~4개로 전체 550자 이내로 간결하게 작성하세요.",
        )
        return {"plan": answer}

    def analyst(current):
        followup = current.get("pending_question", "")
        sup_task = current.get("supervisor_task", "")
        extra_ev = current.get("expanded_evidence", "")
        extra_block = f"\n추가 열람한 원본 뒷부분:\n{extra_ev}" if extra_ev else ""
        sup_block = (
            f"\n슈퍼바이저(기획자)의 작업 지시:\n{clip(sup_task, 1200)}"
            if sup_task
            else ""
        )
        review_block = (
            f"\n검토자의 수정 요청:\n{json.dumps(current['review'], ensure_ascii=False)}"
            if current.get("review")
            else ""
        )
        if followup:
            answer = ask(
                "analyst",
                "요청:\n"
                + brief
                + "\n구성 담당자의 추가 확인 질문:\n"
                + followup
                + sup_block
                + review_block
                + "\n원본 근거:\n"
                + evidence
                + extra_block
                + "\n@보고서 구성 담당님의 질문에 대해 실제 출처 ID([S001] 등)를 들어 2~3문장으로 정확히 답하세요. 없는 사실은 확인 불가로 답하세요.",
                with_images=True,
            )
            combined = current.get("analysis", "") + "\n\n[추가 확인 답변]\n" + answer
            return {"analysis": combined, "pending_question": ""}

        answer = ask(
            "analyst",
            "요청:\n"
            + brief
            + "\n기획자의 지시:\n"
            + clip(current["plan"], 1600)
            + sup_block
            + review_block
            + "\n원본 근거:\n"
            + evidence
            + extra_block
            + "\n첫 문단은 @기획자님에게 핵심 팩트를 답하고 @보고서 구성 담당님에게 주의할 점을 짚어주는 짧은 티키타카 대화(2~3문장, 200자 이내)로 쓰고, 빈 줄 아래에 확인된 사실·그림 해설·미확인 사항을 실제 출처 ID와 함께 핵심만 1000자 이내로 간결하게 정리하세요. 생략된 파일 뒷부분 열람이 꼭 필요하면 끝에 [READ_SOURCE: S001]을 적으세요.",
            with_images=True,
        )
        read_matches = re.findall(r"\[READ_SOURCE\s*:\s*([^\]]+)\]", answer)
        expanded = current.get("expanded_evidence", "")
        if read_matches:
            req_ids = re.findall(r"S\d+", " ".join(read_matches))
            fetched = expand_source_tail(
                manifest,
                req_ids,
                evidence_json=evidence,
                existing_expanded=expanded,
            )
            if fetched:
                expanded = fetched
                messages[-1][
                    "agent_action"
                ] = f"📂 [에이전트 도구 실행] {', '.join(req_ids)} 원본의 생략된 뒷부분을 추가 열람했습니다."
        final_analysis = (
            current["analysis"] + "\n\n[슈퍼바이저 지시 추가 분석]\n" + answer
            if current.get("analysis")
            else answer
        )
        return {"analysis": final_analysis, "expanded_evidence": expanded}

    def roundtable(current):
        """필요할 때만 개입하는 온디맨드 슈퍼바이저(On-Demand Supervisor) 노드입니다.

        매 단계마다 호출되어 토큰을 낭비하지 않고,
        1) 심층 모드에서 1차 분석·추가 원문 열람 직후 쟁점을 조율할 때,
        2) 검토 불합격(approved=False) 등 예외 상황에서 어느 워커가 무엇을 보완할지 결정해야 할 때만 개입합니다.
        """
        extra_block = (
            "\n추가 열람한 원본 뒷부분:\n" + clip(current["expanded_evidence"], 2000)
            if current.get("expanded_evidence")
            else ""
        )
        if current.get("review") and not current["review"].get("approved", True):
            default_next = "writer"
            context_block = (
                "\n검토자의 불합격 사유:\n"
                + json.dumps(current["review"], ensure_ascii=False)
                + "\n현재 초안 앞부분:\n"
                + clip(current.get("draft", ""), 2000)
                + "\n[슈퍼바이저 지시] 검토 지적 사항을 해결하기 위해 다음에 호출할 담당자([NEXT_WORKER: writer|organizer|analyst|finish])와 구체적 수정 지시를 2~3문장(250자 이내)으로 짧게 내려 주세요."
            )
        else:
            default_next = "organizer"
            context_block = (
                "\n@자료·실행 결과 분석자님이 찾은 팩트와 부족한 근거를 받아치며, 무엇을 강조하고 무엇을 한계로 뺄지 @보고서 구성 담당님에게 2~3문장(250자 이내)의 짧은 티키타카 대화로만 조율해 주세요. "
                "필요 시 끝에 [NEXT_WORKER: organizer|analyst|writer]를 지정할 수 있습니다. 없는 사실을 만들지 마세요."
            )
        answer = ask(
            "planner",
            "요청:\n"
            + brief
            + "\n처음 기획:\n"
            + clip(current["plan"], 1200)
            + "\n분석자의 보고:\n"
            + clip(current["analysis"], 2200)
            + extra_block
            + context_block,
        )
        decision = parse_supervisor_decision(
            answer,
            default_next=default_next,
            remaining_budget=call_budget - sequence,
        )
        next_target = decision["next_worker"]
        worker_label = ROLES.get(next_target, {}).get("name", "종료")
        messages[-1][
            "agent_action"
        ] = f"🧭 [온디맨드 슈퍼바이저 판단] 다음 담당자 → {worker_label} ({next_target})"
        return {
            "debate_note": decision["task"] or answer,
            "supervisor_task": decision["task"] or answer,
            "next_worker": next_target,
            "supervisor_turns": current.get("supervisor_turns", 0) + 1,
        }

    def organizer(current):
        debate_section = (
            "\n기획·분석 쟁점 티키타카 조율:\n" + clip(current["debate_note"], 1200)
            if current.get("debate_note")
            else ""
        )
        supervisor_section = (
            "\n슈퍼바이저(기획자)의 작업 지시:\n"
            + clip(current["supervisor_task"], 1200)
            if current.get("supervisor_task")
            else ""
        )
        review_section = (
            "\n검토자의 수정 요청:\n"
            + json.dumps(current["review"], ensure_ascii=False)
            if current.get("review")
            else ""
        )
        extra_section = (
            "\n추가 열람한 원본 뒷부분:\n" + clip(current["expanded_evidence"], 2500)
            if current.get("expanded_evidence")
            else ""
        )
        answer = ask(
            "organizer",
            "요청:\n"
            + brief
            + "\n기획:\n"
            + clip(current["plan"], 1600)
            + "\n분석자의 의견:\n"
            + clip(current["analysis"], 3200)
            + debate_section
            + supervisor_section
            + review_section
            + extra_section
            + "\n참고 보고서의 작성 기준:\n"
            + policy
            + "\n첫 문단은 @기획자님·@자료·실행 결과 분석자님에게 어떻게 구성을 잡았는지 답하고 @본문 작성자님에게 핵심만 당부하는 짧은 티키타카 대화(2~3문장, 180자 이내)로 쓰고, 빈 줄 아래에 7개 대항목별 핵심 구성과 근거 ID만 900자 이내로 간결하게 정리하세요. 담당자 의견 자체는 사실의 근거가 아닙니다.",
        )
        ask_match = re.search(r"\[ASK_ANALYST\s*:\s*([^\]]+)\]", answer)
        if ask_match:
            question = ask_match.group(1).strip()
            turns_used = current.get("chat_turns", 0)
            # 분석자 답변(1) + 구성 확정(1) + 본문 작성(1) + 검토(1) = 최소 4회 여유가 있을 때만 되묻기 허용
            if turns_used < max_chat_turns and (call_budget - sequence) >= 4:
                messages[-1][
                    "agent_action"
                ] = f"🔁 [자율 되묻기 {turns_used + 1}/{max_chat_turns}회] @자료·실행 결과 분석자님에게 추가 확인 요청"
                return {
                    "outline": answer,
                    "pending_question": question,
                    "chat_turns": turns_used + 1,
                }
            messages[-1][
                "agent_action"
            ] = f"🛡️ [호출 한도({sequence}/{call_budget}회)·무한루프 차단] 추가 되묻기를 제한하고 본문 작성으로 이동합니다."
        return {"outline": answer, "pending_question": ""}

    def writer(current):
        revision = current.get("revision", 0) + (1 if current.get("draft") else 0)
        extra_section = (
            "\n추가 열람한 원본 뒷부분:\n" + clip(current["expanded_evidence"], 4000)
            if current.get("expanded_evidence")
            else ""
        )
        supervisor_section = (
            "\n슈퍼바이저(기획자)의 중점 지시:\n"
            + clip(current["supervisor_task"], 1200)
            if current.get("supervisor_task")
            else ""
        )
        prompt = (
            "요청:\n"
            + brief
            + "\n구성 담당자의 안:\n"
            + clip(current.get("outline", ""), 3000)
            + "\n분석자의 관찰:\n"
            + clip(current["analysis"], 3200)
            + supervisor_section
            + "\n작성 기준:\n"
            + policy
            + "\n원본 근거:\n"
            + evidence
            + extra_section
            + "\n7개 대항목을 모두 쓰되 자료가 없으면 확인 한계를 짧게 설명하세요. 전체 초안은 12000자 이내로 작성하세요."
        )
        if current.get("draft"):
            prompt += (
                "\n검토자의 수정 요청:\n"
                + json.dumps(current["review"], ensure_ascii=False)
                + "\n이전 초안:\n"
                + current["draft"]
            )
        draft = ask("writer", prompt, with_images=True)
        if len(draft) > 24000:
            raise ValueError(
                "초안이 프로토타입의 검토 분량을 초과했습니다. 내용을 잘라 검토하지 않고 중단합니다."
            )
        return {"draft": draft, "revision": revision}

    def reviewer(current):
        checks = check_report(current["draft"], manifest)
        extra_section = (
            "\n추가 열람한 원본 뒷부분:\n" + clip(current["expanded_evidence"], 4000)
            if current.get("expanded_evidence")
            else ""
        )
        answer = ask(
            "reviewer",
            "요청:\n"
            + brief
            + "\n원본 근거:\n"
            + evidence
            + extra_section
            + "\n참고 작성 기준:\n"
            + policy
            + "\n검토할 초안 전체:\n"
            + current["draft"]
            + "\nPython 구조 검사:\n"
            + json.dumps(checks, ensure_ascii=False)
            + "\n실제 문제가 없을 때만 승인하세요. approved, summary, issues로 반환하세요.",
            with_images=True,
            schema=REVIEW_SCHEMA,
        )
        last_msg = messages[-1] if messages else {}
        if selected and last_msg.get("images_reviewed") is False:
            unreviewed_ids = [s["id"] for s in selected]
            img_warning = (
                f"선택한 이미지({', '.join(unreviewed_ids)})는 현재 검토 모델({settings['reviewer']['model']}) 연결에서 "
                "직접 픽셀 입력을 지원하지 않아 텍스트 근거만으로 검토했습니다. 그래프·이미지 수치는 직접 확인하세요."
            )
            checks["images_reviewed"] = False
            checks["unreviewed_image_ids"] = unreviewed_ids
            checks["image_review_warning"] = img_warning
            checks["note"] = checks["note"] + " " + img_warning
            last_msg["agent_action"] = "⚠️ [텍스트 근거 검토] " + img_warning
        else:
            checks["images_reviewed"] = True
            checks["unreviewed_image_ids"] = []
        review = json.loads(answer)
        if (
            not isinstance(review.get("approved"), bool)
            or not isinstance(review.get("issues"), list)
            or not isinstance(review.get("summary"), str)
        ):
            raise ValueError("검토 응답 형식이 올바르지 않습니다.")
        # 모델이 승인해도 출처 번호·필수 제목 오류가 있으면 프로그램이 승인하지 않습니다.
        if (
            checks["unknown_source_ids"]
            or checks["missing_section_numbers"]
            or checks["missing_citations"]
            or checks["unresolved_control_tags"]
        ):
            review["approved"] = False
            review["issues"].append(
                "Python 구조 검사 오류: " + json.dumps(checks, ensure_ascii=False)
            )
        if review["issues"]:
            review["approved"] = False
        truncated_roles = [m["role"] for m in messages if m.get("output_truncated")]
        if truncated_roles:
            checks["truncated_roles"] = truncated_roles
            review["approved"] = False
            review["issues"].append(
                "출력 한도로 잘린 답변이 있습니다: " + ", ".join(truncated_roles)
            )
        return {"review": review, "checks": checks}

    def route_analyst(current):
        # 평소에는 바로 구성(organizer)으로 직진하고,
        # 원본 추가 열람([READ_SOURCE])이 발생했거나 심층 조율 요청이 있고 잔여 호출 한도가 충분할 때만 온디맨드 슈퍼바이저가 개입합니다.
        needs_supervisor = bool(debate or current.get("expanded_evidence"))
        if (
            needs_supervisor
            and not current.get("debate_note")
            and (call_budget - sequence) >= 4
        ):
            return "roundtable"
        return "organizer"

    def route_supervisor(current):
        target = current.get("next_worker") or "organizer"
        if target in ALLOWED_WORKERS:
            return target
        return "organizer"

    def route_organizer(current):
        if current.get("pending_question"):
            return "analyst"
        return "writer"

    def route_review(current):
        if current["review"]["approved"]:
            return "finish"
        if current["revision"] < max_revisions:
            # 검토 불합격 시 남은 호출 예산이 3회 이상(슈퍼바이저 1 + 워커 수정 1 + 재검토 1)이면 모드 구분 없이 온디맨드 슈퍼바이저가 개입합니다.
            if current.get("supervisor_turns", 0) < 2 and (call_budget - sequence) >= 3:
                return "roundtable"
            # 남은 호출 예산이 2회뿐이면 슈퍼바이저 호출을 생략하고 바로 작성자(writer)가 수정합니다.
            if (call_budget - sequence) >= 2:
                return "writer"
        if (
            current["revision"] < max_revisions
            and (call_budget - sequence) < 2
            and messages
        ):
            messages[-1][
                "agent_action"
            ] = f"🛡️ [호출 한도({sequence}/{call_budget}회) 보호] 잔여 호출 한도 내 안전 종료를 위해 재작성을 생략합니다."
        return "finish"

    def finish(current):
        has_draft = bool(current.get("draft"))
        review_data = current.get("review")
        if not has_draft or not isinstance(review_data, dict):
            # 초안 작성이나 검토가 완료되기 전에 슈퍼바이저가 finish를 선택한 경우:
            # KeyError를 내지 않고 미완료(needs_review) 상태와 안내 사유를 명시적으로 기록합니다.
            early_reason = (
                current.get("supervisor_task")
                or "본문 작성 및 검토가 완료되기 전에 회의가 조기 종료되었습니다."
            )
            review_data = {
                "approved": False,
                "summary": "초안 작성 또는 검토 전 조기 종료되어 미완료(needs_review) 상태로 처리되었습니다.",
                "issues": [early_reason],
            }
            draft_text = current.get("draft") or ""
            checks_data = current.get("checks") or check_report(draft_text, manifest)
            return {
                "status": "needs_review",
                "draft": draft_text,
                "review": review_data,
                "checks": checks_data,
                "call_count": sequence,
                "max_calls": call_budget,
            }
        return {
            "status": "completed" if review_data.get("approved") else "needs_review",
            "call_count": sequence,
            "max_calls": call_budget,
        }

    # 단일 통합 그래프: 평소에는 직진하고, 추가 열람·되묻기·검토 불합격 등 필요할 때만 온디맨드 슈퍼바이저(roundtable)가 개입합니다.
    graph = StateGraph(MeetingState)
    nodes = [
        ("planner", planner),
        ("analyst", analyst),
        ("roundtable", roundtable),
        ("organizer", organizer),
        ("writer", writer),
        ("reviewer", reviewer),
        ("finish", finish),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "analyst")
    graph.add_conditional_edges(
        "analyst",
        route_analyst,
        {"roundtable": "roundtable", "organizer": "organizer"},
    )
    graph.add_conditional_edges(
        "roundtable",
        route_supervisor,
        {
            "analyst": "analyst",
            "organizer": "organizer",
            "writer": "writer",
            "finish": "finish",
        },
    )
    graph.add_conditional_edges(
        "organizer",
        route_organizer,
        {"analyst": "analyst", "writer": "writer"},
    )
    graph.add_edge("writer", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        route_review,
        {"roundtable": "roundtable", "writer": "writer", "finish": "finish"},
    )
    graph.add_edge("finish", END)
    persist(state)
    try:
        for update in graph.compile().stream(
            state, stream_mode="updates", config={"recursion_limit": 20}
        ):
            for values in update.values():
                state.update(values)
            state["messages"] = list(messages)
            state["call_count"] = sequence
            persist(state)
    except Exception as error:
        state.update(
            status="failed",
            messages=list(messages),
            call_count=sequence,
            error=str(error),
        )
        persist(state)
        raise
    draft_out = state.get("draft", "")
    checks = dict(
        state.get("checks") or check_report(draft_out, manifest),
        meeting_status=state["status"],
        review=state["review"],
        call_count=sequence,
        max_calls=call_budget,
    )
    (meeting_dir / "report.md").write_text(draft_out, encoding="utf-8")
    (meeting_dir / "checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 초안이 생성된 경우에만 작업 폴더의 report.md를 갱신하여, 초안 전 조기 종료 시 이전 정상 보고서가 빈 문자열로 덮어써지지 않게 보호합니다.
    if draft_out or not (run_dir / "report.md").exists():
        (run_dir / "report.md").write_text(draft_out, encoding="utf-8")
    (run_dir / "checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_out, checks, meeting_dir
