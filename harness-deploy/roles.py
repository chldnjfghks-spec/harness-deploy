"""담당자별 역할과 모델 설정입니다. 모델 이름만 바꾸면 역할별 모델을 달리 쓸 수 있습니다."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "templates" / "model_settings.json"
DEFAULT_MODEL = "ollama:qwen3:1.7b"

# 2순위(오라클 클라우드 24시간 CPU 서버 등) 및 로컬 기본 배정: 슈퍼바이저 4B / 작업자 1.7B
ROLE_DEFAULT_MODELS = {
    "planner": "ollama:qwen3:4b",
    "analyst": "ollama:qwen3:1.7b",
    "organizer": "ollama:qwen3:1.7b",
    "writer": "ollama:qwen3:1.7b",
    "reviewer": "ollama:qwen3:4b",
    "slides": "ollama:qwen3:1.7b",
}

# 1순위(캐글 무료 GPU 서버) 연결 시 자동 적용되는 상위 모델 배정:
# 구조·코드 분석은 Qwen3 8B, 한국어 기획·본문 작성·최종 검토는 Gemma 3 12B를 배정합니다.
ROLE_PRIMARY_GPU_MODELS = {
    "planner": "ollama:gemma3:12b",
    "analyst": "ollama:qwen3:8b",
    "organizer": "ollama:qwen3:8b",
    "writer": "ollama:gemma3:12b",
    "reviewer": "ollama:gemma3:12b",
    "slides": "ollama:gemma3:12b",
}


def fallback_model_for_role(role: str) -> str:
    """1순위 GPU 서버 중단으로 2순위 CPU 서버(오라클 클라우드)로 폴백할 때 사용할 경량 모델을 반환합니다."""
    return ROLE_DEFAULT_MODELS.get(role, DEFAULT_MODEL)


def settings_for_tier(tier: str = "fallback", base_settings: dict | None = None) -> dict:
    """연결된 서버 등급('primary'=캐글 GPU 상위 모델, 'fallback'=오라클 CPU 경량 모델)에 맞춰 역할별 설정을 반환합니다."""
    source = base_settings or load_settings()
    target_map = ROLE_PRIMARY_GPU_MODELS if tier == "primary" else ROLE_DEFAULT_MODELS
    return {
        role: {
            "model": target_map.get(role, source.get(role, {}).get("model", DEFAULT_MODEL)),
            "reasoning": source.get(role, {}).get("reasoning", "high"),
        }
        for role in ROLES
    }


def is_antigravity_model(model: str) -> bool:
    """모델 이름이 Antigravity CLI(agy)로 호출할 Gemini·Claude 계열인지 판별합니다."""
    normalized = (model or "").strip().lower()
    return normalized.startswith(("gemini", "claude", "agy:", "antigravity:"))


def is_local_model(model: str) -> bool:
    """모델 이름이 로컬 LLM(Ollama 등)으로 호출할 대상인지 판별합니다."""
    normalized = (model or "").strip().lower()
    return normalized.startswith(
        ("ollama:", "local:", "gemma", "llama", "qwen", "deepseek", "mistral", "phi")
    )


def resolve_local_model(model: str) -> str:
    """로컬 Ollama 서버에 전달할 실제 모델 이름(예: gemma4:e2b)을 정리합니다."""
    cleaned = (model or "").strip()
    for prefix in ("ollama:", "local:", "OLLAMA:", "LOCAL:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned


def resolve_antigravity_model(model: str, reasoning: str = "high") -> str:
    """Antigravity CLI(agy)가 인식하는 실제 모델 슬러그(예: gemini-4-argon-high)로 변환합니다."""
    cleaned = (model or "").strip().lower()
    for prefix in ("agy:", "antigravity:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
    cleaned = cleaned.replace(" ", "-")
    effort = reasoning if reasoning in {"low", "medium", "high"} else "high"
    if cleaned in {
        "gemini-4-argon",
        "gemini-3.8-flash",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    }:
        return f"{cleaned}-{effort}"
    return cleaned


def provider_label(model: str) -> str:
    """화면 표시용 엔진 이름(Codex, Antigravity, 또는 Local LLM)을 반환합니다."""
    if is_local_model(model):
        return "Local LLM"
    return "Antigravity" if is_antigravity_model(model) else "Codex"


# 같은 모델을 여러 번 호출해도 각 호출에 다른 업무와 입력 자료를 줍니다.
# 담당자 이름이 다르다고 실제 모델 종류까지 달라지는 것은 아닙니다.
ROLES = {
    "planner": {
        "name": "기획자",
        "instruction": (
            "당신은 AI 보고서 팀의 기획자이자 필요할 때만 개입하는 슈퍼바이저(팀장)입니다. 장황한 설명 없이 실제 메신저에서 핵심만 짧게 주고받듯 대화하세요. "
            "첫 문단은 반드시 2~3문장(180자 이내)으로 @자료·실행 결과 분석자님과 @보고서 구성 담당님에게 이번 프로젝트에서 가장 먼저 확인할 핵심 질문 1~2가지만 자연스러운 구어체로 건네세요. "
            "팀원 간 추가 확인이나 검토 불합격 등 쟁점이 발생해 슈퍼바이저로 호출되었을 때는 문제를 진단하고 다음에 누가 어떤 보완 작업을 해야 할지([NEXT_WORKER: analyst|organizer|writer|finish]) 명확히 지시하세요. "
            "빈 줄 하나를 띄운 뒤, 아래에는 작업 목표와 핵심 체크포인트만 3~4개 짧은 불릿(-)으로 간결하게 정리하세요. 자료에 없는 성과를 계획에 넣지 마세요."
        ),
    },
    "analyst": {
        "name": "자료·실행 결과 분석자",
        "instruction": (
            "당신은 자료·실행 결과 분석자 에이전트입니다. 첫 문단은 반드시 2~3문장(200자 이내)으로 @기획자님 또는 @보고서 구성 담당님의 질문에 핵심 결론부터 답하고, "
            "가장 중요한 근거([S001] 등)와 빠진 자료(한계점) 한 가지만 짧고 자연스러운 대화체로 짚어주세요. "
            "만약 원본 근거가 '[전송 분량 제한으로 뒷부분 생략]'되어 있어 뒷부분 추가 확인이 꼭 필요하다면 본문 끝에 [READ_SOURCE: S001] 형식으로 열람할 ID를 적으세요. "
            "빈 줄 하나를 띄운 뒤, 아래에는 확인된 사실·실행 결과·부족한 자료를 핵심 불릿(-) 위주로 간결하게 정리하세요. 새 코드를 실행했다고 주장하지 마세요."
        ),
    },
    "organizer": {
        "name": "보고서 구성 담당",
        "instruction": (
            "당신은 보고서 구성 담당 에이전트입니다. 첫 문단은 반드시 2~3문장(180자 이내)으로 @기획자님·@자료·실행 결과 분석자님의 포인트를 어떻게 반영했는지 짧게 답하고, "
            "@본문 작성자님에게 집필 시 주의할 핵심 한 가지만 자연스럽게 당부하세요. "
            "만약 분석자의 설명만으로 핵심 근거가 불충분해 분석자에게 한 번 더 확인을 요청해야 한다면 끝에 [ASK_ANALYST: 확인할 질문 한 문장]을 적으세요. "
            "빈 줄 하나를 띄운 뒤, 아래에는 7개 대항목별 핵심 내용과 연결할 근거 ID([S001] 등)·한계 처리만 간결한 목록으로 제시하세요."
        ),
    },
    "writer": {
        "name": "본문 작성자",
        "instruction": (
            "합의한 구성과 원본 근거로 한국어 보고서 Markdown을 작성하세요. 표지 제목과 7개 대항목 목차를 넣고, 본문에는 ## 1.부터 ## 7.까지 대제목과 그 아래 소제목을 사용하세요. "
            "Markdown 초안의 실제 페이지 번호는 아직 정해지지 않았으므로 만들지 마세요. 페이지 범위는 후속 슬라이드 렌더러가 계산합니다. "
            "사실에는 [S001] 같은 실제 출처를 붙이세요. 검토 의견을 받으면 근거를 재확인해 수정하세요. 보고서 본문만 반환하세요."
        ),
    },
    "reviewer": {
        "name": "근거·품질 검토자",
        "instruction": (
            "초안을 원본 근거와 대조하세요. 수치·출처·실행 여부·누락·표지 제목·7개 대항목 목차와 본문·설명 흐름을 검사하세요. "
            "summary 필드에는 @본문 작성자님과 팀원들에게 건네는 핵심 총평을 2~3문장(180자 이내)의 짧고 자연스러운 대화체로 작성하세요. "
            "현재는 Markdown 초안 단계이며 실제 페이지 범위는 후속 슬라이드 렌더러가 계산합니다. 아직 없는 페이지 번호를 요구하거나 만들게 하지 마세요. "
            "다른 담당자의 동의를 사실의 증거로 사용하지 마세요. 해결이 필요한 문제가 있으면 approved=false로 반환하세요."
        ),
    },
    "slides": {
        "name": "슬라이드 설계 담당",
        "instruction": "보고서를 대항목과 소항목이 구분되는 발표용 슬라이드 설계로 바꾸세요. 자료에 없는 그림이나 수치를 만들지 마세요.",
    },
}


def load_settings():
    """저장 설정을 읽고, 빠진 역할에는 로컬 기본 모델을 적용합니다."""
    saved = {}
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        role: {
            "model": saved.get(role, {}).get(
                "model", ROLE_DEFAULT_MODELS.get(role, DEFAULT_MODEL)
            ),
            "reasoning": saved.get(role, {}).get("reasoning", "high"),
        }
        for role in ROLES
    }


def report_policy(settings=None):
    """배포본의 로컬 보고서 작성 기준을 읽습니다."""
    return (ROOT / "templates/local_report_outline.md").read_text(encoding="utf-8-sig")


def save_settings(settings):
    """화면에서 고른 역할별 모델 설정을 저장합니다. 인증정보는 넣지 않습니다."""
    for role in ROLES:
        model = settings[role]["model"]
        if not is_local_model(model):
            raise ValueError("배포본은 Ollama 로컬 모델만 지원합니다.")
        if not isinstance(model, str) or not model.strip() or len(model) > 100:
            raise ValueError("모델 이름을 확인하세요.")
        if settings[role]["reasoning"] not in {"low", "medium", "high"}:
            raise ValueError("추론 수준을 확인하세요.")
    CONFIG_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
