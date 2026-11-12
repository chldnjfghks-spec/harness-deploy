"""화면 담당: 업로드, 실행 버튼, 자료 확인, 보고서 생성과 다운로드를 연결합니다.
Streamlit은 버튼을 누르는 등 화면 상태가 바뀔 때 이 파일을 위에서부터 다시 실행합니다.
그래서 작업 중인 자료는 st.session_state(화면의 작업 기억 공간)에 보관합니다.
읽는 순서: 이 파일 → engine.py → execution.py → storyboard.py → presentation_export.py."""

import base64
import os
from datetime import datetime
import hashlib
import html
import io
import json
from pathlib import Path
import uuid
import zipfile
import streamlit as st
import importlib
import engine as engine_backend
import presentation_export as presentation_backend
import storyboard as storyboard_backend

# 화면을 다시 그려도 다른 파일의 수정 내용이 자동 반영되지 않을 수 있어 관련 모듈을 다시 읽습니다.
importlib.reload(engine_backend)
importlib.reload(storyboard_backend)
importlib.reload(presentation_backend)
from engine import ROOT, collect, generate
from presentation_export import export_deck
from execution import prepare_project, entries, execute_project
from entry_inspection import inspect_entries, inspect_entry
from meeting_ui import render_meeting_panel
from roles import ROLES, load_settings, provider_label
from workflow import check_report
from cloud_config import workspace, render_connection_panel
import sys

