"""ZIP 속 실행 후보를 코드를 실행하지 않고 읽어 역할과 준비 상태를 설명합니다.

AST는 Python 코드를 구조로 읽는 도구입니다. 분석 자체로 학습·다운로드·API 호출을 하지 않습니다.
정적 검사이므로 동적으로 만든 경로나 다른 파일 안의 모든 동작까지 보증하지는 않습니다.
"""

import ast
import json
from pathlib import Path

from engine import decode


def code_text(path):
    """노트북에서는 코드 셀만 꺼냅니다. 너무 큰 파일은 자동 분석하지 않습니다."""
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("파일이 2MB를 넘어 자동 역할 검사를 생략했습니다.")
    text = decode(path.read_bytes())
    if path.suffix == ".ipynb":
        cells = json.loads(text).get("cells", [])
        text = "\n\n".join(
            "".join(cell.get("source", []))
            for cell in cells
            if cell.get("cell_type") == "code"
        )
    return text


def literal_value(node, values):
    """실행 없이 확인 가능한 문자열 경로만 읽습니다. eval()은 사용하지 않습니다."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.Call) and node.args:
        if ast.unparse(node.func).split(".")[-1] in {"Path", "PurePath"}:
            return literal_value(node.args[0], values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        left, right = literal_value(node.left, values), literal_value(
            node.right, values
        )
        if left is not None and right is not None:
            return (
                str(Path(left) / right)
                if isinstance(node.op, ast.Div)
                else left + right
            )
    return None


def inspect_entry(root, entry, working_dir=None):
    """실행 파일 하나의 역할, 근거, 누락 모듈·입력 파일, 작업 폴더 후보를 반환합니다."""
    root = Path(root).resolve()
    target = (root / entry).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("프로젝트 안의 실행 파일을 선택하세요.")
    directories = []
    current = target.parent
    while current.is_relative_to(root):
        directories.append(current)
        if current == root:
            break
        current = current.parent
    result = {
        "entry": str(target.relative_to(root)),
        "kind": "역할 확인 필요",
        "reasons": [],
        "notices": [],
        "missing_modules": [],
        "missing_inputs": [],
        "input_paths": [],
        "local_imports": [],
        "imports": [],
        "cwd": str(target.parent.relative_to(root)),
        "cwd_options": [str(p.relative_to(root)) for p in directories],
        "training": False,
        "service": False,
        "graphs": False,
        "interactive": False,
        "colab": False,
        "external_calls": False,
        "parse_ok": False,
        "score": -100,
    }
    try:
        text = code_text(target)
        # Notebook의 %·! 명령은 Python AST 구문이 아닙니다. 삭제한 사실을 명시합니다.
        lines = text.splitlines()
        magic = any(line.lstrip().startswith(("%", "!")) for line in lines)
        if magic:
            result["notices"].append(
                "노트북 매직·셸 명령이 있습니다. 로컬 실행 환경을 확인하세요."
            )
        source = "\n".join(
            "" if line.lstrip().startswith(("%", "!")) else line for line in lines
        )
        tree = ast.parse(source)
        result["parse_ok"] = True
    except (ValueError, OSError, SyntaxError) as error:
        result["notices"].append("코드 자동 검사 미완료: " + str(error)[:180])
        return result

    imports = []
    local_imports = []
    aliases = {}
    values = {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for name in node.names:
                imports.append(name.name)
                aliases[name.asname or name.name.split(".")[0]] = (
                    name.name if name.asname else name.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(module)
            for name in node.names:
                aliases[name.asname or name.name] = module + "." + name.name
            if node.level:
                local_imports.append((module, node.level))
    # 실제 경로를 실행 없이 확인하기 위해 문자열 상수와 단순 경로 조합만 추적합니다.
    for node in nodes:
        if isinstance(node, ast.Assign):
            value = literal_value(node.value, values)
            if value is not None:
                for name in node.targets:
                    if isinstance(name, ast.Name):
                        values[name.id] = value

    def full_name(node):
        name = ast.unparse(node)
        first = name.split(".")[0]
        return aliases.get(first, first) + name[len(first) :]

    inputs = set()
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = full_name(node.func)
        end = name.rsplit(".", 1)[-1]
        if end in {"fit", "fit_generator", "backward"}:
            result["training"] = True
        if (
            (
                name.startswith(("matplotlib.", "seaborn.", "plotly."))
                and end not in {"use", "close"}
            )
            or end in {"savefig", "barh", "hist"}
            or ".plot" in name
        ):
            result["graphs"] = True
        if name.startswith("streamlit.") or end in {
            "Flask",
            "FastAPI",
            "launch",
            "run_server",
        }:
            result["service"] = True
        if name in {"input", "getpass.getpass"}:
            result["interactive"] = True
        if name.startswith(
            ("requests.", "httpx.", "openai.", "google.genai.", "urllib.request.")
        ) or ("reporter" in name.lower() and end in {"generate", "invoke"}):
            result["external_calls"] = True
        is_input = end in {
            "read_csv",
            "read_excel",
            "read_json",
            "read_parquet",
            "read_pickle",
        } or name in {"numpy.load", "open"}
        if is_input and node.args:
            if name == "open":
                mode = (
                    literal_value(node.args[1], values) if len(node.args) > 1 else "r"
                )
                mode = next(
                    (
                        literal_value(k.value, values)
                        for k in node.keywords
                        if k.arg == "mode"
                    ),
                    mode,
                )
                if mode and any(char in mode for char in "wax+"):
                    continue
            path = literal_value(node.args[0], values)
            if path and "://" not in path:
                inputs.add(path)

    result["imports"] = sorted(set(imports))
    result["colab"] = (
        any(module.startswith("google.colab") for module in imports)
        or "/content/" in text
    )
    result["input_paths"] = sorted(inputs)
    # src 또는 ZIP 안에 같은 이름의 파일·폴더가 있는 경우 프로젝트 모듈로 검사합니다.
    for module in imports:
        top = module.split(".")[0]
        if top == "src" or any(
            (directory / top).is_dir() or (directory / (top + ".py")).is_file()
            for directory in directories
        ):
            local_imports.append((module, 0))

    def module_exists(base, module):
        path = base.joinpath(*module.split("."))
        return path.is_dir() or path.with_suffix(".py").is_file()

    def input_exists(directory, value):
        path = (directory / value).resolve()
        return path.is_relative_to(root) and path.exists()

    scores = {}
    for directory in directories:
        # 모듈과 데이터를 함께 찾을 수 있는 상위 폴더를 실행 위치로 우선합니다.
        scores[directory] = sum(
            input_exists(directory, value) for value in inputs
        ) + 2 * sum(
            module_exists(directory, module)
            for module, level in local_imports
            if not level
        )
    best = max(directories, key=lambda directory: scores[directory])
    if working_dir is not None:
        requested = (root / working_dir).resolve()
        if requested not in directories:
            raise ValueError("실행 파일의 상위 프로젝트 폴더를 선택하세요.")
        best = requested
    result["cwd"] = str(best.relative_to(root))
    for module, level in local_imports:
        bases = [best, target.parent]
        if level:
            base = target.parent
            for _ in range(level - 1):
                base = base.parent
            exists = base.is_relative_to(root) and module_exists(base, module)
        else:
            exists = any(module_exists(base, module) for base in bases)
        if not exists:
            result["missing_modules"].append(module or "상대 경로 모듈")
    result["local_imports"] = sorted(set(module for module, _ in local_imports))
    result["missing_inputs"] = [
        value for value in inputs if not input_exists(best, value)
    ]
    if result["graphs"]:
        result["reasons"].append("그래프 작성·저장 코드 확인")
    if result["training"]:
        result["reasons"].append("학습 호출 확인: 실행하면 재학습할 수 있음")
    if result["service"]:
        result["kind"] = "서비스·화면 실행"
        result["notices"].append(
            "웹 화면용 코드입니다. 그래프 수집용 일괄 실행과 목적이 다릅니다."
        )
    elif result["training"]:
        result["kind"] = "학습·평가·그래프" if result["graphs"] else "모델 학습"
    elif result["graphs"]:
        result["kind"] = "분석·그래프 생성"
    elif inputs:
        result["kind"] = "데이터 처리"
    if result["interactive"]:
        result["notices"].append(
            "input/getpass 입력이 필요합니다. 무인 실행 중 입력할 수 없습니다."
        )
    if result["colab"]:
        result["notices"].append(
            "Google Colab 전용 코드·경로가 있습니다. 로컬 실행 전 조정이 필요할 수 있습니다."
        )
    if result["external_calls"]:
        result["notices"].append(
            "외부 통신·LLM 호출 후보가 있습니다. 키·네트워크·사용량을 확인하세요."
        )
    result["score"] = (
        (30 if result["graphs"] else 0)
        + (5 if target.suffix == ".ipynb" else 0)
        - (50 if result["service"] else 0)
        - (12 if result["training"] else 0)
        - (20 if result["interactive"] else 0)
        - (20 if result["colab"] else 0)
        - (12 if result["external_calls"] else 0)
    )
    return result


def inspect_entries(run_dir):
    """ZIP 안 실행 후보 전체와 조건부 추천 파일을 반환합니다."""
    root = Path(run_dir) / "project"
    candidates = [
        inspect_entry(root, str(p.relative_to(root)))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".py", ".ipynb"}
    ]
    candidates.sort(key=lambda item: (-item["score"], item["entry"]))
    available = [
        item
        for item in candidates
        if item["parse_ok"]
        and item["graphs"]
        and not item["service"]
        and not item["missing_modules"]
        and not item["missing_inputs"]
        and not item["interactive"]
        and not item["colab"]
        and not item["external_calls"]
    ]
    return candidates, available[0]["entry"] if available else None
