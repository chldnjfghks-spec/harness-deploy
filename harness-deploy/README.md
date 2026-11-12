# 프로젝트 보고서 자동화 하네스 (Report Harness)

프로젝트 ZIP 아카이브(코드·노트북·문서·저장된 그래프)를 업로드하면 정적 구조 점검 후 **로컬/오픈웨이트 LLM 역할별 팀 회의(LangGraph)**를 거쳐 **7개 대항목 한국어 기술 보고서(Markdown)**와 **편집 가능한 발표 자료(PPTX · PDF)**를 생성하는 시스템입니다.

---

## 1. 주요 구성 및 제출 자료 안내

| 구분 | 경로 | 설명 |
| :--- | :--- | :--- |
| **Streamlit 웹앱 엔트리** | `app.py` | 3단계 대시보드(자료 수집·정적 점검 → AI 팀 회의·초안 편집 → PPTX·PDF 발행) |
| **서버 연결 · 이중화 폴백** | `cloud_config.py`, `llm_client.py`, `roles.py` | 1순위(캐글 GPU · 상위 모델 `Gemma 3 12B / Qwen3 8B`) ↔ 2순위(오라클 CPU · 경량 모델 `Qwen3 4B / 1.7B`) 자동 전환 |
| **워크플로 · 정적 점검** | `workflow.py`, `engine.py`, `entry_inspection.py` | 온디맨드 슈퍼바이저 회의 라우팅, AST 기반 실행 파일·누락 모듈 점검, 출처·목차 자동 검증 |
| **PPTX · PDF 생성기** | `portable_renderer.py`, `presentation_export.py`, `storyboard.py` | 외부 Node.js 의존성 없는 순수 Python 기반 네이티브 표·차트 PPTX 및 이미지형 PDF 생성 |
| **비교 실험 노트북** | `notebooks/local_model_check.ipynb`<br>`notebooks/model_family_comparison.ipynb`<br>`notebooks/kaggle_ollama_gpu_server.ipynb` | • 로컬 5단계 회의 및 검토 모델(1.7B vs 4B) 검증 노트북<br>• 로컬 후보 3종(`Qwen3 1.7B` · `Gemma 2 2B` · `Llama 3.2 3B`) 9회 반복 비교 실험 노트북<br>• 캐글 무료 GPU 서버 원클릭 실행 노트북 |

---

## 2. 로컬 실행 방법

1. **패키지 설치 (Python 3.12 권장)**
   ```bash
   pip install -r requirements.txt
   ```
2. **환경변수 설정 (로컬 Ollama 또는 원격 GPU 터널)**
   ```bash
   # 로컬 PC에 설치된 Ollama 사용 시
   export OLLAMA_BASE_URL="http://127.0.0.1:11434"
   ```
3. **Streamlit 앱 실행**
   ```bash
   streamlit run app.py
   ```

---

## 3. Streamlit Community Cloud 배포 및 Secrets 설정 예시

