# 프로젝트 보고서 자동화 하네스 (Report Harness)

프로젝트 ZIP 아카이브(코드·노트북·문서·저장된 그래프)를 업로드하면 **AST 기반 정적 구조 점검** 후 **역할별 AI 팀 회의(LangGraph)**를 거쳐 **7개 대항목 한국어 기술 보고서(Markdown)**와 **편집 가능한 발표 자료(PPTX · PDF)**를 자동 생성하는 AI 웹 서비스입니다.

> 상세 구성 및 배포 가이드는 [`harness-deploy/README.md`](./harness-deploy/README.md)에서 확인할 수 있습니다.

---

## 핵심 특징

- **3단계 Streamlit 대시보드 (`harness-deploy/app.py`):** 자료 수집·정적 점검 → AI 팀 회의·초안 편집 → PPTX·PDF 발행
- **GPU–CPU 이중화 폴백(Fallback) 아키텍처 (`cloud_config.py`, `llm_client.py`):**
  - 1순위 원격 GPU 서버(`Gemma 3 12B / Qwen3 8B`) 연결 실패 또는 스트리밍 단절 시 2순위 경량 CPU 서버(`Qwen3 4B / 1.7B`)로 자동 전환
- **환각 억제 및 자동 검증 (`entry_inspection.py`, `workflow.py`):**
  - 실행 전 AST 기반 정적 구조 점검으로 실제 파일·모듈 목록 추출, 생성 후 출처·목차 자동 검증
- **순수 Python 네이티브 문서 생성 (`portable_renderer.py`, `presentation_export.py`):**
  - 외부 Node.js 의존성 없이 `python-pptx`, `pypdf`, `Matplotlib`만으로 표·차트가 포함된 PPTX·PDF 렌더링

---

## 기술 스택

| 구분 | 기술 |
| :--- | :--- |
| **Language / Web** | Python 3.12, Streamlit |
| **LLM / Workflow** | LangGraph, Ollama (`Gemma 3`, `Qwen3`, `Llama 3.2`) |
| **Document / Render** | `python-pptx`, `pypdf`, `Pillow`, `Matplotlib` |

---

## 프로젝트 구조

```text
harness-deploy/
└── harness-deploy/
    ├── app.py                   # Streamlit 웹앱 엔트리
    ├── cloud_config.py          # 1순위 GPU / 2순위 CPU 서버 설정 및 연결 패널
    ├── llm_client.py            # 이중화 폴백 LLM 호출 클라이언트
    ├── engine.py                # 자료 수집 및 보고서 생성 엔진
    ├── entry_inspection.py      # AST 기반 정적 코드 점검
    ├── workflow.py              # LangGraph 역할별 회의 및 검증 워크플로
    ├── portable_renderer.py     # 순수 Python 네이티브 PPTX·PDF 렌더러
    ├── presentation_export.py   # 발표 자료 내보내기
    ├── notebooks/               # 모델 비교 및 원격 GPU 서버 실험 노트북
    ├── tests/                   # 단위 테스트 (`test_slide_batches.py`)
    ├── requirements.txt
    └── README.md                # 상세 실행·Secrets 설정 가이드
```

---

## 로컬 실행 및 테스트

```bash
cd harness-deploy
pip install -r requirements.txt

# 로컬 Ollama 사용 시
export OLLAMA_BASE_URL="http://127.0.0.1:11434"
streamlit run app.py
```

단위 테스트 실행 (`harness-deploy/harness-deploy` 기준):

```bash
python -m unittest discover -s tests -v
```
