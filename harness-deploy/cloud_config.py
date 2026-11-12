"""Streamlit Secrets의 모델 연결과 세션별 작업 공간을 관리합니다."""

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid


def _validate_url_and_headers(base_raw: str, token_raw: str, label: str = "OLLAMA_BASE_URL"):
    base = (base_raw or "").strip().rstrip("/")
    if not base:
        raise ValueError(f"Streamlit Secrets에 {label}을 설정하세요.")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label}은 인증정보·쿼리가 없는 http(s) 서버 주소여야 합니다."
        )
    headers = {"Content-Type": "application/json"}
    token = (token_raw or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return base, headers


def ollama_connection():
    """1순위(주 서버: 캐글 GPU 또는 기본 OLLAMA_BASE_URL) 연결 정보를 반환합니다."""
    primary_url = (
        os.environ.get("OLLAMA_PRIMARY_URL", "").strip()
        or os.environ.get("OLLAMA_BASE_URL", "").strip()
    )
    primary_key = (
        os.environ.get("OLLAMA_PRIMARY_API_KEY", "").strip()
        or os.environ.get("OLLAMA_API_KEY", "").strip()
    )
    return _validate_url_and_headers(primary_url, primary_key, "OLLAMA_BASE_URL")


def ollama_fallback_connection():
    """2순위(백업 서버: 오라클 클라우드 24시간 CPU 서버 등) 연결 정보를 반환합니다. 미설정 시 None."""
    fallback_url = os.environ.get("OLLAMA_FALLBACK_URL", "").strip()
    if not fallback_url:
        return None
    fallback_key = (
        os.environ.get("OLLAMA_FALLBACK_API_KEY", "").strip()
        or os.environ.get("OLLAMA_API_KEY", "").strip()
    )
    return _validate_url_and_headers(fallback_url, fallback_key, "OLLAMA_FALLBACK_URL")


def _fetch_server_models(base: str, headers: dict, timeout: int = 8) -> set[str]:
    request = urllib.request.Request(base + "/api/tags", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise RuntimeError(
                "모델 서버 인증 실패. Secrets의 OLLAMA_API_KEY를 확인하세요."
            ) from None
        raise RuntimeError(
            f"모델 서버 응답 오류(HTTP {error.code}). Ollama 주소를 확인하세요."
        ) from None
    except (OSError, ValueError):
        raise RuntimeError(
            "모델 서버에 연결하지 못했습니다. 클라우드에서 접속 가능한 주소와 서버 상태를 확인하세요."
        ) from None
    return {item.get("name", item.get("model")) for item in body.get("models", [])}


def probe_connection_tier(settings):
    """1순위(캐글 GPU) 서버를 먼저 확인하고, 응답 실패 시 2순위(오라클 CPU) 백업 서버로 자동 전환합니다.

    반환값: (missing_models, active_tier, effective_settings, active_base_url)
    - active_tier == 'primary'이고 1순위 서버에 상위 GPU 모델(gemma3:12b, qwen3:8b)이 준비되어 있으면 상위 모델 프로필을 자동 적용합니다.
    - 1순위 연결 실패 후 2순위(fallback) 서버로 전환되면 오라클 CPU용 경량 모델(qwen3:4b, qwen3:1.7b) 프로필을 자동 적용합니다.
    """
    from roles import resolve_local_model, settings_for_tier

    base, headers = ollama_connection()
    fallback_conn = ollama_fallback_connection()

    try:
        available = _fetch_server_models(base, headers, timeout=8)
        gpu_settings = settings_for_tier("primary", settings)
        gpu_required = {resolve_local_model(item["model"]) for item in gpu_settings.values()}
        if gpu_required.issubset(available):
            os.environ["OLLAMA_ACTIVE_TIER"] = "primary"
            return [], "primary", gpu_settings, base
        os.environ.pop("OLLAMA_ACTIVE_TIER", None)
        required = {resolve_local_model(item["model"]) for item in settings.values()}
        missing = sorted(required - available)
        return missing, "primary", settings, base
    except RuntimeError as primary_error:
        if fallback_conn is None:
            os.environ.pop("OLLAMA_ACTIVE_TIER", None)
            raise primary_error
        fb_base, fb_headers = fallback_conn
        fb_available = _fetch_server_models(fb_base, fb_headers, timeout=10)
        fb_settings = settings_for_tier("fallback", settings)
        fb_required = {resolve_local_model(item["model"]) for item in fb_settings.values()}
        fb_missing = sorted(fb_required - fb_available)
        os.environ["OLLAMA_ACTIVE_TIER"] = "fallback"
        return fb_missing, "fallback", fb_settings, fb_base


def check_connection(settings):
    missing, _, _, _ = probe_connection_tier(settings)
    return missing


def workspace(root, state):
    identity = state.setdefault("workspace_id", uuid.uuid4().hex)
    if (
        not isinstance(identity, str)
        or len(identity) != 32
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("잘못된 작업 공간 ID입니다.")
    return root / "runs" / identity


def render_connection_panel(settings):
    import streamlit as st

    # Secrets는 저장소 파일로 복사하지 않습니다. 서버의 실행 환경에만 반영합니다.
    try:
        for key in (
            "OLLAMA_BASE_URL",
            "OLLAMA_API_KEY",
            "OLLAMA_PRIMARY_URL",
            "OLLAMA_PRIMARY_API_KEY",
            "OLLAMA_FALLBACK_URL",
            "OLLAMA_FALLBACK_API_KEY",
        ):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except FileNotFoundError:
        pass
    st.subheader("모델 서버 연결")
    try:
        base, headers = ollama_connection()
        fb = ollama_fallback_connection()
        fb_part = (fb[0] + fb[1].get("Authorization", "")) if fb else ""
        fingerprint = hashlib.sha256(
            (
                base
                + headers.get("Authorization", "")
                + fb_part
                + json.dumps(settings, sort_keys=True)
            ).encode()
        ).hexdigest()
    except ValueError as error:
        st.session_state.ollama_ready = False
        st.info(str(error))
        fingerprint = None
    if st.session_state.get("connection_fingerprint") != fingerprint:
        st.session_state.ollama_ready = False
        st.session_state.connection_fingerprint = fingerprint
    if st.button(
        "모델 서버 연결 확인", disabled=fingerprint is None, key="check_ollama"
    ):
        try:
            missing, active_tier, effective_settings, _ = probe_connection_tier(settings)
            st.session_state.ollama_ready = not missing
            st.session_state.active_server_tier = active_tier
            st.session_state.effective_settings = effective_settings
            st.session_state.connection_message = (
                ("서버에 없는 모델: " + ", ".join(missing))
                if missing
                else "연결 확인 완료"
            )
        except (ValueError, RuntimeError) as error:
            st.session_state.ollama_ready = False
            st.session_state.connection_message = str(error)
    if st.session_state.get("ollama_ready"):
        st.success("모델 서버 연결 완료")
        tier = st.session_state.get("active_server_tier", "primary")
        if tier == "fallback":
            st.caption("🟠 1순위 서버 응답 없음 → 2순위 백업 서버(Oracle Cloud · 경량 모델)로 자동 전환됨")
        elif os.environ.get("OLLAMA_ACTIVE_TIER") == "primary":
            st.caption("🟢 1순위 주 서버(Kaggle GPU · 상위 모델 Gemma 3 12B / Qwen3 8B) 연결 활성")
    elif st.session_state.get("connection_message"):
        st.warning(st.session_state.connection_message)
    with st.expander("Streamlit 배포 설정"):
        st.write(
            "GitHub 저장소의 app.py를 선택하고 Python 3.12로 배포하세요. Settings → Secrets에 아래 설정을 넣은 뒤 연결을 확인하세요."
        )
        st.code(
            '# 1순위: 캐글 무료 GPU 서버 (OLLAMA_BASE_URL 또는 OLLAMA_PRIMARY_URL 모두 지원)\n'
            'OLLAMA_BASE_URL = "https://your-kaggle-gpu-tunnel.trycloudflare.com"\n'
            '# 2순위(선택): 오라클 클라우드 24시간 무료 서버 (캐글 종료·중단 시 Qwen3 4B/1.7B로 자동 폴백)\n'
            '# OLLAMA_FALLBACK_URL = "https://your-oracle-cpu-server.trycloudflare.com"\n'
            '# 인증을 사용하는 서버만 추가\n'
            '# OLLAMA_API_KEY = "서버에서 발급한 인증값"',
            language="toml",
        )
        st.caption(
            "1순위(캐글 GPU) 연결 실패 또는 이용 시간 만료 시 2순위(오라클 24시간 CPU 서버)와 경량 모델로 자동 전환됩니다."
        )
