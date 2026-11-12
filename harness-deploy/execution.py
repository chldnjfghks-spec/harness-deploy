"""프로젝트 코드의 실제 실행 담당입니다.
화면에서 실행을 요청하는 부모 프로세스와 코드를 실행하는 worker(자식 프로세스)를 구분합니다."""

from pathlib import Path
from datetime import datetime
import base64, io, json, os, shutil, subprocess, sys, zipfile, time
from PIL import Image
from engine import blocked, redact, MAX_TOTAL


def prepare_project(data, run_dir):
    """ZIP의 실행용 파일을 작업 폴더의 project 안에 풉니다.
    원본 프로젝트를 직접 고치지 않도록 복사본을 만들고, 제외할 파일은 건너뜁니다."""
    root = Path(run_dir) / "project"
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > MAX_TOTAL:
            raise ValueError(
                "실행 자료는 압축 해제 기준 400MB/5,000개 파일 이하로 넣어 주세요."
            )
        for item in infos:
            name = item.filename
            if not item.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("cp949")
                except UnicodeError:
                    pass
            if item.is_dir() or blocked(name) or item.flag_bits & 1:
                continue
            target = root / Path(name)
            if not target.resolve().is_relative_to(root.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    return root


def entries(run_dir):
    """실행 후보인 .ipynb와 .py 파일의 상대 경로 목록을 반환합니다.
    노트북을 먼저 보여 주며, 실제 실행할 파일은 화면에서 선택합니다."""
    root = Path(run_dir) / "project"
    return sorted(
        [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.suffix.lower() in {".ipynb", ".py"}
        ],
        key=lambda x: (not x.endswith(".ipynb"), x),
    )


def execute_project(
    manifest, run_dir, entry, python=sys.executable, timeout=180, working_dir=None
):
    """선택한 코드를 별도 파이썬 프로세스로 실행하고 로그·이미지를 근거에 추가합니다.
    manifest는 자료 목록 딕셔너리이며 이 함수가 실행 결과를 덧붙여 갱신합니다.
    python은 실행할 python.exe 경로, timeout은 최대 대기 시간(초)입니다.
    반환 상태는 success(성공), failed(실패), timeout(시간 초과) 중 하나입니다.
    작업 복사본은 보안 격리 환경이 아니므로 신뢰하는 프로젝트만 실행해야 합니다."""
    if os.environ.get("HARNESS_ALLOW_CODE_EXECUTION", "0") != "1":
        raise PermissionError(
            "배포 환경에서는 업로드 코드 실행이 비활성화되어 있습니다."
        )
    run_dir = Path(run_dir).resolve()
    root = run_dir / "project"
    target = (root / entry).resolve()
    if (
        not target.is_relative_to(root)
        or not target.is_file()
        or target.suffix not in {".py", ".ipynb"}
    ):
        raise ValueError("프로젝트 내부의 파이썬 파일 또는 노트북을 선택하세요.")
    # 파일 위치와 실행 기준 폴더는 다를 수 있습니다. 예: notebooks/main.ipynb와 루트 dataset/.
    execution_cwd = (
        target.parent if working_dir is None else (root / working_dir).resolve()
    )
    if not execution_cwd.is_relative_to(root) or not execution_cwd.is_dir():
        raise ValueError("실행 기준 폴더는 ZIP 프로젝트 내부의 폴더여야 합니다.")
    cwd_relative = execution_cwd.relative_to(root)
    out = run_dir / "executions" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out.mkdir(parents=True)
    # 자식 프로세스용 환경변수 사본을 준비합니다. 부모의 환경변수 자체는 수정하지 않습니다.
    env = os.environ.copy()
    for key in list(env):
        if any(x in key.upper() for x in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")):
            env.pop(key, None)
    env.update(
        PYTHONIOENCODING="utf-8",
        MPLBACKEND="Agg",
        HARNESS_OUTPUT=str(out),
        HARNESS_STARTED=str(time.time()),
    )
    # 별도 프로세스로 실행하지만 보안 샌드박스는 아닙니다. 신뢰하는 프로젝트만 실행합니다.
    # 실행마다 새로운 복사본을 만들어 생성 파일과 원래 업로드 자료를 구분합니다.
    work = out / "work"
    shutil.copytree(root, work)
    env["HARNESS_WORK"] = str(work)
    copied = work / entry
    status = "failed"
    code = None
    with (out / "raw.log").open("wb") as stream:
        proc = subprocess.Popen(
            [str(python), str(Path(__file__).resolve()), "--worker", str(copied)],
            cwd=work / cwd_relative,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            code = proc.wait(timeout=timeout)
            status = "success" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
                )
            else:
                proc.kill()
            proc.wait()
    with (out / "raw.log").open("rb") as stream:
        stream.seek(max(0, (out / "raw.log").stat().st_size - 60000))
        raw = stream.read(60000)
    log = redact(raw[-60000:].decode("utf-8", errors="replace"))
    (out / "execution.log").write_text(log, encoding="utf-8")
    (out / "raw.log").unlink()
    result = {
        "entry": entry,
        "working_dir": str(cwd_relative),
        "status": status,
        "exit_code": code,
        "timeout_seconds": timeout,
        "python": str(python),
        "directory": str(out),
        "images": [],
    }
    # 기존 자료 번호 다음부터 새 로그·이미지 번호를 매깁니다.
    next_id = max([int(s["id"][1:]) for s in manifest["sources"]] + [0]) + 1

    def add_source(**values):
        """실행 결과 하나에 새로운 근거 ID를 붙이고 manifest에 추가합니다.
        nonlocal은 바깥 함수의 next_id 값을 이 함수에서도 갱신하겠다는 뜻입니다."""
        nonlocal next_id
        sid = f"S{next_id:03d}"
        next_id += 1
        manifest["sources"].append(dict(id=sid, **values))
        return sid

    log_id = add_source(
        path=f"실제 실행/{out.name}/{entry}",
        kind="text",
        text=f"실제 실행 상태: {status}; 종료 코드: {code}; 제한: {timeout}초. 실패/시간초과이면 전체 성공이 아닙니다.\n"
        + log,
    )
    for image in sorted((out / "figures").glob("*"))[:40]:
        try:
            with Image.open(image) as im:
                if im.width * im.height > 24000000:
                    continue
                im = im.convert("RGB")
                im.thumbnail((1800, 1800))
                destination = out / (image.stem + ".jpg")
                im.save(destination, quality=92)
            sid = add_source(
                path=f"실제 실행/{out.name}/{image.name}",
                kind="image",
                file=str(destination),
                text=f"{entry} 실제 실행에서 생성된 이미지. 실행 상태 {status}. 연결 로그 {log_id}. 파일명은 실행 순서이며 의미는 코드와 함께 판단할 것.",
            )
            result["images"].append(sid)
        except (OSError, ValueError):
            continue
    # 성공 여부와 무관하게 이번 실행 기록을 남깁니다. 실패 중 생성된 그림도 부분 결과입니다.
    manifest.setdefault("executions", []).append(result)
    manifest["preferred_images"] = result["images"][:6]
    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "evidence.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def worker(entry):
    """별도 프로세스 안에서 실제 Python 파일 또는 노트북을 실행합니다.
    노트북 셀은 같은 실행 공간을 공유하므로 앞 셀의 변수를 다음 셀에서 쓸 수 있습니다.
    오류가 발생하면 멈추고, Matplotlib 그래프와 표시된 이미지를 파일로 남깁니다."""
    import traceback

    target = Path(entry)
    out = Path(os.environ["HARNESS_OUTPUT"])
    figures = out / "figures"
    figures.mkdir()
    counter = 0

    def save_bytes(data, suffix):
        """이미지 바이트를 순서가 있는 파일명으로 저장합니다.
        바깥 counter 값을 증가시켜 그림 파일명이 서로 겹치지 않게 합니다."""
        nonlocal counter
        counter += 1
        (figures / f"figure_{counter:03d}{suffix}").write_bytes(data)

    plt = None
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        def save_figures(*args, **kwargs):
            """열려 있는 Matplotlib 그래프를 PNG로 저장한 뒤 닫습니다.
            plt.show()를 이 함수로 바꿔 창을 띄우는 대신 보고서용 이미지를 남깁니다."""
            for number in list(plt.get_fignums()):
                figure = plt.figure(number)
                buf = io.BytesIO()
                figure.savefig(buf, format="png", dpi=160, bbox_inches="tight")
                save_bytes(buf.getvalue(), ".png")
            plt.close("all")

        plt.show = save_figures
    except ImportError:
        pass
    print(
        "ENTRY:",
        target.name,
        "\nPYTHON:",
        sys.executable,
        "\nWORKING DIRECTORY:",
        Path.cwd(),
        flush=True,
    )
    sys.path.insert(0, str(Path.cwd()))
    sys.path.insert(0, str(target.parent))
    try:
        if target.suffix == ".ipynb":
            from IPython.core.interactiveshell import InteractiveShell
            from IPython.utils.capture import capture_output

            # 같은 shell을 계속 사용해야 앞 셀에서 만든 변수가 다음 셀에도 남습니다.
            shell = InteractiveShell.instance()
            cells = json.loads(target.read_text(encoding="utf-8-sig")).get("cells", [])
            for index, cell in enumerate(cells, 1):
                if cell.get("cell_type") != "code":
                    continue
                print(f"CELL {index} START", flush=True)
                with capture_output() as captured:
                    result = shell.run_cell(
                        "".join(cell.get("source", [])), store_history=False
                    )
                print(captured.stdout, end="", flush=True)
                print(captured.stderr, end="", flush=True)
                for output in captured.outputs:
                    for mime, suffix in [("image/png", ".png"), ("image/jpeg", ".jpg")]:
                        data = output.data.get(mime)
                        if data:
                            save_bytes(base64.b64decode(data), suffix)
                if plt:
                    save_figures()
                if result.error_before_exec or result.error_in_exec:
                    raise RuntimeError(f"CELL {index} FAILED")
                print(f"CELL {index} OK", flush=True)
        else:
            import runpy

            runpy.run_path(str(target), run_name="__main__")
        print("EXECUTION COMPLETE", flush=True)
    # finally는 성공해도 오류가 나도 실행됩니다. 종료 전에 남은 그래프를 저장합니다.
    finally:
        if plt:
            save_figures()
        # Also collect graphs saved directly with savefig()/image export.
        for p in Path(os.environ.get("HARNESS_WORK", str(target.parent))).rglob("*"):
            if (
                p.is_file()
                and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
                and p.stat().st_mtime >= float(os.environ.get("HARNESS_STARTED", "0"))
            ):
                # Only files produced during this execution; caller supplies timestamp.
                if "HARNESS_STARTED" in os.environ:
                    save_bytes(p.read_bytes(), p.suffix.lower())


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--worker":
    worker(sys.argv[2])