# 1. 화면 기본 설정과 웹사이트형 대시보드 스타일·상단 헤더를 표시합니다.
st.set_page_config(page_title="프로젝트 보고서 하네스", page_icon="📑", layout="wide")
WORKSPACE_ROOT = workspace(ROOT, st.session_state)

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem !important;
        padding-bottom: 3rem !important;
        max-width: 1440px !important;
    }
    .harness-topbar {
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 65%, #1d4ed8 100%);
        color: #f8fafc;
        padding: 20px 26px;
        border-radius: 16px;
        margin-bottom: 18px;
        box-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.18);
        border: 1px solid rgba(255, 255, 255, 0.1);
    }
    .harness-topbar-top {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
    }
    .harness-brand-badge {
        display: inline-block;
        background: rgba(59, 130, 246, 0.25);
        border: 1px solid rgba(147, 197, 253, 0.45);
        color: #bfdbfe;
        font-size: 0.74rem;
        font-weight: 700;
        padding: 3px 10px;
        border-radius: 999px;
        letter-spacing: 0.05em;
        margin-bottom: 6px;
    }
    .harness-title {
        margin: 0;
        font-size: 1.55rem;
        font-weight: 800;
        color: #ffffff;
        letter-spacing: -0.02em;
    }
    .harness-subtitle {
        margin: 4px 0 0 0;
        font-size: 0.88rem;
        color: #cbd5e1;
    }
    .harness-steps {
        display: flex;
        gap: 10px;
        margin-top: 16px;
        flex-wrap: wrap;
    }
    .harness-step {
        flex: 1;
        min-width: 180px;
        background: rgba(15, 23, 42, 0.55);
        border: 1px solid rgba(148, 163, 184, 0.25);
        border-radius: 11px;
        padding: 10px 14px;
        font-size: 0.84rem;
        color: #e2e8f0;
    }
    .harness-step.done {
        background: rgba(16, 185, 129, 0.16);
        border-color: rgba(52, 211, 153, 0.55);
        color: #d1fae5;
    }
    .harness-step.active {
        background: rgba(59, 130, 246, 0.24);
        border-color: rgba(96, 165, 250, 0.75);
        color: #ffffff;
    }
    .harness-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 16px 18px;
        margin-bottom: 14px;
        box-shadow: 0 2px 6px rgba(15, 23, 42, 0.03);
    }
    div[data-testid="stTabs"] button[data-baseweb="tab"] {
        font-size: 0.96rem !important;
        font-weight: 700 !important;
        padding: 10px 18px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def sync_edited_report():
    """편집기(st.text_area)에서 사용자가 본문을 수정하면 즉시 report.md에 저장하고 구조 검사를 다시 계산합니다.

    수정 후에도 예전 AI 승인이나 예전 구조 검사 통과 결과가 그대로 남지 않도록 갱신합니다.
    """
    run_dir_str = st.session_state.get("run_dir")
    manifest_data = st.session_state.get("manifest")
    if not run_dir_str or not manifest_data:
        return
    editor_key = "report_editor_" + run_dir_str
    if editor_key not in st.session_state:
        return
    edited_text = st.session_state[editor_key]
    if edited_text != st.session_state.get("report"):
        fresh_checks = check_report(edited_text, manifest_data)
        prev_checks = dict(st.session_state.get("checks") or {})
        updated_checks = dict(prev_checks, **fresh_checks)
        updated_checks["manual_edited"] = True
        updated_checks["meeting_status"] = "needs_review"
        updated_checks["review"] = {
            "approved": False,
            "summary": "사용자가 본문을 직접 수정하여 이전 AI 검토 승인이 해제되었습니다. 구조 검사는 현재 수정 본문 기준으로 다시 계산되었습니다.",
            "issues": ["본문 직접 수정 후 AI 재검토 미실행"],
        }
        run_path = Path(run_dir_str)
        if run_path.exists():
            (run_path / "report.md").write_text(edited_text, encoding="utf-8")
            (run_path / "checks.json").write_text(
                json.dumps(updated_checks, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        st.session_state.report = edited_text
        st.session_state.checks = updated_checks
        st.session_state.pop("deck_key", None)


sync_edited_report()

# 왼쪽 사이드바: 꼭 필요한 '저장된 작업 불러오기'와 'AI 팀원 모델 배정 현황 요약'만 심플하게 표시합니다.
with st.sidebar:
    st.markdown(
        """
        <div style="background:#0f172a;color:#f8fafc;padding:14px 16px;border-radius:12px;margin-bottom:12px;">
            <div style="font-size:0.72rem;color:#93c5fd;font-weight:700;letter-spacing:0.06em;">HARNESS WORKSPACE</div>
            <div style="font-size:1.05rem;font-weight:800;margin-top:2px;">📑 프로젝트 & AI 팀</div>
            <div style="font-size:0.78rem;color:#94a3b8;margin-top:4px;">저장된 작업 불러오기 · 역할별 모델 배정</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader("🗂️ 저장된 작업 불러오기")
    saved_runs = sorted(
        [
            p
            for p in WORKSPACE_ROOT.glob("*")
            if (p / "report.md").exists() and (p / "evidence.json").exists()
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if saved_runs:
        chosen = st.selectbox("이전 작업", saved_runs, format_func=lambda p: p.name)
        if st.button("이 작업 불러오기", use_container_width=True):
            st.session_state.manifest = json.loads(
                (chosen / "evidence.json").read_text(encoding="utf-8")
            )
            st.session_state.run_dir = str(chosen)
            st.session_state.report = (chosen / "report.md").read_text(encoding="utf-8")
            st.session_state.checks = json.loads(
                (chosen / "checks.json").read_text(encoding="utf-8")
            )
            st.session_state.pop("deck_dir", None)
            st.session_state.pop("deck_key", None)
            st.session_state["report_editor_" + str(chosen)] = st.session_state.report
    else:
        st.caption(
            "현재 세션에 저장된 이전 작업이 없습니다. 새 세션이나 서버 재시작 후에는 작업이 유지되지 않을 수 있습니다."
        )
    if st.session_state.get("run_dir"):
        st.caption(f"현재 열린 작업: `{Path(st.session_state.run_dir).name}`")
    st.divider()
    render_connection_panel(load_settings())
    st.divider()
    st.subheader("🤖 AI 팀원 모델 배정 현황")
    current_settings = st.session_state.get("effective_settings") or load_settings()
    role_icons = {
        "planner": "🧭",
        "analyst": "🔎",
        "organizer": "🗂️",
        "writer": "✍️",
        "reviewer": "🔍",
        "slides": "📊",
    }
    for role_key, role_def in ROLES.items():
        model_name = current_settings[role_key]["model"]
        engine_badge = provider_label(model_name)
        st.markdown(
            f"- {role_icons.get(role_key, '🤖')} **{role_def['name']}**: `{model_name}` ({engine_badge})"
        )

manifest = st.session_state.get("manifest")
has_report = bool(st.session_state.get("report"))
has_deck = bool(st.session_state.get("deck_dir"))
active_project_name = (
    manifest.get("title") or Path(st.session_state.get("run_dir", "새 프로젝트")).name
    if manifest
    else "업로드 대기 중"
)
step1_cls = "done" if manifest else "active"
step2_cls = "done" if has_report else ("active" if manifest else "")
step3_cls = "done" if has_deck else ("active" if has_report else "")

st.markdown(
    f"""
    <div class="harness-topbar">
        <div class="harness-topbar-top">
            <div>
                <span class="harness-brand-badge">AI RESEARCH & DECK STUDIO · v8</span>
                <h1 class="harness-title">프로젝트를 넣으면, 보고서 초안으로</h1>
                <p class="harness-subtitle">작은 모델 역할별 티키타카 회의 · LangGraph · 장수 제한 없이 근거에 맞춘 PPTX / PDF 생성</p>
            </div>
            <div style="text-align:right;background:rgba(15,23,42,0.45);padding:10px 16px;border-radius:12px;border:1px solid rgba(255,255,255,0.12);">
                <div style="font-size:0.74rem;color:#93c5fd;font-weight:700;">현재 워크스페이스</div>
                <div style="font-size:0.98rem;font-weight:700;color:#ffffff;margin-top:2px;">{html.escape(str(active_project_name))}</div>
            </div>
        </div>
        <div class="harness-steps">
            <div class="harness-step {step1_cls}">
                <strong>STEP 1 · 자료 수집 & 코드 실행</strong><br>
                <span>{"✅ 자료 수집 완료 (" + str(len(manifest["sources"])) + "개 근거)" if manifest else "🔵 ZIP 업로드 후 자료 조사를 시작하세요"}</span>
            </div>
            <div class="harness-step {step2_cls}">
                <strong>STEP 2 · AI 팀 회의실 & 초안 편집</strong><br>
                <span>{"✅ 보고서 초안 준비 완료" if has_report else ("🔵 역할별 티키타카 회의로 초안 작성" if manifest else "⚪ 자료 조사 후 진행")}</span>
            </div>
            <div class="harness-step {step3_cls}">
                <strong>STEP 3 · PPTX · PDF 슬라이드 발행</strong><br>
                <span>{"✅ 슬라이드 생성 완료" if has_deck else ("🔵 가변 분량 PPTX·PDF 변환 대기" if has_report else "⚪ 초안 완성 후 진행")}</span>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# 2. 메인 화면을 3개의 전문 워크스페이스 탭으로 구성합니다.
tab_data, tab_meeting, tab_export = st.tabs(
    [
        "📁 1. 자료 수집 · 코드 실행",
        "💬 2. AI 팀 회의실 · 초안 편집 (좌우 분할)",
        "📊 3. PPTX · PDF 슬라이드 스튜디오",
    ]
)

with tab_data:
    up_col, guide_col = st.columns([1.35, 1.0], gap="large")
    with up_col:
        st.subheader("📦 새 프로젝트 ZIP 업로드")
        upload = st.file_uploader(
            "프로젝트 ZIP을 놓아 주세요",
            type=["zip"],
            help="100MB 이하. 가상환경·모델 가중치·대용량 원본 데이터는 제외하세요.",
        )
        if upload:
            data = upload.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            if st.session_state.get("digest") != digest:
                for key in [
                    "manifest",
                    "report",
                    "checks",
                    "run_dir",
                    "deck_dir",
                    "deck_key",
                ]:
                    st.session_state.pop(key, None)
                st.session_state.digest = digest
            if st.button("1. 자료 조사", type="primary", use_container_width=True):
                try:
                    run = WORKSPACE_ROOT / (
                        datetime.now().strftime("%Y%m%d_%H%M%S")
                        + "_"
                        + uuid.uuid4().hex[:8]
                    )
                    with st.spinner("ZIP의 자료를 읽고 있습니다…"):
                        st.session_state.manifest = collect(data, run)
                        prepare_project(data, run)
                    st.session_state.run_dir = str(run)
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    with guide_col:
        st.markdown(
            """
            <div class="harness-card" style="background:#f8fafc;">
                <h4 style="margin:0 0 8px 0;font-size:1rem;color:#0f172a;">💡 워크스페이스 이용 안내</h4>
                <ol style="margin:0;padding-left:18px;font-size:0.88rem;color:#334155;line-height:1.7;">
                    <li><strong>자료 조사</strong>: ZIP 안의 코드·노트북·문서·내장 이미지를 근거 ID(<code>S001</code> 등)로 수집합니다.</li>
                    <li><strong>코드 실제 실행</strong>: 선택한 노트북/파이썬 파일을 실행해 그래프와 최신 로그를 추가 수집합니다.</li>
                    <li><strong>AI 팀 회의실 탭</strong>: 기획·분석·구성·작성·검토 담당자의 티키타카 대화와 초안을 좌우 화면에서 동시에 확인합니다.</li>
                </ol>
            </div>
            """,
            unsafe_allow_html=True,
        )

    manifest = st.session_state.get("manifest")
    if manifest:
        st.divider()
        exec_col, stat_col = st.columns([1.25, 1.0], gap="large")
        with exec_col:
            st.subheader("⚡ 코드 실행 · 그래프 자동 수집")
            if os.environ.get("HARNESS_ALLOW_CODE_EXECUTION", "0") != "1":
                st.info(
                    "배포본은 업로드 코드 직접 실행을 꺼두었습니다. ZIP의 코드와 저장된 출력은 보고서 자료로 읽습니다."
                )
            candidates, recommended = inspect_entries(st.session_state.run_dir)
            if candidates:
                st.caption(
                    "ZIP 안의 코드를 실행하지 않고 역할을 먼저 확인합니다. 실행하면 학습·외부 호출도 실제로 수행될 수 있으며 패키지는 자동 설치하지 않습니다."
                )
                st.dataframe(
                    [
                        {
                            "파일": item["entry"],
                            "코드에서 확인한 역할": item["kind"],
                            "추천": "추천 후보" if item["entry"] == recommended else "",
                            "누락 프로젝트 모듈": ", ".join(item["missing_modules"]),
                            "확인할 입력 파일": ", ".join(item["missing_inputs"]),
                        }
                        for item in candidates
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
                if not recommended:
                    st.info(
                        "바로 추천할 그래프 실행 파일을 찾지 못했습니다. 역할과 누락 항목을 확인해 선택하거나, 실행하지 않고 수집 자료로 보고서를 작성할 수 있습니다."
                    )
                by_entry = {item["entry"]: item for item in candidates}
                options = list(by_entry)
                entry = st.selectbox(
                    "실행할 노트북 또는 Python 파일",
                    options,
                    index=options.index(recommended) if recommended else None,
                    placeholder="파일의 역할과 누락 항목을 확인한 뒤 선택하세요",
                    format_func=lambda value: value
                    + " · "
                    + by_entry[value]["kind"]
                    + (" · 추천 후보" if value == recommended else ""),
                    key="execution_entry_" + st.session_state.run_dir,
                )
                working_dir = None
                blocked_entry = False
                if entry:
                    detail = by_entry[entry]
                    working_dir = st.selectbox(
                        "실행 기준 폴더 (상대 데이터 경로의 기준)",
                        detail["cwd_options"],
                        index=detail["cwd_options"].index(detail["cwd"]),
                        key="execution_cwd_" + st.session_state.run_dir + entry,
                        format_func=lambda value: (
                            "ZIP 최상위 폴더" if value == "." else value
                        ),
                    )
                    detail = inspect_entry(
                        Path(st.session_state.run_dir) / "project", entry, working_dir
                    )
                    st.write("**역할:** " + detail["kind"])
                    if detail["reasons"]:
                        st.caption(" · ".join(detail["reasons"]))
                    for notice in detail["notices"]:
                        st.warning(notice)
                    if detail["missing_modules"]:
                        blocked_entry = True
                        st.error(
                            "필요한 프로젝트 모듈이 ZIP에 없습니다: "
                            + ", ".join(detail["missing_modules"])
                            + ". src 등 해당 소스 폴더를 포함해 프로젝트 전체를 다시 압축해 주세요."
                        )
                    if detail["missing_inputs"]:
                        blocked_entry = True
                        st.error(
                            "현재 실행 기준 폴더에서 찾지 못한 입력 파일: "
                            + ", ".join(detail["missing_inputs"])
                            + ". 데이터가 포함되어 있는지, 기준 폴더가 맞는지 확인하세요."
                        )
                    with st.expander("실행 전 확인한 파일·모듈 상세"):
                        st.write("프로젝트 import: ", detail["local_imports"])
                        st.write("입력 경로: ", detail["input_paths"])
                        st.write("사용 라이브러리: ", detail["imports"])
                        st.caption(
                            "정적 검사이며 동적 경로·다른 파일의 내부 동작·선택한 Python 환경의 패키지 설치까지 보증하지 않습니다. 작업 복사본도 보안 샌드박스는 아닙니다."
                        )
                python_path = st.text_input(
                    "실행할 Python 경로",
                    value=sys.executable,
                    help="프로젝트 가상환경의 .venv/Scripts/python.exe를 지정하면 설치된 라이브러리를 사용할 수 있습니다.",
                )
                timeout = st.number_input(
                    "실행 제한 시간(초)",
                    min_value=10,
                    max_value=3600,
                    value=180,
                    step=10,
                )
                if st.button(
                    "코드 실행하고 그래프 수집",
                    type="primary",
                    disabled=not entry
                    or blocked_entry
                    or os.environ.get("HARNESS_ALLOW_CODE_EXECUTION", "0") != "1",
                    use_container_width=True,
                ):
                    try:
                        with st.spinner("코드를 실행하고 그래프를 수집 중입니다…"):
                            execution = execute_project(
                                manifest,
                                st.session_state.run_dir,
                                entry,
                                python_path,
                                int(timeout),
                                working_dir=working_dir,
                            )
                        st.session_state.pop("deck_key", None)
                        st.session_state.pop("report", None)
                        for prefix in ["source_images_", "deck_images_"]:
                            st.session_state.pop(
                                prefix + st.session_state.run_dir, None
                            )
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            else:
                st.info(
                    "이전 작업에는 실행용 원본이 없습니다. ZIP을 다시 넣고 자료 조사를 눌러 주세요."
                )
            for execution in manifest.get("executions", [])[-3:]:
                message = f"{execution['entry']} · {execution['status']} · 이미지 {len(execution['images'])}개"
                (st.success if execution["status"] == "success" else st.warning)(
                    message
                )
                with st.expander("실행 로그 " + Path(execution["directory"]).name):
                    st.code(
                        Path(execution["directory"], "execution.log").read_text(
                            encoding="utf-8"
                        )
                    )

        sources = manifest["sources"]
        pics = [s for s in sources if s["kind"] == "image"]
        with stat_col:
            st.subheader("🔍 수집 자료 요약")
            a, b, c = st.columns(3)
            a.metric("텍스트 자료", sum(s["kind"] == "text" for s in sources))
            b.metric("이미지", sum(s["kind"] == "image" for s in sources))
            c.metric("제외된 파일", len(manifest["skipped"]))
            st.dataframe(
                [
                    {
                        "근거 ID": s["id"],
                        "파일": s["path"],
                        "종류": s["kind"],
                        "일부만 수집": s.get("truncated", False),
                    }
                    for s in sources
                ],
                width="stretch",
                height=260,
            )
            with st.expander("전송할 텍스트 확인 · 비밀정보가 없는지 확인하세요"):
                for s in sources:
                    if s["kind"] == "text":
                        st.markdown(f"**{s['id']} · {s['path']}**")
                        st.code(s["text"], language=None)
            with st.expander("제외된 파일 / 수집 제한"):
                st.json(manifest["skipped"])
                st.write(
                    "텍스트 총 90,000자, 파일별 14,000자, CSV 앞 50줄, PDF 앞 40쪽, 자료 최대 120개, 이미지 최대 20개. 노트북의 저장된 그래프·이미지도 자동 수집합니다. 노트북 소스는 최대 30,000자로 제한됩니다."
                )
            if not pics:
                st.info(
                    "현재 작업에는 수집된 이미지가 없습니다. 이전 버전에서 조사한 작업이라면 ZIP을 다시 넣고 자료 조사를 눌러야 노트북 내장 그래프가 수집됩니다."
                )
            if pics:
                with st.expander("이미지 미리보기"):
                    for s in pics:
                        st.image(
                            s["file"], caption=f"{s['id']} · {s['path']}", width=500
                        )

if manifest:
    sources = manifest["sources"]
    pics = [s for s in sources if s["kind"] == "image"]
    labels = {s["id"]: f"{s['id']} · {s['path']}" for s in pics}

    with tab_meeting:
        st.markdown("#### 📋 회의 브리프 & 자료 전송 동의")
        b_col1, b_col2 = st.columns([1.0, 1.2], gap="medium")
        with b_col1:
            title = st.text_input(
                "프로젝트명 (선택)",
                value=manifest.get("title", ""),
                max_chars=200,
                key="title_" + st.session_state.run_dir,
            )
            selected = st.multiselect(
                "참고 이미지 (최대 6개, 현재 로컬 모델은 이미지 미검토)",
                options=list(labels),
                default=[
                    x for x in manifest.get("preferred_images", []) if x in labels
                ],
                format_func=lambda x: labels[x],
                max_selections=6,
                key="source_images_" + st.session_state.run_dir,
            )
        with b_col2:
            purpose = st.text_area(
                "목적 · 강조할 내용 (선택)",
                max_chars=5000,
                height=108,
                placeholder="비워 두면 README와 코드를 근거로 작성합니다. 알 수 없는 내용은 자료 필요로 표시합니다.",
            )
        # 이 동의는 자료 전송용입니다. 프로젝트 코드 실행 버튼과는 별개입니다.
        consent = st.checkbox(
            "수집 자료와 모델 배정을 확인했습니다. 텍스트를 설정된 Ollama 서버로 전송해 처리합니다. 현재 모델 연결은 이미지를 직접 검토하지 않습니다."
        )
        st.divider()

        # 좌측: 실시간 AI 팀 티키타카 회의실 / 우측: 실시간 보고서 초안 편집기
        chat_col, editor_col = st.columns([1.08, 1.0], gap="large")
        with chat_col:
            meeting_result = render_meeting_panel(
                manifest, st.session_state.run_dir, title, purpose, selected, consent
            )
            if meeting_result:
                st.session_state.report, st.session_state.checks = meeting_result
                st.session_state["report_editor_" + st.session_state.run_dir] = (
                    st.session_state.report
                )
                st.session_state.pop("deck_key", None)
            with st.expander("⚡ 회의 없이 단일 작성자로 빠른 초안 만들기"):
                st.caption(
                    "5인 역할 회의를 거치지 않고 한 번의 호출로 빠르게 보고서 초안을 생성합니다."
                )
                if st.button(
                    "2. 단일 작성자로 빠른 초안 생성",
                    type="primary",
                    disabled=not consent
                    or not sources
                    or not st.session_state.get("ollama_ready", False),
                    use_container_width=True,
                ):
                    try:
                        with st.spinner(
                            "선택한 모델이 보고서를 작성 중입니다. CPU 로컬 실행은 오래 걸릴 수 있어요…"
                        ):
                            report, checks = generate(
                                manifest,
                                st.session_state.run_dir,
                                title,
                                purpose,
                                selected,
                            )
                        st.session_state.report = report
                        st.session_state.checks = checks
                        st.session_state[
                            "report_editor_" + st.session_state.run_dir
                        ] = report
                        st.session_state.pop("deck_key", None)
                    except Exception as e:
                        st.error(str(e))

        with editor_col:
            st.subheader("📝 보고서 초안 & 품질 검사")
            if st.session_state.get("report"):
                editor_key = "report_editor_" + st.session_state.run_dir
                if editor_key not in st.session_state:
                    st.session_state[editor_key] = st.session_state.report
                sync_edited_report()
                checks = st.session_state.checks
                if checks.get("image_review_warning"):
                    st.warning(checks["image_review_warning"])
                if checks.get("manual_edited"):
                    st.info(
                        "✏️ 직접 수정한 본문이 report.md에 자동 저장되었으며, 현재 본문 기준으로 목차·근거 ID 검사를 갱신했습니다."
                    )
                if checks.get("meeting_status") == "needs_review":
                    st.warning("회의 검토에 미해결 의견이 남은 초안입니다.")
                    with st.expander("미해결 검토 의견"):
                        st.json(checks.get("review", {}))
                if checks["unknown_source_ids"] or checks["missing_section_numbers"]:
                    st.warning(
                        "목차 또는 근거 ID 확인 필요: "
                        + json.dumps(checks, ensure_ascii=False)
                    )
                else:
                    st.success(
                        "목차 번호와 근거 ID 검사 통과. 제출 전 내용과 수치는 직접 확인하세요."
                    )
                report = st.text_area(
                    "내용을 확인하고 수정할 수 있어요",
                    height=600,
                    key=editor_key,
                    on_change=sync_edited_report,
                )
            else:
                st.info(
                    "왼쪽 **AI 팀 회의실**에서 회의를 시작하거나 저장된 작업을 불러오면, 이곳에 7개 대항목 보고서 초안 편집기가 실시간으로 열립니다."
                )

    with tab_export:
        if st.session_state.get("report"):
            editor_key = "report_editor_" + st.session_state.run_dir
            report = st.session_state.get(editor_key, st.session_state.report)
            checks = st.session_state.checks
            st.subheader("📊 PPTX · PDF 만들기")
            st.caption(
                "장수를 먼저 정하거나 최대 장수를 제한하지 않습니다. 근거가 많은 주제는 확장하고 관련 내용은 묶으며, 자료 부족은 빈 장 대신 한계에 정리합니다. 참고 보고서의 설명 흐름과 시각적 위계는 유지합니다."
            )
            deck_images = st.multiselect(
                "발표 자료 설계에 사용할 이미지 (최대 6개)",
                options=list(labels),
                default=selected,
                max_selections=6,
                format_func=lambda x: labels[x],
                key="deck_images_" + st.session_state.run_dir,
            )
            # 본문·이미지·양식이 바뀌었는지 확인해 예전 결과를 최신 결과로 착각하지 않게 합니다.
            deck_key = presentation_backend.export_key(report, title, deck_images)
            latest_file = Path(st.session_state.run_dir) / "latest_export.json"
            if not st.session_state.get("deck_dir") and latest_file.exists():
                latest = json.loads(latest_file.read_text(encoding="utf-8"))
                latest_dir = Path(latest["directory"])
                if (
                    latest.get("input_hash") == deck_key
                    and (latest_dir / "files/report.pptx").exists()
                    and (latest_dir / "files/report.pdf").exists()
                ):
                    st.session_state.deck_dir = str(latest_dir)
                    st.session_state.deck_key = deck_key
            if st.button(
                "3. PPTX + PDF 파일 생성",
                type="primary",
                disabled=not consent or not st.session_state.get("ollama_ready", False),
                use_container_width=True,
            ):
                try:
                    with st.spinner(
                        "선택한 모델이 발표 흐름을 설계하고 PPTX·PDF를 만듭니다. 로컬 실행은 오래 걸릴 수 있어요…"
                    ):
                        deck_dir = export_deck(
                            report,
                            manifest,
                            st.session_state.run_dir,
                            title,
                            deck_images,
                        )
                    st.session_state.deck_dir = str(deck_dir)
                    st.session_state.deck_key = deck_key
                except Exception as e:
                    st.error(str(e))
            deck_dir = (
                Path(st.session_state.deck_dir)
                if st.session_state.get("deck_dir")
                and st.session_state.get("deck_key") == deck_key
                else None
            )
            if deck_dir:
                st.success(
                    "발표용 구성 설계와 PPTX·PDF 생성 완료. 아래에서 내려받을 수 있어요."
                )
                d1, d2, d3 = st.columns(3)
                d1.download_button(
                    "PowerPoint 다운로드 (.pptx)",
                    (deck_dir / "files/report.pptx").read_bytes(),
                    file_name="report.pptx",
                    mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    use_container_width=True,
                )
                d2.download_button(
                    "PDF 다운로드 (.pdf)",
                    (deck_dir / "files/report.pdf").read_bytes(),
                    file_name="report.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
                d3.download_button(
                    "슬라이드 설계 JSON 다운로드",
                    (deck_dir / "storyboard.json").read_bytes(),
                    file_name="storyboard.json",
                    mime="application/json",
                    use_container_width=True,
                )
                with st.expander("슬라이드 미리보기 · 전체 페이지 확인", expanded=True):
                    for preview in sorted((deck_dir / "preview").glob("slide-*.png")):
                        st.image(str(preview), caption=preview.stem, width="stretch")
            elif st.session_state.get("deck_dir"):
                st.info(
                    "본문·제목·이미지가 바뀌었습니다. 파일 생성 버튼을 다시 눌러 최신 내용으로 만드세요."
                )
            st.divider()
            st.subheader("📥 보고서 문서 · 번들 패키지 다운로드")
            appendix = ""
            for s in pics:
                image_data = base64.b64encode(Path(s["file"]).read_bytes()).decode()
                appendix += f'<figure><img src="data:image/jpeg;base64,{image_data}"><figcaption>{html.escape(s["id"]+" · "+s["path"])}</figcaption></figure>'
            page = (
                '<!doctype html><html lang="ko"><meta charset="utf-8"><title>프로젝트 보고서</title><style>body{max-width:900px;margin:50px auto;font-family:Malgun Gothic,sans-serif;line-height:1.8;color:#172132;padding:24px}pre{white-space:pre-wrap;font:inherit}img{max-width:100%;max-height:700px}figure{break-inside:avoid;margin:32px 0}button{padding:12px}@media print{button{display:none}body{margin:0}}</style><button onclick="window.print()">인쇄 / PDF로 저장</button><pre>'
                + html.escape(report)
                + "</pre><h2>수집 이미지 부록 (본문 채택 여부 확인 필요)</h2>"
                + appendix
                + "</html>"
            )
            # 메모리 안에서 ZIP을 만든 뒤 다운로드 버튼에 전달합니다. 실제 원본 폴더를 압축하는 것은 아닙니다.
            bundle = io.BytesIO()
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("report.md", report)
                z.writestr("report.html", page)
                if deck_dir:
                    z.write(deck_dir / "files/report.pptx", "report.pptx")
                    z.write(deck_dir / "files/report.pdf", "report.pdf")
                z.writestr(
                    "checks.json", json.dumps(checks, ensure_ascii=False, indent=2)
                )
                z.writestr(
                    "source_index.json",
                    json.dumps(
                        [
                            {"id": s["id"], "path": s["path"], "kind": s["kind"]}
                            for s in sources
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                for s in pics:
                    z.write(s["file"], "images/" + Path(s["file"]).name)
            b1, b2, b3 = st.columns(3)
            b1.download_button(
                "Markdown 다운로드",
                report,
                file_name="report.md",
                use_container_width=True,
            )
            b2.download_button(
                "HTML 다운로드 · 브라우저에서 PDF로 인쇄",
                page,
                file_name="report.html",
                mime="text/html",
                use_container_width=True,
            )
            b3.download_button(
                "보고서·이미지·근거 목록 ZIP 다운로드",
                bundle.getvalue(),
                file_name="report_bundle.zip",
                mime="application/zip",
                use_container_width=True,
            )
            Path(st.session_state.run_dir, "report.html").write_text(
                page, encoding="utf-8"
            )
            st.caption("작업 보관 위치: " + st.session_state.run_dir)
        else:
            st.info(
                "먼저 **2. AI 팀 회의실 · 초안 편집** 탭에서 보고서 초안을 생성해 주세요."
            )
else:
    with tab_meeting:
        st.info(
            "먼저 **1. 자료 수집 · 코드 실행** 탭에서 프로젝트 ZIP을 조사하거나 왼쪽 사이드바에서 **저장된 작업**을 불러와 주세요."
        )
    with tab_export:
        st.info(
            "먼저 프로젝트를 불러오고 보고서 초안을 작성하면 이곳에서 PPTX·PDF 슬라이드를 생성할 수 있습니다."
        )
