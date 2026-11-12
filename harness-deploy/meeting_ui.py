"""담당자 설정과 회의 대화를 보여 주는 Streamlit 화면입니다."""

import html
import json
import re
import time
from pathlib import Path

import streamlit as st

from roles import ROLES, load_settings, provider_label, save_settings
from workflow import run_meeting

AVATARS = {
    "planner": "🧭",
    "analyst": "🔎",
    "organizer": "🗂️",
    "writer": "✍️",
    "reviewer": "🔍",
}

ROLE_BADGES = {
    "planner": ("기획 리드", "#2563eb", "#eff6ff"),
    "analyst": ("팩트·로그 분석", "#0d9488", "#f0fdfa"),
    "organizer": ("구조 스토리보드", "#7c3aed", "#f5f3ff"),
    "writer": ("초안 집필", "#d97706", "#fffbeb"),
    "reviewer": ("품질·근거 검수", "#e11d48", "#fff1f2"),
}


def split_chat_bubbles(text):
    """담당자 발언에서 '짧은 핵심 티키타카 대화(2~3문장)'와 '상세 참고 노트'를 분리합니다.

    대화창에는 핵심 대화 문장만 깔끔하게 말풍선으로 보여 주고,
    아래에 이어지는 긴 불릿 목록이나 구성표는 접기(expander)에 넣어 대화가 길어지지 않게 합니다.
    에이전트 제어용 태그([READ_SOURCE: ...], [ASK_ANALYST: ...])는 화면 본문에서 제거합니다.
    """
    cleaned = re.sub(
        r"\[(?:READ_SOURCE|ASK_ANALYST|NEXT_WORKER)\s*:[^\]]*\]", "", text or ""
    ).strip()
    if not cleaned:
        return "", ""
    parts = re.split(r"\n\s*\n|\n(?=[-#*1-9])", cleaned, maxsplit=1)
    if len(parts) > 1:
        lead, detail = parts[0].strip(), parts[1].strip()
        if not lead.startswith(("#", "-", "*", "1.")):
            return lead, detail
    # 줄바꿈 없이 한 덩어리로 길게 온 경우에도 앞 2문장(핵심 대화)과 나머지를 나눕니다.
    if len(cleaned) > 240:
        sentences = re.split(r"(?<=[.!?다요죠])\s+", cleaned, maxsplit=2)
        if len(sentences) >= 3:
            lead = (sentences[0] + " " + sentences[1]).strip()
            detail = sentences[2].strip()
            return lead, detail
    return cleaned, ""


def format_mentions(text):
    """@담당자님 형태의 호칭을 눈에 잘 띄는 굵은 배지 스타일로 강조합니다."""
    escaped = html.escape(text).replace("\n", "<br>")
    return re.sub(
        r"(@[가-힣·\s]{2,14}?님)",
        r'<span style="background:#dbeafe;color:#1d4ed8;padding:1px 7px;border-radius:999px;font-weight:700;font-size:0.92em;">\1</span>',
        escaped,
    )


def render_talk_bubble(role, text):
    """동료에게 말을 건네는 첫 번째 대화형 말풍선을 그립니다."""
    label, border_color, bg_color = ROLE_BADGES.get(
        role, ("팀원", "#475569", "#f8fafc")
    )
    highlighted = format_mentions(text)
    st.markdown(
        f'<div style="background:{bg_color};border-left:4px solid {border_color};'
        f"padding:10px 14px;border-radius:0 12px 12px 12px;margin-bottom:8px;"
        f'color:#0f172a;line-height:1.6;font-size:0.95rem;">'
        f'<span style="font-size:0.76rem;font-weight:700;color:{border_color};'
        f'display:block;margin-bottom:3px;">💬 {label} 티키타카 발언</span>'
        f"{highlighted}</div>",
        unsafe_allow_html=True,
    )


def review_preview(text):
    """구조화된 검토 JSON에서 지금까지 도착한 설명 문장만 보여 줍니다."""
    match = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not match:
        return "검토 결과를 정리하고 있습니다…"
    fragment = match.group(1)
    try:
        return json.loads('"' + fragment + '"')
    except json.JSONDecodeError:
        # JSON 이스케이프가 도착하는 도중이면 완료된 앞부분만 표시합니다.
        return fragment.rsplit("\\", 1)[0].replace("\\n", "\n")