1. 본 폴더(`harness-deploy`)의 파일들을 GitHub 저장소에 업로드합니다.
2. [Streamlit Community Cloud](https://share.streamlit.io)에서 **`Create app`**을 누르고 해당 저장소와 `app.py`를 선택합니다.
3. **`Advanced settings...`**에서 Python 버전을 **`3.12`**로 지정하고, **`Secrets`** 입력창에 아래 형식으로 외부 Ollama 서버 주소를 입력합니다.

```toml
# [필수] 1순위 주 서버 주소 (OLLAMA_BASE_URL 또는 OLLAMA_PRIMARY_URL 모두 지원)
# 서버에 gemma3:12b, qwen3:8b가 설치되어 있으면 자동으로 상위 GPU 모델 프로필로 배정됩니다.
OLLAMA_BASE_URL = "https://your-kaggle-gpu-tunnel.trycloudflare.com"

# [선택] 2순위 백업 서버 주소 (예: Oracle Cloud 무료 CPU 서버)
# 1순위 서버가 연결 실패하거나 스트리밍 도중 끊기면 실패 감지 후 2순위 서버와 경량 모델(qwen3:4b / 1.7b)로 자동 전환됩니다.
# OLLAMA_FALLBACK_URL = "https://your-oracle-cpu-tunnel.trycloudflare.com"

# [선택] Bearer 인증 토큰을 사용하는 프록시/서버일 때만 설정
# OLLAMA_API_KEY = "your-secret-token"
```

4. 배포된 앱 왼쪽 사이드바에서 **`모델 서버 연결 확인`** 버튼을 눌러 `모델 서버 연결 완료` 상태를 확인한 뒤 ZIP 업로드 및 보고서 생성을 진행합니다.


## 4. 과제 제출용 실험 자료

| 파일 | 목적 | 확인 상태 |
|---|---|---|
| [local_model_check.ipynb](notebooks/local_model_check.ipynb) | 로컬 5단계 회의·잘못된 자체 승인·별도 4B 검토 기록 | 실제 과거 기록 내장 |
| [model_family_comparison.ipynb](notebooks/model_family_comparison.ipynb) | Qwen3 1.7B / Gemma 2 2B / Llama 3.2 3B, 모델별 3회 | 실제 9회 답변·시간·그래프·Codex 원문 대조 평가 내장 |
| [report_comparison.ipynb](notebooks/report_comparison.ipynb) | 과거 클라우드의 단일 작성과 역할 분담 비교 | 조건당 1회 예비 실험. 로컬 모델 순위와 구분 |
| [kaggle_model_comparison.ipynb](notebooks/kaggle_model_comparison.ipynb) | Qwen3 8B / Gemma 2 9B / Llama 3.1 8B 작성·검토 비교 | 실행 준비본. 실제 캐글 결과를 아직 회수하지 않음 |
| [kaggle_ollama_gpu_server.ipynb](notebooks/kaggle_ollama_gpu_server.ipynb) | Streamlit에 연결할 임시 GPU 모델 서버 실행 | 비교 실험이 아닌 서버 실행 도구 |
| [model_family_benchmark.py](experiments/model_family_benchmark.py) | 소형 3계열 비교를 같은 지시·원본으로 다시 실행 | 사용자가 명시적으로 실행할 때 모델 호출 |
| [local_family_protocol.json](experiments/data/local_family_protocol.json) | 원래 실험의 입력·조건·정답 기준 | 고정 근거. 현재 앱 설정과 별개 |
| [oracle_ollama_cpu_setup.sh](deployment/oracle_ollama_cpu_setup.sh) | Ubuntu ARM/x86 백업 모델 서버 준비 | 외부 서버에서 실행할 설치 예제 |

### 저장된 결과만 확인하기

프로젝트 루트에서 다음을 실행하고 VS Code/Jupyter로 노트북을 엽니다.

```bash
python -m pip install -r experiments/requirements.txt
```

처음 세 노트북은 자체에 기록을 포함하므로 원래 개발 폴더나 별도 `runs` 폴더 없이 열립니다.
기본 실행으로 모델을 다시 호출하지 않습니다. `model_family_comparison.ipynb`의 `RUN_MODELS=False`를 유지하세요.
원고·그래프를 수정하면 저장된 과거 실행 결과 자체와 수정한 설명을 구분하세요.

### 로컬 소형 모델 비교 재실행

Ollama가 실행된 PC에서 아래 모델을 준비합니다. 다운로드는 모델 저장 공간을 사용합니다.

```bash
ollama pull qwen3:1.7b
ollama pull gemma2:2b
ollama pull llama3.2:3b
python experiments/model_family_benchmark.py
```

새 기록은 `experiments/results/local_family_실행시각/`에 저장되며 기존 제출 노트북을 덮어쓰지 않습니다.
결과를 원본과 다시 대조해 평가해야 합니다. 과거 9개 답변에 대한 평가를 새 답변에 그대로 붙이지 마세요.
이 재현 코드는 같은 PC의 `http://127.0.0.1:11434`를 호출합니다. Streamlit Cloud에서 이 명령을 실행하지 않습니다.

### 캐글 비교 실험

`kaggle_model_comparison.ipynb`를 캐글에 Import한 뒤 GPU·Internet을 켜고 Run All을 실행합니다.
작성 9회와 검토 9회, 총18회가 계획돼 있습니다. 실제 실행 전에는 성능 결과가 없습니다.
실행 뒤 평가표를 채우고 출력 포함 노트북 및 `harness_comparison_results.zip`을 저장하세요.
이전 CPU와 캐글 GPU는 하드웨어·로드 시간 조건이 다르므로 실행 시간만으로 모델 우열을 단정하지 않습니다.
캐글 계정의 인증·동시 세션·주간 사용 한도와 GPU 자원 상태에 따라 실행이 제한될 수 있습니다.

## 5. Windows PowerShell 실행 예

기존 2절의 `export`는 Linux/macOS 명령입니다. Windows PowerShell에서는 다음처럼 실행합니다.

```powershell
python -m pip install -r requirements.txt
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
python -m streamlit run app.py
```

Community Cloud에서는 로컬 주소 대신 외부 모델 서버 주소를 Secrets에 넣습니다.
인증 프록시를 실제로 구성한 경우에만 해당 키를 설정하세요. 앱에 키를 넣는 것만으로 모델 서버에 인증이 생기지는 않습니다.
`.env`, `.streamlit/secrets.toml`, SSH 개인 키는 제출하거나 GitHub에 올리지 않습니다.

## 6. 실행·검증 범위와 제출 전 확인

- 로컬 비교는 실제 기록, 캐글 큰 모델 비교는 아직 결과 회수 전이라는 상태를 유지합니다.
- 서버 연결과 모델 목록 확인은 전체 보고서 생성 품질·장애 전환 성공의 증거가 아닙니다.
- 캐글/오라클 두 서버의 실제 중단·전환·복구 시나리오는 별도로 기록해야 합니다. 무료 자원과 터널에 24시간 무중단을 보장하지 않습니다.
- 최종 보고서는 코드 폴더와 별도로 제출합니다. 아직 회수하지 않은 캐글 비교나 미검증 배포 성공을 완료 결과로 추가하지 않습니다.
- 제출 ZIP에는 소스, templates, 설정 예시, README, notebooks, experiments, deployment를 포함합니다. 보고서가 제출 요건이면 별도로 첨부합니다.
- `.venv`, `__pycache__`, `.git`, 모델 가중치, 임시 결과와 실제 Secrets는 제외합니다.
- `experiments/results/`와 캐글 결과 ZIP은 원본 보관용입니다. 제출에 실제 결과를 추가할 때는 내용과 용량을 확인하세요.

