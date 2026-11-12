"""같은 근거로 작은 로컬 모델 3개를 비교합니다. 기존 앱 설정은 바꾸지 않습니다."""
import json
import time
from datetime import datetime
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'experiments/results' / ('local_family_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
MODELS = ['qwen3:1.7b', 'gemma2:2b', 'llama3.2:3b']


def api(endpoint, body=None, timeout=1200):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:11434/api/' + endpoint,
                                 data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def prepare():
    # 원래 9회 비교에서 사용한 자료와 지시를 그대로 읽습니다.
    protocol_path = ROOT / 'experiments/data/local_family_protocol.json'
    protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
    protocol['ollama_version'] = api('version')
    protocol['reproduction_started_at'] = datetime.now().isoformat()
    OUT.mkdir(parents=True, exist_ok=False)
    (OUT / 'protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding='utf-8')
    print('새 실행 결과 저장:', OUT, flush=True)
    return protocol


def run():
    protocol = prepare()
    installed = {m['name']: m for m in api('tags')['models']}
    assert all(m in installed for m in MODELS), '비교 모델을 먼저 설치하세요.'
    # 순서를 순환해 항상 같은 모델이 첫 번째가 되는 영향을 줄입니다.
    for repeat in range(3):
        order = MODELS[repeat:] + MODELS[:repeat]
        for model in order:
            stem = f"{repeat+1}_{model.replace(':', '_')}"
            dest = OUT / (stem + '.json')
            if dest.exists():
                continue  # 저장된 호출은 중복 실행하지 않습니다.
            payload = {'model': model, 'prompt': protocol['prompt'], 'stream': False,
                       'keep_alive': 0, 'options': dict(protocol['options'], seed=42+repeat)}
            if model.startswith('qwen3:'):
                payload['think'] = False
                payload['raw'] = True
                payload['prompt'] = '<|im_start|>user\n' + protocol['prompt'] + '\n/no_think<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
            (OUT / (stem + '.request.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            print('START', repeat+1, model, flush=True)
            start = time.perf_counter()
            try:
                raw = api('generate', payload)
                if raw.get('error'):
                    raise RuntimeError(raw['error'])
                result = {'model': model, 'repeat': repeat+1, 'seed': 42+repeat,
                          'wall_seconds': round(time.perf_counter()-start, 3),
                          'installed': installed[model], 'raw': raw}
                dest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                (OUT / (stem + '.md')).write_text(raw.get('response', ''), encoding='utf-8')
                print('DONE', model, result['wall_seconds'], raw.get('done_reason'), flush=True)
            except Exception as exc:
                (OUT / (stem + '.error.json')).write_text(json.dumps({'error': str(exc), 'wall_seconds':time.perf_counter()-start},ensure_ascii=False),encoding='utf-8')
                raise


if __name__ == '__main__':
    run()