def show_body(message):
    """완료된 발언은 핵심 티키타카 말풍선만 눈에 띄게 표시하고 상세 노트는 접어 둡니다."""
    content = message["content"]
    role = message["role"]
    if role == "reviewer":
        try:
            review = json.loads(content)
            render_talk_bubble("reviewer", review["summary"])
            for issue in review["issues"]:
                st.markdown("- " + issue)
            if review["approved"]:
                st.caption("AI 승인 · ✅ 모든 근거와 7개 대항목 구성을 확인했습니다.")
            else:
                st.caption(
                    "수정·확인 요청 · 🛠️ @본문 작성자님에게 보완을 요청했습니다."
                )
        except (ValueError, KeyError):
            st.markdown(content)
    elif role == "writer":
        headings = re.findall(r"(?m)^##\s+(.+)$", content)
        heading_preview = (
            f"총 {len(headings)}개 대항목" if headings else "7개 대항목 초안"
        )
        render_talk_bubble(
            "writer",
            f"@보고서 구성 담당님, 잡아주신 구성대로 {heading_preview} 본문 집필을 마쳤습니다! "
            "@근거·품질 검토자님, 출처 표기와 한계점 확인 부탁드려요.",
        )
        with st.expander("작성·수정한 본문 확인"):
            st.markdown(content)
    else:
        lead, detail = split_chat_bubbles(content)
        if detail:
            render_talk_bubble(role, lead)
            with st.expander("📋 상세 정리 노트 펼쳐보기"):
                st.markdown(detail)
        else:
            render_talk_bubble(role, lead)
    if message.get("agent_action"):
        st.caption(f"🤖 에이전트 자율 판단 · {message['agent_action']}")
    elif message.get("warning"):
        st.caption(f"⚠️ {message['warning']}")


def usage_text(message):
    usage = message.get("usage")
    call_badge = ""
    if message.get("call_index") and message.get("max_calls"):
        call_badge = f"호출 {message['call_index']}/{message['max_calls']}회 · "
    if usage is None:
        return f"{call_badge}토큰 사용량 미제공"
    return (
        f"{call_badge}측정 사용량: 입력 {usage.get('input_tokens', '?')} / "
        f"출력 {usage.get('output_tokens', '?')} 토큰 · {message.get('seconds', '?')}초"
    )


def render_model_settings():
    """화면에서 역할별 모델을 지정합니다. 저장 버튼을 눌러야 실제 호출에 반영됩니다."""
    settings = load_settings()
    with st.expander("⚙️ 담당자별 모델 설정"):
        st.caption(
            "로컬 기본 배정: 기획·슈퍼바이저·검토 Qwen3 4B / 작업 Qwen3 1.7B. Ollama가 순서대로 실행합니다. 로컬 think는 꺼져 있으며 추론 수준 선택은 클라우드 모델용입니다."
        )
        edited = {}
        for role, definition in ROLES.items():
            current_model = settings[role]["model"]
            engine_name = provider_label(current_model)
            st.markdown(
                f"**{AVATARS.get(role, '🤖')} {definition['name']}** · `{engine_name}`"
            )
            model = st.text_input(
                "모델 이름", value=current_model, key="role_model_" + role
            )
            reasoning = st.selectbox(
                "추론 수준",
                ["low", "medium", "high"],
                index=["low", "medium", "high"].index(settings[role]["reasoning"]),
                key="role_reasoning_" + role,
            )
            edited[role] = {"model": model.strip(), "reasoning": reasoning}
        if st.button("담당자 모델 설정 저장", use_container_width=True):
            try:
                save_settings(edited)
                st.success("저장했습니다. 다음 모델 호출부터 적용됩니다.")
            except ValueError as error:
                st.error(str(error))


def show_message(message):
    """모델의 작업 결과를 담당자 이름과 함께 표시합니다. 모델의 내부 추론 과정은 아닙니다."""
    role = message["role"]
    badge_label, badge_color, _ = ROLE_BADGES.get(
        role, ("담당자", "#475569", "#f8fafc")
    )
    engine_name = provider_label(message.get("model", ""))
    with st.chat_message(message["name"], avatar=AVATARS.get(role, "🤖")):
        st.caption(
            f"{message['name']} ({badge_label}) · {engine_name} · {message['model']}"
        )
        show_body(message)
        st.caption(usage_text(message))


def render_meeting_panel(manifest, run_dir, title, purpose, images, consent):
    """회의를 시작하거나 저장된 대화를 표시합니다. 완료 시 (보고서, 검사 결과)를 반환합니다."""
    st.markdown(
        """
        <div style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#f8fafc;padding:16px 18px;border-radius:14px;margin-bottom:14px;border:1px solid #334155;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                    <span style="background:#3b82f6;color:#fff;font-size:0.72rem;font-weight:700;padding:3px 8px;border-radius:999px;letter-spacing:0.04em;">LIVE AI STUDIO</span>
                    <h4 style="margin:6px 0 2px 0;color:#f8fafc;font-size:1.1rem;">💬 역할별 티키타카 회의실 · LangGraph</h4>
                </div>
                <div style="font-size:0.82rem;color:#cbd5e1;">
                    🧭 기획자 ↔ 🔎 분석자 ↔ 🗂️ 구성 ↔ ✍️ 작성 ↔ 🔍 검토
                </div>
            </div>
            <p style="margin:6px 0 0 0;font-size:0.82rem;color:#94a3b8;">
                평소에는 기획 → 분석 → 구성 → 작성 → 검토 순서로 빠르게 직진하고, 생략된 파일 추가 열람·팀원 간 되묻기·검토 불합격 등 쟁점이 생겼을 때만 온디맨드 슈퍼바이저가 개입해 호출 한도(Call Budget) 안에서 해결합니다.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    col_rev, col_budget = st.columns([1.0, 1.2])
    with col_rev:
        revisions = st.selectbox(
            "검토 후 수정 횟수", [0, 1], index=1, key="meeting_revisions"
        )
    with col_budget:
        max_calls = st.selectbox(
            "최대 모델 호출 한도 (무한루프 차단)",
            [5, 6, 7, 8, 9, 10, 12],
            index=3,
            key="meeting_max_calls",
            help="설정된 횟수를 초과해 모델을 호출하지 못하도록 강제 차단하며, 남은 한도가 부족하면 선택적 되묻기·슈퍼바이저 개입을 건너뛰고 본문 작성과 최종 검수를 우선 완료합니다.",
        )
    start = st.button(
        "역할별 회의로 보고서 작성",
        type="primary",
        disabled=not consent
        or not manifest["sources"]
        or not st.session_state.get("ollama_ready", False),
        use_container_width=True,
    )
    progress = st.empty()
    conversation = st.container()
    if start:
        active = {}

        def event_handler(event):
            if event["type"] == "started":
                budget_info = (
                    f" · 호출 {event['call_index']}/{event['max_calls']}회"
                    if event.get("call_index") and event.get("max_calls")
                    else ""
                )
                progress.info(
                    f"{AVATARS.get(event['role'], '🤖')} {ROLES[event['role']]['name']} 발언 작성 중 · {event['model']}{budget_info}"
                )
                with conversation:
                    with st.chat_message(
                        ROLES[event["role"]]["name"],
                        avatar=AVATARS.get(event["role"], "🤖"),
                    ):
                        st.caption(f"{ROLES[event['role']]['name']} · {event['model']}")
                        active["body"] = st.empty()
                        active["status"] = st.empty()
                        active["body"].markdown(
                            "동료들의 의견을 확인하고 답변을 작성하고 있습니다…"
                        )
                        active["status"].caption("응답 대기 중")
                active["last_update"] = 0
            elif event["type"] == "delta":
                # 실시간 스트리밍 중에도 긴 본문 대신 핵심 대화 문장만 말풍선에 깔끔하게 표시합니다.
                now = time.monotonic()
                if now - active["last_update"] >= 0.05:
                    if event["role"] == "reviewer":
                        text = review_preview(event["text"])
                        active["body"].markdown(text + " ▌")
                    elif event["role"] == "writer":
                        headings = re.findall(r"(?m)^##\s+(.+)$", event["text"])
                        current_sec = (
                            headings[-1].strip() if headings else "표지 및 목차"
                        )
                        active["body"].markdown(
                            f"✍️ @보고서 구성 담당님 구성안에 맞춰 초안을 집필 중입니다… (`{current_sec}` 작성 중 ▌)"
                        )
                    else:
                        lead, detail = split_chat_bubbles(event["text"])
                        if detail:
                            active["body"].markdown(
                                lead + "\n\n*(📋 세부 근거·구성 노트 정리 중… ▌)*"
                            )
                        else:
                            active["body"].markdown(lead + " ▌")
                    active["status"].caption("실시간 발언 수신 중")
                    active["last_update"] = now
            elif event["type"] == "completed":
                with active["body"].container():
                    show_body(event)
                active["status"].caption(usage_text(event))

        try:
            report, checks, directory = run_meeting(
                manifest,
                run_dir,
                title,
                purpose,
                images,
                max_revisions=revisions,
                on_event=event_handler,
                stream=True,
                max_calls=max_calls,
            )
            progress.empty()
            if checks["meeting_status"] == "completed":
                st.success("회의와 검토를 마쳤습니다. 제출 전 실제 근거를 확인하세요.")
            else:
                st.warning(
                    "수정 횟수에 도달했으며 미해결 의견이 남았습니다. 아래 초안과 검토 의견을 확인하세요."
                )
            return report, checks
        except Exception as error:
            progress.empty()
            if active.get("status"):
                active["status"].caption(
                    "응답이 중단되었습니다. 위의 일부 답변은 완료 결과가 아닙니다."
                )
            st.error(str(error))
    latest = Path(run_dir) / "latest_meeting.json"
    if latest.exists():
        directory = Path(json.loads(latest.read_text(encoding="utf-8"))["directory"])
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        st.caption(f"저장된 회의 상태: {state['status']} · {directory.name}")
        if not start:
            with st.expander("저장된 담당자 대화 보기", expanded=True):
                for message in state.get("messages", []):
                    show_message(message)
        st.download_button(
            "회의 기록 JSON 다운로드",
            (directory / "state.json").read_bytes(),
            file_name="meeting_state.json",
            mime="application/json",
            use_container_width=True,
        )
    return None
